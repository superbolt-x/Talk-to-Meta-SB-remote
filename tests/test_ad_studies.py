"""
Tests for list_ad_studies and get_ad_study (A/B tests and conversion lift studies).

Shapes follow Meta's reference and guides: a study has cells and objectives edges; a lift objective's `results`
is a list of JSON strings (one per cell with the cell_id breakdown); a split test has no results edge.
"""
import json
from datetime import datetime, timezone

import pytest

from meta_ads_mcp.core import studies
from meta_ads_mcp.core.api import MetaAPIError, api_client

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(api_client, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(studies, "_utc_now", lambda: NOW)


def install(monkeypatch, routes):
    """routes: endpoint -> payload | Exception | callable(params, fields). An unrouted endpoint fails the test."""
    calls = []

    def fake(endpoint, params=None, fields=None):
        calls.append({"endpoint": endpoint, "params": dict(params or {}), "fields": list(fields or [])})
        if endpoint not in routes:
            raise AssertionError(f"unexpected call to {endpoint}")
        out = routes[endpoint]
        if callable(out):
            out = out(dict(params or {}), fields)
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(api_client, "graph_get", fake)
    return calls


def study(sid, name="Test", type="SPLIT_TEST", start="2026-09-01T00:00:00+0000", end="2026-09-30T00:00:00+0000", **extra):
    return {"id": sid, "name": name, "type": type, "start_time": start, "end_time": end,
            "created_time": "2026-08-30T00:00:00+0000", **extra}


# ================================================================ list

def run_list(**kw):
    from meta_ads_mcp.core.studies import list_ad_studies
    kw.setdefault("account_id", "1")
    return list_ad_studies(**kw)


class TestStatus:
    @pytest.mark.parametrize("raw,expected", [
        ({"canceled_time": "2026-09-05T00:00:00+0000", "start_time": "2026-09-01T00:00:00+0000"}, "canceled"),
        ({"start_time": "2026-11-01T00:00:00+0000", "end_time": "2026-11-30T00:00:00+0000"}, "scheduled"),
        ({"start_time": "2026-10-01T00:00:00+0000", "end_time": "2026-10-20T00:00:00+0000"}, "running"),
        ({"start_time": "2026-10-01T00:00:00+0000"}, "running"),
        ({"start_time": "2026-09-01T00:00:00+0000", "end_time": "2026-10-01T00:00:00+0000",
          "observation_end_time": "2026-10-20T00:00:00+0000"}, "observation"),
        ({"start_time": "2026-09-01T00:00:00+0000", "end_time": "2026-09-30T00:00:00+0000"}, "completed"),
        ({"start_time": "2026-09-01T00:00:00+0000", "end_time": "2026-10-01T00:00:00+0000",
          "observation_end_time": "2026-10-05T00:00:00+0000"}, "completed"),
        ({"end_time": "2026-09-30T00:00:00+0000"}, "unknown"),
        ({}, "unknown"),
    ])
    def test_status_comes_from_the_dates(self, raw, expected):
        assert studies._status(raw, NOW) == expected

    def test_epoch_seconds_work_like_iso_strings(self):
        assert studies._status({"start_time": int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()),
                                "end_time": int(datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp())}, NOW) == "running"

    @pytest.mark.parametrize("value,expected", [(0.9, 0.9), (90, 0.9), ("95", 0.95), (None, None), ("x", None)])
    def test_confidence_level_is_a_fraction(self, value, expected):
        assert studies._confidence(value) == expected


    @pytest.mark.parametrize("study_type,expected", [("SPLIT_TEST", True), ("SPLIT_TEST_V2", True), ("split_test", True),
                                                     ("LIFT", False), ("GEO_LIFT", False), ("VERSION_CONTROL", False), (None, False)])
    def test_split_tests_are_recognised_by_type(self, study_type, expected):
        assert studies._is_split(study_type) is expected


