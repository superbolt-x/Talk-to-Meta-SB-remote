"""
Tests for get_catalog_feed_health.

Response shapes follow Meta's Feed API guide (uploads, errors) and the catalog
diagnostics edge:
https://developers.facebook.com/documentation/ads-commerce/catalog/guides/feed-api
"""
from datetime import datetime, timezone

import pytest

from meta_ads_mcp.core import catalogs
from meta_ads_mcp.core.api import MetaAPIError, api_client

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(api_client, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(catalogs, "_utcnow", lambda: NOW)


def upload(uid, start, end="same", detected=1000, persisted=1000, invalid=0, errors=0, warnings=0):
    return {
        "id": uid, "start_time": start,
        "end_time": start if end == "same" else end,
        "num_detected_items": detected, "num_persisted_items": persisted,
        "num_invalid_items": invalid, "num_deleted_items": 0,
        "error_count": errors, "warning_count": warnings, "input_method": "Server Fetch",
    }


DAILY_FEED = {"id": "f1", "name": "Main feed", "product_count": 1000,
              "schedule": {"interval": "DAILY", "url": "https://shop.example/feed.csv"}}


def make_graph(feeds=None, uploads=None, errors=None, diagnostics=None):
    """Fake graph_get routing by endpoint suffix. Values may be dicts or exceptions."""
    routes = {
        "/product_feeds": {"data": feeds if feeds is not None else [DAILY_FEED]},
        "/uploads": {"data": uploads if uploads is not None else []},
        "/errors": errors if errors is not None else {"data": []},
        "/diagnostics": diagnostics if diagnostics is not None else {"data": []},
    }

    def fake(endpoint, params=None, fields=None):
        for suffix, payload in routes.items():
            if endpoint.endswith(suffix):
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise AssertionError(f"unexpected endpoint {endpoint}")

    return fake


def run(monkeypatch, **kw):
    from meta_ads_mcp.core.catalogs import get_catalog_feed_health
    call_kw = {k: kw.pop(k) for k in list(kw) if k in ("feed_id", "upload_limit", "include_errors", "include_diagnostics")}
    monkeypatch.setattr(api_client, "graph_get", make_graph(**kw))
    return get_catalog_feed_health("cat1", **call_kw)


def checks(out):
    return {i["check"] for i in out["issues"]}


class TestHealthyAndStale:
    def test_healthy_feed(self, monkeypatch):
        out = run(monkeypatch, uploads=[upload("u1", "2026-10-08T06:00:00+0000")])
        assert out["health"] == "healthy"
        assert out["issues"] == []
        assert out["feeds"][0]["recent_uploads"][0]["items_persisted"] == 1000

    def test_stale_daily_feed_flagged(self, monkeypatch):
        # 3 days old vs a daily schedule (tolerance 2x = 48h)
        out = run(monkeypatch, uploads=[upload("u1", "2026-10-05T12:00:00+0000")])
        assert "feed_stale" in checks(out)
        assert out["health"] == "partial"

    def test_manual_feed_without_schedule_never_stale(self, monkeypatch):
        manual = {"id": "f1", "name": "Manual", "product_count": 10}
        out = run(monkeypatch, feeds=[manual], uploads=[upload("u1", "2026-01-01T00:00:00+0000")])
        assert "feed_stale" not in checks(out)

    def test_no_uploads_is_high(self, monkeypatch):
        out = run(monkeypatch, uploads=[])
        assert "feed_never_uploaded" in checks(out)

    def test_falls_back_to_latest_upload_embedded_on_feed(self, monkeypatch):
        feed = dict(DAILY_FEED, latest_upload=upload("u9", "2026-10-08T06:00:00+0000"))
        out = run(monkeypatch, feeds=[feed], uploads=[])
        assert out["feeds"][0]["recent_uploads"][0]["id"] == "u9"
        assert "feed_never_uploaded" not in checks(out)


class TestUploadQuality:
    def test_invalid_ratio_severity_bands(self, monkeypatch):
        for invalid, severity in ((300, "CRITICAL"), (100, "HIGH"), (10, "MEDIUM")):
            out = run(monkeypatch, uploads=[upload("u1", "2026-10-08T06:00:00+0000",
                                                   persisted=1000 - invalid, invalid=invalid)])
            issue = next(i for i in out["issues"] if i["check"] == "invalid_items")
            assert issue["severity"] == severity, invalid

    def test_nothing_persisted_is_critical_without_double_reporting(self, monkeypatch):
        out = run(monkeypatch, uploads=[upload("u1", "2026-10-08T06:00:00+0000",
                                               persisted=0, invalid=1000)])
        assert "no_items_persisted" in checks(out)
        assert "invalid_items" not in checks(out)
        assert out["health"] == "degraded"

    def test_item_count_drop_vs_previous_upload(self, monkeypatch):
        out = run(monkeypatch, uploads=[
            upload("u2", "2026-10-08T06:00:00+0000", detected=600, persisted=600),
            upload("u1", "2026-10-07T06:00:00+0000", persisted=1000),
        ])
        issue = next(i for i in out["issues"] if i["check"] == "item_count_drop")
        assert "40%" in issue["message"]

    def test_small_change_not_flagged(self, monkeypatch):
        out = run(monkeypatch, uploads=[
            upload("u2", "2026-10-08T06:00:00+0000", persisted=950, detected=950),
            upload("u1", "2026-10-07T06:00:00+0000", persisted=1000),
        ])
        assert "item_count_drop" not in checks(out)

    def test_uploads_sorted_newest_first_regardless_of_api_order(self, monkeypatch):
        out = run(monkeypatch, uploads=[
            upload("old", "2026-10-06T06:00:00+0000"),
            upload("new", "2026-10-08T06:00:00+0000"),
        ])
        assert [u["id"] for u in out["feeds"][0]["recent_uploads"]] == ["new", "old"]

    def test_unfinished_upload_flagged(self, monkeypatch):
        out = run(monkeypatch, uploads=[upload("u1", "2026-10-08T08:00:00+0000", end=None)])
        assert "upload_not_finished" in checks(out)


class TestErrorsAndDiagnostics:
    ERRORS = {"summary": {"total_count": 2}, "data": [
        {"id": 2, "summary": "GTIN is incorrectly formatted", "description": "d", "severity": "warning",
         "samples": {"data": [{"row_number": 4, "retailer_id": "r4", "id": "p4"}]}},
        {"id": 1, "summary": "A required field is missing: price.", "description": "d", "severity": "fatal",
         "samples": {"data": [{"row_number": i, "retailer_id": f"r{i}", "id": f"p{i}"} for i in range(6)]}},
    ]}

    def test_errors_fatal_first_and_samples_capped(self, monkeypatch):
        out = run(monkeypatch, uploads=[upload("u1", "2026-10-08T06:00:00+0000", errors=2)], errors=self.ERRORS)
        feed = out["feeds"][0]
        assert [e["severity"] for e in feed["latest_upload_errors"]] == ["fatal", "warning"]
        assert len(feed["latest_upload_errors"][0]["samples"]) == catalogs.MAX_ERROR_SAMPLES
        assert feed["latest_upload_error_total"] == 2

    def test_include_errors_false_skips_call(self, monkeypatch):
        out = run(monkeypatch, uploads=[upload("u1", "2026-10-08T06:00:00+0000")],
                  errors=MetaAPIError("should not be called", error_code=1), include_errors=False)
        assert "latest_upload_errors" not in out["feeds"][0]
        assert "errors" not in out

    def test_diagnostics_sorted_must_fix_first_and_raise_issues(self, monkeypatch):
        diag = {"data": [
            {"type": "IMAGE_QUALITY", "severity": "OPPORTUNITY", "title": "Low-res images",
             "number_of_affected_items": 500},
            {"type": "ATTRIBUTES_MISSING", "severity": "MUST_FIX", "title": "Missing GTIN",
             "subtitle": "Add GTIN", "number_of_affected_items": 40},
            {"type": "POLICY_VIOLATION", "severity": "MUST_FIX", "title": "Policy", "number_of_affected_items": 90},
        ]}
        out = run(monkeypatch, uploads=[upload("u1", "2026-10-08T06:00:00+0000")], diagnostics=diag)
        assert [g["title"] for g in out["diagnostics"]] == ["Policy", "Missing GTIN", "Low-res images"]
        assert out["health"] == "partial"  # MUST_FIX => HIGH
        top = out["issues"][0]
        assert top["severity"] == "HIGH" and "Policy (90 items)" in top["message"]
        assert out["issues"][-1]["severity"] == "LOW"


class TestFallbacksAndPartialFailure:
    def test_feed_fields_fall_back_when_rejected(self, monkeypatch):
        from meta_ads_mcp.core.catalogs import get_catalog_feed_health

        seen = []

        def fake(endpoint, params=None, fields=None):
            seen.append((endpoint, fields))
            if endpoint.endswith("/product_feeds"):
                if "item_count" in (fields or []):
                    raise MetaAPIError("(#100) nonexisting field (item_count)", error_code=100)
                return {"data": [DAILY_FEED]}
            if endpoint.endswith("/uploads"):
                return {"data": [upload("u1", "2026-10-08T06:00:00+0000")]}
            return {"data": []}

        monkeypatch.setattr(api_client, "graph_get", fake)
        out = get_catalog_feed_health("cat1")
        feed_calls = [f for e, f in seen if e.endswith("/product_feeds")]
        assert len(feed_calls) == 2 and feed_calls[1] == catalogs.FEED_FIELDS_BASIC
        assert out["feeds"][0]["name"] == "Main feed"
        assert "errors" not in out

    def test_diagnostics_permission_error_does_not_hide_feeds(self, monkeypatch):
        out = run(monkeypatch, uploads=[upload("u1", "2026-10-08T06:00:00+0000")],
                  diagnostics=MetaAPIError("(#200) Permissions error", error_code=200))
        assert out["feeds"][0]["recent_uploads"]
        assert "diagnostics" in out["errors"]
        assert "catalog_management" in out["hint"]
        assert out["health"] == "unknown"  # clean but incomplete is not reported as healthy

    def test_feed_listing_failure_reported(self, monkeypatch):
        from meta_ads_mcp.core.catalogs import get_catalog_feed_health

        def boom(endpoint, params=None, fields=None):
            if endpoint.endswith("/product_feeds"):
                raise MetaAPIError("(#200) no access", error_code=200)
            return {"data": []}

        monkeypatch.setattr(api_client, "graph_get", boom)
        out = get_catalog_feed_health("cat1")
        assert "feeds" in out["errors"]
        assert out["feeds"] == []

    def test_catalog_without_feeds_is_info_only(self, monkeypatch):
        out = run(monkeypatch, feeds=[])
        assert checks(out) == {"feed_exists"}
        assert out["health"] == "healthy"

    def test_feed_id_reads_that_feed_directly(self, monkeypatch):
        from meta_ads_mcp.core.catalogs import get_catalog_feed_health

        endpoints = []

        def fake(endpoint, params=None, fields=None):
            endpoints.append(endpoint)
            if endpoint == "/f7":
                return dict(DAILY_FEED, id="f7")
            if endpoint.endswith("/uploads"):
                return {"data": [upload("u1", "2026-10-08T06:00:00+0000")]}
            return {"data": []}

        monkeypatch.setattr(api_client, "graph_get", fake)
        out = get_catalog_feed_health("cat1", feed_id="f7")
        assert "/cat1/product_feeds" not in endpoints
        assert out["feeds"][0]["id"] == "f7"

    def test_upload_limit_clamped(self, monkeypatch):
        from meta_ads_mcp.core.catalogs import get_catalog_feed_health

        limits = []

        def fake(endpoint, params=None, fields=None):
            if endpoint.endswith("/uploads"):
                limits.append(params["limit"])
            return {"data": [DAILY_FEED]} if endpoint.endswith("/product_feeds") else {"data": []}

        monkeypatch.setattr(api_client, "graph_get", fake)
        get_catalog_feed_health("cat1", upload_limit=999)
        assert limits == [str(catalogs.MAX_UPLOADS)]


def test_registered_as_read_only_tool():
    from meta_ads_mcp.server import mcp
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    assert tools["get_catalog_feed_health"].annotations.readOnlyHint is True
