"""
Account change history (read-only): who changed what, and when.

Wraps /act_<id>/activities, the same record Ads Manager shows on its campaign history page. It answers
"what changed just before performance moved?": budget, status, bid, audience and creative edits, with the
person or app that made them.

Docs: https://developers.facebook.com/docs/marketing-api/reference/ad-activity/
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from meta_ads_mcp.server import mcp
from mcp.types import ToolAnnotations
from meta_ads_mcp.core.api import api_client, MetaAPIError
from meta_ads_mcp.core.utils import ensure_account_id_format, truncation_fields

logger = logging.getLogger("meta-ads-mcp.activity")

ACTIVITY_FIELDS = [
    "actor_id", "actor_name", "application_name", "date_time_in_timezone", "event_time",
    "event_type", "extra_data", "object_id", "object_name", "object_type", "translated_event_type",
]
ACTIVITY_FIELDS_BASIC = [
    "actor_name", "event_time", "event_type", "extra_data", "object_id", "object_name",
    "object_type", "translated_event_type",
]
# Meta's documented event categories for the `category` filter.
CATEGORIES = ("ACCOUNT", "AD", "AD_KEYWORDS", "AD_SET", "AUDIENCE", "BID", "BUDGET", "CAMPAIGN", "DATE", "STATUS", "TARGETING")
MAX_DAYS = 90
MAX_EVENTS = 500
PAGE_SIZE = 100
MAX_RAW_PAGES = 10        # pages read when client-side filters are in use (object_id, event_type)
MAX_DETAIL_CHARS = 200
TOP_N_SUMMARY = 10


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_extra(raw: Any) -> Optional[dict]:
    """`extra_data` is a JSON-encoded string whose shape depends on the event; return it as a dict, trimmed."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {"raw": str(raw)[:MAX_DETAIL_CHARS]}
    if not isinstance(parsed, dict):
        return {"value": parsed}
    return {k: (v[:MAX_DETAIL_CHARS] if isinstance(v, str) else v) for k, v in parsed.items()}


def _normalize_event(raw: dict) -> dict:
    details = _parse_extra(raw.get("extra_data"))
    change = None
    if details and ("old_value" in details or "new_value" in details):
        change = {"from": details.get("old_value"), "to": details.get("new_value")}
    event_type = raw.get("event_type")
    return {
        "time": raw.get("event_time"),
        "local_time": raw.get("date_time_in_timezone"),
        "actor": {"id": raw.get("actor_id"), "name": raw.get("actor_name")},
        "via": raw.get("application_name"),
        "event_type": event_type,
        "what": raw.get("translated_event_type") or (str(event_type).replace("_", " ").capitalize() if event_type else None),
        "object": {"type": raw.get("object_type"), "id": raw.get("object_id"), "name": raw.get("object_name")},
        "change": change,
        "details": details,
    }


