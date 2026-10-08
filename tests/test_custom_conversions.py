"""
Tests for list_custom_conversions (/act_<id>/customconversions).

Fields per Meta's reference: id, name, description, rule, custom_event_type, default_conversion_value,
creation_time, first_fired_time, last_fired_time, is_archived, is_unavailable, event_source_type.
In Insights a custom conversion appears as the action type offsite_conversion.custom.<id>.
"""
from datetime import datetime, timezone

import pytest

from meta_ads_mcp.core import pixels
from meta_ads_mcp.core.api import MetaAPIError, api_client

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(api_client, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(pixels, "_utc_now", lambda: NOW)


def cc(cid, name, last="2026-10-05T10:00:00+0000", **over):
    base = {"id": cid, "name": name, "description": f"{name} d", "rule": '{"url":{"i_contains":"thank-you"}}',
            "custom_event_type": "PURCHASE", "default_conversion_value": 25, "creation_time": "2026-01-01T00:00:00+0000",
            "first_fired_time": "2026-01-02T00:00:00+0000", "last_fired_time": last, "is_archived": False,
            "is_unavailable": False, "event_source_type": "PIXEL"}
    base.update(over)
    return base


def install(monkeypatch, pages):
    calls = []

    def fake(endpoint, params=None, fields=None):
        p = dict(params or {})
        calls.append({"endpoint": endpoint, "params": p, "fields": list(fields or [])})
        page = pages[int(p["after"][1:]) if p.get("after") else 0]
        if isinstance(page, Exception):
            raise page
        return page(p, fields) if callable(page) else page

    monkeypatch.setattr(api_client, "graph_get", fake)
    return calls


def run(**kw):
    from meta_ads_mcp.core.pixels import list_custom_conversions
    kw.setdefault("account_id", "1")
    return list_custom_conversions(**kw)


class TestStatuses:
    def test_each_status_is_derived_and_ordered_active_first(self, monkeypatch):
        rows = [cc("5", "Archived", is_archived=True), cc("4", "Broken", is_unavailable=True),
                cc("3", "Never", last=None), cc("2", "Old", last="2026-08-01T00:00:00+0000"), cc("1", "Live")]
        install(monkeypatch, [{"data": rows}])
        out = run(include_archived=True)
        assert [(c["name"], c["status"]) for c in out["conversions"]] == [
            ("Live", "active"), ("Old", "stale"), ("Never", "never_fired"), ("Broken", "unavailable"), ("Archived", "archived")]
        assert out["counts"] == {"active": 1, "stale": 1, "never_fired": 1, "unavailable": 1, "archived": 1}

    def test_archived_are_hidden_by_default(self, monkeypatch):
        install(monkeypatch, [{"data": [cc("1", "Live"), cc("5", "Archived", is_archived=True)]}])
        assert [c["name"] for c in run()["conversions"]] == ["Live"]

    def test_the_stale_line_is_30_days(self, monkeypatch):
        install(monkeypatch, [{"data": [cc("1", "Edge-in", last="2026-09-09T12:00:00+0000"),    # 29 days
                                        cc("2", "Edge-out", last="2026-09-08T12:00:00+0000")]}])  # 30 days
        status = {c["name"]: c["status"] for c in run()["conversions"]}
        assert status == {"Edge-in": "active", "Edge-out": "stale"}

    def test_days_since_last_fired(self, monkeypatch):
        install(monkeypatch, [{"data": [cc("1", "Live", last="2026-10-05T12:00:00+0000")]}])
        assert run()["conversions"][0]["days_since_last_fired"] == 3

    def test_issues_are_ranked_by_what_needs_attention(self, monkeypatch):
        install(monkeypatch, [{"data": [cc("4", "Broken", is_unavailable=True), cc("3", "Never", last=None),
                                        cc("2", "Old", last="2026-08-01T00:00:00+0000")]}])
        issues = {i["check"]: i["severity"] for i in run()["issues"]}
        assert issues == {"unavailable": "MEDIUM", "stale": "LOW", "never_fired": "INFO"}


class TestContent:
    def test_each_conversion_carries_its_insights_action_type(self, monkeypatch):
        install(monkeypatch, [{"data": [cc("123456", "Thank you page")]}])
        c = run()["conversions"][0]
        assert c["insights_action_type"] == "offsite_conversion.custom.123456"
        assert c["custom_event_type"] == "PURCHASE" and c["default_value"] == 25 and c["event_source_type"] == "PIXEL"
        assert c["rule"] == '{"url":{"i_contains":"thank-you"}}'

    def test_a_long_rule_is_trimmed_and_marked_so(self, monkeypatch):
        install(monkeypatch, [{"data": [cc("1", "Long", rule="x" * 1500)]}])
        c = run()["conversions"][0]
        assert len(c["rule"]) == 1000 and c["rule_truncated"] is True

    def test_a_rule_that_fits_is_returned_whole_without_the_marker(self, monkeypatch):
        install(monkeypatch, [{"data": [cc("1", "Edge", rule="x" * 1000)]}])
        c = run()["conversions"][0]
        assert len(c["rule"]) == 1000 and "rule_truncated" not in c

    def test_request_shape(self, monkeypatch):
        calls = install(monkeypatch, [{"data": []}])
        run(account_id="act_9")
        assert calls[0]["endpoint"] == "/act_9/customconversions" and calls[0]["params"]["limit"] == "100"
        assert "last_fired_time" in calls[0]["fields"]

    def test_an_account_without_any_says_so(self, monkeypatch):
        install(monkeypatch, [{"data": []}])
        out = run()
        assert out["total"] == 0 and "No custom conversions" in out["note"]


class TestRobustness:
    def test_fields_fall_back_if_one_is_rejected(self, monkeypatch):
        seen = []

        def fake(endpoint, params=None, fields=None):
            seen.append(list(fields))
            if "description" in fields:
                raise MetaAPIError("(#100) nonexisting field (description)", error_code=100)
            return {"data": [{"id": "1", "name": "Basic", "rule": "r", "custom_event_type": "LEAD",
                              "last_fired_time": "2026-10-05T10:00:00+0000"}]}

        monkeypatch.setattr(api_client, "graph_get", fake)
        out = run()
        assert "description" in seen[0] and "description" not in seen[1]
        assert out["conversions"][0]["name"] == "Basic" and out["conversions"][0]["status"] == "active"

    def test_pages_are_followed(self, monkeypatch):
        pages = [{"data": [cc("1", "One")], "paging": {"next": "x", "cursors": {"after": "c1"}}}, {"data": [cc("2", "Two")]}]
        install(monkeypatch, pages)
        assert {c["name"] for c in run()["conversions"]} == {"One", "Two"}

    def test_a_failure_returns_an_error_with_a_hint(self, monkeypatch):
        install(monkeypatch, [MetaAPIError("(#200) denied", error_code=200)])
        out = run()
        assert out["error_code"] == 200 and "ads_read" in out["hint"]

    def test_registered_as_read_only_tool(self):
        from meta_ads_mcp.server import mcp
        tools = {t.name: t for t in mcp._tool_manager.list_tools()}
        assert tools["list_custom_conversions"].annotations.readOnlyHint is True
