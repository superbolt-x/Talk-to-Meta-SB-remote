"""
Ad previews (read-only): a link to see how an ad looks in each placement.

Wraps /{ad_id}/previews and /{creative_id}/previews. Meta answers with an HTML <iframe> whose `src` is a
signed preview page; the iframe is unpacked here into a plain link, with its size, so a person can open it.
The links expire (Meta documents about 24 hours), so they are for looking at now, not for storing.

Docs: https://developers.facebook.com/docs/marketing-api/reference/ad-creative/previews/
"""
import html
import logging
import re
from typing import Any, Optional

from meta_ads_mcp.server import mcp
from mcp.types import ToolAnnotations
from meta_ads_mcp.core.api import api_client, MetaAPIError

logger = logging.getLogger("meta-ads-mcp.previews")

DEFAULT_FORMATS = ("DESKTOP_FEED_STANDARD", "MOBILE_FEED_STANDARD", "INSTAGRAM_STANDARD")
MAX_FORMATS = 6
_FORMAT_RE = re.compile(r"^[A-Z0-9_]{3,64}$")
_ID_RE = re.compile(r"^\d{5,25}$")
_ATTR = {name: re.compile(rf'\b{name}\s*=\s*["\']([^"\']*)["\']', re.I) for name in ("src", "width", "height")}


def _unpack_iframe(body: Any) -> Optional[dict]:
    """The preview link, width and height out of Meta's iframe markup (None when there is no iframe)."""
    if not isinstance(body, str) or "<iframe" not in body.lower():
        return None
    tag = re.search(r"<iframe\b[^>]*>", body, re.I)
    if not tag:
        return None
    found = {name: pattern.search(tag.group(0)) for name, pattern in _ATTR.items()}
    if not found["src"]:
        return None
    out: dict[str, Any] = {"url": html.unescape(found["src"].group(1))}
    for name in ("width", "height"):
        raw = found[name].group(1) if found[name] else None
        out[name] = int(raw) if raw and raw.isdigit() else None
    return out


def _hint_for(code: Optional[int]) -> str:
    if code == 100:
        return ("Meta did not accept this format for this ad (for example a Reels format for an image ad), or the "
                "ID is not an ad or creative. Try DESKTOP_FEED_STANDARD, MOBILE_FEED_STANDARD or INSTAGRAM_STANDARD.")
    if code in (10, 190, 200, 102):
        return "Previews need ads_read and access to the ad account, and a valid token."
    return "Meta could not build this preview."


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_ad_previews(
    ad_id: Optional[str] = None,
    creative_id: Optional[str] = None,
    ad_formats: str = ",".join(DEFAULT_FORMATS),
    locale: Optional[str] = None,
) -> dict:
    """
    Links to see how an ad looks in each placement (feed, Instagram, stories, Reels...). Read-only.

    Give an ad ID or a creative ID. Each preview is a Meta page you open in a browser; the link expires
    (Meta documents about 24 hours), so ask again later rather than saving it. A format Meta cannot build
    for this ad is reported under `errors` and does not hide the others.

    Args:
        ad_id: The ad to preview (give this or creative_id).
        creative_id: The ad creative to preview instead of an ad.
        ad_formats: Comma-separated placements, up to 6 (default DESKTOP_FEED_STANDARD,MOBILE_FEED_STANDARD,
            INSTAGRAM_STANDARD). Others include INSTAGRAM_STORY, INSTAGRAM_REELS, FACEBOOK_STORY_MOBILE,
            FACEBOOK_REELS_MOBILE, RIGHT_COLUMN_STANDARD, MARKETPLACE_MOBILE and INSTAGRAM_EXPLORE_GRID_HOME.
        locale: Optional language for the preview text, such as 'en_US'.
    """
    api_client._ensure_initialized()
    ad_id, creative_id = (str(v).strip() if v else None for v in (ad_id, creative_id))
    formats = [f.strip().upper() for f in str(ad_formats or "").split(",") if f.strip()]
    formats = list(dict.fromkeys(formats))  # keep order, drop repeats
    problems = []
    if bool(ad_id) == bool(creative_id):
        problems.append("give exactly one of ad_id or creative_id")
    for label, value in (("ad_id", ad_id), ("creative_id", creative_id)):
        if value and not _ID_RE.match(value):
            problems.append(f"{label} must be a numeric ID")
    if not 1 <= len(formats) <= MAX_FORMATS:
        problems.append(f"ad_formats must list 1 to {MAX_FORMATS} formats")
    bad = [f for f in formats if not _FORMAT_RE.match(f)]
    if bad:
        problems.append(f"not a placement name: {', '.join(bad)}")
    if problems:
        return {"error": "; ".join(problems), "blocked_at": "input_validation"}

    object_id = ad_id or creative_id
    previews: list[dict] = []
    errors: dict[str, str] = {}
    codes: list[Optional[int]] = []
    for fmt in formats:
        params = {"ad_format": fmt}
        if locale:
            params["locale"] = str(locale).strip()
        try:
            res = api_client.graph_get(f"/{object_id}/previews", params=params)
        except MetaAPIError as e:
            errors[fmt] = str(e)
            codes.append(e.error_code)
            continue
        rows = [r for r in (res.get("data") or []) if isinstance(r, dict)]
        unpacked = _unpack_iframe(rows[0].get("body")) if rows else None
        if unpacked:
            previews.append({"format": fmt, **unpacked})
        else:
            errors[fmt] = "Meta returned no preview for this format."
            codes.append(None)

    response: dict[str, Any] = {
        "ad_id" if ad_id else "creative_id": object_id,
        "previews": previews,
        "note": "Open a link in a browser. Links are Meta-signed and expire (Meta documents about 24 hours).",
        "rate_limit_usage_pct": api_client.rate_limits.max_usage_pct,
    }
    if errors:
        response["errors"] = errors
        response["hint"] = _hint_for(next((c for c in codes if c is not None), None))
    if not previews:
        response["note"] = "No preview could be built."
    return response
