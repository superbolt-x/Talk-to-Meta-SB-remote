"""
Tests for get_activity_log: the account change history (/act_<id>/activities).

AdActivity fields per Meta's reference: actor_id, actor_name, application_name, date_time_in_timezone,
event_time, event_type, extra_data (JSON string), object_id, object_name, object_type, translated_event_type.
"""
import json
from datetime import datetime, timezone

import pytest

from meta_ads_mcp.core import activity
from meta_ads_mcp.core.api import MetaAPIError, api_client

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(api_client, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(activity, "_utc_now", lambda: NOW)


def ev(n, event_type="update_campaign_budget", obj="c1", actor="Ana", **over):
    base = {"actor_id": "u1", "actor_name": actor, "application_name": "Ads Manager",
            "event_time": f"2026-10-07T10:{n:02d}:00+0000", "date_time_in_timezone": f"10/07/2026 6:{n:02d}am",
            "event_type": event_type, "translated_event_type": event_type.replace("_", " ").capitalize(),
            "object_id": obj, "object_name": f"Object {obj}", "object_type": "CAMPAIGN",
            "extra_data": json.dumps({"old_value": "5000", "new_value": "7500"})}
    base.update(over)
    return base


def install(monkeypatch, pages):
    """pages: list of payloads served in order (the `after` cursor selects the page). Records calls."""
    calls = []

    def fake(endpoint, params=None, fields=None):
        p = dict(params or {})
        calls.append({"endpoint": endpoint, "params": p, "fields": fields})
        idx = int(p["after"][1:]) if p.get("after") else 0
        page = pages[idx]
        if isinstance(page, Exception):
            raise page
        return page

    monkeypatch.setattr(api_client, "graph_get", fake)
    return calls


def paged(events, size):
    """Split events into pages of `size`, linked with c1, c2, ... cursors."""
    chunks = [events[i:i + size] for i in range(0, len(events), size)] or [[]]
    out = []
    for i, chunk in enumerate(chunks):
        page = {"data": chunk}
        if i < len(chunks) - 1:
            page["paging"] = {"next": "x", "cursors": {"after": f"c{i + 1}"}}
        out.append(page)
    return out


def run(**kw):
    from meta_ads_mcp.core.activity import get_activity_log
    kw.setdefault("account_id", "1")
    return get_activity_log(**kw)


class TestNormalization:
    def test_an_event_is_flattened_with_who_what_and_the_change(self, monkeypatch):
        install(monkeypatch, [{"data": [ev(5)]}])
        e = run()["events"][0]
        assert e["time"] == "2026-10-07T10:05:00+0000" and e["local_time"] == "10/07/2026 6:05am"
        assert e["actor"] == {"id": "u1", "name": "Ana"} and e["via"] == "Ads Manager"
        assert e["what"] == "Update campaign budget" and e["event_type"] == "update_campaign_budget"
        assert e["object"] == {"type": "CAMPAIGN", "id": "c1", "name": "Object c1"}
        assert e["change"] == {"from": "5000", "to": "7500"}

    def test_what_falls_back_to_a_readable_event_type(self, monkeypatch):
        install(monkeypatch, [{"data": [ev(1, translated_event_type=None, event_type="update_ad_run_status")]}])
        assert run()["events"][0]["what"] == "Update ad run status"

    def test_extra_data_without_old_and_new_values_stays_as_details(self, monkeypatch):
        install(monkeypatch, [{"data": [ev(1, extra_data=json.dumps({"currency": "USD", "type": "x"}))]}])
        e = run()["events"][0]
        assert e["change"] is None and e["details"] == {"currency": "USD", "type": "x"}

    def test_odd_extra_data_never_breaks_the_response(self, monkeypatch):
        install(monkeypatch, [{"data": [ev(1, extra_data="not json at all"), ev(2, extra_data="[1, 2]"),
                                        ev(3, extra_data=json.dumps({"note": "x" * 900})), ev(4, extra_data=None)]}])
        events = run()["events"]
        assert events[0]["details"] == {"raw": "not json at all"}
        assert events[1]["details"] == {"value": [1, 2]}
        assert len(events[2]["details"]["note"]) == 200
        assert events[3]["details"] is None and events[3]["change"] is None


class TestRequest:
    def test_window_filters_and_page_size(self, monkeypatch):
        calls = install(monkeypatch, [{"data": []}])
        run(account_id="act_9", days=14, category="budget", user_id="777")
        c = calls[0]
        assert c["endpoint"] == "/act_9/activities"
        assert int(c["params"]["until"]) == int(NOW.timestamp())
        assert int(c["params"]["until"]) - int(c["params"]["since"]) == 14 * 86400
        assert c["params"]["category"] == "BUDGET" and c["params"]["uid"] == "777" and c["params"]["limit"] == "100"
        assert "event_time" in c["fields"] and "extra_data" in c["fields"]

    def test_a_bare_account_id_gets_the_act_prefix(self, monkeypatch):
        calls = install(monkeypatch, [{"data": []}])
        run(account_id="123")
        assert calls[0]["endpoint"] == "/act_123/activities"

    def test_fields_fall_back_if_meta_rejects_one(self, monkeypatch):
        seen = []

        def fake(endpoint, params=None, fields=None):
            seen.append(list(fields))
            if "actor_id" in fields:
                raise MetaAPIError("(#100) nonexisting field (actor_id)", error_code=100)
            return {"data": [ev(1)]}

        monkeypatch.setattr(api_client, "graph_get", fake)
        out = run()
        assert "actor_id" in seen[0] and "actor_id" not in seen[1] and out["total"] == 1


class TestFilteringAndPaging:
    def test_summary_counts_by_type_and_actor_newest_first(self, monkeypatch):
        install(monkeypatch, [{"data": [ev(9), ev(8, event_type="update_ad_set_run_status", actor="Ben"), ev(7, actor="Ana")]}])
        out = run()
        assert out["summary"]["by_type"][0] == {"name": "Update campaign budget", "count": 2}
        assert out["summary"]["by_actor"][0] == {"name": "Ana", "count": 2}
        assert out["summary"]["newest"] == "2026-10-07T10:09:00+0000" and out["summary"]["oldest"] == "2026-10-07T10:07:00+0000"

    def test_object_filter_is_applied_across_pages_until_enough_are_found(self, monkeypatch):
        events = [ev(i % 59, obj="c1" if i % 10 == 0 else "other") for i in range(250)]
        calls = install(monkeypatch, paged(events, 100))
        out = run(object_id="c1", limit=100)
        assert out["total"] == 25 and all(e["object"]["id"] == "c1" for e in out["events"])
        assert len(calls) == 3  # it kept reading pages because the filter is applied here, not by Meta
        assert out["truncated"] is False

    def test_event_type_filter_matches_the_type_or_its_translation_case_insensitively(self, monkeypatch):
        install(monkeypatch, [{"data": [ev(1), ev(2, event_type="update_ad_set_run_status"), ev(3, event_type="ad_review_declined")]}])
        assert [e["event_type"] for e in run(event_type="BUDGET")["events"]] == ["update_campaign_budget"]
        assert [e["event_type"] for e in run(event_type="run status")["events"]] == ["update_ad_set_run_status"]

    def test_limit_stops_reading_and_reports_that_more_exist(self, monkeypatch):
        events = [ev(i % 59) for i in range(250)]
        calls = install(monkeypatch, paged(events, 100))
        out = run(limit=150)
        assert out["total"] == 150 and len(calls) == 2
        assert out["truncated"] is True and "Raise limit" in out["truncation_note"]

    def test_a_complete_history_is_not_flagged_as_truncated(self, monkeypatch):
        install(monkeypatch, paged([ev(i) for i in range(30)], 100))
        out = run(limit=100)
        assert out["total"] == 30 and out["truncated"] is False

    def test_exactly_the_limit_with_nothing_more_is_not_truncated(self, monkeypatch):
        install(monkeypatch, paged([ev(i) for i in range(100)], 100))
        assert run(limit=100)["truncated"] is False

    def test_filters_are_echoed(self, monkeypatch):
        install(monkeypatch, [{"data": []}])
        assert run(category="status", object_id="c9")["filters"] == {"category": "STATUS", "object_id": "c9"}


class TestFailuresAndValidation:
    def test_a_failure_returns_an_error_with_a_hint(self, monkeypatch):
        install(monkeypatch, [MetaAPIError("(#200) denied", error_code=200)])
        out = run()
        assert out["error_code"] == 200 and "ads_read" in out["hint"]

    def test_an_empty_history_explains_itself(self, monkeypatch):
        install(monkeypatch, [{"data": []}])
        out = run()
        assert out["total"] == 0 and "Meta may keep less history" in out["note"]

    @pytest.mark.parametrize("kw", [{"days": 0}, {"days": 91}, {"category": "nonsense"}, {"limit": 0}, {"limit": 501}])
    def test_input_validation(self, kw):
        assert run(**kw)["blocked_at"] == "input_validation"

    def test_registered_as_read_only_tool(self):
        from meta_ads_mcp.server import mcp
        tools = {t.name: t for t in mcp._tool_manager.list_tools()}
        assert tools["get_activity_log"].annotations.readOnlyHint is True
