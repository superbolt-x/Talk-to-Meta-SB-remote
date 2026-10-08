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


def make_graph(feeds=None, uploads=None, errors=None, diagnostics=None, catalog=None):
    """Fake graph_get routing by endpoint suffix. Values may be dicts or exceptions."""
    feed_list = feeds if feeds is not None else [DAILY_FEED]
    catalog_node = catalog if catalog is not None else {
        "id": "cat1", "name": "Test catalog", "product_count": 1000, "feed_count": len(feed_list),
        "vertical": "commerce"}
    routes = {
        "/product_feeds": {"data": feeds if feeds is not None else [DAILY_FEED]},
        "/uploads": {"data": uploads if uploads is not None else []},
        "/errors": errors if errors is not None else {"data": []},
        "/diagnostics": diagnostics if diagnostics is not None else {"data": []},
    }

    def fake(endpoint, params=None, fields=None):
        if endpoint == "/cat1":
            if isinstance(catalog_node, Exception):
                raise catalog_node
            return catalog_node
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

    def test_empty_feed_with_no_uploads_is_high(self, monkeypatch):
        empty = {"id": "f1", "name": "Empty", "product_count": 0}
        out = run(monkeypatch, feeds=[empty], uploads=[])
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


class TestFailedAndAbandonedFeeds:
    """Shape seen on a real catalog: supplementary feed, no schedule, uploads failing since May."""

    ETTIKA_FEED = {"id": "f1", "name": "Meta Multi Treatment", "product_count": 0,
                   "schedule": None, "ingestion_source_type": "SUPPLEMENTARY_FEED"}
    AUTH_ERROR = {"summary": {"total_count": 1}, "data": [
        {"id": 1, "summary": "HTTP Authentication Failed", "severity": "fatal",
         "description": "Meta could not log in to your feed URL.", "samples": {"data": []}}]}

    def failed_upload(self, uid, start, **kw):
        return upload(uid, start, detected=0, persisted=0, invalid=0, errors=kw.get("errors", 1))

    def test_failed_upload_is_flagged_with_the_fatal_reason(self, monkeypatch):
        out = run(monkeypatch, feeds=[self.ETTIKA_FEED], errors=self.AUTH_ERROR,
                  uploads=[self.failed_upload("u5", "2026-05-22T10:00:00+0000"),
                           self.failed_upload("u4", "2026-05-22T09:00:00+0000")])
        failed = next(i for i in out["issues"] if i["check"] == "upload_failed")
        assert failed["severity"] == "HIGH"
        assert failed["reason"] == "HTTP Authentication Failed"
        assert "HTTP Authentication Failed" in failed["message"]
        assert "no_items_persisted" not in checks(out)
        assert out["health"] == "partial"

    def test_abandoned_unscheduled_feed_is_noted(self, monkeypatch):
        out = run(monkeypatch, feeds=[self.ETTIKA_FEED], errors=self.AUTH_ERROR,
                  uploads=[self.failed_upload("u5", "2026-05-22T10:00:00+0000")])
        inactive = next(i for i in out["issues"] if i["check"] == "feed_inactive")
        assert inactive["severity"] == "LOW"
        assert "139 days" in inactive["message"]  # 2026-05-22 -> 2026-10-08

    def test_supplementary_only_catalog_gets_integration_note(self, monkeypatch):
        out = run(monkeypatch, feeds=[self.ETTIKA_FEED],
                  uploads=[upload("u1", "2026-10-07T06:00:00+0000")])
        note = next(i for i in out["issues"] if i["check"] == "no_primary_feed")
        assert note["severity"] == "INFO"
        assert "Shopify" in note["message"]

    def test_primary_feed_present_means_no_integration_note(self, monkeypatch):
        primary = dict(DAILY_FEED, ingestion_source_type="PRIMARY_FEED")
        out = run(monkeypatch, feeds=[primary, self.ETTIKA_FEED],
                  uploads=[upload("u1", "2026-10-08T06:00:00+0000")])
        assert "no_primary_feed" not in checks(out)

    def test_failure_only_visible_in_error_sample_is_still_raised(self, monkeypatch):
        # error_count came back 0, but the sample has a fatal and no item was ever read
        out = run(monkeypatch, feeds=[self.ETTIKA_FEED], errors=self.AUTH_ERROR,
                  uploads=[self.failed_upload("u1", "2026-10-07T06:00:00+0000", errors=0)])
        failed = next(i for i in out["issues"] if i["check"] == "upload_failed")
        assert failed["reason"] == "HTTP Authentication Failed"
        assert len([i for i in out["issues"] if i["check"] == "upload_failed"]) == 1

    def test_failed_upload_without_error_sampling_still_flagged_by_counts(self, monkeypatch):
        out = run(monkeypatch, feeds=[self.ETTIKA_FEED], include_errors=False,
                  uploads=[self.failed_upload("u1", "2026-10-07T06:00:00+0000")])
        failed = next(i for i in out["issues"] if i["check"] == "upload_failed")
        assert "reason" not in failed

    def test_inactivity_threshold_is_30_days(self, monkeypatch):
        manual = {"id": "f1", "name": "Manual", "product_count": 10}
        for start, expected in (("2026-09-09T12:00:00+0000", False),   # 29 days
                                ("2026-09-07T12:00:00+0000", True)):    # 31 days
            out = run(monkeypatch, feeds=[manual], uploads=[upload("u1", start)])
            assert ("feed_inactive" in checks(out)) is expected, start

    def test_scheduled_feed_uses_staleness_not_inactivity(self, monkeypatch):
        out = run(monkeypatch, uploads=[upload("u1", "2026-08-01T12:00:00+0000")])
        assert "feed_stale" in checks(out)
        assert "feed_inactive" not in checks(out)


