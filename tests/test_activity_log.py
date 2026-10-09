"""
Tests for get_activity_log: the account change history (/act_<id>/activities).

AdActivity fields per Meta's reference: actor_id, actor_name, application_name, date_time_in_timezone,
event_time, event_type, extra_data (JSON string), object_id, object_name, object_type, translated_event_type.
The raw shapes below are the ones seen on a real account: budgets as nested objects in cents, billing as a bare
number, legacy object types, three events for one pause.
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


def ev(n, event_type="update_campaign_budget", obj="c1", actor="Ana", extra=..., sec=0, **over):
    if extra is ...:
        extra = {"old_value": "5000", "new_value": "7500"}
    base = {"actor_id": "u1", "actor_name": actor, "application_name": "Power Editor",
            "event_time": f"2026-10-07T10:{n:02d}:{sec:02d}+0000", "date_time_in_timezone": f"10/07/2026 6:{n:02d}am",
            "event_type": event_type, "translated_event_type": event_type.replace("_", " ").capitalize(),
            "object_id": obj, "object_name": f"Object {obj}", "object_type": "CAMPAIGN_GROUP",
            "extra_data": json.dumps(extra) if extra is not None else None}
    base.update(over)
    return base


def install(monkeypatch, handler):
    """handler(endpoint, params, fields) -> payload | raises. `/act_N` (currency) is answered automatically."""
    calls = []

    def fake(endpoint, params=None, fields=None):
        p = dict(params or {})
        calls.append({"endpoint": endpoint, "params": p, "fields": fields})
        if not endpoint.endswith("/activities"):
            return {"currency": "USD"}
        out = handler(endpoint, p, fields)
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(api_client, "graph_get", fake)
    return calls


def pages(events, size=100):
    """Serve `events` in pages linked with c1, c2, ... cursors."""
    chunks = [events[i:i + size] for i in range(0, len(events), size)] or [[]]

    def handler(endpoint, p, fields):
        i = int(p["after"][1:]) if p.get("after") else 0
        page = {"data": chunks[i]}
        if i < len(chunks) - 1:
            page["paging"] = {"next": "x", "cursors": {"after": f"c{i + 1}"}}
        return page

    return handler


def activity_calls(calls):
    return [c for c in calls if c["endpoint"].endswith("/activities")]


def run(**kw):
    from meta_ads_mcp.core.activity import get_activity_log
    kw.setdefault("account_id", "1")
    return get_activity_log(**kw)


# ================================================================ normalization

class TestNormalization:
    def test_an_event_is_flattened_with_who_what_and_where(self, monkeypatch):
        install(monkeypatch, pages([ev(5, extra=None)]))
        e = run()["events"][0]
        assert e["time"] == "2026-10-07T10:05:00+0000" and e["local_time"] == "10/07/2026 6:05am"
        assert e["actor"] == {"id": "u1", "name": "Ana"} and e["via"] == "Power Editor"
        assert e["what"] == "Update campaign budget" and e["event_type"] == "update_campaign_budget"
        assert e["object"] == {"kind": "campaign", "id": "c1", "name": "Object c1", "raw_type": "CAMPAIGN_GROUP"}
        assert e["change"] is None and e["details"] is None

    def test_a_nested_budget_change_is_flattened_to_major_units_with_a_currency(self, monkeypatch):
        # Seen live: from.old_value / to.new_value in cents, labelled "Lifetime budget" or "Per day"
        extra = {"old_value": {"old_value": 308993000, "type": "Lifetime budget"},
                 "new_value": {"new_value": 299910200, "type": "Lifetime budget"}}
        install(monkeypatch, pages([ev(5, extra=extra)]))
        e = run()["events"][0]
        assert e["change"] == {"from": 3089930.0, "to": 2999102.0, "currency": "USD", "kind": "Lifetime budget",
                               "change_pct": -2.9}
        assert e["details"] is None  # the same numbers are no longer repeated

    def test_flat_budget_numbers_work_too(self, monkeypatch):
        install(monkeypatch, pages([ev(5, event_type="update_ad_set_budget")]))  # "5000" -> "7500"
        assert run()["events"][0]["change"] == {"from": 50.0, "to": 75.0, "currency": "USD", "kind": None, "change_pct": 50.0}

    def test_a_billing_amount_is_converted_not_left_as_unlabelled_cents(self, monkeypatch):
        install(monkeypatch, pages([ev(5, event_type="ad_account_billing_charge", extra={"new_value": 14368})]))
        e = run()["events"][0]
        assert e["amount"] == {"value": 143.68, "currency": "USD"} and e["change"] is None

    def test_status_changes_keep_their_text(self, monkeypatch):
        install(monkeypatch, pages([ev(5, event_type="update_ad_run_status",
                                       extra={"old_value": "Active", "new_value": "Inactive"})]))
        assert run()["events"][0]["change"] == {"from": "Active", "to": "Inactive"}

    def test_audience_events_with_null_values_have_no_change(self, monkeypatch):
        install(monkeypatch, pages([ev(5, event_type="update_audience", extra={"old_value": None, "new_value": None, "x": 1})]))
        e = run()["events"][0]
        assert e["change"] is None and e["details"] == {"x": 1}

    def test_a_non_numeric_budget_value_is_not_converted(self, monkeypatch):
        install(monkeypatch, pages([ev(5, extra={"old_value": "n/a", "new_value": "7500"})]))
        assert run()["events"][0]["change"] == {"from": "n/a", "to": "7500"}

    def test_odd_extra_data_never_breaks_the_response(self, monkeypatch):
        install(monkeypatch, pages([ev(1, extra=None, extra_data="not json at all"), ev(2, extra=None, extra_data="[1, 2]"),
                                    ev(3, extra={"note": "x" * 900}), ev(4, extra=None)]))
        events = run()["events"]
        assert events[0]["details"] == {"raw": "not json at all"}
        assert events[1]["details"] == {"value": [1, 2]}
        assert len(events[2]["details"]["note"]) == 200
        assert events[3]["details"] is None and events[3]["change"] is None

    @pytest.mark.parametrize("event_type, raw_type, kind", [
        ("update_ad_run_status", "ADGROUP", "ad"),
        ("update_campaign_budget", "CAMPAIGN_GROUP", "campaign"),
        ("create_campaign_group", "CAMPAIGN_GROUP", "campaign"),
        ("update_ad_set_budget", "CAMPAIGN", "ad set"),
        ("create_audience", "CAMPAIGN", "audience"),
        ("ad_review_declined", "ADGROUP", "ad"),
        ("ad_account_update_spend_limit", "ACCOUNT", "account"),
        ("unrecognised_event", "ADGROUP", "ad"),
        ("unrecognised_event", "CAMPAIGN", "ad set"),
        ("unrecognised_event", "SOMETHING", "something"),
    ])
    def test_the_object_kind_is_read_from_the_event_not_metas_legacy_names(self, monkeypatch, event_type, raw_type, kind):
        install(monkeypatch, pages([ev(5, event_type=event_type, object_type=raw_type, extra=None)]))
        assert run()["events"][0]["object"]["kind"] == kind

    def test_an_ads_campaign_id_is_really_its_ad_set_id(self, monkeypatch):
        install(monkeypatch, pages([ev(1, event_type="update_ad_run_status", object_type="ADGROUP", extra={"campaign_id": "555"}),
                                    ev(2, event_type="create_ad", object_type="ADGROUP", extra={"campaign_id": {"new": 1}}),
                                    ev(3, event_type="update_campaign_budget", extra={"campaign_id": "777"})]))
        d = [e["details"] for e in run()["events"]]
        assert d[0] == {"adset_id": "555"}
        assert d[1] == {"campaign_id": {"new": 1}}  # a structured value is left alone
        assert d[2] == {"campaign_id": "777"}       # only ad events are renamed

    def test_raw_epoch_times_become_timestamps(self, monkeypatch):
        install(monkeypatch, pages([ev(1, extra={"last_learning_exit": 1759900000, "objective_count": 1759900000, "n": 5})]))
        d = run()["events"][0]["details"]
        assert d["last_learning_exit"].startswith("2025-10-08") and d["n"] == 5
        assert d["objective_count"] == 1759900000  # not a time-like key


# ================================================================ pause bursts

class TestStatusBursts:
    def burst(self, obj="a1", minute=23):
        """One real pause, newest first: Meta writes the middle event under its own type
        (update_ad_run_status_to_be_set_after_review), not update_ad_run_status."""
        return [
            ev(minute, event_type="update_ad_run_status", obj=obj, sec=9, extra={"old_value": "Pending Process", "new_value": "Inactive"}),
            ev(minute, event_type="update_ad_run_status_to_be_set_after_review", obj=obj, sec=8,
               extra={"old_value": "Active", "new_value": "Inactive"}),
            ev(minute, event_type="update_ad_run_status", obj=obj, sec=7, extra={"old_value": "Active", "new_value": "Pending Process"}),
        ]

    def test_three_events_for_one_pause_become_one(self, monkeypatch):
        install(monkeypatch, pages(self.burst()))
        out = run()
        assert out["total"] == 1
        e = out["events"][0]
        assert e["change"] == {"from": "Active", "to": "Inactive"} and e["merged_events"] == 3
        assert out["summary"]["by_type"] == [{"name": "Update ad run status", "count": 1}]

    def test_the_merged_event_is_the_plain_status_event_not_the_review_note(self, monkeypatch):
        install(monkeypatch, pages(self.burst()))
        e = run()["events"][0]
        assert e["event_type"] == "update_ad_run_status" and e["time"] == "2026-10-07T10:23:09+0000"

    def test_other_objects_events_in_between_do_not_stop_the_merge(self, monkeypatch):
        b = self.burst()
        install(monkeypatch, pages([b[0], ev(23, obj="c9", sec=8), b[1], ev(23, obj="c8", sec=7), b[2]]))
        out = run()
        assert out["total"] == 3 and sorted(e["object"]["id"] for e in out["events"]) == ["a1", "c8", "c9"]
        assert next(e for e in out["events"] if e["object"]["id"] == "a1")["merged_events"] == 3

    def test_a_blip_that_ends_where_it_began_says_there_was_no_net_change(self, monkeypatch):
        install(monkeypatch, pages([
            ev(30, event_type="update_campaign_run_status", obj="c1", sec=5, extra={"old_value": "Pending Process", "new_value": "Active"}),
            ev(30, event_type="update_campaign_run_status", obj="c1", sec=1, extra={"old_value": "Active", "new_value": "Pending Process"}),
        ]))
        e = run()["events"][0]
        assert e["change"] is None and e["merged_events"] == 2 and "no net change" in e["note"] and "Active" in e["note"]

    def test_bursts_for_different_objects_stay_separate(self, monkeypatch):
        install(monkeypatch, pages(self.burst("a1", 23) + self.burst("a2", 22)))
        assert run()["total"] == 2

    def test_events_far_apart_are_not_merged(self, monkeypatch):
        later = ev(40, event_type="update_ad_run_status", obj="a1", extra={"old_value": "Inactive", "new_value": "Active"})
        install(monkeypatch, pages([later] + self.burst("a1", 23)))
        events = run()["events"]
        assert len(events) == 2 and events[0]["change"] == {"from": "Inactive", "to": "Active"}

    def test_ordinary_status_changes_are_left_alone(self, monkeypatch):
        install(monkeypatch, pages([ev(5, event_type="update_ad_run_status", extra={"old_value": "Active", "new_value": "Inactive"}),
                                    ev(4, event_type="update_ad_run_status", extra={"old_value": "Inactive", "new_value": "Active"})]))
        assert run()["total"] == 2


# ================================================================ windows and errors

class TestWindows:
    def test_a_short_window_is_one_request_with_the_right_params(self, monkeypatch):
        calls = install(monkeypatch, pages([]))
        run(account_id="act_9", days=14, category="budget", user_id="777")
        c = activity_calls(calls)[0]
        assert len(activity_calls(calls)) == 1 and c["endpoint"] == "/act_9/activities"
        assert int(c["params"]["until"]) - int(c["params"]["since"]) == 14 * 86400
        assert c["params"]["category"] == "BUDGET" and c["params"]["uid"] == "777" and c["params"]["limit"] == "100"
        assert "extra_data" in c["fields"]

    def test_a_long_window_is_read_in_30_day_chunks_newest_first(self, monkeypatch):
        calls = install(monkeypatch, pages([]))
        run(days=75)
        spans = [(int(c["params"]["since"]), int(c["params"]["until"])) for c in activity_calls(calls)]
        assert [round((u - s) / 86400) for s, u in spans] == [30, 30, 15]
        assert spans[0][1] == int(NOW.timestamp()) and spans[0][0] == spans[1][1] and spans[1][0] == spans[2][1]

    def test_enough_events_in_the_newest_chunk_means_older_ones_are_not_read(self, monkeypatch):
        calls = install(monkeypatch, pages([ev(i % 59) for i in range(150)]))
        out = run(days=75, limit=100)
        assert len(activity_calls(calls)) == 1  # the 30-day chunk had enough
        assert out["total"] == 100 and out["truncated"] is True

    def test_an_older_chunk_that_fails_keeps_the_newer_events_and_says_where_it_stopped(self, monkeypatch):
        def handler(endpoint, p, fields):
            if int(p["since"]) < int(NOW.timestamp()) - 60 * 86400:
                return MetaAPIError("An unknown error occurred", error_code=1)
            return {"data": [ev(5)]}

        install(monkeypatch, handler)
        out = run(days=90, limit=500)
        assert "error" not in out and out["total"] == 2  # two chunks answered
        assert "could not be read" in out["notes"][0] and "2026-08-09" in out["notes"][0]
        assert out["window"]["events_reach_back_to"] == "2026-08-09T12:00:00+00:00"

    def test_a_first_chunk_failure_is_an_error_with_an_honest_hint(self, monkeypatch):
        install(monkeypatch, lambda e, p, f: MetaAPIError("An unknown error occurred", error_code=1))
        out = run()
        assert out["error_code"] == 1 and "too large for a busy account" in out["hint"] and "ads_read" not in out["hint"]

    def test_a_permission_failure_still_mentions_permissions(self, monkeypatch):
        install(monkeypatch, lambda e, p, f: MetaAPIError("(#200) denied", error_code=200))
        assert "ads_read" in run()["hint"]

    def test_the_response_says_how_far_back_the_events_reach(self, monkeypatch):
        install(monkeypatch, pages([ev(i % 59) for i in range(150)]))
        truncated = run(limit=100)
        assert truncated["window"]["events_reach_back_to"] == truncated["events"][-1]["time"]
        install(monkeypatch, pages([ev(5)]))
        complete = run(days=7)
        assert complete["window"]["events_reach_back_to"] == "2026-10-01T12:00:00+00:00" and complete["truncated"] is False

    def test_searched_back_to_is_the_window_start_when_it_was_read_in_full(self, monkeypatch):
        install(monkeypatch, pages([ev(5)]))
        out = run(days=45)
        assert out["window"]["searched_back_to"] == out["window"]["since"] and out["window"]["events_read"] == 2  # one event in each of the two chunks

    def test_searched_back_to_stops_at_the_last_chunk_read_when_an_older_one_fails(self, monkeypatch):
        def handler(endpoint, p, fields):
            if int(p["since"]) < int(NOW.timestamp()) - 60 * 86400:
                return MetaAPIError("An unknown error occurred", error_code=1)
            return {"data": [ev(5)]}

        install(monkeypatch, handler)
        out = run(days=90, limit=500)
        assert out["window"]["searched_back_to"] == "2026-08-09T12:00:00+00:00"

    def test_fields_fall_back_if_meta_rejects_one(self, monkeypatch):
        seen = []

        def handler(endpoint, p, fields):
            seen.append(list(fields))
            return MetaAPIError("(#100) nonexisting field (actor_id)", error_code=100) if "actor_id" in fields else {"data": [ev(1)]}

        install(monkeypatch, handler)
        out = run()
        assert "actor_id" in seen[0] and "actor_id" not in seen[1] and out["total"] == 1

    def test_a_bare_account_id_gets_the_act_prefix(self, monkeypatch):
        calls = install(monkeypatch, pages([]))
        run(account_id="123")
        assert activity_calls(calls)[0]["endpoint"] == "/act_123/activities"


# ================================================================ object scoping and filters

class TestObjectScoping:
    """Meta has a per-object activities edge only for ad sets, so a campaign or ad is asked for through the
    account's own edge with its `oid` filter."""

    def test_an_object_is_asked_for_on_the_account_edge_with_the_oid_filter(self, monkeypatch):
        calls = install(monkeypatch, pages([ev(2, obj="c1"), ev(1, obj="c1")]))
        out = run(object_id="c1")
        sent = activity_calls(calls)
        assert [c["endpoint"] for c in sent] == ["/act_1/activities"] and sent[0]["params"]["oid"] == "c1"
        assert out["total"] == 2 and "notes" not in out and out["truncated"] is False

    def test_category_and_user_filters_travel_in_the_same_request(self, monkeypatch):
        calls = install(monkeypatch, pages([ev(2, obj="c1")]))
        run(object_id="c1", category="budget", user_id="5")
        sent = activity_calls(calls)
        assert len(sent) == 1 and sent[0]["params"]["oid"] == "c1" and sent[0]["params"]["category"] == "BUDGET" \
            and sent[0]["params"]["uid"] == "5"

    def test_no_object_means_no_oid(self, monkeypatch):
        calls = install(monkeypatch, pages([ev(2)]))
        run()
        assert "oid" not in activity_calls(calls)[0]["params"]

    def test_events_for_other_objects_are_dropped_and_the_note_says_meta_did_not_filter(self, monkeypatch):
        install(monkeypatch, pages([ev(3, obj="c1"), ev(2, obj="other"), ev(1, obj="other")]))
        out = run(object_id="c1")
        assert [e["object"]["id"] for e in out["events"]] == ["c1"]
        assert "2 events for other objects" in out["notes"][0] and "did not apply the object filter" in out["notes"][0]

    def test_if_meta_rejects_oid_it_is_dropped_and_the_scan_keeps_the_full_fields(self, monkeypatch):
        seen = []
        inner = pages([ev(i % 59, obj="c1" if i % 10 == 0 else "other") for i in range(250)])

        def handler(endpoint, p, fields):
            seen.append(("oid" in p, list(fields)))
            if "oid" in p:
                return MetaAPIError("(#100) Invalid parameter oid", error_code=100)
            return inner(endpoint, p, fields)

        install(monkeypatch, handler)
        out = run(object_id="c1", limit=100)
        assert seen[0][0] is True and all(not with_oid for with_oid, _ in seen[1:])
        assert all("actor_id" in f for _, f in seen)  # the rejection was about oid, so the fields were not downgraded
        assert out["total"] == 25 and all(e["actor"]["id"] == "u1" and e["via"] == "Power Editor" and e["local_time"] for e in out["events"])
        assert "did not accept its object filter" in out["notes"][0]

    def test_a_rejected_oid_is_not_retried_for_every_window(self, monkeypatch):
        calls = install(monkeypatch, lambda e, p, f: MetaAPIError("(#100) bad", error_code=100) if "oid" in p else {"data": []})
        run(object_id="c1", days=75)
        assert sum(1 for c in activity_calls(calls) if "oid" in c["params"]) == 1

    def test_if_the_fields_are_rejected_too_both_fallbacks_apply_in_order(self, monkeypatch):
        seen = []

        def handler(endpoint, p, fields):
            seen.append(("oid" in p, "actor_id" in fields))
            return MetaAPIError("(#100) bad", error_code=100) if ("oid" in p or "actor_id" in fields) else {"data": [ev(1, obj="c1")]}

        install(monkeypatch, handler)
        out = run(object_id="c1")
        assert seen == [(True, True), (False, True), (False, False)] and out["total"] == 1

    def test_the_scan_gives_up_at_its_cap_and_says_how_far_it_got(self, monkeypatch):
        old = lambda: ev(1, obj="other", event_time="2026-09-30T10:00:00+0000")
        always_more = lambda e, p, f: {"data": [old() for _ in range(100)], "paging": {"next": "x", "cursors": {"after": "c1"}}}
        calls = install(monkeypatch, always_more)
        out = run(object_id="c1", category="budget", days=30, limit=15)
        assert out["total"] == 0 and out["truncated"] is True and len(activity_calls(calls)) == 10
        assert out["window"]["searched_back_to"] == "2026-09-30T10:00:00+00:00" and out["window"]["events_read"] == 1000
        assert "Read 1000 events back to 2026-09-30 and stopped" in out["notes"][-1]
        assert "Raise limit" not in out["truncation_note"] and "read limit was reached" in out["truncation_note"]
        assert "No changes found" not in out.get("note", "")

    def test_a_capped_window_stops_the_read_instead_of_leaving_a_gap(self, monkeypatch):
        always_more = lambda e, p, f: {"data": [ev(1, obj="other") for _ in range(100)], "paging": {"next": "x", "cursors": {"after": "c1"}}}
        calls = install(monkeypatch, always_more)
        out = run(object_id="c1", days=90)
        assert len(activity_calls(calls)) == 10  # the newest 30-day window only; the older ones were not skipped into
        assert out["truncated"] is True

    def test_the_oldest_a_capped_scan_found_is_reported_with_the_events_it_did_find(self, monkeypatch):
        rows = [ev(i % 59, obj="c1" if i == 0 else "other", event_time=f"2026-10-0{1 + i % 6}T10:00:00+0000") for i in range(100)]
        always_more = lambda e, p, f: {"data": rows, "paging": {"next": "x", "cursors": {"after": "c1"}}}
        install(monkeypatch, always_more)
        out = run(object_id="c1", days=30, limit=50)
        assert out["total"] == 10  # one per page, ten pages
        assert out["window"]["searched_back_to"] == "2026-10-01T10:00:00+00:00"
        assert "older changes were not searched" in out["notes"][-1]

    def test_event_type_filter_matches_the_type_or_its_translation_case_insensitively(self, monkeypatch):
        install(monkeypatch, pages([ev(1), ev(2, event_type="update_ad_set_run_status"), ev(3, event_type="ad_review_declined")]))
        assert [e["event_type"] for e in run(event_type="BUDGET")["events"]] == ["update_campaign_budget"]
        assert [e["event_type"] for e in run(event_type="run status")["events"]] == ["update_ad_set_run_status"]

    def test_filters_are_echoed(self, monkeypatch):
        install(monkeypatch, pages([]))
        assert run(category="status", user_id="5")["filters"] == {"category": "STATUS", "user_id": "5"}


