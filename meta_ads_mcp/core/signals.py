"""
Performance anomaly and trend signals (read-only).

Meta's official MCP offers anomaly signals and performance trends. The Graph API has no
endpoint for either, so they are computed here from daily Insights:

- trend:    the last 7 complete days against the 7 days before, for the object itself.
- anomaly:  for the top campaigns / ad sets / ads by spend, a recent window (default 3 days)
            against the baseline window before it (default 14 days). A move is flagged only
            when it is both large (percent change) and unusual for that entity (z-score
            against its own daily variation), and the metric has enough volume to mean anything.

These are this server's heuristics, not Meta's signals; the thresholds are constants below and
echoed in every response. Today is always excluded (it is a partial day).
"""
import json
import logging
import math
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from meta_ads_mcp.server import mcp
from mcp.types import ToolAnnotations
from meta_ads_mcp.core.api import api_client, MetaAPIError
from meta_ads_mcp.core.insights import _extract_action_value
from meta_ads_mcp.core.utils import get_account_currency

logger = logging.getLogger("meta-ads-mcp.signals")

# --- Heuristics (operator choices, not Meta's) ---
LEVELS = ("self", "campaign", "adset", "ad")
MAX_TOTAL_DAYS = 90
MAX_TOP_N = 25
MIN_BASELINE_DAYS = 7            # days of delivery in the baseline before an entity is judged at all
MIN_CONVERSIONS = 10             # baseline conversions before a cost/volume metric is judged
MIN_BASELINE_IMPRESSIONS = 5000  # before delivery-quality metrics (CTR, CPM, ...) are judged
MIN_RECENT_IMPRESSIONS = 1000
PCT_FLAG_DEFAULT = 25.0          # minimum percent change to flag a rate or cost metric
PCT_FLAG = {"spend": 30.0, "impressions": 30.0, "purchases": 30.0, "leads": 30.0,
            "conversions": 30.0, "revenue": 30.0}
Z_FLAG = 2.5                     # how unusual the move must be for this entity
Z_HIGH = 4.0                     # with PCT_HIGH, marks a worsening as HIGH instead of MEDIUM
PCT_HIGH = 40.0
SPEND_DIP_PCT = 50.0             # spend falling this far is called out as a delivery dip
SIGMA_FLOOR = 0.05               # daily variation is never taken as less than 5% of the level
NO_CONVERSION_SPEND_MULTIPLE = 5.0  # recent spend this many baseline-CPAs with 0 conversions: expecting ~5, P(0) < 1%
TREND_FLAT_PCT = 5.0             # week-over-week moves inside +/-5% are "flat"
MAX_PAGES = 4

THRESHOLDS = {
    "min_baseline_days": MIN_BASELINE_DAYS, "min_conversions": MIN_CONVERSIONS,
    "min_baseline_impressions": MIN_BASELINE_IMPRESSIONS, "min_recent_impressions": MIN_RECENT_IMPRESSIONS,
    "min_percent_change": PCT_FLAG_DEFAULT, "min_percent_change_volume_metrics": 30.0,
    "min_z_score": Z_FLAG, "high_severity_z": Z_HIGH, "high_severity_percent": PCT_HIGH,
    "no_conversion_spend_multiple": NO_CONVERSION_SPEND_MULTIPLE,
}

