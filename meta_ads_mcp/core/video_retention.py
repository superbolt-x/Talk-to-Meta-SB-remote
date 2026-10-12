"""
Video retention and hook rate per ad (read-only).

get_insights returns only the 3-second plays (`video_view`), so retention at 25/50/75/95/100% has no
route through the other insights tools. This one asks the Insights edge for the video fields at
level=ad, merges ads that share a name, and computes the rates with both denominators named, because
"retention" is quoted against either 3-second plays or all plays depending on who made the chart.

Video metrics are counted per impression, so attribution windows do not apply. Ads with no video
(static, GIF) come back with nulls, not zeros: a zero would read as "nobody watched".
"""
import json
import logging
from typing import Any, Optional

from meta_ads_mcp.server import mcp
from mcp.types import ToolAnnotations
from meta_ads_mcp.core.api import api_client, MetaAPIError
from meta_ads_mcp.core.insights import META_DATE_PRESETS, _extract_action_value
from meta_ads_mcp.core.utils import parse_date_range

logger = logging.getLogger("meta-ads-mcp.video_retention")

PAGE_SIZE = 200
MAX_PAGES = 25  # 5,000 ad rows

# output key -> Insights field holding it as [{"action_type": "video_view", "value": "123"}]
VIDEO_COUNT_FIELDS = {
    "plays": "video_play_actions",
    "p25": "video_p25_watched_actions",
    "p50": "video_p50_watched_actions",
    "p75": "video_p75_watched_actions",
    "p95": "video_p95_watched_actions",
    "p100": "video_p100_watched_actions",
    "thruplay": "video_thruplay_watched_actions",
}
AVG_WATCH_FIELD = "video_avg_time_watched_actions"
# The 3-second plays are not a field of their own: they are the `video_view` entry of `actions`.
PLAYS_3S_ACTION = "video_view"

INSIGHT_FIELDS = (
    ["spend", "impressions", "actions", "ad_id", "ad_name", "adset_id", "adset_name",
     "campaign_id", "campaign_name"]
    + list(VIDEO_COUNT_FIELDS.values()) + [AVG_WATCH_FIELD]
)

COMPLETION_POINTS = ("p25", "p50", "p75", "p95", "p100")
GROUP_BY = ("ad_name", "ad_id")

# The order every video ad's counts should be in. 3s >= p25 only holds for videos of 12 seconds or
# more (25% of a 6-second video is 1.5 seconds), so a flag there on a short video is expected.
ORDER = ("plays", "plays_3s") + COMPLETION_POINTS

UNITS = {
    "spend": "account currency", "impressions": "count",
    "plays": "count, all video plays", "plays_3s": "count, plays of 3 seconds or more (video_view)",
    "thruplay": "count, plays to 15 seconds or the end, whichever is shorter",
    "avg_watch_time_s": "seconds per play",
    "rates": "percent (0-100)",
}
DEFINITIONS = {
    "hook_rate": "plays_3s / impressions",
    "retention_pX_vs_3s": "pX / plays_3s (viewers who passed 3 seconds, how many reached X% of the video)",
    "retention_pX_vs_plays": "pX / plays (every play counts, including those that stopped before 3 seconds)",
    "hold_rate": "thruplay / plays_3s",
}


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _count(items: Optional[list]) -> Optional[float]:
    """The value of a video_*_actions field, or None when Meta returned it empty."""
    if not items:
        return None
    for item in items:
        if isinstance(item, dict) and item.get("action_type") == PLAYS_3S_ACTION:
            return _num(item.get("value"))
    first = items[0]
    return _num(first.get("value")) if isinstance(first, dict) else None


def _time_params(time_range: str) -> Optional[dict]:
    """date_preset or time_range for an Insights call, the same way get_insights reads time_range."""
    if "," not in time_range and time_range in META_DATE_PRESETS:
        return {"date_preset": time_range}
    try:
        start, end = parse_date_range(time_range)
    except ValueError:
        return None
    return {"time_range": json.dumps({"since": start, "until": end})}


