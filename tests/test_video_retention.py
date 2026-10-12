"""
Tests for get_video_retention (per-ad video retention and hook rate from Insights).

Rows are shaped like the Insights edge returns them: a video field is a one-entry list of
{"action_type": "video_view", "value": "<n>"} and is absent for an ad with no video.
"""
import asyncio

import pytest

from meta_ads_mcp.core import video_retention
from meta_ads_mcp.core.api import MetaAPIError, api_client
from meta_ads_mcp.core.video_retention import get_video_retention

ACCOUNT = "act_1124102231808022"
CAMPAIGN = "c1"


def metric(value):
    return [{"action_type": "video_view", "value": str(value)}]


def video_row(ad_id, name, *, campaign=CAMPAIGN, adset="Adset A", spend=100.0, impressions=10000,
              plays=4000, plays_3s=2500, p25=1500, p50=1000, p75=700, p95=400, p100=300,
              thruplay=900, avg=6.5):
    row = {
        "ad_id": ad_id, "ad_name": name, "adset_id": "as1", "adset_name": adset,
        "campaign_id": campaign, "campaign_name": f"Campaign {campaign}",
        "spend": str(spend), "impressions": str(impressions),
        "actions": [{"action_type": "link_click", "value": "50"},
                    {"action_type": "video_view", "value": str(plays_3s)}],
        "video_play_actions": metric(plays),
        "video_p25_watched_actions": metric(p25), "video_p50_watched_actions": metric(p50),
        "video_p75_watched_actions": metric(p75), "video_p95_watched_actions": metric(p95),
        "video_p100_watched_actions": metric(p100),
        "video_thruplay_watched_actions": metric(thruplay),
        "video_avg_time_watched_actions": metric(avg),
    }
    return row


def static_row(ad_id, name, *, campaign=CAMPAIGN, spend=50.0, impressions=5000):
    return {
        "ad_id": ad_id, "ad_name": name, "adset_id": "as1", "adset_name": "Adset A",
        "campaign_id": campaign, "campaign_name": f"Campaign {campaign}",
        "spend": str(spend), "impressions": str(impressions),
        "actions": [{"action_type": "link_click", "value": "40"}],
    }


class CallLog(list):
    """Every graph_get request, plus `state` to script the responses."""