# +1: higher is better, -1: lower is better, 0: neutral (a change, not good or bad)
DIRECTION = {
    "spend": 0, "impressions": 0, "ctr": 1, "cpm": -1, "cpc": -1, "frequency": -1,
    "purchases": 1, "revenue": 1, "roas": 1, "cpa": -1,
    "leads": 1, "cpl": -1, "conversions": 1, "cost_per_conversion": -1,
}
COUNT_METRICS = ("spend", "impressions", "purchases", "leads", "revenue", "conversions")  # compared per day
ADDITIVE = ("spend", "impressions", "clicks", "purchases", "leads", "revenue", "conversions")
LABELS = {
    "spend": "Spend/day", "impressions": "Impressions/day", "ctr": "CTR", "cpm": "CPM", "cpc": "CPC",
    "frequency": "Frequency", "purchases": "Purchases/day", "revenue": "Revenue/day", "roas": "ROAS",
    "cpa": "CPA", "leads": "Leads/day", "cpl": "CPL", "conversions": "Conversions/day",
    "cost_per_conversion": "Cost per conversion",
}
BASE_METRICS = ["spend", "impressions", "ctr", "cpm", "cpc", "frequency"]
ARCHETYPE_METRICS = {
    "ecommerce": ["purchases", "cpa", "roas", "revenue"],
    "lead_gen": ["leads", "cpl"],
    "hybrid": ["purchases", "cpa", "roas", "revenue", "leads", "cpl"],
}
# conversion group -> (count metric, cost metric, other metrics that are meaningless without the count)
CONVERSION_GROUPS = (
    ("purchases", "cpa", ("roas", "revenue")),
    ("leads", "cpl", ()),
    ("conversions", "cost_per_conversion", ()),
)
SEVERITY_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}

_PURCHASE_TYPES = ("omni_purchase", "purchase", "offsite_conversion.fb_pixel_purchase")
_LEAD_TYPES = ("lead", "onsite_conversion.lead_grouped", "offsite_conversion.fb_pixel_lead")
INSIGHT_FIELDS = ["spend", "impressions", "clicks", "frequency", "actions", "action_values"]
LEVEL_ID_FIELDS = {"campaign": ("campaign_id", "campaign_name"),
                   "adset": ("adset_id", "adset_name"),
                   "ad": ("ad_id", "ad_name")}


def _today() -> date:
    return datetime.now(timezone.utc).date()


# ----------------------------------------------------------------------------- data shaping

def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _first_value(items: Optional[list], types: tuple) -> float:
    """Value of the first present action type, in priority order (matches get_insights)."""
    for action_type in types:
        raw = _extract_action_value(items or [], action_type)
        if raw is not None:
            return _num(raw)
    return 0.0


def _parse_day(row: dict, conversion_action_type: Optional[str] = None) -> Optional[dict]:
    day = row.get("date_start")
    if not day:
        return None
    actions, values = row.get("actions") or [], row.get("action_values") or []
    return {
        "date": day,
        "spend": _num(row.get("spend")),
        "impressions": _num(row.get("impressions")),
        "clicks": _num(row.get("clicks")),
        "frequency": _num(row.get("frequency")),
        "purchases": _first_value(actions, _PURCHASE_TYPES),
        "leads": _first_value(actions, _LEAD_TYPES),
        "revenue": _first_value(values, _PURCHASE_TYPES),
        "conversions": _first_value(actions, (conversion_action_type,)) if conversion_action_type else 0.0,
    }


def _empty_day(day: str) -> dict:
    return {"date": day, **{k: 0.0 for k in ADDITIVE}, "frequency": 0.0}


def _dates(until: date, days: int) -> list[str]:
    """`days` calendar dates ending at `until`, oldest first."""
    return [(until - timedelta(days=i)).isoformat() for i in reversed(range(days))]


def _window(days_by_date: dict, dates: list[str]) -> list[dict]:
    return [days_by_date.get(d) or _empty_day(d) for d in dates]


def _aggregate(days: list[dict]) -> dict:
    agg = {k: sum(d[k] for d in days) for k in ADDITIVE}
    agg["freq_x_impr"] = sum(d["frequency"] * d["impressions"] for d in days)
    agg["n_days"] = len(days)
    agg["delivered"] = sum(1 for d in days if d["impressions"] > 0)
    return agg


def _value(metric: str, agg: dict) -> Optional[float]:
    """A metric over a window, or None when its denominator is zero."""
    if metric in COUNT_METRICS:
        return agg[metric] / max(agg["n_days"], 1)
    spend, impressions = agg["spend"], agg["impressions"]
    if metric == "ctr":
        return agg["clicks"] / impressions * 100 if impressions > 0 else None
    if metric == "cpm":
        return spend / impressions * 1000 if impressions > 0 else None
    if metric == "cpc":
        return spend / agg["clicks"] if agg["clicks"] > 0 else None
    if metric == "frequency":  # impression-weighted: reach is not additive across days
        return agg["freq_x_impr"] / impressions if impressions > 0 else None
    if metric == "cpa":
        return spend / agg["purchases"] if agg["purchases"] > 0 else None
    if metric == "cpl":
        return spend / agg["leads"] if agg["leads"] > 0 else None
    if metric == "cost_per_conversion":
        return spend / agg["conversions"] if agg["conversions"] > 0 else None
    if metric == "roas":
        return agg["revenue"] / spend if spend > 0 else None
    return None


