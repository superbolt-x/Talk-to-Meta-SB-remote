"""
Delivery-blocking errors across an account (read-only).

Lists the campaigns, ad sets and ads Meta has flagged WITH_ISSUES, DISAPPROVED or PENDING_BILLING_INFO,
with the reasons (issues_info) and, for ads, Meta's review feedback. Our per-object detail tools already
show issues_info for one object; this scans the whole account in one call.
"""
import json
import logging
from typing import Any, Optional

from meta_ads_mcp.server import mcp
from mcp.types import ToolAnnotations
from meta_ads_mcp.core.api import api_client, MetaAPIError
from meta_ads_mcp.core.utils import ensure_account_id_format

logger = logging.getLogger("meta-ads-mcp.delivery")

LEVEL_EDGES = {"campaign": "campaigns", "adset": "adsets", "ad": "ads"}
PROBLEM_STATUSES = ("WITH_ISSUES", "DISAPPROVED", "PENDING_BILLING_INFO")
LEVEL_FIELDS = {
    "campaign": ["id", "name", "effective_status", "issues_info"],
    "adset": ["id", "name", "effective_status", "issues_info", "campaign_id"],
    "ad": ["id", "name", "effective_status", "issues_info", "adset_id", "campaign_id", "ad_review_feedback"],
}
PAGE_SIZE = 100
MAX_PAGES = 3
TOP_REASONS = 10


def _normalize_issues(raw: Any) -> list[dict]:
    out = []
    for item in raw or []:
        if isinstance(item, dict):
            out.append({"code": item.get("error_code"), "summary": item.get("error_summary"),
                        "message": item.get("error_message"), "type": item.get("error_type"),
                        "level": item.get("level")})
    return out


def _fetch_level(endpoint: str, level: str, statuses: list[str]) -> tuple[list[dict], bool]:
    """Problem entities at one level, following cursors up to MAX_PAGES. Returns (rows, more_exist)."""
    params = {"limit": str(PAGE_SIZE),
              "filtering": json.dumps([{"field": "effective_status", "operator": "IN", "value": statuses}])}
    fields = LEVEL_FIELDS[level]
    rows: list[dict] = []
    for _ in range(MAX_PAGES):
        try:
            res = api_client.graph_get(endpoint, fields=fields, params=params)
        except MetaAPIError as e:
            if e.error_code == 100 and "ad_review_feedback" in fields:
                fields = [f for f in fields if f != "ad_review_feedback"]  # not offered here: carry on without it
                continue
            raise
        rows.extend(r for r in res.get("data", []) if isinstance(r, dict))
        paging = res.get("paging") or {}
        cursor = (paging.get("cursors") or {}).get("after")
        if not paging.get("next") or not cursor:
            return rows, False
        params["after"] = cursor
    return rows, True


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_delivery_errors(
    account_id: str,
    campaign_id: Optional[str] = None,
    levels: str = "campaign,adset,ad",
    statuses: Optional[str] = None,
) -> dict:
    """
    Campaigns, ad sets and ads that Meta has flagged as not delivering properly, with the reasons:
    effective status WITH_ISSUES, DISAPPROVED or PENDING_BILLING_INFO, the issue details Meta
    attaches (error code, summary, message) and, for ads, its review feedback.

    Scans the whole account in one call (or one campaign's ad sets and ads). Read-only. For one object's
    full detail use get_campaign_details, get_adset_details or get_ad_details.

    Args:
        account_id: Ad account ID (e.g., 'act_123456789').
        campaign_id: Only this campaign, its ad sets and its ads.
        levels: Comma-separated levels to scan: campaign, adset, ad (default all three).
        statuses: Comma-separated effective statuses to treat as a problem (default
            WITH_ISSUES, DISAPPROVED, PENDING_BILLING_INFO).
    """
    api_client._ensure_initialized()
    account_id = ensure_account_id_format(account_id)
    wanted_levels = [l.strip().lower() for l in levels.split(",") if l.strip()]
    wanted_statuses = [s.strip().upper() for s in statuses.split(",") if s.strip()] if statuses else list(PROBLEM_STATUSES)
    problems = []
    bad_levels = [l for l in wanted_levels if l not in LEVEL_EDGES]
    if bad_levels or not wanted_levels:
        problems.append(f"levels must be from: {', '.join(LEVEL_EDGES)}")
    if not wanted_statuses:
        problems.append("statuses must not be empty")
    if problems:
        return {"error": "; ".join(problems), "blocked_at": "input_validation"}

    entities: list[dict] = []
    errors: dict[str, str] = {}
    truncated_levels: list[str] = []
    scanned = []
    for level in LEVEL_EDGES:
        if level not in wanted_levels:
            continue
        scanned.append(level)
        try:
            if level == "campaign" and campaign_id:
                node = api_client.graph_get(f"/{str(campaign_id).strip()}", fields=LEVEL_FIELDS["campaign"])
                rows, more = ([node] if node.get("effective_status") in wanted_statuses else []), False
            else:
                scope = str(campaign_id).strip() if campaign_id else account_id
                rows, more = _fetch_level(f"/{scope}/{LEVEL_EDGES[level]}", level, wanted_statuses)
        except MetaAPIError as e:
            errors[level] = str(e)
            continue
        if more:
            truncated_levels.append(level)
        for row in rows:
            entity: dict[str, Any] = {
                "level": level, "id": row.get("id"), "name": row.get("name"),
                "status": row.get("effective_status"), "issues": _normalize_issues(row.get("issues_info")),
            }
            for parent in ("campaign_id", "adset_id"):
                if row.get(parent):
                    entity[parent] = row[parent]
            if isinstance(row.get("ad_review_feedback"), dict) and row["ad_review_feedback"]:
                entity["review_feedback"] = row["ad_review_feedback"]
            entities.append(entity)

    by_level = {l: sum(1 for e in entities if e["level"] == l) for l in scanned}
    by_status: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for e in entities:
        by_status[e["status"] or "UNKNOWN"] = by_status.get(e["status"] or "UNKNOWN", 0) + 1
        for issue in e["issues"]:
            key = issue["summary"] or issue["message"] or str(issue["code"])
            reasons[key] = reasons.get(key, 0) + 1
    order = {"campaign": 0, "adset": 1, "ad": 2}
    entities.sort(key=lambda e: (order[e["level"]], str(e["name"])))

    response: dict[str, Any] = {
        "account_id": account_id,
        "scope": {"campaign_id": campaign_id} if campaign_id else "account",
        "statuses_checked": wanted_statuses,
        "total": len(entities),
        "summary": {
            "by_level": by_level, "by_status": by_status,
            "top_reasons": [{"reason": k, "count": v} for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])[:TOP_REASONS]],
        },
        "entities": entities,
        "rate_limit_usage_pct": api_client.rate_limits.max_usage_pct,
    }
    if truncated_levels:
        response["truncated"] = True
        response["truncated_levels"] = truncated_levels
        response["truncation_note"] = (f"Stopped at {PAGE_SIZE * MAX_PAGES} entities per level for "
                                       f"{', '.join(truncated_levels)}; more are flagged. Narrow with campaign_id or levels.")
    else:
        response["truncated"] = False
    if not entities and not errors:
        response["note"] = "Nothing is flagged with these statuses."
    if errors:
        response["errors"] = errors
        response["hint"] = "Meta rejected part of this request. A level that errored is not reported as clean."
    return response