@pytest.fixture
def calls(monkeypatch):
    """Serve `state["pages"]` (a list of row lists) from graph_get, recording every request."""
    log = CallLog()
    state = {"pages": [[]], "error": None}

    def fake(endpoint, params=None, fields=None):
        log.append({"endpoint": endpoint, "params": dict(params or {}), "fields": list(fields or [])})
        if state["error"]:
            raise state["error"]
        after = (params or {}).get("after")
        idx = 0 if after is None else int(after[1:])
        out = {"data": state["pages"][idx]}
        if idx + 1 < len(state["pages"]):
            out["paging"] = {"next": "https://graph.facebook.com/next", "cursors": {"after": f"c{idx + 1}"}}
        return out

    monkeypatch.setattr(api_client, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(api_client, "graph_get", fake)
    log.state = state
    return log


def run(calls, rows, **kw):
    calls.state["pages"] = [rows]
    return get_video_retention(ACCOUNT, **kw)


def only_ad(result):
    assert result["total"] == 1, result
    return result["ads"][0]


class TestRequest:
    def test_asks_for_every_video_field_at_ad_level(self, calls):
        run(calls, [])
        req = calls[0]
        assert req["endpoint"] == f"/{ACCOUNT}/insights"
        assert req["params"]["level"] == "ad"
        for field in ("video_play_actions", "video_p25_watched_actions", "video_p50_watched_actions",
                      "video_p75_watched_actions", "video_p95_watched_actions", "video_p100_watched_actions",
                      "video_thruplay_watched_actions", "video_avg_time_watched_actions",
                      "actions", "spend", "impressions", "ad_id", "ad_name", "adset_name",
                      "campaign_id", "campaign_name"):
            assert field in req["fields"], field

    def test_preset_goes_as_date_preset(self, calls):
        run(calls, [], time_range="last_30d")
        assert calls[0]["params"]["date_preset"] == "last_30d"
        assert "time_range" not in calls[0]["params"]

    def test_explicit_range_goes_as_time_range(self, calls):
        run(calls, [], time_range="2026-09-23,2026-10-05")
        assert calls[0]["params"]["time_range"] == '{"since": "2026-09-23", "until": "2026-10-05"}'
        assert "date_preset" not in calls[0]["params"]

    def test_campaign_id_is_used_as_given(self, calls):
        calls.state["pages"] = [[]]
        get_video_retention("120211111")
        assert calls[0]["endpoint"] == "/120211111/insights"

    def test_empty_result_is_an_empty_list_not_an_error(self, calls):
        result = run(calls, [])
        assert result["total"] == 0 and result["ads"] == [] and result["truncated"] is False
        assert "error" not in result


class TestCountsAndRates:
    def test_counts_are_returned_as_numbers(self, calls):
        ad = only_ad(run(calls, [video_row("1", "SC Ad 22")]))
        assert (ad["plays"], ad["plays_3s"]) == (4000, 2500)
        assert (ad["p25"], ad["p50"], ad["p75"], ad["p95"], ad["p100"]) == (1500, 1000, 700, 400, 300)
        assert ad["thruplay"] == 900 and ad["avg_watch_time_s"] == 6.5
        assert ad["spend"] == 100.0 and ad["impressions"] == 10000 and ad["is_video"] is True

    def test_rates_use_both_denominators(self, calls):
        rates = only_ad(run(calls, [video_row("1", "SC Ad 22")]))["rates"]
        assert rates["hook_rate"] == 25.0                      # 2500 / 10000
        assert rates["retention_p25_vs_3s"] == 60.0            # 1500 / 2500
        assert rates["retention_p50_vs_3s"] == 40.0
        assert rates["retention_p75_vs_3s"] == 28.0
        assert rates["retention_p95_vs_3s"] == 16.0
        assert rates["retention_p100_vs_3s"] == 12.0
        assert rates["retention_p25_vs_plays"] == 37.5         # 1500 / 4000
        assert rates["retention_p50_vs_plays"] == 25.0
        assert rates["retention_p75_vs_plays"] == 17.5
        assert rates["retention_p95_vs_plays"] == 10.0
        assert rates["retention_p100_vs_plays"] == 7.5
        assert rates["hold_rate"] == 36.0                      # 900 / 2500

    def test_the_two_denominators_give_different_answers(self, calls):
        rates = only_ad(run(calls, [video_row("1", "A")]))["rates"]
        assert rates["retention_p25_vs_3s"] != rates["retention_p25_vs_plays"]

    def test_definitions_name_the_denominators(self, calls):
        result = run(calls, [video_row("1", "A")])
        assert "plays_3s" in result["definitions"]["retention_pX_vs_3s"]
        assert "plays" in result["definitions"]["retention_pX_vs_plays"]
        assert result["units"]["rates"].startswith("percent")

    def test_zero_denominators_give_null_rates_not_a_crash(self, calls):
        ad = only_ad(run(calls, [video_row("1", "A", impressions=0, plays=0, plays_3s=0, p25=0, p50=0,
                                           p75=0, p95=0, p100=0, thruplay=0)]))
        assert all(v is None for v in ad["rates"].values())
        assert ad["plays_3s"] == 0 and ad["is_video"] is True

    def test_a_field_meta_omitted_on_a_video_ad_is_zero(self, calls):
        row = video_row("1", "A")
        del row["video_p95_watched_actions"], row["video_p100_watched_actions"]
        ad = only_ad(run(calls, [row]))
        assert ad["p95"] == 0 and ad["p100"] == 0
        assert ad["rates"]["retention_p95_vs_3s"] == 0.0


class TestStaticAds:
    def test_static_ad_has_nulls_not_zeros(self, calls):
        ad = only_ad(run(calls, [static_row("9", "Static 1")]))
        assert ad["is_video"] is False
        for key in ("plays", "plays_3s", "p25", "p50", "p75", "p95", "p100", "thruplay",
                    "avg_watch_time_s", "rates"):
            assert ad[key] is None, key
        assert ad["spend"] == 50.0 and ad["impressions"] == 5000

    def test_static_ads_are_noted(self, calls):
        result = run(calls, [video_row("1", "Video"), static_row("9", "Static 1")])
        assert result["total"] == 2
        assert any("no video data" in n for n in result["notes"])

    def test_a_static_row_merged_into_a_video_ad_does_not_erase_it(self, calls):
        # the same name carrying a video ad id and a static ad id: the video data still counts
        ads = run(calls, [video_row("1", "Mixed"), static_row("2", "Mixed")])["ads"]
        assert len(ads) == 1 and ads[0]["is_video"] is True and ads[0]["plays_3s"] == 2500


class TestMerging:
    def rows(self):
        return [
            video_row("1", "SC Ad 22", spend=60, impressions=6000, plays=2000, plays_3s=1500,
                      p25=900, p50=600, p75=400, p95=200, p100=100, thruplay=500, avg=5.0),
            video_row("2", "SC Ad 22", adset="Adset B", spend=40, impressions=4000, plays=1000,
                      plays_3s=1000, p25=600, p50=400, p75=300, p95=200, p100=100, thruplay=400, avg=8.0),
        ]

    def test_two_ad_ids_with_one_name_collapse_to_one_row(self, calls):
        result = run(calls, self.rows())
        ad = only_ad(result)
        assert ad["ad_ids"] == ["1", "2"] and ad["adset_names"] == ["Adset A", "Adset B"]
        assert result["group_by"] == "ad_name"

    def test_merged_counts_are_sums(self, calls):
        ad = only_ad(run(calls, self.rows()))
        assert (ad["spend"], ad["impressions"]) == (100.0, 10000)
        assert (ad["plays"], ad["plays_3s"], ad["thruplay"]) == (3000, 2500, 900)
        assert (ad["p25"], ad["p50"], ad["p75"], ad["p95"], ad["p100"]) == (1500, 1000, 700, 400, 200)

    def test_merged_rates_come_from_the_merged_counts(self, calls):
        rates = only_ad(run(calls, self.rows()))["rates"]
        assert rates["hook_rate"] == 25.0 and rates["retention_p25_vs_3s"] == 60.0

    def test_average_watch_time_is_weighted_by_plays(self, calls):
        # (5.0 * 2000 + 8.0 * 1000) / 3000 = 6.0, not the plain mean 6.5
        assert only_ad(run(calls, self.rows()))["avg_watch_time_s"] == 6.0

    def test_group_by_ad_id_keeps_them_apart(self, calls):
        result = run(calls, self.rows(), group_by="ad_id")
        assert result["total"] == 2
        assert sorted(a["ad_ids"][0] for a in result["ads"]) == ["1", "2"]

    def test_the_same_name_in_two_campaigns_is_not_merged(self, calls):
        rows = [video_row("1", "Hook A", campaign="c1"), video_row("2", "Hook A", campaign="c2")]
        result = run(calls, rows)
        assert result["total"] == 2
        assert {a["campaign_id"] for a in result["ads"]} == {"c1", "c2"}


class TestFilters:
    def test_ad_name_contains_is_case_insensitive(self, calls):
        rows = [video_row("1", "SC Ad 22"), video_row("2", "NB Whitelisting"), static_row("3", "sc static")]
        names = {a["ad_name"] for a in run(calls, rows, ad_name_contains="sc")["ads"]}
        assert names == {"SC Ad 22", "sc static"}

    def test_min_spend_drops_small_ads(self, calls):
        rows = [video_row("1", "Big", spend=500), video_row("2", "Small", spend=5)]
        assert [a["ad_name"] for a in run(calls, rows, min_spend=100)["ads"]] == ["Big"]

    def test_min_spend_is_applied_after_merging(self, calls):
        rows = [video_row("1", "Split", spend=60), video_row("2", "Split", spend=60)]
        ad = only_ad(run(calls, rows, min_spend=100))
        assert ad["spend"] == 120.0

    def test_results_are_sorted_by_spend(self, calls):
        rows = [video_row("1", "Low", spend=10), video_row("2", "High", spend=900), video_row("3", "Mid", spend=90)]
        assert [a["ad_name"] for a in run(calls, rows)["ads"]] == ["High", "Mid", "Low"]


class TestOrdering:
    def test_a_consistent_ad_has_no_violations(self, calls):
        assert only_ad(run(calls, [video_row("1", "A")]))["ordering_violations"] == []

    def test_out_of_order_counts_are_named(self, calls):
        ad = only_ad(run(calls, [video_row("1", "A", p75=1200)]))  # p75 > p50
        assert ad["ordering_violations"] == ["p75>p50"]

    def test_more_3s_plays_than_plays_is_flagged(self, calls):
        ad = only_ad(run(calls, [video_row("1", "A", plays=2000, plays_3s=2500)]))
        assert "plays_3s>plays" in ad["ordering_violations"]

    def test_violations_add_a_note(self, calls):
        result = run(calls, [video_row("1", "A", p100=2000)])
        assert any("ordering_violations" in n and "shorter than 12 seconds" in n for n in result["notes"])

    def test_static_ads_have_no_violations(self, calls):
        assert only_ad(run(calls, [static_row("9", "S")]))["ordering_violations"] == []


class TestPaging:
    def test_follows_cursors_and_merges_across_pages(self, calls):
        calls.state["pages"] = [[video_row("1", "Split", spend=60)], [video_row("2", "Split", spend=40)]]
        result = get_video_retention(ACCOUNT)
        assert len(calls) == 2 and calls[1]["params"]["after"] == "c1"
        assert only_ad(result)["spend"] == 100.0
        assert result["truncated"] is False

    def test_stopping_at_the_page_cap_is_reported(self, calls, monkeypatch):
        monkeypatch.setattr(video_retention, "MAX_PAGES", 2)
        calls.state["pages"] = [[video_row(str(i), f"Ad {i}")] for i in range(5)]
        result = get_video_retention(ACCOUNT)
        assert len(calls) == 2 and result["truncated"] is True
        assert "more exist" in result["truncation_note"]


class TestInputValidation:
    @pytest.mark.parametrize("kw", [{"level": "campaign"}, {"level": "adset"}])
    def test_only_ad_level(self, calls, kw):
        result = get_video_retention(ACCOUNT, **kw)
        assert result["blocked_at"] == "input_validation" and "level" in result["error"]
        assert calls == []

    def test_unknown_group_by(self, calls):
        result = get_video_retention(ACCOUNT, group_by="campaign")
        assert result["blocked_at"] == "input_validation" and "group_by" in result["error"]
        assert calls == []

    def test_unknown_time_range(self, calls):
        result = get_video_retention(ACCOUNT, time_range="fortnight")
        assert "Unknown time_range" in result["error"] and "last_7d" in result["valid_presets"]
        assert calls == []

    def test_negative_min_spend(self, calls):
        assert "min_spend" in get_video_retention(ACCOUNT, min_spend=-1)["error"]
        assert calls == []


class TestErrors:
    def test_meta_api_error_comes_back_as_an_error_dict(self, calls):
        calls.state["error"] = MetaAPIError("(#100) Invalid parameter", error_code=100)
        result = get_video_retention(ACCOUNT)
        assert "Invalid parameter" in result["error"] and "hint" in result


class TestAcceptanceShape:
    """The MATE influencer ads: four names, one of them carrying two ad IDs."""

    def test_video_view_matches_the_warehouse_and_chain_is_monotonic(self, calls):
        rows = [
            video_row("a1", "NB Whitelisting", plays_3s=17717, plays=30000, p25=13000, p50=9000,
                      p75=7000, p95=5000, p100=4000),
            video_row("a2", "NB Branded", plays_3s=10462, plays=18000, p25=7000, p50=5000,
                      p75=4000, p95=3000, p100=2500),
            video_row("a3", "JL Whitelisting", plays_3s=6915, plays=12000, p25=5000, p50=3500,
                      p75=2800, p95=2000, p100=1700),
            video_row("a4", "JL Branded", plays_3s=3000, plays=5000, p25=2000, p50=1400,
                      p75=1100, p95=800, p100=600),
            video_row("a5", "JL Branded", plays_3s=1085, plays=1800, p25=700, p50=500,
                      p75=400, p95=300, p100=200),
        ]
        result = run(calls, rows)
        by_name = {a["ad_name"]: a for a in result["ads"]}
        assert {n: a["plays_3s"] for n, a in by_name.items()} == {
            "NB Whitelisting": 17717, "NB Branded": 10462, "JL Whitelisting": 6915, "JL Branded": 4085}
        assert result["total"] == 4 and by_name["JL Branded"]["ad_ids"] == ["a4", "a5"]
        assert all(a["ordering_violations"] == [] for a in result["ads"])


def test_registered_as_a_read_only_tool():
    from meta_ads_mcp.server import mcp

    tool = next(t for t in asyncio.run(mcp.list_tools()) if t.name == "get_video_retention")
    assert tool.annotations.readOnlyHint is True
    assert tool.inputSchema["properties"]["group_by"]["default"] == "ad_name"
