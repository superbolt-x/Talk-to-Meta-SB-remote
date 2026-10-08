"""
Opportunity score and Meta recommendations (read-only).

Wraps three Marketing API surfaces:
- ad account field ``opportunity_score`` (0-100, near real-time)
- ``/act_<id>/recommendations`` (Meta's personalised best-practice recommendations,
  each with the opportunity-score points it would add)
- ``/act_<id>/opportunity_score_history`` (daily score, 45-day window, ~2-day latency)

Applying a recommendation (POST /recommendations) is intentionally not exposed:
it edits live ad objects and belongs behind the supervised write path.

Docs: https://developers.facebook.com/documentation/ads-commerce/marketing-api/overview/performance-recommendations
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from meta_ads_mcp.server import mcp
from mcp.types import ToolAnnotations
from meta_ads_mcp.core.api import api_client, MetaAPIError
from meta_ads_mcp.core.utils import ensure_account_id_format

logger = logging.getLogger("meta-ads-mcp.opportunity")

# Meta documents a 45-day maximum window and ~2 days of latency on the history API.
MAX_HISTORY_DAYS = 45
HISTORY_LATENCY_DAYS = 2
# Gap (points) between live score and last history value worth explaining. Operator heuristic.
LIVE_VS_HISTORY_GAP_NOTE = 5


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_recommendation(rec: dict) -> dict:
    """Flatten one recommendation across the ad-account and business response shapes."""
    content = rec.get("recommendation_content") or {}
    return {
        "signature": rec.get("recommendation_signature"),
        # Ad-account requests return `type`; business-level requests return `recommendation_name`.
        "type": rec.get("type") or rec.get("recommendation_name"),
        "stage": rec.get("recommendation_stage"),
        "level": rec.get("level"),
        "object_ids": rec.get("object_ids") or [],
        "score_lift_points": _to_float(
            content.get("opportunity_score_lift", rec.get("opportunity_score_lift"))
        ),
        "lift_estimate": content.get("lift_estimate", rec.get("lift_estimate")),
        "body": content.get("body", rec.get("body")),
        "ads_manager_url": rec.get("url"),
        "created": rec.get("recommendation_time"),
    }


def _extract_recommendations(result: dict) -> list[dict]:
    """Recommendations arrive nested (data[].recommendations[]) but accept a flat list too."""
    found: list[dict] = []
    for item in result.get("data", []) or []:
        if isinstance(item, dict) and isinstance(item.get("recommendations"), list):
            found.extend(r for r in item["recommendations"] if isinstance(r, dict))
        elif isinstance(item, dict):
            found.append(item)
    return found


def _summarize_history(rows: list[dict]) -> dict:
    scored = [r for r in rows if r.get("opportunity_score") is not None]
    if not scored:
        return {}
    first, last = scored[0], scored[-1]
    delta = round(last["opportunity_score"] - first["opportunity_score"], 2)
    return {
        "from": first.get("date"),
        "to": last.get("date"),
        "start_score": first["opportunity_score"],
        "end_score": last["opportunity_score"],
        "change": delta,
        "direction": "up" if delta > 0 else "down" if delta < 0 else "flat",
        "min": min(r["opportunity_score"] for r in scored),
        "max": max(r["opportunity_score"] for r in scored),
    }


def _live_vs_history_note(live: Optional[float], summary: dict) -> Optional[str]:
    """Explain a gap between the live score and the last history point instead of leaving it a mystery."""
    if live is None or not summary:
        return None
    gap = round(live - summary["end_score"], 2)
    if abs(gap) < LIVE_VS_HISTORY_GAP_NOTE:
        return None
    return (
        f"The live score ({live:g}) is {abs(gap):g} points {'above' if gap > 0 else 'below'} the last "
        f"history value ({summary['end_score']:g} on {summary['to']}). History lags about "
        f"{HISTORY_LATENCY_DAYS} days and Meta notes the live score can differ from the historical "
        "series, so this is not necessarily an error. Set explain_history=true to see which "
        "campaign changes moved the score."
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_opportunity_score(
    account_id: str,
    include_recommendations: bool = True,
    history_days: int = 0,
    explain_history: bool = False,
) -> dict:
    """
    Get Meta's opportunity score (0-100) for an ad account, with Meta's recommendations
    and optionally the score's daily history.

    The score reflects how well-optimized the account is; each recommendation shows
    the points it would add if applied. This is Meta's own guidance, not a guarantee
    of performance. Read-only: recommendations are NOT applied by this tool.

    Args:
        account_id: Ad account ID (e.g., 'act_123456789').
        include_recommendations: Include Meta's current recommendations, ranked by
            score lift (default True).
        history_days: Also return the daily score history for this many days (max 45).
            0 (default) skips history. Meta's history lags ~2 days, so it ends 2 days ago.
        explain_history: With history_days, ask Meta for the per-campaign changes that
            moved the score on each day (a `changelog`).
    """
    api_client._ensure_initialized()
    account_id = ensure_account_id_format(account_id)

    response: dict[str, Any] = {"account_id": account_id}
    errors: dict[str, str] = {}

    # --- Current score (field on the ad account) ---
    try:
        acct = api_client.graph_get(
            f"/{account_id}", fields=["opportunity_score", "opportunity_score_weight"],
        )
        response["opportunity_score"] = _to_float(acct.get("opportunity_score"))
        if acct.get("opportunity_score_weight") is not None:
            response["opportunity_score_weight"] = acct["opportunity_score_weight"]
        if response["opportunity_score"] is None:
            response["note"] = (
                "Meta returned no opportunity score for this account "
                "(new, inactive, or not eligible)."
            )
    except MetaAPIError as e:
        errors["opportunity_score"] = str(e)
        response["opportunity_score"] = None

    # --- Recommendations ---
    if include_recommendations:
        try:
            raw = api_client.graph_get(f"/{account_id}/recommendations")
            recs = [_normalize_recommendation(r) for r in _extract_recommendations(raw)]
            recs.sort(key=lambda r: r["score_lift_points"] or 0, reverse=True)
            by_type: dict[str, int] = {}
            for r in recs:
                key = r["type"] or "UNKNOWN"
                by_type[key] = by_type.get(key, 0) + 1
            response["recommendations"] = recs
            response["recommendation_summary"] = {
                "count": len(recs),
                "by_type": by_type,
                # Sum of listed lifts. Meta does not promise they are additive.
                "sum_of_listed_lift_points": round(
                    sum(r["score_lift_points"] or 0 for r in recs), 2
                ),
            }
            if (raw.get("paging") or {}).get("next"):
                response["recommendation_summary"]["has_more_pages"] = True
        except MetaAPIError as e:
            errors["recommendations"] = str(e)

    # --- History ---
    if history_days and history_days > 0:
        days = min(int(history_days), MAX_HISTORY_DAYS)
        to_date = (datetime.now(timezone.utc) - timedelta(days=HISTORY_LATENCY_DAYS)).date()
        from_date = to_date - timedelta(days=days - 1)
        params = {"from_date": from_date.isoformat(), "to_date": to_date.isoformat()}
        if explain_history:
            params["get_reason"] = "true"
        try:
            raw = api_client.graph_get(f"/{account_id}/opportunity_score_history", params=params)
            rows = [r for r in raw.get("data", []) if isinstance(r, dict)]
            for r in rows:
                r["opportunity_score"] = _to_float(r.get("opportunity_score"))
            rows.sort(key=lambda r: r.get("date") or "")
            response["history"] = rows
            response["history_summary"] = _summarize_history(rows)
            gap_note = _live_vs_history_note(response.get("opportunity_score"), response["history_summary"])
            if gap_note:
                response["history_summary"]["live_vs_history_note"] = gap_note
            if history_days > MAX_HISTORY_DAYS:
                response["history_note"] = f"history_days capped at Meta's {MAX_HISTORY_DAYS}-day maximum."
        except MetaAPIError as e:
            errors["history"] = str(e)

    if errors:
        response["errors"] = errors
        response["hint"] = (
            "Meta rejected part of this request. Opportunity score needs ads_read on the token's "
            "ad-account access; very new or inactive accounts may have no score."
        )
    response["rate_limit_usage_pct"] = api_client.rate_limits.max_usage_pct
    return response
