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
# Built on /{catalog}/diagnostics (Meta's Diagnostics API: types=["EVENT_SOURCE_ISSUES"] and
# affected_channels=["da"]). The catalog-level /da_checks edge answers "nonexisting field" live.

ES_GROUP = {"type": "EVENT_SOURCE_ISSUES", "severity": "MUST_FIX",
            "title": "Your catalog or its products have critical issues affecting your match rate.",
            "subtitle": "Fix these issues first to increase your match rate.", "number_of_affected_items": 788,
            "diagnostics": [{
                "type": "INVALID_CONTENT_ID", "description": "788 items need to be fixed",
                "call_to_action": "For more details please go to Commerce Manager",
                "action_uri": "https://business.facebook.com/commerce/catalogs",
                "details": "Valid content IDs for interacted products are required to match products to this catalog.",
                "event_source_id": 532113215325335, "event_source_type": "Pixel", "event_name": "Lead",
                "number_of_affected_items": 788, "sample_affected_items": [{"content_id": "49839823", "num_events": 788}]}]}
DA_GROUP = {"type": "DA_VISIBILITY_ISSUES", "severity": "MUST_FIX", "title": "Items not shown in dynamic ads",
            "subtitle": "Fix the attributes that keep them out.", "number_of_affected_items": 40,
            "affected_channels": ["da"]}
PIXEL_SOURCE = {"data": [{"id": "p1", "name": "Shop pixel", "source_type": "PIXEL"}]}
PIXEL_PASS = {"key": "pixel_decline", "title": "Event volume is stable", "result": "passed"}
PIXEL_FAIL = {"key": "pixel_missing_param_in_events", "title": "Events include required parameters",
              "result": "failed", "user_message": "Purchase events are missing value.",
              "action_uri": "https://adsmanager.facebook.com/fix"}


def readiness(monkeypatch, es=(), da=(), sources=PIXEL_SOURCE, pixel=None, **kw):
    """es / da: groups returned for each diagnostics request. pixel: payload for /p1/da_checks."""
    from meta_ads_mcp.core.catalogs import get_catalog_readiness

    def diagnostics(params, fields):
        groups = es if "types" in params else da
        return groups if isinstance(groups, Exception) else {"data": list(groups)}

    routes = {"/cat1": {"id": "cat1", "name": "Shop catalog", "product_count": 50, "vertical": "commerce"},
              "/cat1/external_event_sources": sources, "/cat1/diagnostics": diagnostics,
              "/p1/da_checks": pixel if pixel is not None else {"data": [PIXEL_PASS]}}
    calls = install(monkeypatch, routes)
    return get_catalog_readiness("cat1", **kw), calls