class TestLiveFindings:
    """Behaviours found by running the tool against real catalogs."""

    def test_feed_with_products_but_no_visible_uploads_is_not_called_never_uploaded(self, monkeypatch):
        # Seen live: product_count 85, recent_uploads [] -> was a false HIGH "never uploaded"
        feed = {"id": "f1", "name": "Shopify-fed", "product_count": 85}
        out = run(monkeypatch, feeds=[feed], uploads=[])
        assert "feed_never_uploaded" not in checks(out)
        note = next(i for i in out["issues"] if i["check"] == "upload_sessions_not_visible")
        assert note["severity"] == "INFO" and "85 products" in note["message"]
        assert out["health"] == "healthy"

    def test_update_schedule_counts_when_schedule_is_null(self, monkeypatch):
        # Seen live: supplementary feeds uploading hourly with schedule null
        feed = {"id": "f1", "name": "Supplementary", "product_count": 5, "schedule": None,
                "update_schedule": {"interval": "HOURLY"}}
        out = run(monkeypatch, feeds=[feed], uploads=[upload("u1", "2026-10-08T01:00:00+0000")])  # 11h old
        assert "feed_stale" in checks(out)
        assert "feed_inactive" not in checks(out)

    def test_schedule_urls_and_credentials_are_never_returned(self, monkeypatch):
        import json
        feed = {"id": "f1", "name": "SFTP feed", "product_count": 5,
                "schedule": {"interval": "DAILY", "hour": 18, "timezone": "America/New_York",
                             "url": "sftp://feeduser:s3cret@feeds.example.com/path/file.csv?token=abc123",
                             "username": "feeduser", "password": "s3cret"},
                "update_schedule": {"interval": "HOURLY", "url": "https://docs.google.com/spreadsheets/d/SHEETID/export"}}
        out = run(monkeypatch, feeds=[feed], uploads=[upload("u1", "2026-10-08T06:00:00+0000")])
        sched = out["feeds"][0]["schedule"]
        assert sched == {"interval": "DAILY", "hour": 18, "timezone": "America/New_York",
                         "source_host": "feeds.example.com"}
        assert out["feeds"][0]["update_schedule"] == {"interval": "HOURLY", "source_host": "docs.google.com"}
        blob = json.dumps(out)
        for secret in ("feeduser", "s3cret", "abc123", "SHEETID", "file.csv"):
            assert secret not in blob, secret

    def test_get_catalog_info_strips_feed_urls_and_credentials(self, monkeypatch):
        import json
        from meta_ads_mcp.core.catalogs import get_catalog_info

        feed = {"id": "f1", "name": "F", "product_count": 5,
                "schedule": {"interval": "DAILY", "url": "https://u:p@host.example.com/f.csv?k=v", "username": "u"},
                "latest_upload": {"id": "u1", "url": "https://u:p@host.example.com/f.csv?k=v", "username": "u"}}

        def fake(endpoint, params=None, fields=None):
            if endpoint.endswith("/product_feeds"):
                return {"data": [feed]}
            return {"id": "cat1", "data": []}

        monkeypatch.setattr(api_client, "graph_get", fake)
        out = get_catalog_info("cat1")
        blob = json.dumps(out["feeds"])
        assert "host.example.com" in blob  # the host is kept
        for secret in ('"u"', ":p@", "k=v", "f.csv", "username"):
            assert secret not in blob, secret

    def test_very_stale_scheduled_feed_is_critical_and_degrades_health(self, monkeypatch):
        # Seen live: daily feed last fetched 1986h (83 days) ago was only "partial"
        out = run(monkeypatch, uploads=[upload("u1", "2026-07-17T12:00:00+0000")])
        stale = next(i for i in out["issues"] if i["check"] == "feed_stale")
        assert stale["severity"] == "CRITICAL" and "looks dead" in stale["message"]
        assert out["health"] == "degraded"

    def test_stale_severity_boundaries(self, monkeypatch):
        hourly = {"id": "f1", "name": "Hourly", "product_count": 5, "schedule": {"interval": "HOURLY"}}
        cases = [
            (DAILY_FEED, "2026-10-06T10:00:00+0000", "HIGH"),      # 50h: stale, not dead
            (DAILY_FEED, "2026-10-01T10:00:00+0000", "HIGH"),      # 170h > 168h but < 10 x 24h
            (DAILY_FEED, "2026-09-28T10:00:00+0000", "CRITICAL"),  # 266h >= 240h
            (hourly, "2026-10-05T12:00:00+0000", "HIGH"),          # 72h: below the 168h floor
            (hourly, "2026-09-30T12:00:00+0000", "CRITICAL"),      # 192h >= 168h
        ]
        for feed, start, severity in cases:
            out = run(monkeypatch, feeds=[feed], uploads=[upload("u1", start)])
            stale = next(i for i in out["issues"] if i["check"] == "feed_stale")
            assert stale["severity"] == severity, (feed["name"], start)

    def test_daily_feed_27h_old_is_not_stale_threshold_is_two_intervals(self, monkeypatch):
        fresh = run(monkeypatch, uploads=[upload("u1", "2026-10-07T09:00:00+0000")])   # 27h
        stale = run(monkeypatch, uploads=[upload("u1", "2026-10-06T11:00:00+0000")])   # 49h
        assert "feed_stale" not in checks(fresh)
        assert "feed_stale" in checks(stale)

    def test_per_feed_issues_are_sorted_most_severe_first(self, monkeypatch):
        feed = {"id": "f1", "name": "Ettika-like", "product_count": 0, "schedule": None,
                "ingestion_source_type": "SUPPLEMENTARY_FEED"}
        errors = {"data": [{"id": 1, "summary": "HTTP Authentication Failed", "severity": "fatal"}]}
        out = run(monkeypatch, feeds=[feed], errors=errors,
                  uploads=[upload("u1", "2026-05-22T10:00:00+0000", detected=0, persisted=0, errors=1)])
        order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
        severities = [i["severity"] for i in out["feeds"][0]["issues"]]
        assert len(severities) >= 2
        assert severities == sorted(severities, key=order.get)

    def test_catalog_context_included(self, monkeypatch):
        out = run(monkeypatch, uploads=[upload("u1", "2026-10-08T06:00:00+0000")])
        assert out["catalog"]["name"] == "Test catalog"
        assert out["catalog"]["feed_count"] == 1

    def test_feed_count_mismatch_is_surfaced(self, monkeypatch):
        # Seen live: catalog says feed_count 3, the feeds edge listed 2
        node = {"id": "cat1", "name": "JMC", "product_count": 50, "feed_count": 3, "vertical": "commerce"}
        out = run(monkeypatch, catalog=node, uploads=[upload("u1", "2026-10-08T06:00:00+0000")])
        note = next(i for i in out["issues"] if i["check"] == "feed_count_mismatch")
        assert note["severity"] == "INFO" and "3 feeds" in note["message"] and "only 1" in note["message"]

    def test_matching_feed_count_has_no_mismatch_note(self, monkeypatch):
        out = run(monkeypatch, uploads=[upload("u1", "2026-10-08T06:00:00+0000")])
        assert "feed_count_mismatch" not in checks(out)

    def test_mismatch_not_reported_when_a_single_feed_was_requested(self, monkeypatch):
        node = {"id": "cat1", "name": "JMC", "feed_count": 3}
        from meta_ads_mcp.core.catalogs import get_catalog_feed_health

        def fake(endpoint, params=None, fields=None):
            if endpoint == "/cat1":
                return node
            if endpoint == "/f1":
                return DAILY_FEED
            if endpoint.endswith("/uploads"):
                return {"data": [upload("u1", "2026-10-08T06:00:00+0000")]}
            return {"data": []}

        monkeypatch.setattr(api_client, "graph_get", fake)
        out = get_catalog_feed_health("cat1", feed_id="f1")
        assert "feed_count_mismatch" not in checks(out)

    def test_catalog_context_failure_is_not_fatal(self, monkeypatch):
        out = run(monkeypatch, catalog=MetaAPIError("(#200) no access", error_code=200),
                  uploads=[upload("u1", "2026-10-08T06:00:00+0000")])
        assert "catalog" not in out
        assert "errors" not in out
        assert out["health"] == "healthy"

    def test_stray_closing_brace_in_meta_text_is_cleaned(self, monkeypatch):
        diag = {"data": [
            {"type": "SHOPS_VISIBILITY_ISSUES", "severity": "MUST_FIX", "title": "Products not being shown",
             "subtitle": "Add the missing fields }", "number_of_affected_items": 462},
            {"type": "OTHER", "severity": "OPPORTUNITY", "title": "Keep {placeholder}", "subtitle": "x"},
        ]}
        out = run(monkeypatch, uploads=[upload("u1", "2026-10-08T06:00:00+0000")], diagnostics=diag)
        by_type = {g["type"]: g for g in out["diagnostics"]}
        assert by_type["SHOPS_VISIBILITY_ISSUES"]["subtitle"] == "Add the missing fields"
        assert out["issues"][0]["fix"] == "Add the missing fields"
        assert by_type["OTHER"]["title"] == "Keep {placeholder}"  # balanced braces untouched


