"""
A/B tests (split tests) and conversion lift studies (read-only).

Meta stores both as "ad studies". `list_ad_studies` finds them; `get_ad_study` reads one: its cells,
its objectives and the results.

Where the results live differs by kind, per Meta's guides:
- Lift studies: each objective carries `results`, a list of JSON strings (one per cell with the
  `cell_id` breakdown), with test/control populations, incremental conversions and confidence.
- Split tests: there is no results edge. Meta says to compare the efficiency metric (such as cost per
  result) of each cell, so the numbers are read from Insights for the campaigns / ad sets in each cell
  over the study's dates. This tool reports them and does not declare a winner or test significance.

Docs: https://developers.facebook.com/documentation/ads-commerce/marketing-api/reference/ad-study
      https://developers.facebook.com/documentation/ads-commerce/marketing-api/guides/lift-studies
"""
import json
import logging
import re
from datetime import date, datetime, timezone
from typing import Any, Optional

from meta_ads_mcp.server import mcp
from mcp.types import ToolAnnotations
from meta_ads_mcp.core.api import api_client, MetaAPIError
from meta_ads_mcp.core.signals import INSIGHT_FIELDS, _aggregate, _parse_day, _value
from meta_ads_mcp.core.utils import ensure_account_id_format, get_account_currency, truncation_fields

logger = logging.getLogger("meta-ads-mcp.studies")

STUDY_FIELDS = ["id", "name", "description", "type", "start_time", "end_time", "cooldown_start_time",
                "observation_end_time", "canceled_time", "created_time", "updated_time",
                "results_first_available_date", "confidence_level"]
STUDY_FIELDS_BASIC = ["id", "name", "description", "type", "start_time", "end_time", "created_time", "updated_time"]
CELL_FIELDS = ["id", "name", "treatment_percentage", "control_percentage", "ad_entities_count"]
CELL_FIELDS_BASIC = ["id", "name"]
OBJECTIVE_FIELDS = ["id", "name", "type", "is_primary", "last_updated_results"]
OBJECTIVE_FIELDS_BASIC = ["id", "name", "type"]

PAGE_SIZE = 100
MAX_PAGES = 5               # studies read for a list (500)
MAX_LIMIT = 100
MAX_CELLS = 20
MAX_OBJECTIVES = 50
DEFAULT_OBJECTIVES = 25      # a study can have 20+ objectives (one per outcome and channel)
ENTITY_PAGE = 100           # campaigns / ad sets read per cell
ENTITY_EXAMPLES = 10
INSIGHT_ID_CHUNK = 50
STATUSES = ("scheduled", "running", "observation", "completed", "canceled", "recurring_config", "unknown")
LIFT_TOP_LEVEL = ("cell_id", "spend", "impressions")
_METRIC_KEY = re.compile(r"^([^._]+)[._](.+)$")   # population_test, conversions.incremental, scoreSum.incremental
_REJECTED_FIELD = re.compile(r"nonexisting field \((\w+)\)", re.I)
BIG_ID = 10 ** 15                                  # a whole number this big would lose digits in a JSON client
CONTEXT_GROUPS = ("population",)                   # who was in the test, not what it found
HOIST_SKIP = ("cell_id", "cell_name", "has_results") + LIFT_TOP_LEVEL
_ID_KEY = re.compile(r"(?:^|[._])id\d*$|[a-z]Id\d*s?$")  # id, experiment.id, topNAdsId1: always text, whatever the size
SPEND_SKEW_RATIO = 1.5      # cells spending this many times apart are not comparable
LEVEL_ID_FIELDS = {"campaign": "campaign_id", "adset": "adset_id"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> Optional[datetime]:
    """Meta datetimes come as ISO strings; the study creation call takes epoch seconds, so accept both."""
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("+0000", "+00:00").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: Any) -> Optional[str]:
    parsed = _parse_time(value)
    return parsed.isoformat() if parsed else None


