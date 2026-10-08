"""
Tests for get_catalog_readiness, get_catalog_data_sources and get_feed_rules.

Shapes follow Meta's references: /{catalog}/da_checks (DACheck: key, title, description, result,
action_uri, user_message), /{feed}/rules, /{upload_error}/suggested_rules (attribute, type, params).
"""
import json

import pytest

from meta_ads_mcp.core.api import MetaAPIError, api_client


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(api_client, "_ensure_initialized", lambda: None)


def install(monkeypatch, routes):
    """routes: {endpoint: payload | Exception | callable(params, fields)}. Records every call."""
    calls = []

    def fake(endpoint, params=None, fields=None):
        calls.append({"endpoint": endpoint, "params": dict(params or {}), "fields": fields})
        payload = routes[endpoint]
        if callable(payload):
            payload = payload(dict(params or {}), fields)
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(api_client, "graph_get", fake)
    return calls


# ============================================================ get_catalog_readiness

PASSED = {"key": "pixel_missing_dpa_event", "title": "Pixel sends the product events", "result": "passed",
          "description": "d1", "action_uri": "https://adsmanager.facebook.com/x", "user_message": "ok"}
FAILED = {"key": "pixel_decline", "title": "Event volume is stable", "result": "failed",
          "description": "Volume fell below 50% of the weekly average.",
          "action_uri": "https://adsmanager.facebook.com/fix", "user_message": "Purchase volume dropped 62%."}
UNAVAILABLE = {"key": "app_missing_dpa_event", "title": "App sends the product events", "result": "unavailable"}


def readiness(monkeypatch, data, **kw):
    from meta_ads_mcp.core.catalogs import get_catalog_readiness
    calls = install(monkeypatch, {"/cat1/da_checks": {"data": data}})
    return get_catalog_readiness("cat1", **kw), calls


class TestReadiness:
    def test_a_failed_check_makes_the_catalog_not_ready_and_carries_metas_explanation(self, monkeypatch):
        out, _ = readiness(monkeypatch, [PASSED, FAILED])
        assert out["status"] == "not_ready" and out["counts"] == {"passed": 1, "failed": 1, "unavailable": 0}
        issue = out["issues"][0]
        assert issue["severity"] == "HIGH" and issue["check"] == "pixel_decline"
        assert issue["message"] == "Event volume is stable: Purchase volume dropped 62%."
        assert issue["fix"] == "Volume fell below 50% of the weekly average."
        assert issue["fix_url"] == "https://adsmanager.facebook.com/fix"
        assert [c["result"] for c in out["checks"]] == ["failed", "passed"]  # failed first

    def test_all_passed_is_ready(self, monkeypatch):
        out, _ = readiness(monkeypatch, [PASSED])
        assert out["status"] == "ready" and out["issues"] == []

    def test_unavailable_checks_are_informational_and_do_not_block(self, monkeypatch):
        out, _ = readiness(monkeypatch, [PASSED, UNAVAILABLE])
        assert out["status"] == "ready"
        assert out["issues"][0]["severity"] == "INFO" and "could not run" in out["issues"][0]["message"]

    def test_nothing_ran_is_unknown_not_ready(self, monkeypatch):
        out, _ = readiness(monkeypatch, [UNAVAILABLE])
        assert out["status"] == "unknown"
        out, _ = readiness(monkeypatch, [])
        assert out["status"] == "unknown" and "no connected pixel or app" in out["note"]

    def test_results_are_case_insensitive(self, monkeypatch):
        out, _ = readiness(monkeypatch, [{**FAILED, "result": "FAILED"}])
        assert out["status"] == "not_ready"

    def test_feed_upload_errors_point_at_the_feed_health_tool(self, monkeypatch):
        failing = {"key": "catalog_has_feed_upload_errors", "title": "Feed uploads succeed", "result": "failed",
                   "description": "generic"}
        out, _ = readiness(monkeypatch, [failing])
        assert "get_catalog_feed_health" in out["issues"][0]["fix"]

    def test_connection_method_is_passed_through_uppercased(self, monkeypatch):
        _, calls = readiness(monkeypatch, [PASSED], connection_method="server")
        assert calls[0]["params"] == {"connection_method": "SERVER"}

    def test_no_connection_method_sends_no_param(self, monkeypatch):
        _, calls = readiness(monkeypatch, [PASSED])
        assert calls[0]["params"] == {}

    def test_invalid_connection_method_is_rejected(self, monkeypatch):
        out, calls = readiness(monkeypatch, [PASSED], connection_method="carrier pigeon")
        assert out["blocked_at"] == "input_validation" and calls == []

    def test_falls_back_to_default_fields_if_one_is_rejected(self, monkeypatch):
        from meta_ads_mcp.core.catalogs import get_catalog_readiness
        seen = []

        def route(params, fields):
            seen.append(fields)
            if fields:
                return MetaAPIError("(#100) nonexisting field (user_message)", error_code=100)
            return {"data": [PASSED]}

        install(monkeypatch, {"/cat1/da_checks": route})
        assert get_catalog_readiness("cat1")["status"] == "ready"
        assert seen[0] and seen[1] is None

    def test_failure_returns_an_error_with_a_hint(self, monkeypatch):
        from meta_ads_mcp.core.catalogs import get_catalog_readiness
        install(monkeypatch, {"/cat1/da_checks": MetaAPIError("(#200) denied", error_code=200)})
        out = get_catalog_readiness("cat1")
        assert out["error_code"] == 200 and "catalog_management" in out["hint"]