class TestList:
    ROWS = [
        study("1", "Old split", "SPLIT_TEST", "2026-07-01T00:00:00+0000", "2026-07-20T00:00:00+0000"),
        study("2", "Creative test", "SPLIT_TEST_V2", "2026-10-01T00:00:00+0000", "2026-10-20T00:00:00+0000"),
        study("3", "Lift", "LIFT", "2026-09-01T00:00:00+0000", "2026-10-01T00:00:00+0000", observation_end_time="2026-10-20T00:00:00+0000"),
        study("4", "Geo", "GEO_LIFT", "2026-08-01T00:00:00+0000", "2026-08-30T00:00:00+0000", canceled_time="2026-08-10T00:00:00+0000"),
    ]

    def test_an_account_is_read_from_its_own_edge_newest_first_with_status(self, monkeypatch):
        calls = install(monkeypatch, {"/act_1/ad_studies": {"data": self.ROWS}})
        out = run_list(account_id="1")
        assert calls[0]["endpoint"] == "/act_1/ad_studies" and "type" in calls[0]["fields"]
        assert [s["id"] for s in out["studies"]] == ["2", "3", "4", "1"]
        assert {s["id"]: s["status"] for s in out["studies"]} == {"1": "completed", "2": "running", "3": "observation", "4": "canceled"}
        assert out["summary"] == {"by_type": {"SPLIT_TEST_V2": 1, "LIFT": 1, "GEO_LIFT": 1, "SPLIT_TEST": 1},
                                  "by_status": {"running": 1, "observation": 1, "canceled": 1, "completed": 1}}
        assert out["scope"] == {"type": "account", "id": "act_1"} and out["truncated"] is False

    def test_a_business_is_read_from_the_business_edge(self, monkeypatch):
        calls = install(monkeypatch, {"/555/ad_studies": {"data": self.ROWS}})
        out = run_list(account_id=None, business_id="555")
        assert calls[0]["endpoint"] == "/555/ad_studies" and out["scope"] == {"type": "business", "id": "555"}

    def test_study_type_matches_by_containing_text(self, monkeypatch):
        install(monkeypatch, {"/act_1/ad_studies": {"data": self.ROWS}})
        assert sorted(s["id"] for s in run_list(study_type="split")["studies"]) == ["1", "2"]
        assert sorted(s["id"] for s in run_list(study_type="LIFT")["studies"]) == ["3", "4"]
        assert [s["id"] for s in run_list(study_type="SPLIT_TEST_V2")["studies"]] == ["2"]

    def test_status_filter(self, monkeypatch):
        install(monkeypatch, {"/act_1/ad_studies": {"data": self.ROWS}})
        out = run_list(status="Running")
        assert [s["id"] for s in out["studies"]] == ["2"] and out["filters"] == {"status": "Running"}

    def test_limit_keeps_the_newest_and_says_so(self, monkeypatch):
        install(monkeypatch, {"/act_1/ad_studies": {"data": self.ROWS}})
        out = run_list(limit=2)
        assert [s["id"] for s in out["studies"]] == ["2", "3"] and out["total"] == 4
        assert "newest 2 of 4" in out["studies_note"]

    def test_nothing_found_says_so(self, monkeypatch):
        install(monkeypatch, {"/act_1/ad_studies": {"data": []}})
        out = run_list()
        assert out["total"] == 0 and "No studies found" in out["note"]

    def test_if_the_account_has_no_studies_edge_the_owning_business_is_read_with_a_note(self, monkeypatch):
        calls = install(monkeypatch, {
            "/act_1/ad_studies": MetaAPIError("(#100) nonexisting field (ad_studies)", error_code=100),
            "/act_1": {"business": {"id": "555"}},
            "/555/ad_studies": {"data": self.ROWS},
        })
        out = run_list()
        assert [c["endpoint"] for c in calls if c["endpoint"].endswith("ad_studies")][-1] == "/555/ad_studies"
        assert out["scope"] == {"type": "business", "id": "555"} and "owning business (555)" in out["notes"][0]

    def test_an_account_with_no_business_to_fall_back_on_is_an_error(self, monkeypatch):
        install(monkeypatch, {"/act_1/ad_studies": MetaAPIError("(#100) bad", error_code=100), "/act_1": {}})
        assert run_list()["error_code"] == 100

    def test_rejected_optional_fields_are_dropped_and_the_read_retried(self, monkeypatch):
        seen = []

        def handler(p, fields):
            seen.append(list(fields))
            return MetaAPIError("(#100) nonexisting field (confidence_level)", error_code=100) if "confidence_level" in fields else {"data": self.ROWS}

        install(monkeypatch, {"/act_1/ad_studies": handler})
        out = run_list()
        assert "confidence_level" in seen[0] and "confidence_level" not in seen[1] and out["total"] == 4

    def test_a_long_list_is_flagged_as_cut_off(self, monkeypatch):
        calls = install(monkeypatch, {"/act_1/ad_studies": lambda p, f: {"data": [study("9")], "paging": {"next": "x", "cursors": {"after": "c"}}}})
        out = run_list()
        assert len(calls) == 5 and out["truncated"] is True and "study_type or status" in out["truncation_note"]

    def test_a_permission_error_comes_back_with_a_hint(self, monkeypatch):
        install(monkeypatch, {"/act_1/ad_studies": MetaAPIError("(#200) denied", error_code=200)})
        out = run_list()
        assert out["error_code"] == 200 and "ads_read" in out["hint"] and "limited-access" in out["hint"]

    @pytest.mark.parametrize("kw,fragment", [
        ({"account_id": None}, "exactly one"), ({"business_id": "5"}, "exactly one"),
        ({"status": "paused"}, "status must be one of"), ({"limit": 0}, "limit must be"), ({"limit": 101}, "limit must be"),
    ])
    def test_input_validation(self, kw, fragment):
        out = run_list(**kw)
        assert out["blocked_at"] == "input_validation" and fragment in out["error"]