class TestUploadInFlight:
    """Seen live: hourly feeds are mid-upload for ~10 minutes of every hour. The running upload reads
    0 items, which used to hide the previous upload's real problems (JMC lost its CRITICAL
    invalid-items flag, Seed lost its warning) so `health` flipped depending on when you called."""

    RUNNING = dict(end=None, detected=0, persisted=0)

    def running(self, uid="u_run", start="2026-10-08T11:30:00+0000"):
        return upload(uid, start, **self.RUNNING)

    def test_problems_in_the_last_completed_upload_survive_a_running_one(self, monkeypatch):
        out = run(monkeypatch, uploads=[
            self.running(),
            upload("u_done", "2026-10-08T10:20:00+0000", detected=67, persisted=43, invalid=24, errors=24),
        ])
        invalid = next(i for i in out["issues"] if i["check"] == "invalid_items")
        assert invalid["severity"] == "CRITICAL" and "24 of 67" in invalid["message"]
        assert out["health"] == "degraded"
        assert out["feeds"][0]["latest_upload_in_progress"] is True

    def test_warning_is_not_lost_while_an_upload_runs(self, monkeypatch):
        out = run(monkeypatch, uploads=[
            self.running(),
            upload("u_done", "2026-10-08T10:55:00+0000", warnings=12),
        ])
        assert "upload_warnings" in checks(out)

    def test_error_sample_comes_from_the_completed_upload_not_the_running_one(self, monkeypatch):
        from meta_ads_mcp.core.catalogs import get_catalog_feed_health

        sampled = []

        def fake(endpoint, params=None, fields=None):
            if endpoint == "/cat1":
                return {"id": "cat1", "name": "C", "feed_count": 1}
            if endpoint.endswith("/product_feeds"):
                return {"data": [DAILY_FEED]}
            if endpoint.endswith("/uploads"):
                return {"data": [self.running(), upload("u_done", "2026-10-08T10:20:00+0000", errors=3)]}
            if endpoint.endswith("/errors"):
                sampled.append(endpoint)
                return {"data": []}
            return {"data": []}

        monkeypatch.setattr(api_client, "graph_get", fake)
        out = get_catalog_feed_health("cat1")
        assert sampled == ["/u_done/errors"]
        assert out["feeds"][0]["errors_from_upload"] == "u_done"

    def test_failed_upload_is_judged_on_the_completed_one(self, monkeypatch):
        errors = {"data": [{"id": 1, "summary": "HTTP Authentication Failed", "severity": "fatal"}]}
        feed = {"id": "f1", "name": "F", "product_count": 0, "schedule": {"interval": "HOURLY"}}
        out = run(monkeypatch, feeds=[feed], errors=errors, uploads=[
            self.running(),
            upload("u_done", "2026-10-08T10:20:00+0000", detected=0, persisted=0, errors=1),
        ])
        failed = next(i for i in out["issues"] if i["check"] == "upload_failed")
        assert failed["reason"] == "HTTP Authentication Failed"

    def test_a_feed_with_only_a_running_upload_gets_no_quality_verdict(self, monkeypatch):
        out = run(monkeypatch, uploads=[self.running()])
        assert checks(out) <= {"upload_not_finished"}
        assert "no_items_persisted" not in checks(out)
        assert out["health"] in ("healthy", "partial")  # never degraded from an empty in-flight upload

    def test_item_drop_compares_completed_uploads_only(self, monkeypatch):
        out = run(monkeypatch, uploads=[
            self.running(),
            upload("u2", "2026-10-08T10:20:00+0000", detected=600, persisted=600),
            upload("u1", "2026-10-08T09:20:00+0000", persisted=1000),
        ])
        assert "item_count_drop" in checks(out)

    def test_a_running_upload_still_counts_as_recent_activity_for_staleness(self, monkeypatch):
        feed = {"id": "f1", "name": "F", "product_count": 5, "schedule": {"interval": "HOURLY"}}
        out = run(monkeypatch, feeds=[feed], uploads=[
            self.running(start="2026-10-08T11:45:00+0000"),
            upload("u_done", "2026-10-08T08:00:00+0000"),  # 4h old: stale on its own
        ])
        assert "feed_stale" not in checks(out)

    def test_upload_that_has_run_for_hours_is_still_flagged(self, monkeypatch):
        out = run(monkeypatch, uploads=[self.running(start="2026-10-08T06:00:00+0000"),
                                        upload("u_done", "2026-10-08T05:00:00+0000")])
        assert "upload_not_finished" in checks(out)