def _status(raw: dict, now: datetime) -> str:
    """Where a study is in its life, worked out from its dates (Meta has no status field on the study)."""
    if _parse_time(raw.get("canceled_time")):
        return "canceled"
    if str(raw.get("type") or "").upper() == "CONTINUOUS_LIFT_CONFIG":
        return "recurring_config"  # a template that starts studies, not a study run
    start, end, observed = (_parse_time(raw.get(k)) for k in ("start_time", "end_time", "observation_end_time"))
    if not start:
        return "unknown"
    if now < start:
        return "scheduled"
    if not end or now <= end:
        return "running"
    if observed and now <= observed:
        return "observation"
    return "completed"


def _confidence(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number / 100, 4) if number > 1 else number


def _study(raw: dict, now: datetime) -> dict:
    description = raw.get("description")
    return {
        "id": raw.get("id"), "name": raw.get("name"), "type": raw.get("type"), "status": _status(raw, now),
        "description": description[:200] if isinstance(description, str) else None,
        "start": _iso(raw.get("start_time")), "end": _iso(raw.get("end_time")),
        "observation_end": _iso(raw.get("observation_end_time")), "canceled": _iso(raw.get("canceled_time")),
        "created": _iso(raw.get("created_time")), "updated": _iso(raw.get("updated_time")),
        "results_first_available": raw.get("results_first_available_date"),
        "confidence_level": _confidence(raw.get("confidence_level")),
    }


def _is_split(study_type: Any) -> bool:
    return "SPLIT" in str(study_type or "").upper()


def _hint_for(code: Optional[int]) -> str:
    if code == 100:
        return "Meta did not accept this ID: it may not be an ad study, or this token cannot see it."
    if code in (200, 10, 190, 102, 270):
        return ("Reading studies needs ads_read (and business_management for a business's studies) and access to "
                "the account or business that owns them. Conversion Lift is a limited-access product: ask your "
                "Meta representative if it is missing.")
    return "Meta rejected the request."


def _next_fields(fields: list[str], error: MetaAPIError, basic: Optional[list[str]]) -> Optional[list[str]]:
    """The fields to retry with after Meta rejects one: just the one it names when it names one, otherwise the
    basic set. None when there is nothing left to drop."""
    named = _REJECTED_FIELD.search(str(error))
    if named and named.group(1) in fields:
        return [f for f in fields if f != named.group(1)]
    if basic and fields != basic:
        return list(basic)
    return None


def _read(endpoint: str, fields: list[str], fallback: Optional[list[str]], params: Optional[dict] = None,
          max_pages: int = 1) -> tuple[list[dict], bool, list[str]]:
    """Rows from an edge, following cursors up to max_pages. When Meta rejects a field with a code 100, only the
    field it names is dropped (or the `fallback` set when it names none) and the read retried.

    Returns (rows, more_exist, fields_dropped)."""
    params = dict(params or {})
    chosen = list(fields)
    rows: list[dict] = []
    for _ in range(max_pages):
        while True:
            try:
                res = api_client.graph_get(endpoint, fields=chosen, params=params)
                break
            except MetaAPIError as e:
                retry = _next_fields(chosen, e, fallback) if e.error_code == 100 else None
                if retry is None:
                    raise
                chosen = retry
        rows.extend(r for r in res.get("data", []) if isinstance(r, dict))
        paging = res.get("paging") or {}
        cursor = (paging.get("cursors") or {}).get("after")
        if not paging.get("next") or not cursor:
            return rows, False, [f for f in fields if f not in chosen]
        params["after"] = cursor
    return rows, True, [f for f in fields if f not in chosen]


def _get_node(endpoint: str, fields: list[str], fallback: list[str]) -> tuple[dict, list[str]]:
    """One object (no `data` list), with the same field-by-field retry as _read."""
    chosen = list(fields)
    while True:
        try:
            return api_client.graph_get(endpoint, fields=chosen), [f for f in fields if f not in chosen]
        except MetaAPIError as e:
            retry = _next_fields(chosen, e, fallback) if e.error_code == 100 else None
            if retry is None:
                raise
            chosen = retry


