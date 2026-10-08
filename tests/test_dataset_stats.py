"""
Tests for get_dataset_stats: event volume per day from /{pixel}/stats, with a browser vs server split.

The clock is pinned to 2026-10-08 12:00 UTC, so 6 complete days are 2026-10-02..2026-10-07 and
2026-10-08 is the partial day.
"""
from datetime import datetime, timezone

import pytest

from meta_ads_mcp.core import pixels
from meta_ads_mcp.core.api import MetaAPIError, api_client

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
DAYS = ["2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07"]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(api_client, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(pixels, "_utc_now", lambda: NOW)


def buckets(series, hour="10"):
    """series: {event: {date: count}} -> hourly stats buckets, one per date."""
    dates = sorted({d for per_day in series.values() for d in per_day})
    return [{"aggregation": "event", "start_time": f"{d}T{hour}:00:00+0000",
             "data": [{"value": name, "count": str(per_day[d])} for name, per_day in series.items() if d in per_day]}
            for d in dates]


def flat(count, days=DAYS, today=None):
    out = {d: count for d in days}
    if today is not None:
        out["2026-10-08"] = today
    return out


def install(monkeypatch, all_events, web=None, server=None, split_error=None):
    """Routes /{pixel}/stats by its event_source param and records the calls."""
    calls = []

    def fake(endpoint, params=None, fields=None):
        p = dict(params or {})
        calls.append({"endpoint": endpoint, "params": p})
        source = p.get("event_source")
        if source and split_error:
            raise split_error
        if source == "WEB_ONLY":
            return {"data": buckets(web or {})}
        if source == "SERVER_ONLY":
            return {"data": buckets(server or {})}
        return all_events(p) if callable(all_events) else {"data": buckets(all_events)}

    monkeypatch.setattr(api_client, "graph_get", fake)
    return calls


def run(**kw):
    from meta_ads_mcp.core.pixels import get_dataset_stats
    kw.setdefault("pixel_id", "123")
    return get_dataset_stats(**kw)


def issues(out, check=None):
    return [i for i in out["issues"] if check is None or i["check"] == check]


class TestTotals:
    def test_per_day_totals_average_and_today(self, monkeypatch):
        install(monkeypatch, {"Purchase": flat(10, today=4), "PageView": flat(100, today=60)})
        out = run(include_source_split=False)
        purchase = next(e for e in out["events"] if e["event"] == "Purchase")
        assert purchase["total"] == 60 and purchase["avg_per_day"] == 10.0
        assert purchase["per_day"] == flat(10)
        assert purchase["today_so_far"] == 4  # the partial day is shown but not counted in the total
        assert out["window"] == {"from": "2026-10-02", "to": "2026-10-07", "days": 6,
                                 "timezone": "UTC", "today_partial": "2026-10-08"}
        assert [e["event"] for e in out["events"]] == ["PageView", "Purchase"]  # biggest first

    def test_days_with_no_events_count_as_zero(self, monkeypatch):
        install(monkeypatch, {"Lead": {"2026-10-07": 5}})
        lead = run(include_source_split=False)["events"][0]
        assert lead["per_day"]["2026-10-02"] == 0 and lead["total"] == 5

    def test_request_window_is_midnight_utc_to_now_and_never_more_than_7_days(self, monkeypatch):
        calls = install(monkeypatch, {"Purchase": flat(10)})
        run(include_source_split=False)
        p = calls[0]["params"]
        assert p["aggregation"] == "event"
        assert int(p["start_time"]) == int(datetime(2026, 10, 2, tzinfo=timezone.utc).timestamp())
        assert int(p["end_time"]) == int(NOW.timestamp())
        assert (int(p["end_time"]) - int(p["start_time"])) < 7 * 86400  # Meta keeps ~7 days

    def test_buckets_are_assigned_to_their_utc_day(self, monkeypatch):
        late = [{"start_time": "2026-10-05T23:00:00-0700", "data": [{"value": "Lead", "count": "7"}]}]  # = 10-06 06:00 UTC
        install(monkeypatch, lambda p: {"data": late})
        assert run(include_source_split=False)["events"][0]["per_day"]["2026-10-06"] == 7

    def test_paged_stats_are_followed(self, monkeypatch):
        pages = {None: {"data": buckets({"Lead": {"2026-10-06": 3}}),
                        "paging": {"next": "x", "cursors": {"after": "c1"}}},
                 "c1": {"data": buckets({"Lead": {"2026-10-07": 4}})}}
        install(monkeypatch, lambda p: pages[p.get("after")])
        lead = run(include_source_split=False)["events"][0]
        assert lead["total"] == 7


class TestSelection:
    def test_events_filter_is_case_insensitive(self, monkeypatch):
        install(monkeypatch, {"Purchase": flat(10), "PageView": flat(100), "Lead": flat(3)})
        out = run(events="purchase, LEAD", include_source_split=False)
        assert {e["event"] for e in out["events"]} == {"Purchase", "Lead"}

    def test_unknown_events_say_so(self, monkeypatch):
        install(monkeypatch, {"Purchase": flat(10)})
        assert "None of the requested events" in run(events="Nope", include_source_split=False)["note"]

    def test_top_n_limits_the_default_view(self, monkeypatch):
        install(monkeypatch, {f"E{i}": flat(100 - i) for i in range(10)})
        out = run(top_n=3, include_source_split=False)
        assert [e["event"] for e in out["events"]] == ["E0", "E1", "E2"]


class TestVolumeDrops:
    def case(self, monkeypatch, last, prior=100, n_days=6):
        days = DAYS[-n_days:]
        series = {d: prior for d in days[:-1]}
        series[days[-1]] = last
        install(monkeypatch, {"Purchase": series})
        return run(days=n_days, include_source_split=False)

    def test_a_big_drop_is_medium_and_a_collapse_is_high(self, monkeypatch):
        assert issues(self.case(monkeypatch, 40), "volume_drop")[0]["severity"] == "MEDIUM"   # -60%
        assert issues(self.case(monkeypatch, 10), "volume_drop")[0]["severity"] == "HIGH"     # -90%
        assert issues(self.case(monkeypatch, 0), "volume_drop")[0]["severity"] == "HIGH"      # stopped

    def test_message_names_the_numbers(self, monkeypatch):
        message = issues(self.case(monkeypatch, 40), "volume_drop")[0]["message"]
        assert "Purchase: 40 events on 2026-10-07 vs 100/day over the 5 days before (-60%)" == message

    def test_a_small_dip_is_not_flagged(self, monkeypatch):
        assert issues(self.case(monkeypatch, 70), "volume_drop") == []  # -30%

    def test_low_volume_events_are_not_judged(self, monkeypatch):
        assert issues(self.case(monkeypatch, 0, prior=10), "volume_drop") == []

    def test_too_few_prior_days_are_not_judged(self, monkeypatch):
        assert issues(self.case(monkeypatch, 0, n_days=3), "volume_drop") == []

    def test_only_complete_days_are_compared(self, monkeypatch):
        install(monkeypatch, {"Purchase": flat(100, today=1)})  # today has just started
        assert issues(run(include_source_split=False), "volume_drop") == []


class TestSourceSplit:
    def test_split_totals_and_server_share(self, monkeypatch):
        calls = install(monkeypatch, {"Purchase": flat(100)},
                        web={"Purchase": flat(60)}, server={"Purchase": flat(40)})
        purchase = run()["events"][0]
        assert purchase["by_source"] == {"web": 360, "server": 240, "server_share_pct": 40.0}
        sources = [c["params"].get("event_source") for c in calls]
        assert sources == [None, "WEB_ONLY", "SERVER_ONLY"]

    def test_core_event_with_no_server_events_is_medium_others_info(self, monkeypatch):
        install(monkeypatch, {"Purchase": flat(100), "PageView": flat(500)},
                web={"Purchase": flat(100), "PageView": flat(500)}, server={})
        found = {i["event"]: i for i in issues(run(), "no_server_events")}
        assert found["Purchase"]["severity"] == "MEDIUM" and found["PageView"]["severity"] == "INFO"
        assert "Conversions API" in found["Purchase"]["message"]

    def test_server_only_event_is_informational(self, monkeypatch):
        install(monkeypatch, {"Purchase": flat(100)}, web={}, server={"Purchase": flat(100)})
        found = issues(run(), "no_browser_events")
        assert found[0]["severity"] == "INFO"

    def test_small_totals_are_not_flagged(self, monkeypatch):
        install(monkeypatch, {"Lead": flat(5)}, web={"Lead": flat(5)}, server={})  # 30 web events
        assert issues(run(), "no_server_events") == []

    def test_split_failure_keeps_the_main_data(self, monkeypatch):
        install(monkeypatch, {"Purchase": flat(10)}, split_error=MetaAPIError("(#100) bad event_source", error_code=100))
        out = run()
        assert out["events"][0]["total"] == 60 and "by_source" not in out["events"][0]
        assert "split unavailable" in out["notes"][0]

    def test_split_can_be_skipped(self, monkeypatch):
        calls = install(monkeypatch, {"Purchase": flat(10)})
        run(include_source_split=False)
        assert len(calls) == 1


class TestFailuresAndValidation:
    def test_main_call_failure_returns_an_error_with_a_hint(self, monkeypatch):
        install(monkeypatch, lambda p: (_ for _ in ()).throw(MetaAPIError("(#200) no access", error_code=200)))
        out = run()
        assert out["error_code"] == 200 and "ads_read" in out["hint"]

    def test_no_events_explains_why(self, monkeypatch):
        install(monkeypatch, {})
        assert "may be new or inactive" in run()["note"]

    @pytest.mark.parametrize("kw", [{"pixel_id": "abc"}, {"days": 0}, {"days": 7}])
    def test_input_validation(self, kw):
        assert run(**kw)["blocked_at"] == "input_validation"

    def test_registered_as_read_only_tool(self):
        from meta_ads_mcp.server import mcp
        tools = {t.name: t for t in mcp._tool_manager.list_tools()}
        assert tools["get_dataset_stats"].annotations.readOnlyHint is True