# ============================================================ get_catalog_data_sources

FEED = {"id": "f1", "name": "Main feed", "product_count": 120, "ingestion_source_type": "PRIMARY_FEED",
        "schedule": {"interval": "DAILY", "hour": 4, "url": "sftp://user:s3cret@feeds.example.com/x.csv?token=abc",
                     "username": "user"},
        "latest_upload": {"id": "u1", "start_time": "2026-10-08T04:00:00+0000", "end_time": "2026-10-08T04:01:00+0000",
                          "num_detected_items": 120, "num_persisted_items": 118, "num_invalid_items": 2,
                          "input_method": "Server Fetch", "url": "sftp://user:s3cret@feeds.example.com/x.csv"}}
NODE = {"id": "cat1", "name": "Shop catalog", "product_count": 120, "feed_count": 1, "vertical": "commerce"}
SOURCES = {"data": [{"id": "p1", "name": "Shop pixel", "source_type": "PIXEL"}]}


def sources_tool(monkeypatch, feeds, event_sources=SOURCES, node=NODE, **extra):
    from meta_ads_mcp.core.catalogs import get_catalog_data_sources
    install(monkeypatch, {"/cat1": node, "/cat1/product_feeds": {"data": feeds},
                          "/cat1/external_event_sources": event_sources, **extra})
    return get_catalog_data_sources("cat1")