# ----------------------------------------------------------------------------- list

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_ad_studies(
    account_id: Optional[str] = None,
    business_id: Optional[str] = None,
    study_type: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 25,
) -> dict:
    """
    List A/B tests (split tests) and conversion lift studies, newest first. Read-only.

    Give an ad account or a business. Use `get_ad_study` for one study's cells and results. A study's
    `status` is worked out from its dates (scheduled, running, observation, completed or canceled).

    Args:
        account_id: Ad account ID (e.g., 'act_123456789'). Its studies are read from the account.
        business_id: Business ID, to list every study the business owns instead.
        study_type: Only types containing this text, case-insensitive: 'SPLIT' (split tests), 'LIFT' (lift
            and geo lift), or an exact type such as SPLIT_TEST_V2.
        status: Only this status: scheduled, running, observation, completed or canceled.
        limit: Maximum studies returned, 1-100 (default 25).
    """
    api_client._ensure_initialized()
    wanted_status = status.strip().lower() if status else None
    wanted_type = study_type.strip().upper() if study_type else None
    problems = []
    if bool(account_id) == bool(business_id):
        problems.append("give exactly one of account_id or business_id")
    if wanted_status and wanted_status not in STATUSES:
        problems.append(f"status must be one of {', '.join(STATUSES)}")
    if not 1 <= int(limit) <= MAX_LIMIT:
        problems.append(f"limit must be between 1 and {MAX_LIMIT}")
    if problems:
        return {"error": "; ".join(problems), "blocked_at": "input_validation"}
    limit = int(limit)

    account = ensure_account_id_format(account_id) if account_id else None
    business = str(business_id).strip() if business_id else None
    params = {"limit": str(PAGE_SIZE)}
    notes: list[str] = []
    try:
        try:
            rows, more, _ = _read(f"/{account or business}/ad_studies", STUDY_FIELDS, STUDY_FIELDS_BASIC, params, MAX_PAGES)
        except MetaAPIError as e:
            if not (account and e.error_code == 100):
                raise
            owner = api_client.graph_get(f"/{account}", fields=["business"]).get("business") or {}
            if not owner.get("id"):
                raise
            business = str(owner["id"])
            notes.append(f"Meta has no studies list on the ad account, so the owning business ({business}) was "
                         "read; it also lists studies for its other accounts.")
            rows, more, _ = _read(f"/{business}/ad_studies", STUDY_FIELDS, STUDY_FIELDS_BASIC, params, MAX_PAGES)
    except MetaAPIError as e:
        return {"error": str(e), "error_code": e.error_code, "hint": _hint_for(e.error_code)}

    now = _utc_now()
    studies = [_study(r, now) for r in rows]
    if wanted_type:
        studies = [s for s in studies if wanted_type in str(s["type"] or "").upper()]
    if wanted_status:
        studies = [s for s in studies if s["status"] == wanted_status]
    far_past = datetime.min.replace(tzinfo=timezone.utc)
    studies.sort(key=lambda s: _parse_time(s["start"]) or _parse_time(s["created"]) or far_past, reverse=True)

    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for s in studies:
        by_type[s["type"] or "UNKNOWN"] = by_type.get(s["type"] or "UNKNOWN", 0) + 1
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1
    cut_off = truncation_fields({"next": "more"} if more else None, len(rows),
                                "Studies are read newest-first up to a limit; filter by study_type or status to narrow.")
    if len(studies) > limit and not cut_off["truncated"]:
        cut_off = {"truncated": True,
                   "truncation_note": (f"Showing the newest {limit} of {len(studies)} found; total and the summary cover "
                                       f"all {len(studies)}. Raise limit (max {MAX_LIMIT}) to see more.")}
    response: dict[str, Any] = {
        "scope": {"type": "account", "id": account} if account and not notes else {"type": "business", "id": business},
        "filters": {k: v for k, v in (("study_type", study_type), ("status", status)) if v},
        "total": len(studies),
        "summary": {"by_type": by_type, "by_status": by_status},
        "studies": studies[:limit],
        **cut_off,
        "rate_limit_usage_pct": api_client.rate_limits.max_usage_pct,
    }
    if notes:
        response["notes"] = notes
    if not studies:
        response["note"] = "No studies found with these filters. Studies Meta keeps on the business may not be visible to this token."
    return response


