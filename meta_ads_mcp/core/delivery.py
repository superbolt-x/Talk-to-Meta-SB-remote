"""
Delivery-blocking errors across an account (read-only).

Lists the campaigns, ad sets and ads Meta has flagged WITH_ISSUES, DISAPPROVED or PENDING_BILLING_INFO,
with the reasons (issues_info) and, for ads, Meta's review feedback. Our per-object detail tools already
show issues_info for one object; this scans the whole account in one call.

On a long-lived account most flags are legacy (old ad sets with a deleted custom audience), so the answer
leads with the reasons grouped and counted, then the most recently updated entities, and can be limited
to recently updated ones.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from meta_ads_mcp.server import mcp
from mcp.types import ToolAnnotations
from meta_ads_mcp.core.api import api_client, MetaAPIError
from meta_ads_mcp.core.utils import ensure_account_id_format

logger = logging.getLogger("meta-ads-mcp.delivery")

LEVEL_EDGES = {"campaign": "campaigns", "adset": "adsets", "ad": "ads"}
PROBLEM_STATUSES = ("WITH_ISSUES", "DISAPPROVED", "PENDING_BILLING_INFO")
LEVEL_FIELDS = {
    "campaign": ["id", "name", "effective_status", "issues_info", "updated_time"],
    "adset": ["id", "name", "effective_status", "issues_info", "updated_time", "campaign_id"],
    "ad": ["id", "name", "effective_status", "issues_info", "updated_time", "adset_id", "campaign_id", "ad_review_feedback"],
}
PAGE_SIZE = 100
MAX_PAGES = 3
MAX_REASONS = 15
EXAMPLES_PER_REASON = 3
DEFAULT_MAX_ENTITIES = 25
MAX_ENTITIES = 1000


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("+0000", "+00:00").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _normalize_issues(raw: Any) -> list[dict]:
    out = []
    for item in raw or []:
        if isinstance(item, dict):
            out.append({"code": item.get("error_code"), "summary": item.get("error_summary"),
                        "message": item.get("error_message"), "type": item.get("error_type"),
                        "level": item.get("level")})
    return out


def _review_reasons(feedback: Any) -> list[dict]:
    """Meta's review feedback repeats the same policy text under `global` and under each placement, sometimes
    with stray spaces. Fold it into one entry per policy and text, listing the placements it applies to."""
    if not isinstance(feedback, dict):
        return []
    found: dict[tuple, list[str]] = {}

    def add(policy: Any, text: Any, placement: str) -> None:
        key = (str(policy).strip(), " ".join(str(text or "").split()))
        places = found.setdefault(key, [])
        if placement not in places:
            places.append(placement)

    if isinstance(feedback.get("global"), dict):
        for policy, text in feedback["global"].items():
            add(policy, text, "global")
    if isinstance(feedback.get("placement_specific"), dict):
        for placement, entries in feedback["placement_specific"].items():
            if isinstance(entries, dict):
                for policy, text in entries.items():
                    add(policy, text, str(placement))
    return [{"policy": k[0], "text": k[1], "placements": v} for k, v in found.items()]


def _fetch_level(endpoint: str, level: str, statuses: list[str], since_ts: Optional[int]) -> tuple[list[dict], bool, list[str]]:
    """Problem entities at one level, following cursors up to MAX_PAGES.

    Returns (rows, more_exist, dropped) where `dropped` lists optional request parts Meta rejected and that
    were left out: the server-side recency filter, the ad review feedback, the updated_time field.
    """
    flt = [{"field": "effective_status", "operator": "IN", "value": statuses}]
    if since_ts:
        flt.append({"field": "updated_time", "operator": "GREATER_THAN", "value": since_ts})
    params = {"limit": str(PAGE_SIZE), "filtering": json.dumps(flt)}
    fields = list(LEVEL_FIELDS[level])
    dropped: list[str] = []
    rows: list[dict] = []
    pages = 0
    while pages < MAX_PAGES:
        try:
            res = api_client.graph_get(endpoint, fields=fields, params=params)
        except MetaAPIError as e:
            if e.error_code != 100:
                raise
            # Something optional was rejected: leave out one piece at a time, most dispensable first.
            if since_ts and "recency_filter" not in dropped:
                flt = flt[:1]
                params["filtering"] = json.dumps(flt)
                dropped.append("recency_filter")
            elif "ad_review_feedback" in fields:
                fields.remove("ad_review_feedback")
                dropped.append("ad_review_feedback")
            elif "updated_time" in fields:
                fields.remove("updated_time")
                dropped.append("updated_time")
            else:
                raise
            continue
        pages += 1
        rows.extend(r for r in res.get("data", []) if isinstance(r, dict))
        paging = res.get("paging") or {}
        cursor = (paging.get("cursors") or {}).get("after")
        if not paging.get("next") or not cursor:
            return rows, False, dropped
        params["after"] = cursor
    return rows, True, dropped


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_delivery_errors(
    account_id: str,
    campaign_id: Optional[str] = None,
    levels: str = "campaign,adset,ad",
    statuses: Optional[str] = None,
    recent_days: Optional[int] = None,
    max_entities: int = DEFAULT_MAX_ENTITIES,
) -> dict:
    """
    Campaigns, ad sets and ads that Meta has flagged as not delivering properly: effective status
    WITH_ISSUES, DISAPPROVED or PENDING_BILLING_INFO, with the reasons Meta attaches (error code,
    summary, message) and, for ads, its review feedback folded into one entry per policy.

    Answers lead with `reasons`: each distinct reason, how many entities it affects, and a few examples.
    On an old account most flags are legacy (e.g. 2022 ad sets with a deleted custom audience), so use
    `recent_days` to look only at entities updated lately. Read-only. For one object's full detail use
    get_campaign_details, get_adset_details or get_ad_details.

    Args:
        account_id: Ad account ID (e.g., 'act_123456789').
        campaign_id: Only this campaign, its ad sets and its ads.
        levels: Comma-separated levels to scan: campaign, adset, ad (default all three).
        statuses: Comma-separated effective statuses to treat as a problem (default
            WITH_ISSUES, DISAPPROVED, PENDING_BILLING_INFO).
        recent_days: Only entities updated in the last N days (1-365). Default: all flagged entities.
        max_entities: Entities listed per level, most recently updated first (default 25, max 1000, 0 for
            none). The summary and reasons always cover everything read.
    """
    api_client._ensure_initialized()
    account_id = ensure_account_id_format(account_id)
    wanted_levels = [l.strip().lower() for l in levels.split(",") if l.strip()]
    wanted_statuses = [s.strip().upper() for s in statuses.split(",") if s.strip()] if statuses else list(PROBLEM_STATUSES)
    problems = []
    if [l for l in wanted_levels if l not in LEVEL_EDGES] or not wanted_levels:
        problems.append(f"levels must be from: {', '.join(LEVEL_EDGES)}")
    if not wanted_statuses:
        problems.append("statuses must not be empty")
    if recent_days is not None and not 1 <= int(recent_days) <= 365:
        problems.append("recent_days must be between 1 and 365")
    if not 0 <= int(max_entities) <= MAX_ENTITIES:
        problems.append(f"max_entities must be between 0 and {MAX_ENTITIES}")
    if problems:
        return {"error": "; ".join(problems), "blocked_at": "input_validation"}
    max_entities = int(max_entities)

    cutoff = since_ts = None
    if recent_days is not None:
        cutoff = _utc_now() - timedelta(days=int(recent_days))
        since_ts = int(cutoff.timestamp())

    entities: list[dict] = []
    errors: dict[str, str] = {}
    notes: list[str] = []
    truncated_levels: list[str] = []
    scanned = []
    for level in LEVEL_EDGES:
        if level not in wanted_levels:
            continue
        scanned.append(level)
        try:
            if level == "campaign" and campaign_id:
                node = api_client.graph_get(f"/{str(campaign_id).strip()}", fields=LEVEL_FIELDS["campaign"])
                rows, more, dropped = ([node] if node.get("effective_status") in wanted_statuses else []), False, []
            else:
                scope = str(campaign_id).strip() if campaign_id else account_id
                rows, more, dropped = _fetch_level(f"/{scope}/{LEVEL_EDGES[level]}", level, wanted_statuses, since_ts)
        except MetaAPIError as e:
            errors[level] = str(e)
            continue
        if more:
            truncated_levels.append(level)
        if "recency_filter" in dropped:
            notes.append(f"Meta would not filter {level} by update time, so recent_days was applied here; "
                         "very old flagged entities may have used up the read limit.")
        if "updated_time" in dropped and recent_days is not None:
            notes.append(f"recent_days could not be applied to {level}: Meta did not return update times.")
        for row in rows:
            updated = _parse_time(row.get("updated_time"))
            if cutoff and updated and updated < cutoff:
                continue
            entity: dict[str, Any] = {
                "level": level, "id": row.get("id"), "name": row.get("name"), "status": row.get("effective_status"),
                "updated": row.get("updated_time"), "issues": _normalize_issues(row.get("issues_info")),
            }
            for parent in ("campaign_id", "adset_id"):
                if row.get(parent):
                    entity[parent] = row[parent]
            reasons = _review_reasons(row.get("ad_review_feedback"))
            if reasons:
                entity["review_reasons"] = reasons
            entities.append(entity)

    # --- reasons, counted per entity (an entity with the same reason twice counts once) ---
    reason_stats: dict[str, dict] = {}
    for e in entities:
        keys = {i["summary"] or i["message"] or str(i["code"]) for i in e["issues"]}
        keys |= {f"Review: {r['policy']}" for r in e.get("review_reasons", [])}
        for key in keys:
            stat = reason_stats.setdefault(key, {"reason": key, "count": 0, "levels": {}, "examples": []})
            stat["count"] += 1
            stat["levels"][e["level"]] = stat["levels"].get(e["level"], 0) + 1
            if len(stat["examples"]) < EXAMPLES_PER_REASON:
                stat["examples"].append({"level": e["level"], "id": e["id"], "name": e["name"]})
    reasons_out = sorted(reason_stats.values(), key=lambda r: -r["count"])[:MAX_REASONS]

    by_level = {l: sum(1 for e in entities if e["level"] == l) for l in scanned}
    by_status: dict[str, int] = {}
    for e in entities:
        by_status[e["status"] or "UNKNOWN"] = by_status.get(e["status"] or "UNKNOWN", 0) + 1

    # --- the list: per level, most recently updated first ---
    order = {"campaign": 0, "adset": 1, "ad": 2}
    far_past = datetime.min.replace(tzinfo=timezone.utc)
    shown: list[dict] = []
    for level in sorted(scanned, key=order.get):
        at_level = sorted((e for e in entities if e["level"] == level),
                          key=lambda e: _parse_time(e["updated"]) or far_past, reverse=True)
        shown.extend(at_level[:max_entities])

    response: dict[str, Any] = {
        "account_id": account_id,
        "scope": {"type": "campaign", "id": str(campaign_id).strip()} if campaign_id else {"type": "account", "id": account_id},
        "statuses_checked": wanted_statuses,
        "recent_days": recent_days,
        "total": len(entities),
        "summary": {"by_level": by_level, "by_status": by_status},
        "reasons": reasons_out,
        "entities_shown": len(shown),
        "entities": shown,
        "rate_limit_usage_pct": api_client.rate_limits.max_usage_pct,
    }
    if len(shown) < len(entities):
        response["entities_note"] = (f"Showing the {max_entities} most recently updated per level of {len(entities)}. "
                                     "The summary and reasons cover all of them; raise max_entities or use recent_days.")
    if truncated_levels:
        response["truncated"] = True
        response["truncated_levels"] = truncated_levels
        response["truncation_note"] = (f"Stopped at {PAGE_SIZE * MAX_PAGES} entities per level for "
                                       f"{', '.join(truncated_levels)}; more are flagged. Narrow with recent_days, campaign_id or levels.")
    else:
        response["truncated"] = False
    if notes:
        response["notes"] = notes
    if not entities and not errors:
        response["note"] = "Nothing is flagged with these statuses" + (f" in the last {recent_days} days." if recent_days else ".")
    if errors:
        response["errors"] = errors
        response["hint"] = "Meta rejected part of this request. A level that errored is not reported as clean."
    return response