class TestUrlValuedNames:
    """Seen live: a Shopify-fed feed whose name and file_name were its own URL."""

    FEED = {"id": "f1", "name": "https://user:pw@mustelausa-dev.myshopify.com/", "product_count": 85,
            "file_name": "https://mustelausa-dev.myshopify.com/feed.csv?token=zzz"}

    def test_feed_health_shows_only_the_host(self, monkeypatch):
        import json
        out = run(monkeypatch, feeds=[self.FEED], uploads=[])
        report = out["feeds"][0]
        assert report["name"] == "mustelausa-dev.myshopify.com"
        assert report["file_name"] == "mustelausa-dev.myshopify.com"
        blob = json.dumps(out)
        for leaked in ("https://", "user:pw", "feed.csv", "zzz"):
            assert leaked not in blob, leaked
        assert "mustelausa-dev.myshopify.com: Meta returned no upload sessions" in out["issues"][0]["message"]

    def test_ordinary_names_are_untouched(self, monkeypatch):
        feed = {"id": "f1", "name": "Main feed (daily)", "product_count": 5, "file_name": "products.csv"}
        out = run(monkeypatch, feeds=[feed], uploads=[upload("u1", "2026-10-08T06:00:00+0000")])
        assert out["feeds"][0]["name"] == "Main feed (daily)"
        assert out["feeds"][0]["file_name"] == "products.csv"

    def test_get_catalog_info_hides_url_names_too(self, monkeypatch):
        from meta_ads_mcp.core.catalogs import get_catalog_info

        def fake(endpoint, params=None, fields=None):
            if endpoint.endswith("/product_feeds"):
                return {"data": [self.FEED]}
            return {"id": "cat1", "data": []}

        monkeypatch.setattr(api_client, "graph_get", fake)
        feeds = get_catalog_info("cat1")["feeds"]
        assert feeds[0]["name"] == "mustelausa-dev.myshopify.com"
        assert "https://" not in str(feeds)


def test_registered_as_read_only_tool():
    from meta_ads_mcp.server import mcp
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    assert tools["get_catalog_feed_health"].annotations.readOnlyHint is True