# ----------------------------------------------------------------------------- one study

def _round(value: Any) -> Any:
    """Numbers as a person reads them: floats rounded, and whole numbers too big to survive a JSON client
    (Meta's ad IDs, for one) as text so no digits are lost."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return str(value) if abs(value) >= BIG_ID else value
    if isinstance(value, float):
        return round(value, 4 if abs(value) < 1 else 2)
    if isinstance(value, list):
        return [_round(v) for v in value]
    return value


def _as_id(value: Any) -> Any:
    """An ID Meta sent as a number, as text: IDs are labels, not quantities, and large ones lose digits as numbers."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return [_as_id(v) for v in value]
    return value


def _annotate_group(metrics: dict, level: Optional[float]) -> None:
    """Say what Meta's own numbers imply, without changing them: a cost per incremental result means nothing when
    nothing incremental was found, and a range that spans zero cannot be told apart from no effect."""
    incremental = metrics.get("incremental")
    cost_keys = [k for k in metrics if k.startswith("CPi")]
    if cost_keys and isinstance(incremental, (int, float)) and incremental <= 0:
        for key in cost_keys:
            metrics[key] = None
        metrics["cost_per_incremental_note"] = "No positive incremental result, so a cost per incremental result is not meaningful."
    low, high = metrics.get("incremental_lower"), metrics.get("incremental_upper")
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (low, high)):
        metrics["range_includes_zero"] = low <= 0 <= high
    confidence = metrics.get("confidence")
    if level is not None and isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        metrics["meets_study_confidence_level"] = confidence >= level


def _lift_row(raw: Any, cell_names: dict[str, str], level: Optional[float]) -> Optional[dict]:
    """One lift result (a JSON string per cell) grouped by the first word of each metric, whether Meta separates
    it with an underscore or a dot: population_test -> population.test, scoreSum.incremental -> scoreSum.incremental.
    Metrics Meta left empty are dropped; a row with only who was in the test (population) and delivery figures
    says `has_results: false`."""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {"raw": str(raw)[:300]}
    if not isinstance(data, dict):
        return None
    top: dict[str, Any] = {}
    groups: dict[str, dict] = {}
    for key, value in data.items():
        if key == "cell_id":
            continue
        found = None if key in LIFT_TOP_LEVEL else _METRIC_KEY.match(key)
        if found:
            groups.setdefault(found.group(1), {})[found.group(2)] = (
                _as_id(value) if _ID_KEY.search(found.group(2)) else _round(value))
        else:
            top[key] = _as_id(value) if _ID_KEY.search(key) else _round(value)
    cell_id = str(data.get("cell_id")) if data.get("cell_id") is not None else None
    kept: dict[str, dict] = {}
    for name, metrics in groups.items():
        metrics = {k: v for k, v in metrics.items() if v is not None}
        if metrics:
            _annotate_group(metrics, level)
            kept[name] = metrics
    row: dict[str, Any] = {"cell_id": cell_id, "cell_name": cell_names.get(cell_id or "")}
    row.update({k: v for k, v in top.items() if v is not None})
    row["has_results"] = any(name not in CONTEXT_GROUPS for name in kept)
    row.update(kept)
    return row