# ================================================================ one study

def run_get(**kw):
    from meta_ads_mcp.core.studies import get_ad_study
    kw.setdefault("study_id", "900")
    return get_ad_study(**kw)


def lift_row(cell_id, **over):
    row = {"cell_id": cell_id, "population_test": 2334212, "population_control": 123407, "population_reached": 1862084,
           "impressions": 19020874, "spend": 26059, "conversions_test": 104412.89695396, "conversions_control_scaled": 104575.81331581,
           "conversions_incremental": -162.91636184894, "conversions_incremental_lower": -3470.6251396487,
           "conversions_incremental_upper": 3235.0644420632, "conversions_confidence": 0.69291721817069,
           "conversions_CPiC": -159.95324044961, "conversions_multicell_rank": None}
    row.update(over)
    return json.dumps(row)


class TestLift:
    def routes(self, results, **study_over):
        return {
            "/900": study("900", "Brand lift", "LIFT", confidence_level=0.9, **study_over),
            "/900/cells": {"data": [{"id": "c1", "name": "Test group", "treatment_percentage": 90, "control_percentage": 10}]},
            "/900/objectives": {"data": [{"id": "o1", "name": "Purchases", "type": "CONVERSIONS", "is_primary": True}]},
            "/o1": {"results": results, "last_updated_results": "2026-10-07"},
        }

    def test_results_are_grouped_by_what_they_measure_with_the_cell_named(self, monkeypatch):
        calls = install(monkeypatch, self.routes([lift_row("c1")]))
        out = run_get()
        assert out["study"]["type"] == "LIFT" and out["study"]["confidence_level"] == 0.9
        assert [c["name"] for c in out["cells"]] == ["Test group"]
        obj = out["objectives"][0]
        assert obj["name"] == "Purchases" and obj["last_updated_results"] == "2026-10-07"
        row = obj["results"][0]
        assert row["cell_id"] == "c1" and row["cell_name"] == "Test group" and row["spend"] == 26059 and row["impressions"] == 19020874
        assert row["population"] == {"test": 2334212, "control": 123407, "reached": 1862084}
        conv = row["conversions"]
        assert conv["incremental"] == -162.92 and conv["incremental_lower"] == -3470.63 and conv["incremental_upper"] == 3235.06
        assert conv["confidence"] == 0.6929 and conv["CPiC"] == -159.95 and conv["multicell_rank"] is None
        obj_call = next(c for c in calls if c["endpoint"] == "/o1")
        assert obj_call["params"]["breakdowns"] == '["cell_id"]' and "results" in obj_call["fields"]

    def test_each_group_says_whether_it_meets_the_studys_confidence_level(self, monkeypatch):
        install(monkeypatch, self.routes([lift_row("c1"), lift_row("c1", conversions_confidence=0.95)]))
        rows = run_get()["objectives"][0]["results"]
        assert rows[0]["conversions"]["meets_study_confidence_level"] is False
        assert rows[1]["conversions"]["meets_study_confidence_level"] is True

    def test_without_a_confidence_level_nothing_is_claimed(self, monkeypatch):
        routes = self.routes([lift_row("c1")])
        routes["/900"] = study("900", "Lift", "LIFT")
        install(monkeypatch, routes)
        assert "meets_study_confidence_level" not in run_get()["objectives"][0]["results"][0]["conversions"]

    def test_an_unparsable_result_is_kept_as_raw_text_not_dropped(self, monkeypatch):
        install(monkeypatch, self.routes(["{not json"]))
        assert run_get()["objectives"][0]["results"] == [{"raw": "{not json"}]

    def test_no_results_yet_says_so(self, monkeypatch):
        install(monkeypatch, self.routes([]))
        obj = run_get()["objectives"][0]
        assert obj["results"] == [] and "No results yet" in obj["note"]

    def test_include_results_false_lists_objectives_without_reading_results(self, monkeypatch):
        calls = install(monkeypatch, self.routes([lift_row("c1")]))
        out = run_get(include_results=False)
        assert "results" not in out["objectives"][0] and not any(c["endpoint"] == "/o1" for c in calls)

    def test_an_objective_whose_results_fail_is_reported_and_the_rest_survives(self, monkeypatch):
        routes = self.routes([])
        routes["/o1"] = MetaAPIError("(#200) denied", error_code=200)
        install(monkeypatch, routes)
        out = run_get()
        assert "denied" in out["objectives"][0]["error"] and out["cells"][0]["id"] == "c1"


