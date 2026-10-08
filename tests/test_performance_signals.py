"""
Tests for get_performance_signals (anomalies and trends from daily Insights).

Series are synthetic with known answers. `stable_days` scales every additive metric by the same
daily factor, so ratios (CTR, CPA, ROAS) are exactly constant and the z-score floor decides how
unusual a change is; that keeps each scenario's expected result easy to reason about.
"""
import json
from datetime import date, timedelta

import pytest

from meta_ads_mcp.core import signals
from meta_ads_mcp.core.api import MetaAPIError, api_client
from meta_ads_mcp.core.signals import analyze_entity, build_trend

TODAY = date(2026, 10, 8)
UNTIL = date(2026, 10, 7)  # today is excluded
FACTORS = [0.97, 1.03, 0.99, 1.01, 0.98, 1.02, 1.0]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(api_client, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(signals, "_today", lambda: TODAY)


def d(offset):
    """Calendar date `offset` days before UNTIL (0 = yesterday)."""
    return (UNTIL - timedelta(days=offset)).isoformat()


ALL = signals._dates(UNTIL, 17)
RECENT = ALL[-3:]
BASELINE = ALL[:-3][-14:]


# 40 purchases/day at CPA 20 and ROAS 5. Conversion rules treat small counts as noisy, so scenarios
# about conversion metrics need volume like this to be conclusive within a 3-day window.
HV = {"spend": 800.0, "purchases": 40.0, "revenue": 4000.0}


def day(date_str, spend=100.0, impressions=10000.0, clicks=200.0, purchases=5.0, revenue=500.0,
        leads=0.0, frequency=1.5, factor=1.0, conversions=0.0):
    return {"date": date_str, "spend": spend * factor, "impressions": impressions * factor,
            "clicks": clicks * factor, "purchases": purchases * factor, "revenue": revenue * factor,
            "leads": leads * factor, "frequency": frequency, "conversions": conversions * factor}


def stable_days(dates, **kw):
    return {dt: day(dt, factor=FACTORS[i % len(FACTORS)], **kw) for i, dt in enumerate(dates)}


def series(baseline_kw=None, recent_kw=None, baseline=BASELINE, recent=RECENT):
    out = stable_days(baseline, **(baseline_kw or {}))
    out.update(stable_days(recent, **(recent_kw if recent_kw is not None else (baseline_kw or {}))))
    return out


def run_entity(days, metrics=None):
    metrics = metrics or ["spend", "impressions", "ctr", "cpm", "cpc", "frequency",
                          "purchases", "cpa", "roas", "revenue"]
    return analyze_entity(days, RECENT, BASELINE, metrics)


def by_metric(anomalies):
    return {a["metric"]: a for a in anomalies}


# ================================================================ anomaly detection

class TestStableAndNoise:
    def test_stable_series_has_no_anomalies(self):
        found, reason = run_entity(series())
        assert found == [] and reason is None

    def test_a_big_move_inside_normal_noise_is_not_flagged(self):
        noisy = {dt: day(dt, spend=50.0 if i % 2 else 150.0) for i, dt in enumerate(BASELINE)}
        noisy.update({dt: day(dt, spend=70.0) for dt in RECENT})  # -30% but the baseline swings +-50%
        found, _ = run_entity(noisy, ["spend"])
        assert found == []

    def test_flat_baseline_does_not_divide_by_zero_and_flags_a_real_change(self):
        flat = {dt: day(dt) for dt in BASELINE}
        flat.update({dt: day(dt, spend=140.0) for dt in RECENT})  # +40%, zero baseline variance
        found, _ = run_entity(flat, ["spend"])
        assert by_metric(found)["spend"]["change_pct"] == 40.0
        assert by_metric(found)["spend"]["z_score"] > signals.Z_FLAG

    def test_a_small_change_is_not_flagged_even_when_unusual(self):
        found, _ = run_entity(series(recent_kw={"spend": 110.0}), ["spend"])  # +10%
        assert found == []


class TestWorsening:
    def test_cpa_spike_flags_the_whole_funnel_as_worse_and_high(self):
        found, _ = run_entity(series(baseline_kw=HV, recent_kw={**HV, "purchases": 20.0, "revenue": 2000.0}))
        m = by_metric(found)
        assert m["cpa"]["change_pct"] == 100.0 and m["cpa"]["assessment"] == "worse"
        assert m["cpa"]["severity"] == "HIGH"
        assert m["roas"]["change_pct"] == -50.0 and m["roas"]["assessment"] == "worse"
        assert m["purchases"]["change_pct"] == pytest.approx(-50.0, abs=1.0)  # per-day averages shift a little with the daily factors
        assert "ctr" not in m and "cpm" not in m  # delivery metrics did not move
        assert m["cpa"]["z_score"] >= signals.Z_HIGH

    def test_worsening_just_over_the_flag_threshold_is_medium_not_high(self):
        # cpa +42.9%: past the percent bar, but only ~3.3 sigma against the expected conversion count
        found, _ = run_entity(series(baseline_kw=HV, recent_kw={**HV, "purchases": 28.0, "revenue": 2800.0}), ["cpa"])
        assert by_metric(found)["cpa"]["severity"] == "MEDIUM"

    def test_a_cost_rise_within_conversion_count_noise_is_not_flagged(self):
        # cpa +28% looks sizeable, but 31 vs 40 purchases/day is within ordinary Poisson noise over 3 days
        found, _ = run_entity(series(baseline_kw=HV, recent_kw={**HV, "purchases": 31.2, "revenue": 3120.0}), ["cpa"])
        assert found == []

    def test_frequency_is_impression_weighted_and_rising_is_worse(self):
        found, _ = run_entity(series(recent_kw={"frequency": 2.4}), ["frequency"])
        freq = by_metric(found)["frequency"]
        assert freq["recent"] == 2.4 and freq["baseline"] == 1.5
        assert freq["change_pct"] == 60.0 and freq["assessment"] == "worse"

    def test_messages_are_human_readable(self):
        found, _ = run_entity(series(baseline_kw=HV, recent_kw={**HV, "purchases": 20.0, "revenue": 2000.0}), ["cpa"])
        assert found[0]["message"] == "CPA up 100%: 40.00 over the last 3d vs 20.00 over the 14d before"


class TestImprovements:
    def test_improvement_is_reported_as_better_and_informational(self):
        found, _ = run_entity(series(recent_kw={"clicks": 320.0}), ["ctr", "cpc"])  # CTR +60%, CPC -37.5%
        m = by_metric(found)
        assert m["ctr"]["assessment"] == "better" and m["ctr"]["severity"] == "INFO"
        assert m["cpc"]["assessment"] == "better" and m["cpc"]["change_pct"] == -37.5

    def test_neutral_metric_change_is_low_not_good_or_bad(self):
        found, _ = run_entity(series(recent_kw={"spend": 150.0}), ["spend"])
        assert by_metric(found)["spend"]["assessment"] == "changed"
        assert by_metric(found)["spend"]["severity"] == "LOW"

    def test_a_spend_collapse_is_called_out_as_a_delivery_dip(self):
        found, _ = run_entity(series(recent_kw={"spend": 40.0}), ["spend"])  # -60%
        assert by_metric(found)["spend"]["severity"] == "MEDIUM"


class TestDeliveryAndConversionStops:
    def test_stopped_delivering_is_one_clear_finding_and_skips_the_rest(self):
        only_baseline = stable_days(BASELINE)
        found, reason = run_entity(only_baseline)
        assert reason is None and len(found) == 1
        assert found[0]["metric"] == "delivery" and found[0]["severity"] == "MEDIUM"
        assert "Stopped delivering" in found[0]["message"]
        assert found[0]["baseline"] == pytest.approx(100.0, abs=1.5)

    def test_a_campaign_that_died_long_ago_is_not_news(self):
        long_dead = stable_days(BASELINE[:9])  # stopped 5 days before the recent window
        found, reason = run_entity(long_dead)
        assert found == [] and reason is None

    def test_spending_with_no_conversions_is_high_and_replaces_the_derived_noise(self):
        found, _ = run_entity(series(recent_kw={"purchases": 0.0, "revenue": 0.0}))
        m = by_metric(found)
        assert m["purchases_stopped"]["severity"] == "HIGH"
        assert "No purchases in the last 3d despite" in m["purchases_stopped"]["message"]
        for redundant in ("purchases", "cpa", "roas", "revenue"):
            assert redundant not in m

    def test_not_flagged_when_too_little_was_spent_to_expect_a_conversion(self):
        # baseline CPA is 20; expecting ~5 conversions (5 x 20 = 100 of spend) makes zero a real surprise
        found, _ = run_entity(series(recent_kw={"spend": 40.0, "purchases": 0.0, "revenue": 0.0}),
                              ["purchases", "cpa"])  # 120 spent over 3d >= 100 -> flagged
        assert "purchases_stopped" in by_metric(found)
        found, _ = run_entity(series(recent_kw={"spend": 20.0, "purchases": 0.0, "revenue": 0.0}),
                              ["purchases", "cpa"])  # 60 < 100: zero conversions would not be surprising yet
        assert "purchases_stopped" not in by_metric(found)


class TestLowVolumeHonesty:
    """Seen in simulation: at ~0.6-2 purchases/day, ~16% of *unchanged* campaigns got a false HIGH
    until count noise and the no-conversion rule were tightened."""

    LOW = {"spend": 100.0, "purchases": 1.0, "revenue": 100.0}  # 14 baseline purchases: just over the floor

    def test_a_low_volume_campaign_that_is_simply_quiet_is_not_flagged(self):
        # 3 days with no purchases is ordinary at 1/day (P ~ 5%); spend of 300 is only ~3 CPAs
        found, _ = run_entity(series(baseline_kw=self.LOW, recent_kw={**self.LOW, "purchases": 0.0, "revenue": 0.0}),
                              ["purchases", "cpa", "roas", "revenue"])
        assert "purchases_stopped" not in by_metric(found)

    def test_cost_metrics_need_to_beat_count_noise_not_just_daily_variation(self):
        # CPA doubles (1 -> 0.5 purchases/day) on 14 baseline purchases: only ~1.2 sigma in count terms
        found, _ = run_entity(series(baseline_kw=self.LOW, recent_kw={**self.LOW, "purchases": 0.5, "revenue": 50.0}),
                              ["cpa", "purchases", "roas", "revenue"])
        assert found == []

    def test_the_same_change_at_high_volume_is_flagged(self):
        found, _ = run_entity(series(baseline_kw=HV, recent_kw={**HV, "purchases": 20.0, "revenue": 2000.0}), ["cpa"])
        assert "cpa" in by_metric(found)

    def test_the_tool_says_which_entities_were_too_low_volume_to_judge(self, monkeypatch):
        low = series(baseline_kw={"spend": 100.0, "purchases": 0.5, "revenue": 50.0})
        install(monkeypatch, account_series(), [("L", "Low volume", low)])
        out = run(object_id="act_1")
        assert out["low_volume"][0]["id"] == "L"
        assert "only 7 in the baseline (needs 10)" in out["low_volume"][0]["notes"][0]
        assert "recent_days=7" in out["low_volume_hint"]

    def test_no_low_volume_note_for_a_campaign_with_enough_conversions(self, monkeypatch):
        install(monkeypatch, account_series(), [("H", "High volume", stable_days(ALL, **HV))])
        assert "low_volume" not in run(object_id="act_1")

    def test_no_note_for_a_campaign_with_no_purchases_at_all(self, monkeypatch):
        none = stable_days(ALL, purchases=0.0, revenue=0.0)
        install(monkeypatch, account_series(), [("N", "Awareness", none)])
        assert "low_volume" not in run(object_id="act_1")


class TestVolumeAndAgeGuards:
    def test_conversion_metrics_need_enough_baseline_conversions(self):
        found, _ = run_entity(series(baseline_kw={"purchases": 0.5, "revenue": 50.0},
                                     recent_kw={"purchases": 0.25, "revenue": 25.0}),
                              ["cpa", "purchases", "roas"])
        assert found == []  # 7 baseline purchases < 10: CPA doubling means nothing

    def test_delivery_metrics_need_enough_impressions(self):
        tiny = {"impressions": 300.0, "clicks": 6.0, "spend": 3.0}
        found, _ = run_entity(series(baseline_kw=tiny, recent_kw={**tiny, "clicks": 12.0}), ["ctr"])
        assert found == []

    def test_a_new_campaign_is_skipped_with_a_reason(self):
        new = stable_days(BASELINE[-4:] + RECENT)
        found, reason = run_entity(new)
        assert found == [] and reason == "only 4 days of baseline delivery (needs 7)"

    def test_nothing_in_the_baseline_is_skipped_with_a_reason(self):
        found, reason = run_entity(stable_days(RECENT))
        assert found == [] and reason == "no delivery in the baseline window"

    def test_days_before_launch_are_not_counted_as_zero_days(self):
        # Launched 9 days into the baseline, with a stable 100/day: baseline average must be ~100, not ~64
        launched = stable_days(BASELINE[5:])
        launched.update(stable_days(RECENT, spend=300.0))
        found, _ = run_entity(launched, ["spend"])
        assert by_metric(found)["spend"]["baseline"] == pytest.approx(100.0, abs=1.5)

    def test_gap_days_after_launch_do_count_as_zero_for_volume_metrics(self):
        gappy = stable_days(BASELINE)
        for gap in BASELINE[6:8]:
            del gappy[gap]  # two days with no delivery
        gappy.update(stable_days(RECENT, spend=300.0))
        found, _ = run_entity(gappy, ["spend"])
        baseline_avg = by_metric(found)["spend"]["baseline"]
        assert baseline_avg < 90.0  # 12 delivered days out of 14


class TestOrdering:
    def test_worse_before_better_and_bigger_spend_first(self):
        from meta_ads_mcp.core.signals import _sort_anomalies
        items = [
            {"severity": "INFO", "z_score": 9, "_spend": 500, "metric": "ctr"},
            {"severity": "MEDIUM", "z_score": 3, "_spend": 10, "metric": "cpm"},
            {"severity": "HIGH", "z_score": 5, "_spend": 50, "metric": "cpa"},
            {"severity": "MEDIUM", "z_score": 3, "_spend": 90, "metric": "cpc"},
        ]
        assert [a["metric"] for a in _sort_anomalies(items)] == ["cpa", "cpc", "cpm", "ctr"]


# ================================================================ trend

class TestTrend:
    def test_week_over_week_changes_and_assessments(self):
        last7, prior7 = signals._dates(UNTIL, 7), signals._dates(UNTIL - timedelta(days=7), 7)
        days = stable_days(prior7)
        days.update(stable_days(last7, spend=200.0, purchases=2.5, revenue=250.0))  # spend x2, purchases /2
        trend = build_trend(days, UNTIL, ["spend", "ctr", "purchases", "cpa"])
        m = trend["metrics"]
        assert trend["last_7d"]["to"] == UNTIL.isoformat() and trend["prior_7d"]["to"] == (UNTIL - timedelta(7)).isoformat()
        assert m["spend"]["direction"] == "up" and m["spend"]["assessment"] == "neutral"
        assert m["ctr"]["direction"] == "flat"  # only spend and purchases changed, not clicks/impressions
        assert m["cpa"]["direction"] == "up" and m["cpa"]["assessment"] == "worse"
        assert m["purchases"]["direction"] == "down" and m["purchases"]["assessment"] == "worse"

    def test_changes_inside_five_percent_are_flat(self):
        last7, prior7 = signals._dates(UNTIL, 7), signals._dates(UNTIL - timedelta(days=7), 7)
        days = stable_days(prior7)
        days.update(stable_days(last7, spend=103.0))
        m = build_trend(days, UNTIL, ["spend"])["metrics"]["spend"]
        assert m["direction"] == "flat" and m["assessment"] == "neutral"

    def test_no_prior_week_says_so(self):
        days = stable_days(signals._dates(UNTIL, 7))
        trend = build_trend(days, UNTIL, ["spend"])
        assert "nothing to compare" in trend["note"]
        assert "change_pct" not in trend["metrics"]["spend"]

    def test_no_delivery_at_all(self):
        assert "No delivery" in build_trend({}, UNTIL, ["spend"])["note"]


# ================================================================ the tool

def rows(days_by_date, **ids):
    out = []
    for dt in sorted(days_by_date):
        r = days_by_date[dt]
        row = {"date_start": dt, "date_stop": dt, "spend": str(r["spend"]), "impressions": str(r["impressions"]),
               "clicks": str(r["clicks"]), "frequency": str(r["frequency"]),
               "actions": [{"action_type": "omni_purchase", "value": str(r["purchases"])}],
               "action_values": [{"action_type": "omni_purchase", "value": str(r["revenue"])}]}
        if r["conversions"]:
            row["actions"].append({"action_type": "offsite_conversion.custom.123", "value": str(r["conversions"])})
        row.update(ids)
        out.append(row)
    return out


def install(monkeypatch, object_days, entities=None, ranking_error=None, object_error=None,
            entity_daily_error=None, currency="USD", active=None, active_error=None):
    """entities: list of (id, name, days_by_date). active: ACTIVE entities for the status lookup.
    Routes by request shape and records calls."""
    calls = []
    entities = entities or []

    def fake(endpoint, params=None, fields=None):
        p = dict(params or {})
        calls.append({"endpoint": endpoint, "params": p, "fields": fields})
        if endpoint.rsplit("/", 1)[-1] in ("campaigns", "adsets", "ads"):
            if active_error:
                raise active_error
            return {"data": active or []}
        if endpoint.endswith("/insights"):
            if "filtering" in p and "time_increment" not in p:  # "did these ACTIVE entities deliver?"
                wanted = json.loads(p["filtering"])[0]["value"]
                window = json.loads(p["time_range"])
                return {"data": [
                    {"campaign_id": eid, "spend": "10", "impressions": "1000"}
                    for eid, _, dd in entities
                    if eid in wanted and any(window["since"] <= dt <= window["until"] and rec["spend"] > 0
                                             for dt, rec in dd.items())]}
            if "filtering" in p:
                if entity_daily_error:
                    raise entity_daily_error
                wanted = json.loads(p["filtering"])[0]["value"]
                out = []
                for eid, name, dd in entities:
                    if eid in wanted:
                        out += rows(dd, campaign_id=eid, campaign_name=name)
                return {"data": out}
            if "sort" in p:
                if ranking_error:
                    raise ranking_error
                return {"data": [{"campaign_id": eid, "campaign_name": name, "spend": "1"}
                                 for eid, name, _ in entities]}
            if object_error:
                raise object_error
            return {"data": rows(object_days)}
        return {"currency": currency}

    monkeypatch.setattr(api_client, "graph_get", fake)
    return calls


def run(**kw):
    from meta_ads_mcp.core.signals import get_performance_signals
    return get_performance_signals(**kw)


def account_series():
    days = stable_days(ALL)
    return days


class TestTool:
    def entities(self):
        return [
            ("A", "Stable campaign", stable_days(ALL)),
            ("B", "Broken campaign", series(baseline_kw=HV, recent_kw={**HV, "purchases": 20.0, "revenue": 2000.0})),
            ("C", "Brand new", stable_days(ALL[-5:])),
        ]

    def test_scans_top_entities_and_ranks_the_findings(self, monkeypatch):
        calls = install(monkeypatch, account_series(), self.entities())
        out = run(object_id="act_1", top_n=5)

        assert [a["entity"]["id"] for a in out["anomalies"]] == ["B"] * len(out["anomalies"])
        top = out["anomalies"][0]
        assert top["severity"] == "HIGH" and top["entity"] == {"level": "campaign", "id": "B", "name": "Broken campaign"}
        assert out["summary"]["entities_analyzed"] == 2 and out["summary"]["entities_skipped"] == 1
        assert out["skipped"] == [{"id": "C", "name": "Brand new",
                                   "reason": "only 2 days of baseline delivery (needs 7)"}]
        assert out["summary"]["worse"] >= 1 and out["summary"]["top_finding"] == top["message"]
        assert out["currency"] == "USD"
        assert out["metrics_analyzed"][:3] == ["spend", "impressions", "ctr"]

        # request shapes: object series, spend ranking, then daily rows for just the top ids
        object_call, rank_call, daily_call = [c for c in calls if c["endpoint"].endswith("/insights")]
        assert json.loads(object_call["params"]["time_range"]) == {"since": ALL[0], "until": UNTIL.isoformat()}
        assert object_call["params"]["time_increment"] == "1"
        assert rank_call["params"]["level"] == "campaign" and rank_call["params"]["sort"] == "spend_descending"
        assert rank_call["params"]["limit"] == "5"
        flt = json.loads(daily_call["params"]["filtering"])
        assert flt == [{"field": "campaign.id", "operator": "IN", "value": ["A", "B", "C"]}]
        assert daily_call["params"]["time_increment"] == "1"

    def test_today_is_never_requested(self, monkeypatch):
        calls = install(monkeypatch, account_series(), self.entities())
        run(object_id="act_1")
        for c in calls:
            if "time_range" in c["params"]:
                assert json.loads(c["params"]["time_range"])["until"] == UNTIL.isoformat()
                assert json.loads(c["params"]["time_range"])["until"] < TODAY.isoformat()

    def test_self_level_analyzes_the_object_with_one_call(self, monkeypatch):
        broken = {**stable_days(ALL[:3], **HV),
                  **series(baseline_kw=HV, recent_kw={**HV, "purchases": 20.0, "revenue": 2000.0})}
        calls = install(monkeypatch, broken)
        out = run(object_id="act_1", level="self")
        insights = [c for c in calls if c["endpoint"].endswith("/insights")]
        assert len(insights) == 1
        assert out["anomalies"][0]["entity"] == {"level": "self", "id": "act_1", "name": None}
        assert out["anomalies"][0]["severity"] == "HIGH"

    def test_account_is_an_alias_for_self(self, monkeypatch):
        install(monkeypatch, account_series())
        assert run(object_id="act_1", level="account")["level"] == "self"

    def test_trend_comes_from_the_object_series_and_include_daily_adds_the_days(self, monkeypatch):
        days = stable_days(ALL[:-7])
        days.update(stable_days(ALL[-7:], spend=200.0))
        install(monkeypatch, days, self.entities())
        out = run(object_id="act_1", include_daily=True)
        assert out["trend"]["metrics"]["spend"]["direction"] == "up"
        assert len(out["trend"]["daily"]) == 17
        assert out["trend"]["daily"][0]["date"] == ALL[0]
        assert set(out["trend"]["daily"][0]) >= {"date", "spend", "ctr", "cpa"}

    def test_trend_daily_is_omitted_by_default(self, monkeypatch):
        install(monkeypatch, account_series(), self.entities())
        assert "daily" not in run(object_id="act_1")["trend"]

    def test_metrics_argument_restricts_the_analysis(self, monkeypatch):
        install(monkeypatch, account_series(), self.entities())
        out = run(object_id="act_1", metrics="cpa, roas")
        assert out["metrics_analyzed"] == ["cpa", "roas"]
        assert {a["metric"] for a in out["anomalies"]} <= {"cpa", "roas", "purchases_stopped"}
        assert set(out["trend"]["metrics"]) <= {"cpa", "roas"}

    def test_default_metrics_skip_what_the_account_has_no_data_for(self, monkeypatch):
        no_leads = account_series()
        install(monkeypatch, no_leads, self.entities())
        out = run(object_id="act_1", archetype="hybrid")
        assert "leads" not in out["metrics_analyzed"] and "cpl" not in out["metrics_analyzed"]
        assert "cpa" in out["metrics_analyzed"]

    def test_custom_conversion_action_type_is_analyzed(self, monkeypatch):
        base = stable_days(BASELINE, conversions=8.0)
        base.update(stable_days(RECENT, conversions=2.0))
        install(monkeypatch, base)
        out = run(object_id="act_1", level="self", conversion_action_type="offsite_conversion.custom.123",
                  metrics="conversions,cost_per_conversion")
        found = {a["metric"]: a for a in out["anomalies"]}
        assert found["cost_per_conversion"]["assessment"] == "worse"
        assert found["conversions"]["change_pct"] == pytest.approx(-75.0, abs=0.5)

    def test_paged_entity_rows_are_followed(self, monkeypatch):
        pages = {}
        entities = self.entities()[:2]

        def fake(endpoint, params=None, fields=None):
            p = dict(params or {})
            if not endpoint.endswith("/insights"):
                return {"currency": "USD"}
            if "filtering" in p:
                all_rows = []
                for eid, name, dd in entities:
                    all_rows += rows(dd, campaign_id=eid, campaign_name=name)
                half = len(all_rows) // 2
                if p.get("after") == "c1":
                    pages["second"] = True
                    return {"data": all_rows[half:]}
                return {"data": all_rows[:half], "paging": {"next": "x", "cursors": {"after": "c1"}}}
            if "sort" in p:
                return {"data": [{"campaign_id": e, "campaign_name": n, "spend": "1"} for e, n, _ in entities]}
            return {"data": rows(account_series())}

        monkeypatch.setattr(api_client, "graph_get", fake)
        out = run(object_id="act_1")
        assert pages.get("second") is True
        assert out["summary"]["entities_skipped"] == 0
        assert any(a["entity"]["id"] == "B" for a in out["anomalies"])


class TestToolFailuresAndValidation:
    def test_unreadable_object_returns_an_error_with_a_hint_not_an_exception(self, monkeypatch):
        install(monkeypatch, {}, object_error=MetaAPIError("(#200) no access", error_code=200))
        out = run(object_id="act_1")
        assert "Could not read insights" in out["error"] and "hint" in out
        assert out["window"]["today_excluded"] is True

    def test_ranking_failure_keeps_the_trend(self, monkeypatch):
        install(monkeypatch, account_series(), ranking_error=MetaAPIError("(#100) bad sort", error_code=100))
        out = run(object_id="act_1")
        assert "entities" in out["errors"] and out["anomalies"] == []
        assert out["trend"]["metrics"]
        assert "trend (if present) is still valid" in out["hint"]

    def test_entity_daily_failure_keeps_the_trend(self, monkeypatch):
        install(monkeypatch, account_series(), [("A", "A", stable_days(ALL))],
                entity_daily_error=MetaAPIError("(#17) rate limited", error_code=17))
        out = run(object_id="act_1")
        assert "entity_daily" in out["errors"] and out["trend"]["metrics"]

    @pytest.mark.parametrize("kw, fragment", [
        ({"level": "galaxy"}, "level must be one of"),
        ({"recent_days": 0}, "recent_days"),
        ({"recent_days": 8}, "recent_days"),
        ({"baseline_days": 6}, "baseline_days"),
        ({"baseline_days": 61}, "baseline_days"),
        ({"metrics": "cpa,vibes"}, "unknown metrics: vibes"),
        ({"metrics": "conversions"}, "need conversion_action_type"),
    ])
    def test_input_validation(self, kw, fragment):
        out = run(object_id="act_1", **kw)
        assert out["blocked_at"] == "input_validation" and fragment in out["error"]

    def test_top_n_is_clamped(self, monkeypatch):
        calls = install(monkeypatch, account_series(), [("A", "A", stable_days(ALL))])
        run(object_id="act_1", top_n=500)
        rank = next(c for c in calls if "sort" in c["params"])
        assert rank["params"]["limit"] == str(signals.MAX_TOP_N)

    def test_windows_are_echoed(self, monkeypatch):
        install(monkeypatch, account_series(), [])
        out = run(object_id="act_1", recent_days=2, baseline_days=10)
        assert out["window"]["recent"] == {"from": d(1), "to": d(0), "days": 2}
        assert out["window"]["baseline"] == {"from": d(11), "to": d(2), "days": 10}
        assert out["thresholds"]["min_z_score"] == signals.Z_FLAG


class TestUnits:
    """Seen live: trend `last_7d` spend 23,537 was the daily mean (164,760 / 7), not the 7-day total."""

    def test_trend_count_metrics_give_totals_and_per_day_averages_with_units(self):
        days = stable_days(signals._dates(UNTIL, 14))
        m = build_trend(days, UNTIL, ["spend", "purchases", "ctr", "cpa"])["metrics"]
        week = sum(days[dt]["spend"] for dt in signals._dates(UNTIL, 7))
        assert m["spend"]["last_7d"] == pytest.approx(week, abs=0.01)
        assert m["spend"]["last_7d_per_day"] == pytest.approx(week / 7, abs=0.01)
        assert m["spend"]["unit"] == "currency"
        assert m["purchases"]["last_7d"] == pytest.approx(
            sum(days[dt]["purchases"] for dt in signals._dates(UNTIL, 7)), abs=0.01)

    def test_trend_ratio_metrics_have_a_unit_and_no_per_day_fields(self):
        days = stable_days(signals._dates(UNTIL, 14))
        m = build_trend(days, UNTIL, ["ctr", "cpa"])["metrics"]
        assert m["ctr"]["unit"] == "percent" and "last_7d_per_day" not in m["ctr"]
        assert m["cpa"]["unit"] == "currency per purchase"
        assert m["ctr"]["last_7d"] == pytest.approx(2.0, abs=0.01)

    def test_percent_changes_are_unaffected_by_the_totals_change(self):
        last7, prior7 = signals._dates(UNTIL, 7), signals._dates(UNTIL - timedelta(days=7), 7)
        days = {**stable_days(prior7), **stable_days(last7, spend=200.0)}
        spend = build_trend(days, UNTIL, ["spend"])["metrics"]["spend"]
        assert spend["change_pct"] == pytest.approx(100.0, abs=3.0)  # totals and averages agree on the change
        assert spend["last_7d"] == pytest.approx(2 * spend["prior_7d"], rel=0.03)

    def test_anomalies_say_that_count_metrics_are_daily_averages(self):
        found, _ = run_entity(series(recent_kw={"spend": 150.0}), ["spend", "ctr"])
        assert by_metric(found)["spend"]["unit"] == "currency, daily average"
        found, _ = run_entity(series(recent_kw={"clicks": 320.0}), ["ctr"])
        assert by_metric(found)["ctr"]["unit"] == "percent"


def step_change_account():
    """Healthy for 7 days, then a step change ~10 days ago that never came back (Seed on Sep 28)."""
    dates = signals._dates(UNTIL, 17)
    healthy = stable_days(dates[:7], **HV)
    degraded = stable_days(dates[7:], **{**HV, "purchases": 26.0, "revenue": 2600.0})  # CPA +54%
    return {**healthy, **degraded}


class TestTrendWorsening:
    def test_a_sustained_decline_is_listed_with_severity(self):
        last7, prior7 = signals._dates(UNTIL, 7), signals._dates(UNTIL - timedelta(days=7), 7)
        days = {**stable_days(prior7, **HV), **stable_days(last7, **{**HV, "purchases": 26.0, "revenue": 2600.0})}
        worse = {f["metric"]: f for f in build_trend(days, UNTIL, ["spend", "cpa", "purchases", "roas", "ctr"])["worsening"]}
        assert worse["cpa"]["severity"] == "HIGH" and worse["cpa"]["change_pct"] == pytest.approx(53.8, abs=1.0)
        assert worse["purchases"]["severity"] == "MEDIUM"  # -35%: past the bar, short of the 40% HIGH line
        assert "spend" not in worse and "ctr" not in worse  # unchanged or neutral
        assert worse["cpa"]["message"].startswith("CPA up 54% vs the prior 7 days")

    def test_improvements_are_not_listed_as_worsening(self):
        last7, prior7 = signals._dates(UNTIL, 7), signals._dates(UNTIL - timedelta(days=7), 7)
        days = {**stable_days(prior7, **HV), **stable_days(last7, **{**HV, "purchases": 60.0, "revenue": 6000.0})}
        assert build_trend(days, UNTIL, ["cpa", "purchases", "roas"])["worsening"] == []

    def test_small_counts_do_not_count_as_a_decline(self):
        last7, prior7 = signals._dates(UNTIL, 7), signals._dates(UNTIL - timedelta(days=7), 7)
        low = {"spend": 100.0, "purchases": 2.0, "revenue": 200.0}
        days = {**stable_days(prior7, **low), **stable_days(last7, **{**low, "purchases": 1.4, "revenue": 140.0})}
        assert build_trend(days, UNTIL, ["cpa", "purchases"])["worsening"] == []  # 14 -> ~10 purchases: noise

    def test_flags_are_most_severe_first(self):
        last7, prior7 = signals._dates(UNTIL, 7), signals._dates(UNTIL - timedelta(days=7), 7)
        days = {**stable_days(prior7, **HV), **stable_days(last7, **{**HV, "purchases": 26.0, "revenue": 2600.0})}
        sev = [f["severity"] for f in build_trend(days, UNTIL, ["purchases", "cpa", "roas"])["worsening"]]
        assert sev == sorted(sev, key=signals.SEVERITY_RANK.get)


class TestTopFindingAndSummary:
    """Seen live: Seed read 'healthy' (0 anomalies, top_finding null) while the trend showed CPA +44%."""

    def test_a_step_change_older_than_the_recent_window_surfaces_through_the_trend(self, monkeypatch):
        install(monkeypatch, step_change_account())
        out = run(object_id="act_1", level="self", archetype="ecommerce")
        assert out["summary"]["trend_worse"] >= 1
        assert out["summary"]["top_finding_source"] == "trend"
        assert out["summary"]["top_finding"].startswith("CPA up")
        assert not [a for a in out["anomalies"] if a["metric"] == "cpa"]  # 3d-vs-14d cannot see it

    def test_summary_separates_worse_better_and_changed(self, monkeypatch):
        mixed = {**stable_days(BASELINE), **stable_days(RECENT, clicks=320.0, spend=150.0)}  # CTR up, spend up
        install(monkeypatch, mixed)
        s = run(object_id="act_1", level="self", metrics="ctr,spend")["summary"]
        assert s["better"] >= 1 and s["changed"] >= 1 and s["worse"] == 0
        assert "anomalies" not in s  # no longer a total that mixes good news with bad
        assert s["top_finding"] is None  # nothing worsened: improvements are not the headline

    def test_the_worse_of_anomaly_and_trend_is_the_headline(self, monkeypatch):
        install(monkeypatch, step_change_account(),
                [("B", "Broken", series(baseline_kw=HV, recent_kw={**HV, "purchases": 12.0, "revenue": 1200.0}))])
        s = run(object_id="act_1", archetype="ecommerce")["summary"]
        assert s["top_finding_source"] == "anomaly"  # HIGH anomaly beats a MEDIUM/HIGH trend tie
        assert s["worse"] >= 1

    def test_nothing_wrong_means_no_headline(self, monkeypatch):
        install(monkeypatch, account_series(), [("A", "Stable", stable_days(ALL))])
        s = run(object_id="act_1")["summary"]
        assert s["top_finding"] is None and s["top_finding_source"] is None and s["worse"] == 0


class TestMaterialityFloor:
    """Seen live: ad sets at $36-107/day were flagged on a $35.7k/day account (the floor is level-independent)."""

    def scenario(self, monkeypatch, **kw):
        big_account = stable_days(ALL, spend=100000.0)
        tiny = series(recent_kw={"spend": 300.0}, baseline_kw={"spend": 100.0})  # spend x3, but ~0.1% of the account
        install(monkeypatch, big_account, [("T", "Tiny campaign", tiny)])
        return run(object_id="act_1", **kw)

    def test_an_entity_far_below_the_floor_is_skipped_with_a_reason(self, monkeypatch):
        out = self.scenario(monkeypatch)
        assert out["anomalies"] == []
        assert out["skipped"][0]["id"] == "T"
        assert "0.10% of the total scanned" in out["skipped"][0]["reason"] and "floor 0.5%" in out["skipped"][0]["reason"]

    def test_the_floor_can_be_lowered_or_disabled(self, monkeypatch):
        out = self.scenario(monkeypatch, min_spend_share=0)
        assert any(a["metric"] == "spend" for a in out["anomalies"])
        out = self.scenario(monkeypatch, min_spend_share=0.0005)  # 0.05%: the entity is above it
        assert any(a["metric"] == "spend" for a in out["anomalies"])

    def test_a_meaningful_share_is_analyzed_normally(self, monkeypatch):
        install(monkeypatch, account_series(), [("A", "Big share", series(recent_kw={"spend": 300.0}))])
        out = run(object_id="act_1")
        assert any(a["metric"] == "spend" for a in out["anomalies"])

    def test_the_floor_does_not_apply_when_analyzing_the_object_itself(self, monkeypatch):
        install(monkeypatch, series(recent_kw={"spend": 300.0}))
        assert any(a["metric"] == "spend" for a in run(object_id="act_1", level="self")["anomalies"])

    def test_floor_is_validated(self):
        assert run(object_id="act_1", min_spend_share=0.9)["blocked_at"] == "input_validation"
        assert run(object_id="act_1", min_spend_share=-0.1)["blocked_at"] == "input_validation"


class TestActiveButNotDelivering:
    """Seen live: an ACTIVE $3,000/day campaign whose ad sets were all paused produced nothing at all,
    because Insights returns no rows for an entity that is not delivering."""

    LONG_AGO = "2026-01-01T00:00:00+0000"

    def active(self):
        return [
            {"id": "S1", "name": "Silent campaign", "effective_status": "ACTIVE", "daily_budget": "300000",
             "created_time": self.LONG_AGO},
            {"id": "A", "name": "Delivering", "effective_status": "ACTIVE", "created_time": self.LONG_AGO},
            {"id": "NEW", "name": "Created yesterday", "effective_status": "ACTIVE",
             "created_time": "2026-10-07T09:00:00+0000"},
            {"id": "LATER", "name": "Scheduled", "effective_status": "ACTIVE", "created_time": self.LONG_AGO,
             "start_time": "2026-10-20T00:00:00+0000"},
        ]

    def test_silent_active_campaign_is_flagged_with_its_budget(self, monkeypatch):
        install(monkeypatch, account_series(), [("A", "Delivering", stable_days(ALL))], active=self.active())
        out = run(object_id="act_1")
        silent = [a for a in out["anomalies"] if a["metric"] == "not_delivering"]
        assert [a["entity"]["id"] for a in silent] == ["S1"]
        assert silent[0]["severity"] == "MEDIUM" and silent[0]["assessment"] == "worse"
        assert "ACTIVE but no spend in the last 3 days (daily budget USD 3000.00)" in silent[0]["message"]
        assert out["summary"]["worse"] == 1 and out["summary"]["top_finding"] == silent[0]["message"]

    def test_new_and_scheduled_entities_get_the_benefit_of_the_doubt(self, monkeypatch):
        install(monkeypatch, account_series(), [("A", "Delivering", stable_days(ALL))], active=self.active())
        ids = {a["entity"]["id"] for a in run(object_id="act_1")["anomalies"] if a["metric"] == "not_delivering"}
        assert "NEW" not in ids and "LATER" not in ids and "A" not in ids

    def test_asks_only_for_active_entities_and_checks_delivery_for_just_the_candidates(self, monkeypatch):
        calls = install(monkeypatch, account_series(), [("A", "Delivering", stable_days(ALL))], active=self.active())
        run(object_id="act_1")
        status = next(c for c in calls if c["endpoint"] == "/act_1/campaigns")
        assert json.loads(status["params"]["filtering"]) == [
            {"field": "effective_status", "operator": "IN", "value": ["ACTIVE"]}]
        check = [c for c in calls if c["endpoint"].endswith("/insights") and "filtering" in c["params"]
                 and "time_increment" not in c["params"]]
        assert len(check) == 1
        assert json.loads(check[0]["params"]["filtering"])[0]["value"] == ["S1", "A"]  # not NEW / LATER

    def test_ad_set_level_asks_the_adsets_edge(self, monkeypatch):
        calls = install(monkeypatch, account_series(), [], active=[])
        run(object_id="act_1", level="adset")
        assert any(c["endpoint"] == "/act_1/adsets" for c in calls)

    def test_self_level_makes_no_status_calls(self, monkeypatch):
        calls = install(monkeypatch, account_series(), active=self.active())
        run(object_id="act_1", level="self")
        assert not [c for c in calls if c["endpoint"].rsplit("/", 1)[-1] in ("campaigns", "adsets", "ads")]

    def test_status_lookup_failure_is_a_note_not_an_error(self, monkeypatch):
        install(monkeypatch, account_series(), [("A", "A", stable_days(ALL))],
                active_error=MetaAPIError("(#100) bad edge", error_code=100))
        out = run(object_id="act_1")
        assert "errors" not in out
        assert "Could not check for ACTIVE campaigns" in out["notes"][0]

    def test_no_active_entities_means_no_second_lookup(self, monkeypatch):
        calls = install(monkeypatch, account_series(), [("A", "A", stable_days(ALL))], active=[])
        run(object_id="act_1")
        assert not [c for c in calls if c["endpoint"].endswith("/insights") and "filtering" in c["params"]
                    and "time_increment" not in c["params"]]


class TestNotDeliveringLiveFindings:
    """Round-two live findings: Stripes was missed, a PAUSED ad set was flagged, and a "USD 0.00" budget showed."""

    LONG_AGO = "2026-01-01T00:00:00+0000"

    def stripes(self, delivered_days):
        entity = ("ST", "Stripes campaign", stable_days(delivered_days))
        active = [{"id": "ST", "name": "Stripes campaign", "effective_status": "ACTIVE",
                   "daily_budget": "300000", "created_time": self.LONG_AGO}]
        return entity, active

    def flagged(self, out):
        return [a for a in out["anomalies"] if a["metric"] == "not_delivering"]

    def test_an_active_campaign_that_spent_earlier_in_the_window_but_not_since_is_flagged(self, monkeypatch):
        entity, active = self.stripes(ALL[:2])  # spent Sep 21-22, then every ad set was paused
        install(monkeypatch, account_series(), [entity], active=active)
        out = run(object_id="act_1")
        silent = self.flagged(out)
        assert [a["entity"]["id"] for a in silent] == ["ST"]
        assert "ACTIVE but no spend since 2026-09-22 (daily budget USD 3000.00)" in silent[0]["message"]
        assert out["summary"]["top_finding"] == silent[0]["message"]

    def test_the_old_rule_ignored_an_entity_that_died_before_the_recent_window_but_it_is_news_when_active(self, monkeypatch):
        entity, active = self.stripes(ALL[:9])  # 9 days of delivery, then nothing for 8 days
        install(monkeypatch, account_series(), [entity], active=active)
        out = run(object_id="act_1")
        assert [a["metric"] for a in out["anomalies"]] == ["not_delivering"]  # analyze_entity alone says "not news"
        assert "since 2026-09-29" in out["anomalies"][0]["message"]

    def test_the_delivery_check_looks_at_the_recent_window_only(self, monkeypatch):
        entity, active = self.stripes(ALL[:2])
        calls = install(monkeypatch, account_series(), [entity], active=active)
        run(object_id="act_1")
        check = next(c for c in calls if c["endpoint"].endswith("/insights") and "filtering" in c["params"]
                     and "time_increment" not in c["params"])
        assert json.loads(check["params"]["time_range"]) == {"since": RECENT[0], "until": RECENT[-1]}

    def test_an_active_entity_that_is_spending_now_is_not_flagged(self, monkeypatch):
        install(monkeypatch, account_series(), [("OK", "Spending", stable_days(ALL))],
                active=[{"id": "OK", "name": "Spending", "effective_status": "ACTIVE", "created_time": self.LONG_AGO}])
        assert self.flagged(run(object_id="act_1")) == []

    def test_a_paused_entity_returned_despite_the_filter_is_ignored(self, monkeypatch):
        # Seen live: ad set 6985984762441 was PAUSED yet came back from the ACTIVE-filtered lookup
        active = [
            {"id": "P", "name": "Broad Website", "effective_status": "PAUSED", "created_time": self.LONG_AGO,
             "lifetime_budget": "11473200", "daily_budget": "0"},
            {"id": "CP", "name": "Parent paused", "effective_status": "CAMPAIGN_PAUSED", "created_time": self.LONG_AGO},
        ]
        install(monkeypatch, account_series(), [("OK", "Spending", stable_days(ALL))], active=active)
        out = run(object_id="act_1", level="campaign")
        assert self.flagged(out) == []

    def test_a_zero_budget_is_not_shown_and_the_real_one_is(self, monkeypatch):
        # Seen live: a lifetime-budget ad set reported daily_budget "0", printed as "daily budget USD 0.00"
        entity = ("L", "Lifetime", stable_days(ALL[:2]))
        active = [{"id": "L", "name": "Lifetime", "effective_status": "ACTIVE", "created_time": self.LONG_AGO,
                   "daily_budget": "0", "lifetime_budget": "11473200"}]
        install(monkeypatch, account_series(), [entity], active=active)
        message = self.flagged(run(object_id="act_1"))[0]["message"]
        assert "(lifetime budget USD 114732.00)" in message and "0.00)" not in message.replace("114732.00)", "")

    def test_no_budget_at_all_means_no_budget_text(self, monkeypatch):
        entity = ("N", "No budget", stable_days(ALL[:2]))
        active = [{"id": "N", "name": "No budget", "effective_status": "ACTIVE", "created_time": self.LONG_AGO,
                   "daily_budget": "0", "lifetime_budget": "0"}]
        install(monkeypatch, account_series(), [entity], active=active)
        message = self.flagged(run(object_id="act_1"))[0]["message"]
        assert message.startswith("ACTIVE but no spend since 2026-09-22. ")
        assert "budget" not in message.split(". ")[0]

    def test_a_stopped_delivering_finding_is_not_duplicated_and_notes_the_entity_is_still_active(self, monkeypatch):
        stopped = ("SD", "Stopped", stable_days(BASELINE))  # ran the whole baseline, nothing in the last 3 days
        active = [{"id": "SD", "name": "Stopped", "effective_status": "ACTIVE", "created_time": self.LONG_AGO}]
        install(monkeypatch, account_series(), [stopped], active=active)
        out = run(object_id="act_1")
        mine = [a for a in out["anomalies"] if a["entity"]["id"] == "SD"]
        assert [a["metric"] for a in mine] == ["delivery"]
        assert mine[0]["message"].endswith("It is still ACTIVE.")
        assert out["summary"]["worse"] == 1


def test_registered_as_read_only_tool():
    from meta_ads_mcp.server import mcp
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    assert tools["get_performance_signals"].annotations.readOnlyHint is True