def _hoist_shared(rows: list[dict]) -> dict:
    """A figure Meta repeats unchanged on every row (such as a brand study's top ads, which come as numbered keys
    like topNAdsId1) is moved out once. Identity and delivery keys stay on their rows."""
    if len(rows) < 2:
        return {}
    shared: dict[str, Any] = {}
    for name, value in list(rows[0].items()):
        if name in HOIST_SKIP:
            continue
        if not isinstance(value, dict) and value not in (None, [], "") and all(r.get(name) == value for r in rows[1:]):
            shared[name] = value
            for r in rows:
                r.pop(name, None)
        elif isinstance(value, dict):
            for key, inner in list(value.items()):
                if isinstance(inner, list) and inner and all(isinstance(r.get(name), dict) and r[name].get(key) == inner for r in rows[1:]):
                    shared[f"{name}.{key}"] = inner
                    for r in rows:
                        r[name].pop(key, None)
    return shared


def _objective_results(objective: dict, cell_names: dict[str, str], level: Optional[float]) -> dict:
    out = {k: objective.get(k) for k in ("id", "name", "type", "is_primary", "last_updated_results")}
    try:
        res = api_client.graph_get(f"/{objective['id']}", fields=["results", "last_updated_results"],
                                   params={"breakdowns": json.dumps(["cell_id"])})
    except MetaAPIError as e:
        out["error"] = str(e)
        return out
    out["last_updated_results"] = res.get("last_updated_results") or out["last_updated_results"]
    rows = [r for r in (_lift_row(r, cell_names, level) for r in (res.get("results") or [])) if r]
    out["results"] = rows
    out["has_results"] = any(r.get("has_results") for r in rows)
    if not rows:
        out["note"] = "No results yet. Meta publishes them once the study has run (see results_first_available)."
    elif not out["has_results"]:
        out["note"] = "Meta returned only population and delivery figures for this objective, no outcome values, and does not say why."
    shared = _hoist_shared(rows)
    if shared:
        out["shared"] = shared
    return out


def _cell_accounts(cell_id: Any) -> tuple[dict[str, Optional[str]], list[str]]:
    """The ad accounts behind a cell as {account id: currency Meta gave, or None}: its own ad accounts, else the
    accounts of its campaigns, else of its ad sets. Also returns what Meta refused along the way."""
    problems: list[str] = []
    for label, edge, fields in (("ad accounts", "adaccounts", ["id", "currency"]),
                                ("campaigns", "campaigns", ["id", "account_id"]),
                                ("ad sets", "adsets", ["id", "account_id"])):
        try:
            rows, _, _ = _read(f"/{cell_id}/{edge}", fields, ["id"], {"limit": "25"})
        except MetaAPIError as e:
            problems.append(f"{label}: {e}")
            continue
        accounts: dict[str, Optional[str]] = {}
        for row in rows:
            account = row.get("account_id") or (row.get("id") if edge == "adaccounts" else None)
            if account:
                accounts[ensure_account_id_format(str(account))] = row.get("currency") if edge == "adaccounts" else None
        if accounts:
            return accounts, problems
    return {}, problems