class TestPagingSummaryAndValidation:
    def test_summary_counts_by_type_and_actor_newest_first(self, monkeypatch):
        install(monkeypatch, pages([ev(9), ev(8, event_type="update_ad_set_run_status", actor="Ben"), ev(7, actor="Ana")]))
        s = run()["summary"]
        assert s["by_type"][0] == {"name": "Update campaign budget", "count": 2}
        assert s["by_actor"][0] == {"name": "Ana", "count": 2}
        assert s["newest"] == "2026-10-07T10:09:00+0000" and s["oldest"] == "2026-10-07T10:07:00+0000"

    def test_limit_stops_reading_and_reports_that_more_exist(self, monkeypatch):
        calls = install(monkeypatch, pages([ev(i % 59) for i in range(250)]))
        out = run(limit=150)
        assert out["total"] == 150 and len(activity_calls(calls)) == 2
        assert out["truncated"] is True and "Raise limit" in out["truncation_note"]

    def test_a_complete_history_is_not_flagged_as_truncated(self, monkeypatch):
        install(monkeypatch, pages([ev(i) for i in range(30)]))
        assert run(limit=100)["truncated"] is False

    def test_exactly_the_limit_with_nothing_more_is_not_truncated(self, monkeypatch):
        install(monkeypatch, pages([ev(i % 59) for i in range(100)]))
        assert run(limit=100)["truncated"] is False

    def test_an_empty_history_explains_itself(self, monkeypatch):
        install(monkeypatch, pages([]))
        out = run()
        assert out["total"] == 0 and "Meta may keep less history" in out["note"]

    @pytest.mark.parametrize("kw", [{"days": 0}, {"days": 91}, {"category": "nonsense"}, {"limit": 0}, {"limit": 501}])
    def test_input_validation(self, kw):
        assert run(**kw)["blocked_at"] == "input_validation"

    def test_registered_as_read_only_tool(self):
        from meta_ads_mcp.server import mcp
        tools = {t.name: t for t in mcp._tool_manager.list_tools()}
        assert tools["get_activity_log"].annotations.readOnlyHint is True