def _round(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(value, 4 if abs(value) < 1 else 2)


# ----------------------------------------------------------------------------- metric selection

def _default_metrics(archetype: str, conversion_action_type: Optional[str]) -> list[str]:
    chosen = BASE_METRICS + ARCHETYPE_METRICS.get(archetype, [])
    if conversion_action_type:
        chosen = chosen + ["conversions", "cost_per_conversion"]
    return chosen


def _has_data(metric: str, agg: dict) -> bool:
    """Whether a metric means anything for this window (e.g. no CPA for an account with no purchases)."""
    needs = {"purchases": "purchases", "cpa": "purchases", "roas": "purchases", "revenue": "revenue",
             "leads": "leads", "cpl": "leads", "conversions": "conversions",
             "cost_per_conversion": "conversions", "ctr": "clicks", "cpc": "clicks"}
    if metric in needs:
        return agg[needs[metric]] > 0
    return agg["impressions"] > 0 or agg["spend"] > 0


# ----------------------------------------------------------------------------- anomaly detection

def _z_score(recent: float, baseline: float, daily_values: list[float], n_recent: int) -> Optional[float]:
    """How many standard errors the recent level is from baseline, given the entity's own daily spread."""
    if len(daily_values) < 3 or n_recent < 1:
        return None
    spread = statistics.stdev(daily_values)
    spread = max(spread, SIGMA_FLOOR * abs(baseline), 1e-9)
    return (recent - baseline) / (spread / math.sqrt(n_recent))


def _poisson_z(metric: str, base: dict, recent: dict, base_len: int, recent_len: int) -> Optional[float]:
    """How surprising the recent conversion count is if the baseline rate had simply continued.

    Daily variation alone understates the noise in small counts (days with no conversion drop out of
    a cost ratio), so conversion-based metrics must also clear this. Rate per unit spend for cost
    metrics, per day for volume metrics. None when the metric is not conversion-based.
    """
    for count, cost, extras in CONVERSION_GROUPS:
        if metric in (count, cost) or metric in extras:
            if metric == count:
                expected = base[count] / base_len * recent_len
            else:
                if base["spend"] <= 0 or recent["spend"] <= 0:
                    return None
                expected = base[count] * recent["spend"] / base["spend"]
            return (recent[count] - expected) / math.sqrt(max(expected, 1.0))
    return None


def _format(metric: str, value: float) -> str:
    if metric == "ctr":
        return f"{value:.2f}%"
    if metric in ("frequency", "roas"):
        return f"{value:.2f}"
    return f"{value:,.2f}" if abs(value) < 1000 else f"{value:,.0f}"


def _assess(metric: str, pct: float) -> str:
    sign = DIRECTION[metric]
    if sign == 0:
        return "changed"
    return "better" if (pct > 0) == (sign > 0) else "worse"


def _severity(metric: str, assessment: str, pct: float, z: float) -> str:
    if assessment == "worse":
        return "HIGH" if abs(z) >= Z_HIGH and abs(pct) >= PCT_HIGH else "MEDIUM"
    if assessment == "better":
        return "INFO"
    if metric == "spend" and pct <= -SPEND_DIP_PCT:
        return "MEDIUM"  # a delivery dip, whatever the cause
    return "LOW"


def _passes_volume_gates(metric: str, base: dict, recent: dict) -> bool:
    if metric in ("ctr", "cpm", "cpc", "frequency"):
        return base["impressions"] >= MIN_BASELINE_IMPRESSIONS and recent["impressions"] >= MIN_RECENT_IMPRESSIONS
    if metric == "impressions":
        return base["impressions"] >= MIN_BASELINE_IMPRESSIONS
    if metric == "spend":
        return base["spend"] > 0
    for count, cost, extras in CONVERSION_GROUPS:
        if metric in (count, cost) or metric in extras:
            return base[count] >= MIN_CONVERSIONS
    return True


def _check_metric(metric: str, base_days: list[dict], recent_days: list[dict],
                  base: dict, recent: dict, base_len: int, recent_len: int) -> Optional[dict]:
    baseline, current = _value(metric, base), _value(metric, recent)
    if baseline is None or current is None or baseline == 0:
        return None
    if not _passes_volume_gates(metric, base, recent):
        return None

    pct = (current - baseline) / abs(baseline) * 100
    if abs(pct) < PCT_FLAG.get(metric, PCT_FLAG_DEFAULT):
        return None

    if metric in COUNT_METRICS:
        daily = [d[metric] for d in base_days]
        n_recent = recent_len
    else:
        daily = [v for v in (_value(metric, _aggregate([d])) for d in base_days) if v is not None]
        n_recent = sum(1 for d in recent_days if _value(metric, _aggregate([d])) is not None)
    z = _z_score(current, baseline, daily, n_recent)
    if z is None:
        return None
    poisson = _poisson_z(metric, base, recent, base_len, recent_len)
    if poisson is not None and abs(poisson) < abs(z):
        z = math.copysign(abs(poisson), z)  # the noisier of the two views decides how unusual this is
    if abs(z) < Z_FLAG:
        return None

    assessment = _assess(metric, pct)
    severity = _severity(metric, assessment, pct, z)
    return {
        "metric": metric,
        "assessment": assessment,
        "severity": severity,
        "recent": _round(current),
        "baseline": _round(baseline),
        "change_pct": round(pct, 1),
        "z_score": round(z, 1),
        "message": (f"{LABELS[metric]} {'up' if pct > 0 else 'down'} {abs(pct):.0f}%: "
                    f"{_format(metric, current)} over the last {recent_len}d vs {_format(metric, baseline)} "
                    f"over the {base_len}d before"),
    }


def analyze_entity(days_by_date: dict, recent_dates: list[str], baseline_dates: list[str],
                   metrics: list[str], notes: Optional[list] = None) -> tuple[list[dict], Optional[str]]:
    """Anomalies for one entity's daily series, or (empty, reason) when it cannot be judged.

    `notes`, when given, receives the conversion groups skipped for low volume.
    """
    delivered = [d for d in baseline_dates if (days_by_date.get(d) or {}).get("impressions", 0) > 0]
    if not delivered:
        return [], "no delivery in the baseline window"
    base_dates = [d for d in baseline_dates if d >= delivered[0]]  # days before it existed are not "zero days"
    if len(base_dates) < MIN_BASELINE_DAYS:
        return [], f"only {len(base_dates)} days of baseline delivery (needs {MIN_BASELINE_DAYS})"

    base_days, recent_days = _window(days_by_date, base_dates), _window(days_by_date, recent_dates)
    base, recent = _aggregate(base_days), _aggregate(recent_days)
    base_len, recent_len = len(base_days), len(recent_days)

    # Stopped delivering: nothing recently, but it was running just before the recent window.
    if recent["delivered"] == 0:
        was_running = any((days_by_date.get(d) or {}).get("impressions", 0) > 0 for d in baseline_dates[-3:])
        if was_running and base["spend"] > 0:
            per_day = base["spend"] / base_len
            return [{
                "metric": "delivery", "assessment": "worse", "severity": "MEDIUM",
                "recent": 0.0, "baseline": _round(per_day), "change_pct": -100.0, "z_score": None,
                "message": (f"Stopped delivering: no spend in the last {recent_len}d, was averaging "
                            f"{_format('spend', per_day)}/day. May be paused or out of budget on purpose."),
            }], None
        return [], None  # was already dead before the recent window: not news

    anomalies: list[dict] = []
    handled: set[str] = set()
    for count, cost, extras in CONVERSION_GROUPS:
        if count not in metrics and cost not in metrics:
            continue
        if 0 < base[count] < MIN_CONVERSIONS and notes is not None:
            notes.append(f"{count}: only {base[count]:g} in the baseline (needs {MIN_CONVERSIONS}), so "
                         f"{count}-based metrics were not judged")
        if base[count] >= MIN_CONVERSIONS and recent[count] == 0:
            baseline_cost = base["spend"] / base[count]
            if recent["spend"] >= NO_CONVERSION_SPEND_MULTIPLE * baseline_cost:
                anomalies.append({
                    "metric": f"{count}_stopped", "assessment": "worse", "severity": "HIGH",
                    "recent": 0.0, "baseline": _round(base[count] / base_len), "change_pct": -100.0, "z_score": None,
                    "message": (f"No {count} in the last {recent_len}d despite {_format('spend', recent['spend'])} "
                                f"spent (baseline cost per {count.rstrip('s')}: {_format('cpa', baseline_cost)})"),
                })
                handled.update({count, cost, *extras})

    for metric in metrics:
        if metric in handled:
            continue
        found = _check_metric(metric, base_days, recent_days, base, recent, base_len, recent_len)
        if found:
            anomalies.append(found)
    return anomalies, None


def _sort_anomalies(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda a: (SEVERITY_RANK[a["severity"]], -a.get("_spend", 0.0),
                                        -abs(a["z_score"] or 0.0)))


# ----------------------------------------------------------------------------- trend

def build_trend(days_by_date: dict, until: date, metrics: list[str]) -> dict:
    """Last 7 complete days against the 7 before, per metric."""
    last_dates = _dates(until, 7)
    prior_dates = _dates(until - timedelta(days=7), 7)
    last, prior = _aggregate(_window(days_by_date, last_dates)), _aggregate(_window(days_by_date, prior_dates))
    out: dict[str, Any] = {
        "last_7d": {"from": last_dates[0], "to": last_dates[-1]},
        "prior_7d": {"from": prior_dates[0], "to": prior_dates[-1]},
        "metrics": {},
    }
    if last["delivered"] == 0 and prior["delivered"] == 0:
        out["note"] = "No delivery in the last 14 days."
        return out
    if prior["delivered"] == 0:
        out["note"] = "No delivery in the prior 7 days, so there is nothing to compare against."
    for metric in metrics:
        l, p = _value(metric, last), _value(metric, prior)
        if l is None and p is None:
            continue
        entry: dict[str, Any] = {"last_7d": _round(l), "prior_7d": _round(p)}
        if l is not None and p not in (None, 0):
            pct = (l - p) / abs(p) * 100
            entry["change_pct"] = round(pct, 1)
            entry["direction"] = "flat" if abs(pct) < TREND_FLAT_PCT else ("up" if pct > 0 else "down")
            entry["assessment"] = ("neutral" if DIRECTION[metric] == 0 or entry["direction"] == "flat"
                                   else _assess(metric, pct))
        out["metrics"][metric] = entry
    return out


# ----------------------------------------------------------------------------- fetching

def _insights(endpoint: str, fields: list[str], params: dict) -> tuple[list[dict], bool]:
    """All insight rows for a request, following cursors up to MAX_PAGES. Returns (rows, more_exist)."""
    rows: list[dict] = []
    params = dict(params)
    for _ in range(MAX_PAGES):
        res = api_client.graph_get(endpoint, fields=fields, params=params)
        rows.extend(r for r in res.get("data", []) if isinstance(r, dict))
        paging = res.get("paging") or {}
        cursor = (paging.get("cursors") or {}).get("after")
        if not paging.get("next") or not cursor:
            return rows, False
        params["after"] = cursor
    return rows, True


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_performance_signals(
    object_id: str,
    level: str = "campaign",
    recent_days: int = 3,
    baseline_days: int = 14,
    top_n: int = 10,
    metrics: Optional[str] = None,
    archetype: str = "hybrid",
    conversion_action_type: Optional[str] = None,
    include_daily: bool = False,
) -> dict:
    """
    Performance anomalies and trends from daily Insights. Answers "anything unusual lately?"
    and "which way are we moving?".

    - trend: the object's last 7 complete days against the 7 before, per metric.
    - anomalies: for the top entities by spend at `level`, the recent window against the
      baseline window before it. A move is flagged only when it is both large and unusual for
      that entity (z-score on its own daily variation), the metric has enough volume, and the
      entity has at least 7 days of baseline delivery. Also flags campaigns that stopped
      delivering and ones still spending with no conversions. Worsening moves rank first.

    These are this server's heuristics, not Meta's signals; thresholds are echoed in the
    response. Today is excluded (partial day). Spend, CPA etc. are in the account's currency.

    Args:
        object_id: Ad account ID ('act_123456789') to scan across campaigns, or a campaign /
            ad set ID to look inside it.
        level: What to scan: 'campaign' (default), 'adset', 'ad', or 'self' to analyze the
            object itself (e.g. one account's overall performance).
        recent_days: Days in the recent window, 1-7 (default 3).
        baseline_days: Days in the baseline window before it, 7-60 (default 14).
        top_n: How many entities to scan, ranked by spend over the whole window, 1-25 (default 10).
        metrics: Comma-separated subset to analyze, from: spend, impressions, ctr, cpm, cpc,
            frequency, purchases, cpa, roas, revenue, leads, cpl, conversions, cost_per_conversion.
            Default depends on `archetype`; metrics with no data are skipped.
        archetype: 'ecommerce', 'lead_gen' or 'hybrid' (default) - which conversion metrics to include.
        conversion_action_type: Also analyze this specific action type (e.g. a custom conversion's
            'offsite_conversion.custom.123') as `conversions` and `cost_per_conversion`.
        include_daily: Include the object's daily series in `trend.daily`.
    """
    api_client._ensure_initialized()
    level = "self" if level == "account" else level
    problems = []
    if level not in LEVELS:
        problems.append(f"level must be one of {', '.join(LEVELS)} (or 'account' for 'self')")
    if not 1 <= int(recent_days) <= 7:
        problems.append("recent_days must be between 1 and 7")
    if not 7 <= int(baseline_days) <= 60:
        problems.append("baseline_days must be between 7 and 60")
    requested = [m.strip() for m in metrics.split(",") if m.strip()] if metrics else None
    if requested:
        unknown = [m for m in requested if m not in DIRECTION]
        if unknown:
            problems.append(f"unknown metrics: {', '.join(unknown)}; choose from {', '.join(DIRECTION)}")
        if any(m in ("conversions", "cost_per_conversion") for m in requested) and not conversion_action_type:
            problems.append("conversions / cost_per_conversion need conversion_action_type")
    if problems:
        return {"error": "; ".join(problems), "blocked_at": "input_validation"}

    recent_days, baseline_days = int(recent_days), int(baseline_days)
    top_n = max(1, min(int(top_n), MAX_TOP_N))
    object_id = str(object_id).strip()

    until = _today() - timedelta(days=1)
    total_days = min(max(recent_days + baseline_days, 14), MAX_TOTAL_DAYS)
    all_dates = _dates(until, total_days)
    recent_dates = all_dates[-recent_days:]
    baseline_dates = all_dates[len(all_dates) - recent_days - baseline_days: len(all_dates) - recent_days]
    time_range = json.dumps({"since": all_dates[0], "until": all_dates[-1]})

    response: dict[str, Any] = {
        "object_id": object_id, "level": level,
        "window": {
            "recent": {"from": recent_dates[0], "to": recent_dates[-1], "days": recent_days},
            "baseline": {"from": baseline_dates[0], "to": baseline_dates[-1], "days": len(baseline_dates)},
            "today_excluded": True,
        },
        "thresholds": THRESHOLDS,
    }
    if object_id.startswith("act_"):
        response["currency"] = get_account_currency(object_id)
    errors: dict[str, str] = {}

    # --- The object's own daily series (feeds the trend, and the anomalies when level == 'self') ---
    try:
        rows, more = _insights(f"/{object_id}/insights", INSIGHT_FIELDS,
                               {"time_range": time_range, "time_increment": "1", "limit": "500"})
    except MetaAPIError as e:
        return {**response, "error": f"Could not read insights for {object_id}: {e}",
                "hint": "Check the ID and that the token has ads_read on this account."}
    object_days = {d["date"]: d for d in (_parse_day(r, conversion_action_type) for r in rows) if d}
    if more:
        response["note"] = "Daily series was cut off at the page limit; older days may be missing."

    window_agg = _aggregate(_window(object_days, all_dates))
    selected = requested or _default_metrics(archetype, conversion_action_type)
    if not requested:
        selected = [m for m in selected if _has_data(m, window_agg)]

    response["trend"] = build_trend(object_days, until, selected)
    if include_daily:
        response["trend"]["daily"] = [
            {"date": d["date"], **{m: _round(_value(m, _aggregate([d]))) for m in selected}}
            for d in _window(object_days, all_dates)
        ]

    # --- Entities to scan ---
    entities: list[dict] = []
    if level == "self":
        entities = [{"id": object_id, "name": None, "days": object_days}]
    else:
        id_field, name_field = LEVEL_ID_FIELDS[level]
        ranked: list[dict] = []
        try:
            ranked_rows, _ = _insights(f"/{object_id}/insights", ["spend", id_field, name_field],
                                       {"level": level, "time_range": time_range,
                                        "sort": "spend_descending", "limit": str(top_n)})
            ranked = [r for r in ranked_rows if r.get(id_field)][:top_n]
        except MetaAPIError as e:
            errors["entities"] = str(e)
        if ranked:
            try:
                ids = [r[id_field] for r in ranked]
                daily_rows, cut = _insights(
                    f"/{object_id}/insights", INSIGHT_FIELDS + [id_field, name_field],
                    {"level": level, "time_range": time_range, "time_increment": "1", "limit": "500",
                     "filtering": json.dumps([{"field": f"{level}.id", "operator": "IN", "value": ids}])},
                )
                by_id: dict[str, dict] = {}
                for row in daily_rows:
                    day = _parse_day(row, conversion_action_type)
                    if day and row.get(id_field):
                        by_id.setdefault(row[id_field], {})[day["date"]] = day
                entities = [{"id": r[id_field], "name": r.get(name_field), "days": by_id.get(r[id_field], {})}
                            for r in ranked]
                if cut:
                    response["note"] = (response.get("note", "") +
                                        " Entity daily rows were cut off at the page limit.").strip()
            except MetaAPIError as e:
                errors["entity_daily"] = str(e)

    # --- Anomalies ---
    anomalies: list[dict] = []
    skipped: list[dict] = []
    low_volume: list[dict] = []
    for entity in entities:
        entity_notes: list[str] = []
        found, reason = analyze_entity(entity["days"], recent_dates, baseline_dates, selected, entity_notes)
        if entity_notes:
            low_volume.append({"id": entity["id"], "name": entity["name"], "notes": entity_notes})
        spend_per_day = _value("spend", _aggregate(_window(entity["days"], recent_dates)))
        for item in found:
            item["entity"] = {"level": level, "id": entity["id"], "name": entity["name"]}
            item["_spend"] = spend_per_day or 0.0
        anomalies.extend(found)
        if reason:
            skipped.append({"id": entity["id"], "name": entity["name"], "reason": reason})
    anomalies = _sort_anomalies(anomalies)
    for item in anomalies:
        item.pop("_spend", None)

    worse = sum(1 for a in anomalies if a["assessment"] == "worse")
    response["metrics_analyzed"] = selected
    response["summary"] = {
        "entities_analyzed": len(entities) - len(skipped),
        "entities_skipped": len(skipped),
        "anomalies": len(anomalies),
        "worse": worse,
        "better": sum(1 for a in anomalies if a["assessment"] == "better"),
        "top_finding": anomalies[0]["message"] if anomalies else None,
    }
    response["anomalies"] = anomalies
    if skipped:
        response["skipped"] = skipped
    if low_volume:
        response["low_volume"] = low_volume
        response["low_volume_hint"] = ("Conversion metrics need volume to be judged. For low-volume campaigns use "
                                       "recent_days=7 and a longer baseline_days.")
    if errors:
        response["errors"] = errors
        response["hint"] = "Meta rejected part of this request; the trend (if present) is still valid."
    response["rate_limit_usage_pct"] = api_client.rate_limits.max_usage_pct
    return response
