"""
Tests for get_opportunity_score and get_dataset_quality.

Responses mirror the shapes in Meta's docs:
- /recommendations:  https://developers.facebook.com/documentation/ads-commerce/marketing-api/overview/performance-recommendations
- /dataset_quality:  https://developers.facebook.com/documentation/ads-commerce/conversions-api/dataset-quality-api
"""
from datetime import datetime, timedelta, timezone

import pytest

from meta_ads_mcp.core.api import MetaAPIError, api_client


@pytest.fixture(autouse=True)
def _no_real_client(monkeypatch):
    monkeypatch.setattr(api_client, "_ensure_initialized", lambda: None)


# ---------------------------------------------------------------- opportunity score

AD_ACCOUNT_RECS = {
    "data": [{
        "recommendations": [
            {
                "recommendation_signature": "111", "recommendation_stage": "mid_flight_recommendation",
                "type": "MUSIC", "object_ids": ["1", "2"],
                "recommendation_content": {"lift_estimate": "Up to 3% more Traffic", "body": "b1",
                                           "opportunity_score_lift": "5"},
                "url": "https://adsmanager.facebook.com/x",
            },
            {
                "recommendation_signature": "222", "recommendation_stage": "mid_flight_recommendation",
                "type": "ADVANTAGE_PLUS_AUDIENCE", "object_ids": ["9"],
                "recommendation_content": {"opportunity_score_lift": "14"},
            },
        ]
    }]
}


class TestGetOpportunityScore:
    def test_score_and_ranked_recommendations(self, monkeypatch):
        from meta_ads_mcp.core.opportunity import get_opportunity_score

        def fake_get(endpoint, params=None, fields=None):
            if endpoint == "/act_5":
                assert "opportunity_score" in fields
                return {"opportunity_score": 78, "opportunity_score_weight": 123456}
            if endpoint == "/act_5/recommendations":
                return AD_ACCOUNT_RECS
            raise AssertionError(endpoint)

        monkeypatch.setattr(api_client, "graph_get", fake_get)
        out = get_opportunity_score("5")
        assert out["account_id"] == "act_5"
        assert out["opportunity_score"] == 78.0
        recs = out["recommendations"]
        assert [r["type"] for r in recs] == ["ADVANTAGE_PLUS_AUDIENCE", "MUSIC"]  # ranked by lift
        assert recs[0]["score_lift_points"] == 14.0
        assert recs[1]["lift_estimate"] == "Up to 3% more Traffic"
        assert out["recommendation_summary"]["count"] == 2
        assert out["recommendation_summary"]["sum_of_listed_lift_points"] == 19.0
        assert "errors" not in out
        assert "history" not in out  # off by default

    def test_accepts_business_level_shape(self, monkeypatch):
        from meta_ads_mcp.core.opportunity import get_opportunity_score

        biz_shape = {"data": [{"ad_account_id": "5", "opportunity_score": 60, "recommendations": [{
            "recommendation_name": "reels_pc_recommendation", "level": "ad_set",
            "opportunity_score_lift": "26", "recommendation_content": {"body": "Use 9:16"},
        }]}]}
        monkeypatch.setattr(api_client, "graph_get", lambda e, params=None, fields=None:
                            {"opportunity_score": 60} if e == "/act_5" else biz_shape)
        rec = get_opportunity_score("act_5")["recommendations"][0]
        assert rec["type"] == "reels_pc_recommendation"
        assert rec["level"] == "ad_set"
        assert rec["score_lift_points"] == 26.0

    def test_history_window_respects_latency_and_cap(self, monkeypatch):
        from meta_ads_mcp.core.opportunity import get_opportunity_score

        seen = {}

        def fake_get(endpoint, params=None, fields=None):
            if endpoint.endswith("/opportunity_score_history"):
                seen.update(params)
                return {"data": [
                    {"date": "2026-09-01", "opportunity_score": 70},
                    {"date": "2026-09-03", "opportunity_score": 76},
                    {"date": "2026-09-02", "opportunity_score": 72},
                ]}
            return {"opportunity_score": 76}

        monkeypatch.setattr(api_client, "graph_get", fake_get)
        out = get_opportunity_score("5", include_recommendations=False, history_days=90,
                                    explain_history=True)
        today = datetime.now(timezone.utc).date()
        assert seen["to_date"] == (today - timedelta(days=2)).isoformat()
        span = (datetime.fromisoformat(seen["to_date"]) - datetime.fromisoformat(seen["from_date"])).days
        assert span == 44  # 45 days inclusive: capped at Meta's maximum
        assert seen["get_reason"] == "true"
        assert out["history_note"]
        assert [r["date"] for r in out["history"]] == ["2026-09-01", "2026-09-02", "2026-09-03"]
        summary = out["history_summary"]
        assert (summary["start_score"], summary["end_score"], summary["change"]) == (70.0, 76.0, 6.0)
        assert summary["direction"] == "up"

    def _score_vs_history(self, monkeypatch, live, last_history):
        from meta_ads_mcp.core.opportunity import get_opportunity_score

        def fake_get(endpoint, params=None, fields=None):
            if endpoint.endswith("/opportunity_score_history"):
                return {"data": [{"date": "2026-10-05", "opportunity_score": 60},
                                 {"date": "2026-10-06", "opportunity_score": last_history}]}
            return {"opportunity_score": live}

        monkeypatch.setattr(api_client, "graph_get", fake_get)
        return get_opportunity_score("5", include_recommendations=False, history_days=14)

    def test_large_live_vs_history_gap_is_explained(self, monkeypatch):
        # Seen live: headline score 86 while history ended at 62 two days earlier.
        note = self._score_vs_history(monkeypatch, 86, 62)["history_summary"]["live_vs_history_note"]
        assert "24 points above" in note
        assert "62" in note and "2026-10-06" in note
        assert "explain_history=true" in note

    def test_gap_below_the_threshold_gets_no_note(self, monkeypatch):
        summary = self._score_vs_history(monkeypatch, 66, 62)["history_summary"]
        assert "live_vs_history_note" not in summary

    def test_live_score_below_history_is_described_as_below(self, monkeypatch):
        note = self._score_vs_history(monkeypatch, 50, 62)["history_summary"]["live_vs_history_note"]
        assert "12 points below" in note

    def test_partial_failure_still_returns_what_worked(self, monkeypatch):
        from meta_ads_mcp.core.opportunity import get_opportunity_score

        def fake_get(endpoint, params=None, fields=None):
            if endpoint.endswith("/recommendations"):
                raise MetaAPIError("(#200) Permissions error", error_code=200)
            return {"opportunity_score": 81}

        monkeypatch.setattr(api_client, "graph_get", fake_get)
        out = get_opportunity_score("5")
        assert out["opportunity_score"] == 81.0
        assert "recommendations" in out["errors"]
        assert "hint" in out
        assert "recommendations" not in out

    def test_no_score_available(self, monkeypatch):
        from meta_ads_mcp.core.opportunity import get_opportunity_score

        monkeypatch.setattr(api_client, "graph_get", lambda e, params=None, fields=None: {"data": []})
        out = get_opportunity_score("5")
        assert out["opportunity_score"] is None
        assert "no opportunity score" in out["note"]

    def test_is_read_only_tool(self):
        from meta_ads_mcp.server import mcp
        tools = {t.name: t for t in mcp._tool_manager.list_tools()}
        assert tools["get_opportunity_score"].annotations.readOnlyHint is True