def _fetch_rows(object_id: str, params: dict) -> tuple[list[dict], bool]:
    """All ad-level insight rows, following cursors up to MAX_PAGES. Returns (rows, more_exist)."""
    rows: list[dict] = []
    params = dict(params)
    for _ in range(MAX_PAGES):
        res = api_client.graph_get(f"/{object_id}/insights", fields=INSIGHT_FIELDS, params=params)
        rows.extend(r for r in res.get("data", []) if isinstance(r, dict))
        paging = res.get("paging") or {}
        cursor = (paging.get("cursors") or {}).get("after")
        if not paging.get("next") or not cursor:
            return rows, False
        params["after"] = cursor
    return rows, True


def _group_key(row: dict, group_by: str) -> tuple:
    # One ad can carry two ad_ids, so an ad name is only unique within a campaign.
    if group_by == "ad_id":
        return (row.get("ad_id"),)
    return (row.get("campaign_id"), row.get("ad_name"))


def _append_unique(values: list, value: Any) -> None:
    if value and value not in values:
        values.append(value)


def _merge(rows: list[dict], group_by: str) -> list[dict]:
    """Sum the rows of each group. A count stays absent until some row in the group has it."""
    groups: dict[tuple, dict] = {}
    for row in rows:
        g = groups.setdefault(_group_key(row, group_by), {
            "ad_name": row.get("ad_name"), "campaign_id": row.get("campaign_id"),
            "campaign_name": row.get("campaign_name"), "ad_ids": [], "adset_names": [],
            "spend": 0.0, "impressions": 0.0, "counts": {}, "watch": [],
        })
        _append_unique(g["ad_ids"], row.get("ad_id"))
        _append_unique(g["adset_names"], row.get("adset_name"))
        g["spend"] += _num(row.get("spend"))
        g["impressions"] += _num(row.get("impressions"))
        seen = {key: _count(row.get(field)) for key, field in VIDEO_COUNT_FIELDS.items()}
        plays_3s = _extract_action_value(row.get("actions") or [], PLAYS_3S_ACTION)
        seen["plays_3s"] = _num(plays_3s) if plays_3s is not None else None
        for key, value in seen.items():
            if value is not None:
                g["counts"][key] = g["counts"].get(key, 0.0) + value
        avg = _count(row.get(AVG_WATCH_FIELD))
        if avg is not None:
            # seconds per play, so averages merge weighted by the plays they cover
            g["watch"].append((avg, seen["plays"] or seen["plays_3s"] or 0.0))
    return list(groups.values())