class TestReadiness:
    def test_an_event_source_issue_makes_the_catalog_not_ready_with_metas_explanation(self, monkeypatch):
        out, calls = readiness(monkeypatch, es=[ES_GROUP])
        assert out["status"] == "not_ready" and out["summary"]["blocking_issues"] == 1
        issue = out["issues"][0]
        assert issue["severity"] == "HIGH" and issue["check"] == "INVALID_CONTENT_ID"
        assert issue["message"] == "788 items need to be fixed (Lead on Pixel 532113215325335)"
        assert issue["fix"] == "For more details please go to Commerce Manager"
        assert issue["fix_url"] == "https://business.facebook.com/commerce/catalogs"
        asked = next(c for c in calls if c["endpoint"] == "/cat1/diagnostics" and "types" in c["params"])
        assert json.loads(asked["params"]["types"]) == ["EVENT_SOURCE_ISSUES"]

    def test_issues_on_the_dynamic_ads_channel_are_included(self, monkeypatch):
        out, calls = readiness(monkeypatch, da=[DA_GROUP])
        assert out["status"] == "not_ready"
        assert out["issues"][0]["message"] == "Items not shown in dynamic ads"
        asked = next(c for c in calls if "affected_channels" in c["params"])
        assert json.loads(asked["params"]["affected_channels"]) == ["da"]

    def test_the_same_group_returned_by_both_requests_is_reported_once(self, monkeypatch):
        out, _ = readiness(monkeypatch, es=[DA_GROUP], da=[DA_GROUP])
        assert len(out["issues"]) == 1

    def test_nothing_wrong_is_ready(self, monkeypatch):
        out, _ = readiness(monkeypatch)
        assert out["status"] == "ready" and out["issues"] == []
        assert out["summary"] == {"blocking_issues": 0, "issues": 0, "event_sources": 1}
        assert out["catalog"]["name"] == "Shop catalog"

    def test_no_pixel_or_app_connected_blocks_even_with_no_diagnostics(self, monkeypatch):
        out, _ = readiness(monkeypatch, sources={"data": []})
        assert out["status"] == "not_ready"
        assert out["issues"][0]["check"] == "no_event_source_connected"

    def test_warnings_do_not_block(self, monkeypatch):
        warning = {**ES_GROUP, "severity": "WARNING"}
        out, _ = readiness(monkeypatch, es=[warning])
        assert out["status"] == "ready" and out["issues"][0]["severity"] == "MEDIUM"

    def test_a_section_that_errors_is_never_reported_as_clean(self, monkeypatch):
        out, _ = readiness(monkeypatch, es=MetaAPIError("(#200) denied", error_code=200), da=[])
        assert out["status"] == "unknown" and "event_source_issues" in out["errors"]
        assert out["dynamic_ads_issues"] == []  # the other section is still reported
        assert "catalog_management" in out["hint"]

    def test_an_error_does_not_hide_a_real_problem(self, monkeypatch):
        out, _ = readiness(monkeypatch, es=MetaAPIError("(#200) denied", error_code=200), da=[DA_GROUP])
        assert out["status"] == "not_ready" and "errors" in out

    def test_nested_detail_falls_back_to_the_group_if_meta_rejects_the_field(self, monkeypatch):
        from meta_ads_mcp.core.catalogs import get_catalog_readiness
        seen = []

        def diagnostics(params, fields):
            seen.append(list(fields or []))
            if "diagnostics" in (fields or []):
                return MetaAPIError("(#100) nonexisting field (diagnostics)", error_code=100)
            return {"data": [{k: v for k, v in ES_GROUP.items() if k != "diagnostics"}]}

        install(monkeypatch, {"/cat1": {"id": "cat1"}, "/cat1/external_event_sources": PIXEL_SOURCE,
                              "/cat1/diagnostics": diagnostics, "/p1/da_checks": {"data": []}})
        out = get_catalog_readiness("cat1")
        assert "diagnostics" in seen[0] and "diagnostics" not in seen[1]
        assert out["issues"][0]["message"].startswith("Your catalog or its products have critical issues")
        assert "errors" not in out

    def test_a_stray_closing_brace_in_metas_text_is_cleaned(self, monkeypatch):
        group = {**DA_GROUP, "title": "Items not shown in dynamic ads }"}
        out, _ = readiness(monkeypatch, da=[group])
        assert out["issues"][0]["message"] == "Items not shown in dynamic ads"


class TestReadinessPixelChecks:
    def test_a_failed_pixel_check_blocks_and_carries_metas_explanation(self, monkeypatch):
        out, _ = readiness(monkeypatch, pixel={"data": [PIXEL_PASS, PIXEL_FAIL]})
        assert out["status"] == "not_ready"
        issue = out["issues"][0]
        assert issue["message"] == "Shop pixel: Events include required parameters: Purchase events are missing value."
        assert issue["fix_url"] == "https://adsmanager.facebook.com/fix"
        assert out["pixel_checks"][0]["checks"][0]["result"] == "passed"

    def test_unavailable_pixel_checks_are_a_note_not_an_error_or_a_block(self, monkeypatch):
        # Seen live for the catalog-level da_checks edge: Meta answers "nonexisting field"
        out, _ = readiness(monkeypatch, pixel=MetaAPIError("(#100) Tried accessing nonexisting field (da_checks)", error_code=100))
        assert out["status"] == "ready" and "errors" not in out
        assert "Pixel checks for Shop pixel are not available" in out["notes"][0]

    def test_only_pixels_are_checked_and_no_more_than_three(self, monkeypatch):
        many = {"data": [{"id": f"p{i}", "name": f"P{i}", "source_type": "PIXEL"} for i in range(5)]
                + [{"id": "app1", "name": "App", "source_type": "APP"}]}
        from meta_ads_mcp.core.catalogs import get_catalog_readiness
        routes = {"/cat1": {"id": "cat1"}, "/cat1/external_event_sources": many,
                  "/cat1/diagnostics": {"data": []}, **{f"/p{i}/da_checks": {"data": []} for i in range(5)}}
        calls = install(monkeypatch, routes)
        get_catalog_readiness("cat1")
        checked = [c["endpoint"] for c in calls if c["endpoint"].endswith("/da_checks")]
        assert checked == ["/p0/da_checks", "/p1/da_checks", "/p2/da_checks"]

    def test_connection_method_goes_to_the_pixel_checks_uppercased(self, monkeypatch):
        _, calls = readiness(monkeypatch, connection_method="server")
        pixel_call = next(c for c in calls if c["endpoint"] == "/p1/da_checks")
        assert pixel_call["params"] == {"connection_method": "SERVER"}

    def test_invalid_connection_method_is_rejected_without_any_call(self, monkeypatch):
        out, calls = readiness(monkeypatch, connection_method="carrier pigeon")
        assert out["blocked_at"] == "input_validation" and calls == []