def _study_currency(cells: list[dict], account_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """The currency of a lift study's `spend` (Meta gives none): the ad account the caller named, else the ad
    accounts behind the first cell when they all agree. Returns (currency, why_not)."""
    if account_id:
        currency = get_account_currency(account_id)
        if currency:
            return currency, None
    if not cells:
        return None, "the study lists no cells"
    accounts, problems = _cell_accounts(cells[0]["id"])
    if not accounts:
        return None, ("Meta does not say which ad account the first cell belongs to"
                      + (f" ({'; '.join(problems)})" if problems else ": it lists no ad accounts, campaigns or ad sets"))
    found = {a: given or get_account_currency(a) for a, given in sorted(accounts.items())[:10]}
    unknown = [a for a, c in found.items() if not c]
    if unknown:
        return None, f"could not read the currency of {', '.join(unknown)}"
    currencies = sorted(set(found.values()))
    if len(currencies) > 1:
        return None, f"its ad accounts use different currencies ({', '.join(currencies)})"
    return currencies[0], None


def _cell_entities(cell_id: str) -> tuple[list[dict], list[dict], list[str], list[str]]:
    """The campaigns and ad sets assigned to a cell, read up to ENTITY_PAGE each. Ads and ad accounts have no
    edge usable for this. Returns (campaigns, ad_sets, problems, edges_cut_off)."""
    problems: list[str] = []
    cut: list[str] = []
    found: dict[str, list[dict]] = {"campaigns": [], "ad sets": []}
    # The Graph edge names have no underscore (/adsets), unlike the SDK's method names (get_ad_sets).
    for label, edge, fields in (("campaigns", "campaigns", ["id", "name", "account_id"]),
                                ("ad sets", "adsets", ["id", "name", "campaign_id", "account_id"])):
        try:
            found[label], more, _ = _read(f"/{cell_id}/{edge}", fields, ["id", "name"], {"limit": str(ENTITY_PAGE)})
            if more:
                cut.append(label)
        except MetaAPIError as e:
            problems.append(f"{label}: {e}")
    return found["campaigns"], found["ad sets"], problems, cut


def _entity_insights(entities: list[dict], level: str, account_default: Optional[str], since: str, until: str,
                     errors: dict) -> dict[str, dict]:
    """Totals per entity id over the study's dates, one Insights request per account and chunk of ids."""
    id_field = LEVEL_ID_FIELDS[level]
    by_account: dict[str, list[str]] = {}
    for e in entities:
        account = e.get("account_id") or account_default
        if account and e.get("id"):
            by_account.setdefault(ensure_account_id_format(str(account)), []).append(str(e["id"]))
    totals: dict[str, dict] = {}
    for account, ids in by_account.items():
        for i in range(0, len(ids), INSIGHT_ID_CHUNK):
            chunk = ids[i:i + INSIGHT_ID_CHUNK]
            try:
                res = api_client.graph_get(f"/{account}/insights", fields=INSIGHT_FIELDS + [id_field], params={
                    "level": level, "time_range": json.dumps({"since": since, "until": until}), "limit": "500",
                    "filtering": json.dumps([{"field": f"{level}.id", "operator": "IN", "value": chunk}]),
                })
            except MetaAPIError as e:
                errors[f"{account}:{level}"] = str(e)
                continue
            for row in res.get("data", []):
                day = _parse_day(row) if isinstance(row, dict) else None
                if day and row.get(id_field):
                    totals[str(row[id_field])] = day
    return totals


def _cell_performance(days: list[dict]) -> dict:
    agg = _aggregate(days)
    out = {"spend": agg["spend"], "impressions": agg["impressions"], "clicks": agg["clicks"],
           "ctr": _value("ctr", agg), "cpm": _value("cpm", agg), "cpc": _value("cpc", agg),
           "purchases": agg["purchases"], "cpa": _value("cpa", agg), "revenue": agg["revenue"], "roas": _value("roas", agg),
           "leads": agg["leads"], "cpl": _value("cpl", agg)}
    return {k: _round(v) for k, v in out.items()}


def _compare(cells: list[dict]) -> Optional[dict]:
    """Rank the cells by one efficiency metric when every cell has the results behind it."""
    perf = [(c["name"] or c["id"], c.get("performance")) for c in cells if c.get("performance")]
    if len(perf) < 2:
        return None
    for metric, count in (("cpa", "purchases"), ("cpl", "leads")):
        if all(p[count] and p[metric] is not None for _, p in perf):
            ranked = sorted(perf, key=lambda item: item[1][metric])
            best = ranked[0][1][metric]
            out: dict[str, Any] = {
                "efficiency_metric": metric, "lower_is_better": True,
                "ranking": [{"cell": name, metric: p[metric], "vs_best_pct": round((p[metric] / best - 1) * 100, 1) if best else None,
                             "spend": p["spend"], count: p[count]} for name, p in ranked],
                "note": ("Ranked by raw efficiency only. This does not test statistical significance, and Meta's guide says "
                         "to compare cells only when their size and spend are comparable."),
            }
            spends = [p["spend"] for _, p in perf if p["spend"]]
            if len(spends) > 1 and max(spends) / min(spends) > SPEND_SKEW_RATIO:
                out["caution"] = (f"The highest-spending cell spent over {SPEND_SKEW_RATIO:g}x the lowest, so the cells are "
                                  "not comparable on cost alone; weigh the volume of results too.")
            return out
    return {"efficiency_metric": None,
            "note": "No efficiency metric could be compared: not every cell has purchases or leads in this window."}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_ad_study(
    study_id: str,
    include_results: bool = True,
    account_id: Optional[str] = None,
    max_objectives: int = DEFAULT_OBJECTIVES,
) -> dict:
    """
    One A/B test or conversion lift study: its cells, its objectives and the results. Read-only.

    Lift studies: each objective's results per cell, grouped (population, conversions, buyers...) with
    incremental conversions, their range and Meta's confidence. Split tests: Meta has no results edge, so
    each cell's campaigns and ad sets are read from Insights over the study's dates and the cells are
    ranked by cost per result. The ranking does not test significance; Meta advises comparing cells only
    when their size and spend are comparable. Find studies with `list_ad_studies`.

    Args:
        study_id: The study ID.
        include_results: Read results (lift) or per-cell performance (split test). Default true.
        account_id: Ad account. Split tests: where to read Insights when a cell's campaigns do not say which
            account they belong to. Lift studies: names the currency of `spend`, which Meta does not give.
        max_objectives: Lift objectives whose results are read, primary first (default 25, max 50). A study can
            have 20 or more (one per outcome and channel); the rest are listed without results.
    """
    api_client._ensure_initialized()
    study_id = str(study_id).strip()
    if not study_id.isdigit():
        return {"error": "study_id must be a numeric ID", "blocked_at": "input_validation"}
    if not 1 <= int(max_objectives) <= MAX_OBJECTIVES:
        return {"error": f"max_objectives must be between 1 and {MAX_OBJECTIVES}", "blocked_at": "input_validation"}
    max_objectives = int(max_objectives)
    default_account = ensure_account_id_format(account_id) if account_id else None

    now = _utc_now()
    errors: dict[str, str] = {}
    notes: list[str] = []
    try:
        node, unavailable = _get_node(f"/{study_id}", STUDY_FIELDS, STUDY_FIELDS_BASIC)
    except MetaAPIError as e:
        return {"study_id": study_id, "error": str(e), "error_code": e.error_code, "hint": _hint_for(e.error_code)}
    study = _study(node, now)
    if unavailable:
        notes.append(f"Meta would not return {', '.join(unavailable)} for this study, so they show as empty.")

    cells_raw: list[dict] = []
    try:
        cells_raw, _, _ = _read(f"/{study_id}/cells", CELL_FIELDS, CELL_FIELDS_BASIC, {"limit": str(PAGE_SIZE)})
    except MetaAPIError as e:
        errors["cells"] = str(e)
    objectives_raw: list[dict] = []
    try:
        objectives_raw, _, _ = _read(f"/{study_id}/objectives", OBJECTIVE_FIELDS, OBJECTIVE_FIELDS_BASIC, {"limit": str(PAGE_SIZE)})
    except MetaAPIError as e:
        errors["objectives"] = str(e)

    cells = [{"id": c.get("id"), "name": c.get("name"), "treatment_percentage": c.get("treatment_percentage"),
              "control_percentage": c.get("control_percentage"), "ad_entities_count": c.get("ad_entities_count")}
             for c in cells_raw[:MAX_CELLS]]
    if len(cells_raw) > MAX_CELLS:
        notes.append(f"Showing the first {MAX_CELLS} of {len(cells_raw)} cells.")
    cell_names = {str(c["id"]): c["name"] for c in cells if c["id"]}

    response: dict[str, Any] = {"study": study, "cells": cells}
    if objectives_raw:
        level = study["confidence_level"]
        objectives_raw.sort(key=lambda o: not o.get("is_primary"))  # primary first, order otherwise kept
        read, rest = objectives_raw[:max_objectives], objectives_raw[max_objectives:]
        plain = lambda o: {k: o.get(k) for k in ("id", "name", "type", "is_primary", "last_updated_results")}
        response["objectives"] = ([_objective_results(o, cell_names, level) for o in read] if include_results
                                  else [plain(o) for o in read])
        if rest:
            response["objectives"] += [{**plain(o), "results_read": False} for o in rest]
            notes.append(f"Results were read for the first {max_objectives} of {len(objectives_raw)} objectives (primary "
                         f"first); the other {len(rest)} are listed without results. Raise max_objectives (max {MAX_OBJECTIVES}).")
        if include_results:
            response["results_status"] = "interim" if study["status"] in ("running", "observation", "scheduled") else "final"
            if response["results_status"] == "interim":
                notes.append("The study has not finished, so these results are interim and can still change.")
            currency, why = _study_currency(cells, default_account)
            response["units"] = {
                "spend": currency or f"the currency of the study's ad accounts (not determined: {why}; pass account_id to name it)",
                "confidence": "a fraction between 0 and 1",
            }

    if include_results and _is_split(study["type"]) and cells:
        start, end = _parse_time(study["start"]), _parse_time(study["end"])
        if not start or start > now:
            notes.append("This split test has not started, so there is no performance to read yet.")
        else:
            since = start.date().isoformat()
            until = min(end.date(), now.date()).isoformat() if end else now.date().isoformat()
            response["performance_window"] = {"since": since, "until": until}
            for cell in cells:
                campaigns, ad_sets, problems, cut = _cell_entities(str(cell["id"]))
                if problems:
                    errors[f"cell {cell['id']} entities"] = "; ".join(problems)
                if cut:
                    notes.append(f"Cell '{cell['name']}' has more than {ENTITY_PAGE} {' and '.join(cut)}; performance covers "
                                 f"the first {ENTITY_PAGE} of each.")
                cell["campaigns"] = [{"id": c.get("id"), "name": c.get("name")} for c in campaigns[:ENTITY_EXAMPLES]]
                cell["ad_sets"] = [{"id": a.get("id"), "name": a.get("name")} for a in ad_sets[:ENTITY_EXAMPLES]]
                cell["campaign_count"], cell["ad_set_count"] = len(campaigns), len(ad_sets)
                if not campaigns and not ad_sets:
                    if not problems:
                        notes.append(f"Cell '{cell['name']}' has no campaigns or ad sets Meta will list, so its "
                                     "performance was not read (creative tests assign ads, which have no cell edge).")
                    continue
                days: list[dict] = []
                # Campaigns and ad sets of one cell overlap when an ad set sits under a listed campaign; count campaigns first.
                covered = {str(c.get("id")) for c in campaigns}
                loose = [a for a in ad_sets if str(a.get("campaign_id")) not in covered]
                if any(not (e.get("account_id") or default_account) for e in campaigns + loose):
                    notes.append(f"Cell '{cell['name']}': Meta did not say which ad account its campaigns or ad sets belong to; "
                                 "pass account_id so their performance can be read.")
                before = len(errors)
                days += list(_entity_insights(campaigns, "campaign", default_account, since, until, errors).values())
                days += list(_entity_insights(loose, "adset", default_account, since, until, errors).values())
                if days:
                    cell["performance"] = _cell_performance(days)
                elif len(errors) == before and not any("did not say which ad account" in n and cell["name"] in n for n in notes):
                    notes.append(f"Cell '{cell['name']}' has no spend or delivery in the study's dates, so it has no performance.")
            comparison = _compare(cells)
            if comparison:
                response["performance_comparison"] = comparison
    elif include_results and not objectives_raw and not _is_split(study["type"]):
        notes.append(f"No results are read for study type {study['type']}: only lift studies (objective results) "
                     "and split tests (cell performance) are supported.")

    if notes:
        response["notes"] = notes
    if errors:
        response["errors"] = errors
        response["hint"] = "Meta rejected part of this request (see errors); the rest is valid."
    response["rate_limit_usage_pct"] = api_client.rate_limits.max_usage_pct
    return response