class TestSplit:
    START, END = "2026-09-01T00:00:00+0000", "2026-09-30T00:00:00+0000"

    def routes(self, a_spend=1000, b_spend=1000, a_buys=50, b_buys=25, **over):
        def insights(params, fields):
            ids = json.loads(params["filtering"])[0]["value"]
            rows = {"c-a": ("camp-a", a_spend, a_buys), "c-b": ("camp-b", b_spend, b_buys)}
            return {"data": [
                {"campaign_id": cid, "date_start": "2026-09-01", "date_stop": "2026-09-30", "spend": str(sp), "impressions": "100000",
                 "clicks": "2000", "frequency": "1.5",
                 "actions": [{"action_type": "omni_purchase", "value": str(b)}],
                 "action_values": [{"action_type": "omni_purchase", "value": str(b * 40)}]}
                for key, (cid, sp, b) in rows.items() if cid in ids]}

        base = {
            "/900": study("900", "Audience test", "SPLIT_TEST", self.START, self.END),
            "/900/cells": {"data": [{"id": "c-a", "name": "Group A", "treatment_percentage": 50}, {"id": "c-b", "name": "Group B", "treatment_percentage": 50}]},
            "/900/objectives": {"data": []},
            "/c-a/campaigns": {"data": [{"id": "camp-a", "name": "Camp A", "account_id": "7"}]},
            "/c-a/ad_sets": {"data": []},
            "/c-b/campaigns": {"data": [{"id": "camp-b", "name": "Camp B", "account_id": "7"}]},
            "/c-b/ad_sets": {"data": []},
            "/act_7/insights": insights,
        }
        base.update(over)
        return base

    def test_each_cell_is_read_from_insights_over_the_study_dates_and_ranked_by_cost_per_result(self, monkeypatch):
        calls = install(monkeypatch, self.routes())
        out = run_get()
        assert out["performance_window"] == {"since": "2026-09-01", "until": "2026-09-30"}
        a, b = out["cells"]
        assert a["performance"]["spend"] == 1000 and a["performance"]["purchases"] == 50 and a["performance"]["cpa"] == 20
        assert a["performance"]["roas"] == 2 and b["performance"]["cpa"] == 40
        assert a["campaigns"] == [{"id": "camp-a", "name": "Camp A"}] and a["campaign_count"] == 1
        cmp_ = out["performance_comparison"]
        assert cmp_["efficiency_metric"] == "cpa" and [r["cell"] for r in cmp_["ranking"]] == ["Group A", "Group B"]
        assert cmp_["ranking"][0]["vs_best_pct"] == 0.0 and cmp_["ranking"][1]["vs_best_pct"] == 100.0
        assert "does not test statistical significance" in cmp_["note"] and "caution" not in cmp_
        ins = next(c for c in calls if c["endpoint"] == "/act_7/insights")
        assert ins["params"]["level"] == "campaign" and json.loads(ins["params"]["time_range"]) == {"since": "2026-09-01", "until": "2026-09-30"}
        assert json.loads(ins["params"]["filtering"]) == [{"field": "campaign.id", "operator": "IN", "value": ["camp-a"]}]

    def test_a_running_test_is_read_up_to_today(self, monkeypatch):
        routes = self.routes()
        routes["/900"] = study("900", "Running", "SPLIT_TEST", "2026-10-01T00:00:00+0000", "2026-10-30T00:00:00+0000")
        install(monkeypatch, routes)
        assert run_get()["performance_window"] == {"since": "2026-10-01", "until": "2026-10-08"}

    def test_cells_that_spent_very_differently_carry_a_caution(self, monkeypatch):
        install(monkeypatch, self.routes(a_spend=1000, b_spend=3000))
        assert "not comparable on cost alone" in run_get()["performance_comparison"]["caution"]

    def test_a_cell_with_no_purchases_means_no_ranking_is_claimed(self, monkeypatch):
        install(monkeypatch, self.routes(b_buys=0))
        cmp_ = run_get()["performance_comparison"]
        assert cmp_["efficiency_metric"] is None and "ranking" not in cmp_

    def test_ad_sets_under_a_listed_campaign_are_not_counted_twice(self, monkeypatch):
        routes = self.routes()
        routes["/c-a/ad_sets"] = {"data": [{"id": "as-1", "name": "Set", "campaign_id": "camp-a", "account_id": "7"}]}
        calls = install(monkeypatch, routes)
        out = run_get()
        assert out["cells"][0]["ad_set_count"] == 1 and out["cells"][0]["performance"]["spend"] == 1000
        assert not any(json.loads(c["params"]["filtering"])[0]["field"] == "adset.id" for c in calls if c["endpoint"] == "/act_7/insights")

    def test_loose_ad_sets_are_read_at_ad_set_level(self, monkeypatch):
        routes = self.routes()
        routes["/c-a/campaigns"] = {"data": []}
        routes["/c-a/ad_sets"] = {"data": [{"id": "as-1", "name": "Set", "campaign_id": "other", "account_id": "7"}]}
        seen = []

        def insights(params, fields):
            seen.append(params["level"])
            ids = json.loads(params["filtering"])[0]["value"]
            if params["level"] == "adset":
                return {"data": [{"adset_id": "as-1", "date_start": "2026-09-01", "spend": "500", "impressions": "1000"}]}
            return {"data": [{"campaign_id": i, "date_start": "2026-09-01", "spend": "100", "impressions": "1000"} for i in ids]}

        routes["/act_7/insights"] = insights
        install(monkeypatch, routes)
        out = run_get()
        assert "adset" in seen and out["cells"][0]["performance"]["spend"] == 500

    def test_the_account_argument_covers_entities_that_do_not_name_one(self, monkeypatch):
        routes = self.routes()
        routes["/c-a/campaigns"] = {"data": [{"id": "camp-a", "name": "Camp A"}]}
        routes["/c-b/campaigns"] = {"data": [{"id": "camp-b", "name": "Camp B"}]}
        routes["/act_7/insights"] = self.routes()["/act_7/insights"]
        calls = install(monkeypatch, routes)
        out = run_get(account_id="7")
        assert out["cells"][0]["performance"]["spend"] == 1000 and not out.get("notes")
        assert {c["endpoint"] for c in calls if c["endpoint"].endswith("/insights")} == {"/act_7/insights"}

    def test_without_an_account_anywhere_it_says_to_pass_one_instead_of_going_quiet(self, monkeypatch):
        routes = self.routes()
        routes["/c-a/campaigns"] = {"data": [{"id": "camp-a", "name": "Camp A"}]}
        install(monkeypatch, routes)
        out = run_get()
        assert "performance" not in out["cells"][0] and out["cells"][1]["performance"]["spend"] == 1000
        assert any("Group A" in n and "pass account_id" in n for n in out["notes"])

    def test_a_cell_that_never_delivered_says_so(self, monkeypatch):
        routes = self.routes()
        routes["/act_7/insights"] = lambda p, f: {"data": []}
        install(monkeypatch, routes)
        out = run_get()
        assert "performance" not in out["cells"][0] and any("no spend or delivery" in n for n in out["notes"])

    def test_a_cell_with_more_entities_than_the_read_limit_says_so(self, monkeypatch):
        routes = self.routes()
        routes["/c-a/campaigns"] = lambda p, f: {"data": [{"id": "camp-a", "name": "A", "account_id": "7"}],
                                                 "paging": {"next": "x", "cursors": {"after": "c"}}}
        install(monkeypatch, routes)
        assert any("Group A" in n and "more than 100 campaigns" in n for n in run_get()["notes"])

    def test_a_test_that_has_not_started_has_no_performance_to_read(self, monkeypatch):
        routes = self.routes()
        routes["/900"] = study("900", "Later", "SPLIT_TEST", "2026-11-01T00:00:00+0000", "2026-11-30T00:00:00+0000")
        calls = install(monkeypatch, routes)
        out = run_get()
        assert "has not started" in out["notes"][0] and not any(c["endpoint"].endswith("/insights") for c in calls)

    def test_a_cell_without_campaigns_or_ad_sets_explains_why_it_has_no_numbers(self, monkeypatch):
        routes = self.routes()
        routes["/c-a/campaigns"] = {"data": []}
        install(monkeypatch, routes)
        out = run_get()
        assert "performance" not in out["cells"][0] and any("Group A" in n and "no campaigns or ad sets" in n for n in out["notes"])

    def test_a_cell_edge_that_fails_is_reported_not_passed_off_as_empty(self, monkeypatch):
        routes = self.routes()
        routes["/c-a/campaigns"] = MetaAPIError("(#200) denied", error_code=200)
        install(monkeypatch, routes)
        out = run_get()
        assert "cell c-a entities" in out["errors"] and not any("Group A" in n for n in out.get("notes", []))
        assert out["cells"][1]["performance"]["cpa"] == 40

    def test_an_insights_failure_is_reported_and_the_rest_of_the_study_survives(self, monkeypatch):
        routes = self.routes()
        routes["/act_7/insights"] = MetaAPIError("(#100) bad", error_code=100)
        install(monkeypatch, routes)
        out = run_get()
        assert "act_7:campaign" in out["errors"] and out["study"]["name"] == "Audience test" and "performance" not in out["cells"][0]

    def test_include_results_false_reads_no_cell_entities_or_insights(self, monkeypatch):
        calls = install(monkeypatch, self.routes())
        out = run_get(include_results=False)
        assert [c["endpoint"] for c in calls] == ["/900", "/900/cells", "/900/objectives"] and "performance_comparison" not in out