class TestDataSources:
    def test_lists_feeds_and_event_sources(self, monkeypatch):
        out = sources_tool(monkeypatch, [FEED])
        feed = out["feeds"][0]
        assert feed["name"] == "Main feed" and feed["type"] == "PRIMARY_FEED" and feed["product_count"] == 120
        assert feed["latest_upload"] == {"start_time": "2026-10-08T04:00:00+0000", "end_time": "2026-10-08T04:01:00+0000",
                                         "completed": True, "input_method": "Server Fetch",
                                         "items_detected": 120, "items_persisted": 118, "items_invalid": 2}
        assert out["event_sources"] == [{"id": "p1", "name": "Shop pixel", "type": "PIXEL"}]
        assert out["summary"] == {"feeds": 1, "event_sources": 1}
        assert out["catalog"]["name"] == "Shop catalog" and "notes" not in out

    def test_feed_urls_and_credentials_are_never_returned(self, monkeypatch):
        blob = json.dumps(sources_tool(monkeypatch, [FEED]))
        for secret in ("s3cret", "token=abc", "x.csv", '"user"', "username"):
            assert secret not in blob, secret
        assert "feeds.example.com" in blob  # the host is kept

    def test_a_feed_named_after_its_url_shows_only_the_host(self, monkeypatch):
        out = sources_tool(monkeypatch, [{**FEED, "name": "https://store.myshopify.com/", "file_name": "https://store.myshopify.com/f.csv"}])
        assert out["feeds"][0]["name"] == "store.myshopify.com" and out["feeds"][0]["file_name"] == "store.myshopify.com"

    def test_a_catalog_with_only_supplementary_feeds_says_products_come_from_elsewhere(self, monkeypatch):
        out = sources_tool(monkeypatch, [{**FEED, "ingestion_source_type": "SUPPLEMENTARY_FEED"}])
        assert any("integration" in n and "Shopify" in n for n in out["notes"])

    def test_no_feeds_and_no_event_sources_are_both_called_out(self, monkeypatch):
        out = sources_tool(monkeypatch, [], event_sources={"data": []}, node={**NODE, "feed_count": 0})
        assert len(out["notes"]) == 2
        assert any("No pixel or app is connected" in n for n in out["notes"])

    def test_declared_feed_count_above_listed_feeds_is_noted(self, monkeypatch):
        out = sources_tool(monkeypatch, [FEED], node={**NODE, "feed_count": 3})
        assert any("Meta reports 3 feeds but only 1" in n for n in out["notes"])

    def test_one_section_failing_does_not_hide_the_other(self, monkeypatch):
        out = sources_tool(monkeypatch, [FEED], event_sources=MetaAPIError("(#200) denied", error_code=200))
        assert out["feeds"] and out["event_sources"] == []
        assert "event_sources" in out["errors"] and "catalog_management" in out["hint"]
        assert not any("No pixel or app" in n for n in out.get("notes", []))  # unknown is not "none"

    def test_event_source_fields_fall_back_if_one_is_rejected(self, monkeypatch):
        def route(params, fields):
            if "source_type" in (fields or []):
                return MetaAPIError("(#100) nonexisting field", error_code=100)
            return {"data": [{"id": "p1", "name": "Shop pixel"}]}

        out = sources_tool(monkeypatch, [FEED], event_sources=route)
        assert out["event_sources"] == [{"id": "p1", "name": "Shop pixel", "type": None}]


# ============================================================ get_feed_rules

RULES = {"data": [
    {"id": "r1", "attribute": "title", "type": "letter_case_rule", "params": [{"key": "case", "value": "title"}]},
    {"id": "r2", "attribute": "price", "type": "mapping_rule", "params": {"from": "sale_price"}},
]}
ERRORS = {"data": [
    {"id": "e1", "summary": "A warning", "severity": "warning"},
    {"id": "e2", "summary": "A required field is missing: price.", "severity": "fatal"},
    {"id": "e3", "summary": "Another fatal", "severity": "fatal"},
]}
SUGGESTION = {"data": [{"attribute": "price", "type": "mapping_rule",
                        "params": [{"key": "from", "value": "regular_price"}]}]}
UPLOADS = {"data": [{"id": "u_run", "start_time": "2026-10-08T11:00:00+0000"},
                    {"id": "u_done", "start_time": "2026-10-08T10:00:00+0000", "end_time": "2026-10-08T10:05:00+0000"}]}


def rules_tool(monkeypatch, extra=None, **kw):
    from meta_ads_mcp.core.catalogs import get_feed_rules
    routes = {"/f1/rules": RULES, "/f1/uploads": UPLOADS, "/u_done/errors": ERRORS,
              "/e2/suggested_rules": SUGGESTION, "/e3/suggested_rules": {"data": []}, "/e1/suggested_rules": SUGGESTION}
    routes.update(extra or {})
    calls = install(monkeypatch, routes)
    kw.setdefault("feed_id", "f1")
    return get_feed_rules(**kw), calls


