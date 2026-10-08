"""
Tests for get_delivery_errors: campaigns, ad sets and ads Meta has flagged, with the reasons.

issues_info fields per Meta's reference: error_code, error_message, error_summary, error_type, level.
Review feedback shape (seen live): {global: {policy: text}, placement_specific: {facebook: {...}, instagram: {...}}}.
"""
import json
from datetime import datetime, timezone

import pytest

from meta_ads_mcp.core import delivery
from meta_ads_mcp.core.api import MetaAPIError, api_client

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(api_client, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(delivery, "_utc_now", lambda: NOW)


def issue(code=1487, summary="Ad set paused by billing", message="The payment method failed."):
    return {"error_code": code, "error_summary": summary, "error_message": message, "error_type": "BILLING", "level": "ad_set"}


def row(rid, name, status="WITH_ISSUES", updated="2026-10-07T10:00:00+0000", issues=None, **extra):
    return {"id": rid, "name": name, "effective_status": status, "updated_time": updated,
            "issues_info": [issue()] if issues is None else issues, **extra}


def install(monkeypatch, routes):
    """routes: endpoint -> payload | Exception | callable(params, fields). Records calls."""
    calls = []

    def fake(endpoint, params=None, fields=None):
        calls.append({"endpoint": endpoint, "params": dict(params or {}), "fields": list(fields or [])})
        payload = routes[endpoint]
        if callable(payload):
            payload = payload(dict(params or {}), fields)
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(api_client, "graph_get", fake)
    return calls


def run(**kw):
    from meta_ads_mcp.core.delivery import get_delivery_errors
    kw.setdefault("account_id", "1")
    return get_delivery_errors(**kw)


CAMPAIGNS = {"data": [row("c1", "Camp A")]}
ADSETS = {"data": [row("s1", "Set A", campaign_id="c1",
                       issues=[issue(code=2, summary="Targeting too narrow", message="Audience below the minimum.")])]}
FEEDBACK = {"global": {"Financial and Insurance Products and Services": " Ads must not promote loans. "},
            "placement_specific": {"facebook": {"Financial and Insurance Products and Services": "Ads must not promote loans."},
                                   "instagram": {"Financial and Insurance Products and Services": "Ads must not promote loans."}}}
ADS = {"data": [row("a1", "Ad A", status="DISAPPROVED", issues=[], campaign_id="c1", adset_id="s1", ad_review_feedback=FEEDBACK)]}
ROUTES = {"/act_1/campaigns": CAMPAIGNS, "/act_1/adsets": ADSETS, "/act_1/ads": ADS}


class TestAccountScan:
    def test_flagged_entities_across_all_levels_with_their_reasons(self, monkeypatch):
        install(monkeypatch, dict(ROUTES))
        out = run(account_id="act_1")
        assert out["total"] == 3 and [e["level"] for e in out["entities"]] == ["campaign", "adset", "ad"]
        camp = out["entities"][0]
        assert camp["status"] == "WITH_ISSUES" and camp["updated"] == "2026-10-07T10:00:00+0000"
        assert camp["issues"] == [{"code": 1487, "summary": "Ad set paused by billing", "message": "The payment method failed.",
                                   "type": "BILLING", "level": "ad_set"}]
        ad = out["entities"][2]
        assert ad["status"] == "DISAPPROVED" and ad["campaign_id"] == "c1" and ad["adset_id"] == "s1" and ad["issues"] == []
        assert "review_reasons" not in camp and "review_feedback" not in ad

    def test_review_feedback_repeated_per_placement_is_folded_into_one_entry(self, monkeypatch):
        # Seen live: the same text under global, facebook and instagram, some with stray spaces
        install(monkeypatch, dict(ROUTES))
        reasons = run()["entities"][2]["review_reasons"]
        assert reasons == [{"policy": "Financial and Insurance Products and Services",
                            "text": "Ads must not promote loans.", "placements": ["global", "facebook", "instagram"]}]

    def test_different_text_under_one_policy_stays_separate(self, monkeypatch):
        fb = {"global": {"Policy A": "Text one"}, "placement_specific": {"facebook": {"Policy A": "Text two"}}}
        install(monkeypatch, {**ROUTES, "/act_1/ads": {"data": [row("a1", "Ad", status="DISAPPROVED", issues=[], ad_review_feedback=fb)]}})
        reasons = run()["entities"][2]["review_reasons"]
        assert [(r["text"], r["placements"]) for r in reasons] == [("Text one", ["global"]), ("Text two", ["facebook"])]

    def test_a_policy_repeated_with_stray_spaces_in_one_placement_lists_it_once(self, monkeypatch):
        fb = {"global": {"Policy A": "Same text", " Policy A ": "Same  text "}}
        install(monkeypatch, {**ROUTES, "/act_1/ads": {"data": [row("a1", "Ad", status="DISAPPROVED", issues=[], ad_review_feedback=fb)]}})
        reasons = run()["entities"][2]["review_reasons"]
        assert reasons == [{"policy": "Policy A", "text": "Same text", "placements": ["global"]}]

    def test_summary_counts_levels_and_statuses(self, monkeypatch):
        many = {"data": [row(f"c{i}", f"C{i}") for i in range(3)]}
        install(monkeypatch, {**ROUTES, "/act_1/campaigns": many})
        s = run()["summary"]
        assert s["by_level"] == {"campaign": 3, "adset": 1, "ad": 1}
        assert s["by_status"] == {"WITH_ISSUES": 4, "DISAPPROVED": 1}

    def test_the_default_filter_asks_for_the_three_problem_statuses(self, monkeypatch):
        calls = install(monkeypatch, dict(ROUTES))
        run()
        flt = json.loads(calls[0]["params"]["filtering"])
        assert flt == [{"field": "effective_status", "operator": "IN",
                        "value": ["WITH_ISSUES", "DISAPPROVED", "PENDING_BILLING_INFO"]}]

    def test_statuses_can_be_chosen_and_are_uppercased(self, monkeypatch):
        calls = install(monkeypatch, dict(ROUTES))
        out = run(statuses="with_issues, pending_review")
        assert json.loads(calls[0]["params"]["filtering"])[0]["value"] == ["WITH_ISSUES", "PENDING_REVIEW"]
        assert out["statuses_checked"] == ["WITH_ISSUES", "PENDING_REVIEW"]

    def test_only_the_requested_levels_are_scanned(self, monkeypatch):
        calls = install(monkeypatch, dict(ROUTES))
        out = run(levels="ad")
        assert [c["endpoint"] for c in calls] == ["/act_1/ads"] and out["summary"]["by_level"] == {"ad": 1}

    def test_scope_always_has_the_same_shape(self, monkeypatch):
        install(monkeypatch, dict(ROUTES))
        assert run()["scope"] == {"type": "account", "id": "act_1"}
        install(monkeypatch, {"/c1": row("c1", "C"), "/c1/adsets": ADSETS, "/c1/ads": ADS})
        assert run(campaign_id="c1")["scope"] == {"type": "campaign", "id": "c1"}

    def test_nothing_flagged_says_so(self, monkeypatch):
        install(monkeypatch, {k: {"data": []} for k in ROUTES})
        out = run()
        assert out["total"] == 0 and out["note"] == "Nothing is flagged with these statuses." and out["truncated"] is False


class TestReasonsAndCaps:
    """Seen live: 602 flagged entities, 632 issues, only 14 distinct messages, a 458 KB answer."""

    def legacy_account(self, monkeypatch, n=120):
        ad_sets = {"data": [row(f"s{i}", f"Old set {i}", updated=f"2022-0{1 + i % 9}-15T10:00:00+0000",
                                issues=[issue(code=9, summary="Custom audience not available", message="Removed.")]) for i in range(n)]}
        fresh = {"data": [row("s-new", "New set", updated="2026-10-07T10:00:00+0000",
                              issues=[issue(code=2, summary="Targeting too narrow", message="Too small.")])]}
        both = {"data": ad_sets["data"] + fresh["data"]}
        install(monkeypatch, {**ROUTES, "/act_1/adsets": both, "/act_1/campaigns": {"data": []}, "/act_1/ads": {"data": []}})

    def test_reasons_are_grouped_counted_per_entity_with_examples(self, monkeypatch):
        self.legacy_account(monkeypatch)
        out = run()
        first = out["reasons"][0]
        assert first["reason"] == "Custom audience not available" and first["count"] == 120
        assert first["levels"] == {"adset": 120} and len(first["examples"]) == 3
        assert first["examples"][0].keys() == {"level", "id", "name"}
        assert out["reasons"][1] == {"reason": "Targeting too narrow", "count": 1, "levels": {"adset": 1},
                                     "examples": [{"level": "adset", "id": "s-new", "name": "New set"}]}

    def test_an_entity_repeating_a_reason_counts_once_for_it(self, monkeypatch):
        twice = {"data": [row("c1", "C", issues=[issue(), issue()])]}
        install(monkeypatch, {**ROUTES, "/act_1/campaigns": twice})
        assert run(levels="campaign")["reasons"][0]["count"] == 1

    def test_disapproved_ads_with_no_issues_still_show_their_policy_as_a_reason(self, monkeypatch):
        install(monkeypatch, dict(ROUTES))
        reasons = {r["reason"]: r for r in run()["reasons"]}
        assert reasons["Review: Financial and Insurance Products and Services"]["count"] == 1

    def test_only_the_most_recently_updated_entities_are_listed_but_the_summary_covers_all(self, monkeypatch):
        self.legacy_account(monkeypatch)
        out = run(max_entities=5)
        assert out["total"] == 121 and out["entities_shown"] == 5 and out["summary"]["by_level"]["adset"] == 121
        assert out["entities"][0]["id"] == "s-new"  # newest first
        assert "Showing the 5 most recently updated per level of 121" in out["entities_note"]

    def test_the_default_list_is_25_per_level(self, monkeypatch):
        self.legacy_account(monkeypatch)
        assert run()["entities_shown"] == 25

    def test_zero_entities_means_just_the_summary(self, monkeypatch):
        self.legacy_account(monkeypatch)
        out = run(max_entities=0)
        assert out["entities"] == [] and out["reasons"] and out["total"] == 121

    def test_a_short_list_has_no_cut_off_note(self, monkeypatch):
        install(monkeypatch, dict(ROUTES))
        assert "entities_note" not in run()


class TestRecency:
    def old_and_new(self, monkeypatch):
        rows = {"data": [row("old", "Old", updated="2022-03-01T00:00:00+0000"), row("new", "New", updated="2026-10-05T00:00:00+0000")]}
        return install(monkeypatch, {**ROUTES, "/act_1/campaigns": rows, "/act_1/adsets": {"data": []}, "/act_1/ads": {"data": []}})

    def test_recent_days_asks_meta_to_filter_and_also_filters_here(self, monkeypatch):
        calls = self.old_and_new(monkeypatch)
        out = run(recent_days=30)
        flt = json.loads(calls[0]["params"]["filtering"])
        assert flt[1] == {"field": "updated_time", "operator": "GREATER_THAN", "value": int(datetime(2026, 9, 8, 12, tzinfo=timezone.utc).timestamp())}
        assert [e["id"] for e in out["entities"]] == ["new"] and out["recent_days"] == 30

    def test_if_meta_refuses_the_time_filter_it_is_applied_here_with_a_note(self, monkeypatch):
        seen = []
        data = {"data": [row("old", "Old", updated="2022-03-01T00:00:00+0000"), row("new", "New", updated="2026-10-05T00:00:00+0000")]}

        def campaigns(params, fields):
            seen.append(json.loads(params["filtering"]))
            return MetaAPIError("(#100) invalid filter field", error_code=100) if len(seen[-1]) > 1 else data

        install(monkeypatch, {**ROUTES, "/act_1/campaigns": campaigns})
        out = run(recent_days=30, levels="campaign")
        assert len(seen[0]) == 2 and len(seen[1]) == 1
        assert [e["id"] for e in out["entities"]] == ["new"]
        assert "recent_days was applied here" in out["notes"][0]

    def test_if_update_times_are_not_returned_the_filter_cannot_be_applied_and_says_so(self, monkeypatch):
        def adsets(params, fields):
            if "updated_time" in fields or len(json.loads(params["filtering"])) > 1:
                return MetaAPIError("(#100) nonexisting field (updated_time)", error_code=100)
            return {"data": [{"id": "s1", "name": "Set", "effective_status": "WITH_ISSUES", "issues_info": []}]}

        install(monkeypatch, {**ROUTES, "/act_1/adsets": adsets})
        out = run(levels="adset", recent_days=30)
        assert out["total"] == 1 and any("recent_days could not be applied to adset" in n for n in out["notes"])

    def test_without_recent_days_nothing_is_filtered_by_time(self, monkeypatch):
        calls = self.old_and_new(monkeypatch)
        out = run()
        assert len(json.loads(calls[0]["params"]["filtering"])) == 1 and out["total"] == 2


class TestCampaignScope:
    def routes(self, node_status="WITH_ISSUES"):
        return {"/c1": row("c1", "Camp A", status=node_status), "/c1/adsets": ADSETS, "/c1/ads": ADS}

    def test_a_campaign_is_checked_with_its_ad_sets_and_ads(self, monkeypatch):
        calls = install(monkeypatch, self.routes())
        out = run(campaign_id="c1")
        assert [c["endpoint"] for c in calls] == ["/c1", "/c1/adsets", "/c1/ads"] and out["total"] == 3

    def test_a_healthy_campaign_node_is_not_listed(self, monkeypatch):
        install(monkeypatch, self.routes(node_status="ACTIVE"))
        assert [e["level"] for e in run(campaign_id="c1")["entities"]] == ["adset", "ad"]


class TestFailuresAndPaging:
    def test_one_level_failing_does_not_hide_the_others_or_pass_as_clean(self, monkeypatch):
        install(monkeypatch, {**ROUTES, "/act_1/adsets": MetaAPIError("(#200) denied", error_code=200)})
        out = run()
        assert out["total"] == 2 and "adset" in out["errors"] and "not reported as clean" in out["hint"] and "note" not in out

    def test_review_feedback_is_dropped_and_the_scan_retried_if_meta_rejects_it(self, monkeypatch):
        seen = []

        def ads(params, fields):
            seen.append(list(fields))
            if "ad_review_feedback" in fields:
                return MetaAPIError("(#100) nonexisting field (ad_review_feedback)", error_code=100)
            return {"data": [row("a1", "Ad A", status="DISAPPROVED", issues=[])]}

        install(monkeypatch, {**ROUTES, "/act_1/ads": ads})
        out = run(levels="ad")
        assert "ad_review_feedback" in seen[0] and "ad_review_feedback" not in seen[1]
        assert out["total"] == 1 and "errors" not in out

    def test_pages_are_followed_and_a_long_list_is_flagged_as_cut_off(self, monkeypatch):
        def campaigns(params, fields):
            n = int(params["after"][1:]) if params.get("after") else 0
            rows = [row(f"c{n}-{i}", "x", issues=[]) for i in range(100)]
            return {"data": rows, "paging": {"next": "x", "cursors": {"after": f"c{n + 1}"}}}

        calls = install(monkeypatch, {**ROUTES, "/act_1/campaigns": campaigns})
        out = run(levels="campaign")
        assert out["total"] == 300 and len(calls) == 3
        assert out["truncated"] is True and out["truncated_levels"] == ["campaign"] and "Narrow with" in out["truncation_note"]

    @pytest.mark.parametrize("kw", [{"levels": "galaxy"}, {"levels": ""}, {"statuses": " , "}, {"recent_days": 0},
                                    {"recent_days": 366}, {"max_entities": -1}, {"max_entities": 1001}])
    def test_input_validation(self, kw):
        assert run(**kw)["blocked_at"] == "input_validation"

    def test_registered_as_read_only_tool(self):
        from meta_ads_mcp.server import mcp
        tools = {t.name: t for t in mcp._tool_manager.list_tools()}
        assert tools["get_delivery_errors"].annotations.readOnlyHint is True