# ============================================================ get_catalog_data_sources

FEED = {"id": "f1", "name": "Main feed", "product_count": 120, "ingestion_source_type": "PRIMARY_FEED",
        "schedule": {"interval": "DAILY", "hour": 4, "url": "sftp://user:s3cret@feeds.example.com/x.csv?token=abc",
                     "username": "user"},
        "latest_upload": {"id": "u1", "start_time": "2026-10-08T04:00:00+0000", "end_time": "2026-10-08T04:01:00+0000",
                          "num_detected_items": 120, "num_persisted_items": 118, "num_invalid_items": 2,
                          "input_method": "Server Fetch", "url": "sftp://user:s3cret@feeds.example.com/x.csv"}}
NODE = {"id": "cat1", "name": "Shop catalog", "product_count": 120, "feed_count": 1, "vertical": "commerce"}
SOURCES = {"data": [{"id": "p1", "name": "Shop pixel", "source_type": "PIXEL"}]}


UPLOAD_SESSION = {"id": "u1", "start_time": "2026-10-08T04:00:00+0000", "end_time": "2026-10-08T04:01:00+0000",
                  "num_detected_items": 120, "num_persisted_items": 118, "num_invalid_items": 2,
                  "input_method": "Server Fetch"}


def sources_tool(monkeypatch, feeds, event_sources=SOURCES, node=NODE, uploads=None, **extra):
    from meta_ads_mcp.core.catalogs import get_catalog_data_sources
    routes = {"/cat1": node, "/cat1/product_feeds": {"data": feeds},
              "/cat1/external_event_sources": event_sources,
              "/f1/uploads": uploads if uploads is not None else {"data": [UPLOAD_SESSION]}, **extra}
    install(monkeypatch, routes)
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

    def test_the_last_upload_is_filled_in_from_the_uploads_edge_not_left_null(self, monkeypatch):
        # Seen live: input_method and the item counts were all null, because the summary embedded
        # on the feed has no counts.
        latest = sources_tool(monkeypatch, [FEED])["feeds"][0]["latest_upload"]
        assert latest["items_detected"] == 120 and latest["items_persisted"] == 118 and latest["items_invalid"] == 2
        assert latest["input_method"] == "Server Fetch" and latest["completed"] is True

    def test_the_newest_session_is_used_even_if_listed_last(self, monkeypatch):
        older = {**UPLOAD_SESSION, "id": "old", "start_time": "2026-10-01T04:00:00+0000", "num_detected_items": 5}
        newest = sources_tool(monkeypatch, [FEED], uploads={"data": [older, UPLOAD_SESSION]})["feeds"][0]["latest_upload"]
        assert newest["items_detected"] == 120

    def test_if_the_uploads_edge_fails_the_embedded_summary_is_used(self, monkeypatch):
        out = sources_tool(monkeypatch, [FEED], uploads=MetaAPIError("(#200) denied", error_code=200))
        assert out["feeds"][0]["latest_upload"] == {"start_time": "2026-10-08T04:00:00+0000",
                                                    "end_time": "2026-10-08T04:01:00+0000", "completed": True}
        assert "errors" not in out

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

    def test_suggestion_keys_are_always_present_even_with_no_uploads(self, monkeypatch):
        # Seen live: Mustela (no visible uploads) omitted `suggestions`, other feeds returned an empty list
        out, _ = rules_tool(monkeypatch, {"/f1/uploads": {"data": []}})
        feed = out["feeds"][0]
        assert feed["suggestions"] == [] and feed["suggestions_from_upload"] is None
        assert feed["suggestions_note"] == "No finished upload to take errors from."

    def test_a_feed_with_only_a_running_upload_has_no_finished_upload_to_use(self, monkeypatch):
        running_only = {"data": [{"id": "u_run", "start_time": "2026-10-08T11:00:00+0000"}]}
        out, calls = rules_tool(monkeypatch, {"/f1/uploads": running_only})
        assert out["feeds"][0]["suggestions"] == [] and "suggestions_note" in out["feeds"][0]
        assert not [c for c in calls if c["endpoint"].endswith("/errors")]

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