class TestFeedRules:
    def test_rules_are_listed_with_params_as_plain_dicts(self, monkeypatch):
        out, _ = rules_tool(monkeypatch)
        rules = out["feeds"][0]["rules"]
        assert out["feeds"][0]["rule_count"] == 2
        assert rules[0] == {"id": "r1", "attribute": "title", "type": "letter_case_rule", "params": {"case": "title"}}
        assert rules[1]["params"] == {"from": "sale_price"}

    def test_suggestions_come_from_the_latest_finished_upload_fatal_errors_first(self, monkeypatch):
        out, calls = rules_tool(monkeypatch)
        feed = out["feeds"][0]
        assert feed["suggestions_from_upload"] == "u_done"  # not the one still running
        assert [s["error"] for s in feed["suggestions"]] == ["A required field is missing: price.", "A warning"]
        assert feed["suggestions"][0]["severity"] == "fatal"
        assert feed["suggestions"][0]["suggested_rules"] == [
            {"attribute": "price", "type": "mapping_rule", "params": {"from": "regular_price"}}]
        order = [c["endpoint"] for c in calls if c["endpoint"].endswith("/suggested_rules")]
        assert order[:2] == ["/e2/suggested_rules", "/e3/suggested_rules"]  # fatal ones are asked first

    def test_errors_without_a_suggestion_are_left_out(self, monkeypatch):
        out, _ = rules_tool(monkeypatch)
        assert "Another fatal" not in [s["error"] for s in out["feeds"][0]["suggestions"]]

    def test_only_the_top_few_errors_are_asked_about(self, monkeypatch):
        many = {"data": [{"id": f"x{i}", "summary": f"err {i}", "severity": "fatal"} for i in range(12)]}
        extra = {"/u_done/errors": many, **{f"/x{i}/suggested_rules": SUGGESTION for i in range(12)}}
        _, calls = rules_tool(monkeypatch, extra)
        assert len([c for c in calls if c["endpoint"].endswith("/suggested_rules")]) == 5

    def test_a_failing_suggestion_call_is_skipped_not_fatal(self, monkeypatch):
        out, _ = rules_tool(monkeypatch, {"/e2/suggested_rules": MetaAPIError("(#100) bad", error_code=100)})
        assert [s["error"] for s in out["feeds"][0]["suggestions"]] == ["A warning"]
        assert "errors" not in out

    def test_suggestions_can_be_turned_off(self, monkeypatch):
        out, calls = rules_tool(monkeypatch, include_suggestions=False)
        assert "suggestions" not in out["feeds"][0]
        assert not [c for c in calls if c["endpoint"] in ("/f1/uploads", "/u_done/errors")]

    def test_rule_fields_fall_back_if_one_is_rejected(self, monkeypatch):
        def route(params, fields):
            if fields:
                return MetaAPIError("(#100) nonexisting field", error_code=100)
            return {"data": [{"id": "r9", "attribute": "brand", "rule_type": "fallback_rule"}]}

        out, _ = rules_tool(monkeypatch, {"/f1/rules": route})
        assert out["feeds"][0]["rules"][0]["type"] == "fallback_rule"

    def test_catalog_mode_covers_each_feed_and_isolates_failures(self, monkeypatch):
        extra = {"/cat1/product_feeds": {"data": [{"id": "f1", "name": "https://shop.example.com/"}, {"id": "f2", "name": "Two"}]},
                 "/f2/rules": MetaAPIError("(#200) denied", error_code=200),
                 "/f2/uploads": {"data": []}}
        out, _ = rules_tool(monkeypatch, extra, feed_id=None, catalog_id="cat1")
        assert [f["feed_id"] for f in out["feeds"]] == ["f1", "f2"]
        assert out["feeds"][0]["name"] == "shop.example.com" and out["feeds"][0]["rules"]
        assert "rules:f2" in out["errors"] and "catalog_management" in out["hint"]

    def test_catalog_mode_caps_the_number_of_feeds(self, monkeypatch):
        many = {"data": [{"id": f"f{i}", "name": f"F{i}"} for i in range(25)]}
        routes = {"/cat1/product_feeds": many, **{f"/f{i}/rules": {"data": []} for i in range(25)},
                  **{f"/f{i}/uploads": {"data": []} for i in range(25)}}
        from meta_ads_mcp.core.catalogs import get_feed_rules
        install(monkeypatch, routes)
        assert len(get_feed_rules(catalog_id="cat1")["feeds"]) == 10

    def test_a_catalog_without_feeds_says_so(self, monkeypatch):
        out, _ = rules_tool(monkeypatch, {"/cat1/product_feeds": {"data": []}}, feed_id=None, catalog_id="cat1")
        assert "no product feeds" in out["note"]

    def test_exactly_one_identifier_is_required(self, monkeypatch):
        from meta_ads_mcp.core.catalogs import get_feed_rules
        assert get_feed_rules()["blocked_at"] == "input_validation"
        assert get_feed_rules(feed_id="f1", catalog_id="cat1")["blocked_at"] == "input_validation"


def test_all_three_are_registered_as_read_only_tools():
    from meta_ads_mcp.server import mcp
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    for name in ("get_catalog_readiness", "get_catalog_data_sources", "get_feed_rules"):
        assert tools[name].annotations.readOnlyHint is True, name
