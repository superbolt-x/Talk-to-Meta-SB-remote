"""
Account change history (read-only): who changed what, and when.

Wraps /act_<id>/activities, the same record Ads Manager shows on its campaign history page. It answers
"what changed just before performance moved?": budget, status, bid, audience and creative edits, with the
person or app that made them.

Meta's raw events are uneven (budgets as nested objects in cents, billing as a bare number, three events
for one pause, legacy object names), so each event is normalized: money in major units with a currency,
a readable object kind, and one event per status change.

Docs: https://developers.facebook.com/docs/marketing-api/reference/ad-activity/
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from meta_ads_mcp.server import mcp
from mcp.types import ToolAnnotations
from meta_ads_mcp.core.api import api_client, MetaAPIError
from meta_ads_mcp.core.utils import ensure_account_id_format, get_account_currency, truncation_fields

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
CHUNK_DAYS = 30            # long windows are read in chunks: Meta fails one big query on a busy account
MAX_EVENTS = 500
PAGE_SIZE = 100
MAX_RAW_PAGES = 10         # pages read per window when a client-side filter is in use
MAX_DETAIL_CHARS = 200
TOP_N_SUMMARY = 10
MERGE_WITHIN_SECONDS = 120  # events for one status change are recorded within moments of each other

BILLING_TYPES = ("ad_account_billing_charge", "ad_account_billing_decline", "ad_account_billing_refund")
RAW_TYPE_KIND = {"ADGROUP": "ad", "CAMPAIGN_GROUP": "campaign", "ACCOUNT": "account", "AD_ACCOUNT": "account"}


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


def _object_kind(event_type: Any, raw_type: Any) -> Optional[str]:
    """Meta's object types are legacy names (ads are ADGROUP, campaigns CAMPAIGN_GROUP, and ad sets and
    audiences are both CAMPAIGN), so the kind is read from the event type first."""
    et = str(event_type or "")
    if "audience" in et:
        return "audience"
    if "ad_set" in et:
        return "ad set"
    if et.startswith("ad_account") or et in ("add_images", "edit_images", "delete_images", "add_funding_source",
                                              "remove_funding_source") or "agency_fee" in et:
        return "account"
    if "campaign" in et:
        return "campaign"
    if et in ("create_ad", "update_ad_creative") or et.startswith("update_ad_") or et.startswith("ad_review"):
        return "ad"
    raw = str(raw_type or "").upper()
    if raw in RAW_TYPE_KIND:
        return RAW_TYPE_KIND[raw]
    return "ad set" if raw == "CAMPAIGN" else (raw.lower() or None)


def _unwrap(value: Any, key: str) -> tuple[Any, Optional[str]]:
    """Meta sometimes wraps a value as {"old_value": 123, "type": "Lifetime budget"}; return (123, "Lifetime budget")."""
    if isinstance(value, dict):
        return value.get(key, value.get("value")), value.get("type")
    return value, None


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _epoch_to_iso(details: dict) -> dict:
    """Raw epoch seconds under time-like keys (e.g. last_learning_exit) become ISO timestamps."""
    out = {}
    for k, v in details.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool) and 1e9 <= v <= 4e9 and any(w in k for w in ("time", "exit", "date")):
            out[k] = datetime.fromtimestamp(v, tz=timezone.utc).isoformat()
        else:
            out[k] = v
    return out


def _normalize_event(raw: dict, currency: Optional[str]) -> dict:
    details = _parse_extra(raw.get("extra_data"))
    event_type = raw.get("event_type")
    kind = _object_kind(event_type, raw.get("object_type"))
    change: Optional[dict] = None
    amount: Optional[dict] = None

    if details:
        old, old_kind = _unwrap(details.get("old_value"), "old_value")
        new, new_kind = _unwrap(details.get("new_value"), "new_value")
        has_change = "old_value" in details or "new_value" in details
        if event_type in BILLING_TYPES and _number(new) is not None:
            amount = {"value": round(_number(new) / 100, 2), "currency": currency}
            details = {k: v for k, v in details.items() if k not in ("old_value", "new_value")}
        elif has_change and "budget" in str(event_type) and _number(old) is not None and _number(new) is not None:
            change = {"from": round(_number(old) / 100, 2), "to": round(_number(new) / 100, 2), "currency": currency,
                      "kind": old_kind or new_kind}
            if _number(old):
                change["change_pct"] = round((_number(new) - _number(old)) / abs(_number(old)) * 100, 1)
            details = {k: v for k, v in details.items() if k not in ("old_value", "new_value")}
        elif has_change:
            if old is not None or new is not None:  # audience events carry null on both sides: no change to report
                change = {"from": old, "to": new}
            details = {k: v for k, v in details.items() if k not in ("old_value", "new_value")}
        if details and kind == "ad" and isinstance(details.get("campaign_id"), (str, int)):
            details = {("adset_id" if k == "campaign_id" else k): v for k, v in details.items()}  # Meta's key is wrong
        if details:
            details = _epoch_to_iso(details)

    return {
        "time": raw.get("event_time"),
        "local_time": raw.get("date_time_in_timezone"),
        "actor": {"id": raw.get("actor_id"), "name": raw.get("actor_name")},
        "via": raw.get("application_name"),
        "event_type": event_type,
        "what": raw.get("translated_event_type") or (str(event_type).replace("_", " ").capitalize() if event_type else None),
        "object": {"kind": kind, "id": raw.get("object_id"), "name": raw.get("object_name"), "raw_type": raw.get("object_type")},
        "change": change,
        "amount": amount,
        "details": details or None,
    }


def _is_pending(value: Any) -> bool:
    return "pending" in str(value or "").lower()


def _merge_status_changes(events: list[dict]) -> list[dict]:
    """One pause or activation is recorded as several events (Active -> Pending Process, a note, Pending
    Process -> Inactive). Fold each such burst into one event from the first real state to the last."""
    out: list[dict] = []
    i = 0
    while i < len(events):
        ev = events[i]
        if not str(ev["event_type"] or "").endswith("run_status") or not _parse_time(ev["time"]):
            out.append(ev)
            i += 1
            continue
        group = [ev]
        j = i + 1
        while (j < len(events) and events[j]["event_type"] == ev["event_type"]
               and events[j]["object"]["id"] == ev["object"]["id"] and _parse_time(events[j]["time"])
               and abs((_parse_time(ev["time"]) - _parse_time(events[j]["time"])).total_seconds()) <= MERGE_WITHIN_SECONDS):
            group.append(events[j])
            j += 1
        chrono = list(reversed(group))  # oldest first
        starts = [e["change"]["from"] for e in chrono if e["change"] and e["change"].get("from") and not _is_pending(e["change"]["from"])]
        ends = [e["change"]["to"] for e in chrono if e["change"] and e["change"].get("to") and not _is_pending(e["change"]["to"])]
        if len(group) > 1 and (starts or ends) and any(
                e["change"] and (_is_pending(e["change"].get("from")) or _is_pending(e["change"].get("to"))) for e in group):
            merged = dict(group[0])
            merged["change"] = {"from": starts[0] if starts else None, "to": ends[-1] if ends else None}
            merged["merged_events"] = len(group)
            out.append(merged)
        else:
            out.extend(group)
        i = j
    return out


def _top(counter: dict, n: int = TOP_N_SUMMARY) -> list[dict]:
    return [{"name": k, "count": v} for k, v in sorted(counter.items(), key=lambda kv: -kv[1])[:n]]


def _hint_for(code: Optional[int]) -> str:
    if code in (1, 2):
        return ("Meta's activity service failed (error 1 or 2: an unknown or temporary error). It usually means the "
                "window is too large for a busy account. Retry with fewer days, or narrow with category or user_id.")
    if code in (10, 190, 200, 102):
        return "Reading the activity log needs ads_read and access to this ad account, and a valid token."
    return "Meta rejected the request."


def _read_pages(endpoint: str, params: dict, max_pages: int, state: dict) -> tuple[list[dict], bool]:
    """Raw activity rows for one window, following cursors up to max_pages. Returns (rows, more_exist)."""
    params = dict(params)
    rows: list[dict] = []
    for _ in range(max_pages):
        while True:
            try:
                res = api_client.graph_get(endpoint, fields=state["fields"], params=params)
                break
            except MetaAPIError as e:
                if e.error_code == 100 and state["fields"] is ACTIVITY_FIELDS:
                    state["fields"] = ACTIVITY_FIELDS_BASIC
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

    Newest first. Read-only. Budget and billing amounts are converted to major units (e.g. dollars) in
    the account's currency, assuming 2 decimals. A pause or activation, which Meta records as several
    events, is shown as one. Windows over 30 days are read in 30-day chunks; if Meta fails on an older
    chunk the newer events are still returned, with a note saying how far back it could read.

    Args:
        account_id: Ad account ID (e.g., 'act_123456789').
        days: How many days back to look, 1-90 (default 7).
        category: Only this category: ACCOUNT, AD, AD_KEYWORDS, AD_SET, AUDIENCE, BID, BUDGET, CAMPAIGN,
            DATE, STATUS or TARGETING.
        user_id: Only changes made by this Facebook user ID.
        object_id: Events for this campaign / ad set / ad. Uses Meta's per-object history when possible
            (exact); otherwise scans the account's history and keeps events whose object is this ID.
        event_type: Only event types containing this text (e.g. 'budget', 'run_status', 'review').
        limit: Maximum events returned, 1-500 (default 100). The response says how far back they reach.
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
    base_params: dict[str, str] = {"limit": str(PAGE_SIZE)}
    if cat:
        base_params["category"] = cat
    if user_id:
        base_params["uid"] = str(user_id).strip()
    wanted_type = event_type.strip().lower() if event_type else None
    object_id = str(object_id).strip() if object_id else None

    # Meta's per-object history is exact and cheap. It takes no category / user filter, so those use the account scan.
    use_object_edge = bool(object_id and not cat and not user_id)
    client_filter = bool(wanted_type or (object_id and not use_object_edge))
    max_pages = MAX_RAW_PAGES if client_filter else -(-limit // PAGE_SIZE)
    currency = get_account_currency(account_id)

    windows = []
    end = now
    while end > since:
        start = max(since, end - timedelta(days=CHUNK_DAYS))
        windows.append((start, end))
        end = start

    state = {"fields": ACTIVITY_FIELDS}
    notes: list[str] = []
    events: list[dict] = []
    more = False
    oldest_read: Optional[datetime] = None
    for idx, (start, end) in enumerate(windows):
        params = {**base_params, "since": str(int(start.timestamp())), "until": str(int(end.timestamp()))}
        endpoint = f"/{object_id}/activities" if use_object_edge else f"/{account_id}/activities"
        try:
            try:
                rows, window_more = _read_pages(endpoint, params, max_pages, state)
            except MetaAPIError as e:
                if not (use_object_edge and e.error_code == 100):
                    raise
                # No per-object history for this kind of ID: scan the account and keep this object's events.
                use_object_edge, client_filter, max_pages = False, True, MAX_RAW_PAGES
                notes.append("Meta's per-object history was not available for this ID, so the account's history "
                             "was scanned and filtered here; very busy accounts may show only part of it.")
                rows, window_more = _read_pages(f"/{account_id}/activities", params, max_pages, state)
        except MetaAPIError as e:
            if oldest_read is None:
                return {"account_id": account_id, "error": str(e), "error_code": e.error_code, "hint": _hint_for(e.error_code)}
            notes.append(f"History before {end.date().isoformat()} could not be read ({e}); newer events are shown.")
            break
        oldest_read = start
        for raw in rows:
            if object_id and not use_object_edge and str(raw.get("object_id")) != object_id:
                continue
            if wanted_type and wanted_type not in (str(raw.get("event_type", "")) + " " + str(raw.get("translated_event_type", ""))).lower():
                continue
            events.append(_normalize_event(raw, currency))
        events = _merge_status_changes(events)
        if len(events) >= limit:
            more = window_more or len(events) > limit or idx < len(windows) - 1
            break
        if window_more:
            more = True
    events = events[:limit]

    by_type: dict[str, int] = {}
    by_actor: dict[str, int] = {}
    for ev in events:
        by_type[ev["what"] or "Unknown"] = by_type.get(ev["what"] or "Unknown", 0) + 1
        actor = ev["actor"]["name"] or ev["actor"]["id"] or "Unknown"
        by_actor[actor] = by_actor.get(actor, 0) + 1

    reaches_back_to = events[-1]["time"] if (more and events) else (oldest_read or since).isoformat()
    response: dict[str, Any] = {
        "account_id": account_id,
        "window": {"since": since.isoformat(), "until": now.isoformat(), "days": days,
                   "events_reach_back_to": reaches_back_to},
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
    if notes:
        response["notes"] = notes
    if not events:
        response["note"] = ("No changes found in this window. Meta may keep less history than requested, "
                            "or the filters excluded everything.")
    return response