class TestStudyEnvelope:
    def test_study_types_without_results_say_so(self, monkeypatch):
        install(monkeypatch, {"/900": study("900", "VC", "VERSION_CONTROL"), "/900/cells": {"data": []}, "/900/objectives": {"data": []}})
        assert "No results are read for study type VERSION_CONTROL" in run_get()["notes"][0]

    def test_the_study_node_falls_back_to_basic_fields(self, monkeypatch):
        seen = []

        def node(params, fields):
            seen.append(list(fields))
            return MetaAPIError("(#100) nonexisting field", error_code=100) if "confidence_level" in fields else study("900", "S", "VERSION_CONTROL")

        install(monkeypatch, {"/900": node, "/900/cells": {"data": []}, "/900/objectives": {"data": []}})
        out = run_get()
        assert "confidence_level" in seen[0] and "confidence_level" not in seen[1] and out["study"]["name"] == "S"

    def test_cells_or_objectives_failing_are_reported_and_the_study_still_comes_back(self, monkeypatch):
        install(monkeypatch, {"/900": study("900", "S", "VERSION_CONTROL"),
                              "/900/cells": MetaAPIError("(#100) bad", error_code=100),
                              "/900/objectives": MetaAPIError("(#200) denied", error_code=200)})
        out = run_get()
        assert set(out["errors"]) == {"cells", "objectives"} and out["study"]["id"] == "900" and "see errors" in out["hint"]

    def test_a_missing_study_is_an_error_with_a_hint(self, monkeypatch):
        install(monkeypatch, {"/900": MetaAPIError("(#100) Unsupported get request", error_code=100)})
        out = run_get()
        assert out["error_code"] == 100 and "may not be an ad study" in out["hint"]

    @pytest.mark.parametrize("sid", ["abc", "12 3", ""])
    def test_study_id_must_be_numeric(self, sid):
        assert run_get(study_id=sid)["blocked_at"] == "input_validation"

    def test_cells_over_the_cap_are_cut_with_a_note(self, monkeypatch):
        many = {"data": [{"id": f"c{i}", "name": f"C{i}"} for i in range(25)]}
        install(monkeypatch, {"/900": study("900", "S", "VERSION_CONTROL"), "/900/cells": many, "/900/objectives": {"data": []}})
        out = run_get()
        assert len(out["cells"]) == 20 and "first 20 of 25 cells" in out["notes"][0]

    def test_both_tools_are_read_only(self):
        from meta_ads_mcp.server import mcp
        tools = {t.name: t for t in mcp._tool_manager.list_tools()}
        assert tools["list_ad_studies"].annotations.readOnlyHint is True and tools["get_ad_study"].annotations.readOnlyHint is True