def _top(counter: dict, n: int = TOP_N_SUMMARY) -> list[dict]:
    return [{"name": k, "count": v} for k, v in sorted(counter.items(), key=lambda kv: -kv[1])[:n]]


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_activity_log(
    account_id: str,
    days: int = 7,
    category: Optional[str] = None,
    user_id: Optional[str] = None,
    object_id: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = 100,
) -> dict:
    """
    Change history for an ad account: who changed what, and when (budget, status, bid, audience,
    creative and targeting edits, ad review results, billing events). The same record as the campaign
    history page in Ads Manager. Use it to find what changed just before performance moved.

    Newest first. Read-only. Budget amounts in `change` / `details` are as Meta returns them, normally
    in the currency's smallest unit (cents). How far back Meta keeps history is not documented, so a long
    window may simply return less than asked.

    Args:
        account_id: Ad account ID (e.g., 'act_123456789').
        days: How many days back to look, 1-90 (default 7).
        category: Only this category: ACCOUNT, AD, AD_KEYWORDS, AD_SET, AUDIENCE, BID, BUDGET, CAMPAIGN,
            DATE, STATUS or TARGETING.
        user_id: Only changes made by this Facebook user ID.
        object_id: Only events whose object is this campaign / ad set / ad / audience ID.
        event_type: Only event types containing this text (e.g. 'budget', 'run_status', 'review').
        limit: Maximum events returned, 1-500 (default 100).
    """
    api_client._ensure_initialized()
    account_id = ensure_account_id_format(account_id)
    problems = []
    if not 1 <= int(days) <= MAX_DAYS:
        problems.append(f"days must be between 1 and {MAX_DAYS}")
    cat = category.strip().upper() if category else None
    if cat and cat not in CATEGORIES:
        problems.append(f"category must be one of {', '.join(CATEGORIES)}")
    if not 1 <= int(limit) <= MAX_EVENTS:
        problems.append(f"limit must be between 1 and {MAX_EVENTS}")
    if problems:
        return {"error": "; ".join(problems), "blocked_at": "input_validation"}
    days, limit = int(days), int(limit)

    now = _utc_now()
    since = now - timedelta(days=days)
    params: dict[str, str] = {"since": str(int(since.timestamp())), "until": str(int(now.timestamp())),
                              "limit": str(PAGE_SIZE)}
    if cat:
        params["category"] = cat
    if user_id:
        params["uid"] = str(user_id).strip()

    wanted_type = event_type.strip().lower() if event_type else None
    client_filtered = bool(object_id or wanted_type)
    max_pages = MAX_RAW_PAGES if client_filtered else -(-limit // PAGE_SIZE)

    events: list[dict] = []
    more = False
    fields = ACTIVITY_FIELDS
    pages = 0
    try:
        while True:
            try:
                res = api_client.graph_get(f"/{account_id}/activities", fields=fields, params=params)
            except MetaAPIError as e:
                if e.error_code == 100 and fields is ACTIVITY_FIELDS:
                    fields = ACTIVITY_FIELDS_BASIC
                    continue
                raise
            pages += 1
            for raw in (r for r in res.get("data", []) if isinstance(r, dict)):
                if object_id and str(raw.get("object_id")) != str(object_id).strip():
                    continue
                if wanted_type and wanted_type not in (str(raw.get("event_type", "")) + " " + str(raw.get("translated_event_type", ""))).lower():
                    continue
                events.append(_normalize_event(raw))
            paging = res.get("paging") or {}
            cursor = (paging.get("cursors") or {}).get("after")
            has_next = bool(paging.get("next") and cursor)
            if len(events) >= limit:
                more = has_next or len(events) > limit
                break
            if not has_next:
                break
            if pages >= max_pages:
                more = True
                break
            params["after"] = cursor
    except MetaAPIError as e:
        return {"account_id": account_id, "error": str(e), "error_code": e.error_code,
                "hint": "Reading the activity log needs ads_read and access to this ad account."}

    events = events[:limit]
    by_type: dict[str, int] = {}
    by_actor: dict[str, int] = {}
    for ev in events:
        by_type[ev["what"] or "Unknown"] = by_type.get(ev["what"] or "Unknown", 0) + 1
        actor = ev["actor"]["name"] or ev["actor"]["id"] or "Unknown"
        by_actor[actor] = by_actor.get(actor, 0) + 1

    response: dict[str, Any] = {
        "account_id": account_id,
        "window": {"since": since.isoformat(), "until": now.isoformat(), "days": days},
        "filters": {k: v for k, v in (("category", cat), ("user_id", user_id), ("object_id", object_id),
                                      ("event_type", event_type)) if v},
        "total": len(events),
        "summary": {"by_type": _top(by_type), "by_actor": _top(by_actor),
                    "newest": events[0]["time"] if events else None,
                    "oldest": events[-1]["time"] if events else None},
        **truncation_fields({"next": "more"} if more else None, len(events),
                            "Raise limit (max 500), shorten days, or narrow with category, object_id or user_id."),
        "events": events,
        "rate_limit_usage_pct": api_client.rate_limits.max_usage_pct,
    }
    if not events:
        response["note"] = ("No changes found in this window. Meta may keep less history than requested, "
                            "or the filters excluded everything.")
    return response
