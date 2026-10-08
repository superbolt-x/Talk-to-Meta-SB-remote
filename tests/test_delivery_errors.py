"""
Tests for get_delivery_errors: campaigns, ad sets and ads Meta has flagged, with the reasons.

issues_info fields per Meta's reference: error_code, error_message, error_summary, error_type, level.
"""
import json

import pytest

from meta_ads_mcp.core.api import MetaAPIError, api_client


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(api_client, "_ensure_initialized", lambda: None)


def issue(code=1487, summary="Ad set paused by billing", message="The payment method failed."):
    return {"error_code": code, "error_summary": summary, "error_message": message, "error_type": "BILLING", "level": "ad_set"}


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


CAMPAIGNS = {"data": [{"id": "c1", "name": "Camp A", "effective_status": "WITH_ISSUES", "issues_info": [issue()]}]}
ADSETS = {"data": [{"id": "s1", "name": "Set A", "effective_status": "WITH_ISSUES", "campaign_id": "c1",
                    "issues_info": [issue(code=2, summary="Targeting too narrow", message="Audience below the minimum.")]}]}
ADS = {"data": [{"id": "a1", "name": "Ad A", "effective_status": "DISAPPROVED", "campaign_id": "c1", "adset_id": "s1",
                 "issues_info": [], "ad_review_feedback": {"global": {"ADULT_CONTENT": "Adult content"}}}]}
ROUTES = {"/act_1/campaigns": CAMPAIGNS, "/act_1/adsets": ADSETS, "/act_1/ads": ADS}


class TestAccountScan:
    def test_flagged_entities_across_all_levels_with_their_reasons(self, monkeypatch):
        install(monkeypatch, dict(ROUTES))
        out = run(account_id="act_1")
        assert out["total"] == 3 and [e["level"] for e in out["entities"]] == ["campaign", "adset", "ad"]
        camp = out["entities"][0]
        assert camp["status"] == "WITH_ISSUES"
        assert camp["issues"] == [{"code": 1487, "summary": "Ad set paused by billing", "message": "The payment method failed.",
                                   "type": "BILLING", "level": "ad_set"}]
        ad = out["entities"][2]
        assert ad["status"] == "DISAPPROVED" and ad["campaign_id"] == "c1" and ad["adset_id"] == "s1"
        assert ad["review_feedback"] == {"global": {"ADULT_CONTENT": "Adult content"}}
        assert "review_feedback" not in camp

    def test_summary_counts_levels_statuses_and_most_common_reasons(self, monkeypatch):
        many = {"data": [{"id": f"c{i}", "name": f"C{i}", "effective_status": "WITH_ISSUES", "issues_info": [issue()]} for i in range(3)]}
        install(monkeypatch, {**ROUTES, "/act_1/campaigns": many})
        s = run()["summary"]
        assert s["by_level"] == {"campaign": 3, "adset": 1, "ad": 1}
        assert s["by_status"] == {"WITH_ISSUES": 4, "DISAPPROVED": 1}
        assert s["top_reasons"][0] == {"reason": "Ad set paused by billing", "count": 3}

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

    def test_ad_fields_include_parents_and_review_feedback(self, monkeypatch):
        calls = install(monkeypatch, dict(ROUTES))
        run()
        ad_call = next(c for c in calls if c["endpoint"].endswith("/ads"))
        assert {"adset_id", "campaign_id", "ad_review_feedback", "issues_info"} <= set(ad_call["fields"])

    def test_nothing_flagged_says_so(self, monkeypatch):
        install(monkeypatch, {k: {"data": []} for k in ROUTES})
        out = run()
        assert out["total"] == 0 and out["note"] == "Nothing is flagged with these statuses." and out["truncated"] is False


class TestCampaignScope:
    def routes(self, node_status="WITH_ISSUES"):
        return {"/c1": {"id": "c1", "name": "Camp A", "effective_status": node_status, "issues_info": [issue()]},
                "/c1/adsets": ADSETS, "/c1/ads": ADS}

    def test_a_campaign_is_checked_with_its_ad_sets_and_ads(self, monkeypatch):
        calls = install(monkeypatch, self.routes())
        out = run(campaign_id="c1")
        assert [c["endpoint"] for c in calls] == ["/c1", "/c1/adsets", "/c1/ads"]
        assert out["total"] == 3 and out["scope"] == {"campaign_id": "c1"}

    def test_a_healthy_campaign_node_is_not_listed(self, monkeypatch):
        install(monkeypatch, self.routes(node_status="ACTIVE"))
        assert [e["level"] for e in run(campaign_id="c1")["entities"]] == ["adset", "ad"]


class TestFailuresAndPaging:
    def test_one_level_failing_does_not_hide_the_others_or_pass_as_clean(self, monkeypatch):
        install(monkeypatch, {**ROUTES, "/act_1/adsets": MetaAPIError("(#200) denied", error_code=200)})
        out = run()
        assert out["total"] == 2 and "adset" in out["errors"] and "not reported as clean" in out["hint"]
        assert "note" not in out

    def test_review_feedback_is_dropped_and_the_scan_retried_if_meta_rejects_it(self, monkeypatch):
        seen = []

        def ads(params, fields):
            seen.append(list(fields))
            if "ad_review_feedback" in fields:
                return MetaAPIError("(#100) nonexisting field (ad_review_feedback)", error_code=100)
            return {"data": [{"id": "a1", "name": "Ad A", "effective_status": "DISAPPROVED", "issues_info": []}]}

        install(monkeypatch, {**ROUTES, "/act_1/ads": ads})
        out = run(levels="ad")
        assert "ad_review_feedback" in seen[0] and "ad_review_feedback" not in seen[1]
        assert out["total"] == 1 and "errors" not in out

    def test_pages_are_followed_and_a_long_list_is_flagged_as_cut_off(self, monkeypatch):
        def campaigns(params, fields):
            n = int(params["after"][1:]) if params.get("after") else 0
            rows = [{"id": f"c{n}-{i}", "name": "x", "effective_status": "WITH_ISSUES", "issues_info": []} for i in range(100)]
            return {"data": rows, "paging": {"next": "x", "cursors": {"after": f"c{n + 1}"}}}

        calls = install(monkeypatch, {**ROUTES, "/act_1/campaigns": campaigns})
        out = run(levels="campaign")
        assert out["total"] == 300 and len([c for c in calls]) == 3
        assert out["truncated"] is True and out["truncated_levels"] == ["campaign"] and "Narrow with" in out["truncation_note"]

    @pytest.mark.parametrize("kw", [{"levels": "galaxy"}, {"levels": ""}, {"statuses": " , "}])
    def test_input_validation(self, kw):
        assert run(**kw)["blocked_at"] == "input_validation"

    def test_registered_as_read_only_tool(self):
        from meta_ads_mcp.server import mcp
        tools = {t.name: t for t in mcp._tool_manager.list_tools()}
        assert tools["get_delivery_errors"].annotations.readOnlyHint is True