def _pct(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or not denominator:
        return None
    return round(numerator / denominator * 100, 2)


def _shape(g: dict) -> dict:
    """One merged group as a response row, with rates and the ordering check."""
    is_video = bool(g["counts"])
    out: dict[str, Any] = {
        "ad_name": g["ad_name"], "ad_ids": g["ad_ids"], "campaign_id": g["campaign_id"],
        "campaign_name": g["campaign_name"], "adset_names": g["adset_names"],
        "spend": round(g["spend"], 2), "impressions": int(g["impressions"]), "is_video": is_video,
    }
    if not is_video:
        # static / GIF: Meta returns every video field empty, so nothing is measured, not zero
        out.update({key: None for key in ("plays", "plays_3s", *COMPLETION_POINTS, "thruplay", "avg_watch_time_s")})
        out["rates"] = None
        out["ordering_violations"] = []
        return out

    # Meta omits a video field with no events, so for an ad that has video data a missing one is 0.
    c = {key: g["counts"].get(key, 0.0) for key in ("plays", "plays_3s", *COMPLETION_POINTS, "thruplay")}
    out.update({key: int(round(value)) for key, value in c.items()})

    watch_weight = sum(w for _, w in g["watch"])
    if watch_weight > 0:
        out["avg_watch_time_s"] = round(sum(a * w for a, w in g["watch"]) / watch_weight, 2)
    elif g["watch"]:
        out["avg_watch_time_s"] = round(sum(a for a, _ in g["watch"]) / len(g["watch"]), 2)
    else:
        out["avg_watch_time_s"] = None

    rates: dict[str, Optional[float]] = {"hook_rate": _pct(c["plays_3s"], g["impressions"])}
    for point in COMPLETION_POINTS:
        rates[f"retention_{point}_vs_3s"] = _pct(c[point], c["plays_3s"])
    for point in COMPLETION_POINTS:
        rates[f"retention_{point}_vs_plays"] = _pct(c[point], c["plays"])
    rates["hold_rate"] = _pct(c["thruplay"], c["plays_3s"])
    out["rates"] = rates

    out["ordering_violations"] = [
        f"{later}>{earlier}" for earlier, later in zip(ORDER, ORDER[1:]) if c[later] > c[earlier]
    ]
    return out


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_video_retention(
    object_id: str,
    time_range: str = "last_7d",
    level: str = "ad",
    ad_name_contains: Optional[str] = None,
    min_spend: float = 0,
    group_by: str = "ad_name",
) -> dict:
    """
    Video retention and hook rate per ad: plays, 3-second plays, 25/50/75/95/100% watched, ThruPlay
    and average watch time, with the rates computed against both denominators.

    get_insights only returns 3-second plays; this is the route to the rest of the video funnel.
    Read-only. Static and GIF ads are returned with nulls (not zeros) for every video field.

    Rates, in percent: hook_rate = 3-second plays / impressions; retention_pX_vs_3s = pX / 3-second
    plays; retention_pX_vs_plays = pX / all plays; hold_rate = ThruPlay / 3-second plays. Both
    retention denominators are returned and named so a chart can say which one it uses.

    Args:
        object_id: Ad account ID (act_XXX), or a campaign or ad set ID to look inside it.
        time_range: Date preset ('last_7d', 'last_30d', 'this_month', ...) or an explicit
            range 'YYYY-MM-DD,YYYY-MM-DD'.
        level: Only 'ad' is supported.
        ad_name_contains: Keep only ads whose name contains this text (case-insensitive).
        min_spend: Drop ads that spent less than this over the range (applied after merging).
        group_by: 'ad_name' (default) merges ads with the same name inside a campaign, because one
            ad can carry two ad IDs; 'ad_id' keeps every ad ID apart.
    """
    if level != "ad":
        return {"error": f"Unsupported level: '{level}'. Only level='ad' is supported.",
                "blocked_at": "input_validation"}
    if group_by not in GROUP_BY:
        return {"error": f"Invalid group_by: '{group_by}'. Valid: {', '.join(GROUP_BY)}",
                "blocked_at": "input_validation"}
    try:
        min_spend = float(min_spend or 0)
    except (TypeError, ValueError):
        return {"error": f"min_spend must be a number, got '{min_spend}'.", "blocked_at": "input_validation"}
    if min_spend < 0:
        return {"error": "min_spend cannot be negative.", "blocked_at": "input_validation"}

    time_params = _time_params(time_range)
    if time_params is None:
        return {"error": f"Unknown time_range: '{time_range}'", "valid_presets": META_DATE_PRESETS,
                "blocked_at": "input_validation"}

    api_client._ensure_initialized()

    params = {"level": "ad", "limit": str(PAGE_SIZE), **time_params}
    try:
        rows, more = _fetch_rows(object_id, params)
    except MetaAPIError as e:
        return {"error": f"Could not read video insights for {object_id}: {e}",
                "hint": "Check the ID and that the token has ads_read on this account."}

    fetched = len(rows)
    if ad_name_contains:
        needle = ad_name_contains.lower()
        rows = [r for r in rows if needle in (r.get("ad_name") or "").lower()]

    ads = [_shape(g) for g in _merge(rows, group_by)]
    ads = [a for a in ads if a["spend"] >= min_spend]
    ads.sort(key=lambda a: (-a["spend"], a["ad_name"] or ""))

    notes: list[str] = []
    static = sum(1 for a in ads if not a["is_video"])
    if static:
        notes.append(f"{static} ad(s) have no video data (static or GIF); their video fields are null.")
    flagged = sum(1 for a in ads if a["ordering_violations"])
    if flagged:
        notes.append(
            f"{flagged} ad(s) list ordering_violations: counts out of order against "
            "plays >= plays_3s >= p25 >= p50 >= p75 >= p95 >= p100. A p25 above plays_3s is expected "
            "for videos shorter than 12 seconds.")

    response: dict[str, Any] = {
        "object_id": object_id,
        "time_range": time_range,
        "level": "ad",
        "group_by": group_by,
        "total": len(ads),
        "ads": ads,
        "units": UNITS,
        "definitions": DEFINITIONS,
        "truncated": more,
        "rate_limit_usage_pct": api_client.rate_limits.max_usage_pct,
    }
    if more:
        response["truncation_note"] = (
            f"Stopped after {fetched} ad rows but more exist, so these ads are not the whole range. "
            "Query a campaign or ad set ID, or a shorter time_range.")
    if notes:
        response["notes"] = notes
    return response