# ---------------------------------------------------------------- dataset quality

PURCHASE = {
    "event_name": "Purchase",
    "event_match_quality": {
        "composite_score": 3.5,
        "match_key_feedback": [{"identifier": "email", "coverage": {"percentage": 40}}],
        "diagnostics": [{"name": "Server sending mismatched IP addresses", "percentage": 61.5}],
    },
    "acr": {"percentage": 37.9, "description": "..."},
    "event_coverage": {"percentage": 34.1, "goal_percentage": 75, "description": "..."},
    "dedupe_key_feedback": [{
        "dedupe_key": "event_id",
        "browser_events_with_dedupe_key": {"percentage": 100},
        "server_events_with_dedupe_key": {"percentage": 55},
    }],
    "data_freshness": {"upload_frequency": "hourly", "description": "..."},
}
ADD_TO_CART = {
    "event_name": "AddToCart",
    "event_match_quality": {"composite_score": 8.2, "match_key_feedback": []},
    "event_coverage": {"percentage": 90, "goal_percentage": 75},
    "data_freshness": {"upload_frequency": "real_time"},
}


class TestGetDatasetQuality:
    def test_combined_query_and_assessment(self, monkeypatch):
        from meta_ads_mcp.core.pixels import get_dataset_quality

        calls = []

        def fake_get(endpoint, params=None, fields=None):
            calls.append((endpoint, params, fields))
            return {"web": [PURCHASE, ADD_TO_CART]}

        monkeypatch.setattr(api_client, "graph_get", fake_get)
        out = get_dataset_quality("123456")

        assert len(calls) == 1
        endpoint, params, fields = calls[0]
        assert endpoint == "/dataset_quality"
        assert params == {"dataset_id": "123456"}
        assert fields[0].startswith("web{") and fields[0].endswith("event_name}")
        assert "event_match_quality{composite_score" in fields[0]

        assert out["query_mode"] == "combined"
        assert out["event_count"] == 2
        assert out["lowest_match_quality"] == {"event": "Purchase", "score": 3.5}
        checks = {(i["event"], i["check"]) for i in out["issues"]}
        assert ("Purchase", "event_match_quality") in checks
        assert ("Purchase", "event_coverage") in checks
        assert ("Purchase", "dedupe_event_id") in checks
        assert ("Purchase", "data_freshness") in checks
        assert not [i for i in out["issues"] if i["event"] == "AddToCart"]
        assert out["issues"][0]["severity"] == "HIGH"  # EMQ 3.5 < 4 sorts first
        # Meta's own diagnostics are passed through untouched
        purchase = next(e for e in out["events"] if e["event_name"] == "Purchase")
        assert purchase["event_match_quality"]["diagnostics"][0]["percentage"] == 61.5

    def test_event_name_filter_and_agent_name(self, monkeypatch):
        from meta_ads_mcp.core.pixels import get_dataset_quality

        seen = {}

        def fake_get(endpoint, params=None, fields=None):
            seen.update(params)
            return {"web": [PURCHASE, ADD_TO_CART]}

        monkeypatch.setattr(api_client, "graph_get", fake_get)
        out = get_dataset_quality("123456", event_name="addtocart", agent_name="acme")
        assert seen["agent_name"] == "acme"
        assert [e["event_name"] for e in out["events"]] == ["AddToCart"]

    def test_falls_back_per_metric_when_a_field_is_rejected(self, monkeypatch):
        from meta_ads_mcp.core.pixels import get_dataset_quality

        def fake_get(endpoint, params=None, fields=None):
            sel = fields[0]
            if sel.count("{") > 3 and "acr{" in sel and "data_freshness" in sel:
                raise MetaAPIError("(#100) Tried accessing nonexisting field", error_code=100)
            if "dedupe_key_feedback" in sel:
                raise MetaAPIError("(#100) Tried accessing nonexisting field (dedupe_key_feedback)",
                                   error_code=100)
            if "dedup_key_feedback" in sel:
                return {"web": [{"event_name": "Purchase", "dedup_key_feedback": [{
                    "dedupe_key": "event_id",
                    "browser_events_with_dedupe_key": {"percentage": 100},
                    "server_events_with_dedupe_key": {"percentage": 100}}]}]}
            if "acr{" in sel:
                raise MetaAPIError("(#100) acr not available for dataset", error_code=100)
            if "event_match_quality{diagnostics}" in sel:
                return {"web": [{"event_name": "Purchase",
                                 "event_match_quality": {"diagnostics": [{"name": "d"}]}}]}
            if "event_match_quality{" in sel:
                return {"web": [{"event_name": "Purchase",
                                 "event_match_quality": {"composite_score": 7.0}}]}
            if "event_coverage" in sel:
                return {"web": [{"event_name": "Purchase",
                                 "event_coverage": {"percentage": 80, "goal_percentage": 75}}]}
            if "data_freshness" in sel:
                return {"web": [{"event_name": "Purchase",
                                 "data_freshness": {"upload_frequency": "real_time"}}]}
            raise AssertionError(sel)

        monkeypatch.setattr(api_client, "graph_get", fake_get)
        out = get_dataset_quality("123456")

        assert out["query_mode"] == "per_metric_fallback"
        purchase = out["events"][0]
        # EMQ score and diagnostics from separate requests merged into one object
        assert purchase["event_match_quality"]["composite_score"] == 7.0
        assert purchase["event_match_quality"]["diagnostics"] == [{"name": "d"}]
        # dedupe worked via the alternate spelling, so it is not reported unavailable
        assert "dedupe" not in out.get("unavailable_metrics", {})
        assert "acr" in out["unavailable_metrics"]
        assert out["issue_count"] == 0

    def test_auth_error_returns_hint_not_exception(self, monkeypatch):
        from meta_ads_mcp.core.pixels import get_dataset_quality

        def fake_get(endpoint, params=None, fields=None):
            raise MetaAPIError("(#200) Requires Use events dataset", error_code=200)

        monkeypatch.setattr(api_client, "graph_get", fake_get)
        out = get_dataset_quality("123456")
        assert out["error_code"] == 200
        assert "Use events dataset" in out["hint"]

    def test_expired_token_hint(self, monkeypatch):
        from meta_ads_mcp.core.pixels import get_dataset_quality

        def fake_get(endpoint, params=None, fields=None):
            raise MetaAPIError("expired", error_code=190)

        monkeypatch.setattr(api_client, "graph_get", fake_get)
        assert "expired" in get_dataset_quality("123456")["hint"]

    def test_rejects_non_numeric_pixel_id(self):
        from meta_ads_mcp.core.pixels import get_dataset_quality
        out = get_dataset_quality("abc; DROP")
        assert out["blocked_at"] == "input_validation"

    def test_empty_response_explains_why(self, monkeypatch):
        from meta_ads_mcp.core.pixels import get_dataset_quality

        monkeypatch.setattr(api_client, "graph_get", lambda e, params=None, fields=None: {})
        out = get_dataset_quality("123456")
        assert out["event_count"] == 0
        assert "Conversions API" in out["note"]

    def test_is_read_only_tool(self):
        from meta_ads_mcp.server import mcp
        tools = {t.name: t for t in mcp._tool_manager.list_tools()}
        assert tools["get_dataset_quality"].annotations.readOnlyHint is True
