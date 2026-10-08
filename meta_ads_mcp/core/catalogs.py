"""
Catalog management and diagnostic tools.

Provides catalog health checks, product inspection, product set CRUD,
feed status, and connection chain validation.

Diagnostic-first: outputs classify catalog health and surface connection
gaps, stale feeds, rejected products, and ecommerce readiness issues.

Phase: v1.1 (Read) / v1.3 (Write)
"""
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from meta_ads_mcp.server import mcp
from mcp.types import ToolAnnotations
from meta_ads_mcp.core.api import api_client, MetaAPIError
from meta_ads_mcp.core.utils import ensure_account_id_format

logger = logging.getLogger("meta-ads-mcp.catalogs")

SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_HIGH = "HIGH"
SEVERITY_MEDIUM = "MEDIUM"
SEVERITY_LOW = "LOW"
SEVERITY_INFO = "INFO"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_catalog_info(catalog_id: str) -> dict:
    """
    Get catalog details including product count, vertical, name,
    and connected event sources (pixels).

    Args:
        catalog_id: Product catalog ID (numeric string).
    """
    api_client._ensure_initialized()

    try:
        result = api_client.graph_get(
            f"/{catalog_id}",
            fields=[
                "id", "name", "product_count", "vertical",
                "da_display_settings",
            ],
        )

        # Get connected pixels (external_event_sources)
        try:
            event_sources = api_client.graph_get(
                f"/{catalog_id}/external_event_sources",
                fields=["id", "name"],
            )
            result["connected_pixels"] = event_sources.get("data", [])
        except MetaAPIError:
            result["connected_pixels"] = []

        # Get product sets
        try:
            sets_result = api_client.graph_get(
                f"/{catalog_id}/product_sets",
                fields=["id", "name", "product_count"],
            )
            result["product_sets"] = sets_result.get("data", [])
        except MetaAPIError:
            result["product_sets"] = []

        # Get feeds
        try:
            feeds_result = api_client.graph_get(
                f"/{catalog_id}/product_feeds",
                fields=["id", "name", "product_count", "latest_upload", "schedule"],
            )
            result["feeds"] = feeds_result.get("data", [])
        except MetaAPIError:
            result["feeds"] = []

        result["rate_limit_usage_pct"] = api_client.rate_limits.max_usage_pct
        return result

    except MetaAPIError:
        raise


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_catalog_products(
    catalog_id: str,
    limit: int = 25,
    filter_availability: Optional[str] = None,
) -> dict:
    """
    List products in a catalog with price, availability, review status, and URLs.

    Args:
        catalog_id: Product catalog ID.
        limit: Max products to return (default 25, max 100).
        filter_availability: Optional filter: 'in stock', 'out of stock', 'discontinued'.
    """
    api_client._ensure_initialized()

    params: dict[str, str] = {"limit": str(min(limit, 100))}

    if filter_availability:
        params["filter"] = f'{{"availability":{{"eq":"{filter_availability}"}}}}'

    try:
        result = api_client.graph_get(
            f"/{catalog_id}/products",
            fields=[
                "id", "name", "price", "currency",
                "availability", "review_status",
                "image_url", "url",
                "retailer_id", "brand",
            ],
            params=params,
        )

        products = result.get("data", [])

        # Aggregate stats
        avail_counts: dict[str, int] = {}
        review_counts: dict[str, int] = {}
        price_values: list[float] = []

        for p in products:
            avail = p.get("availability", "unknown")
            review = p.get("review_status", "unknown") or "no_review"
            avail_counts[avail] = avail_counts.get(avail, 0) + 1
            review_counts[review] = review_counts.get(review, 0) + 1

            # Parse price for stats
            price_str = p.get("price", "")
            if price_str:
                try:
                    # Price format: "€33.00", "$33.00", "33.00 USD" or just "3300"
                    cleaned = re.sub(r"[^\d.,-]", "", price_str).replace(",", ".").strip()
                    price_val = float(cleaned)
                    if price_val > 500:  # Likely in cents
                        price_val /= 100
                    price_values.append(price_val)
                except (ValueError, TypeError):
                    pass

        stats = {
            "total_returned": len(products),
            "availability_breakdown": avail_counts,
            "review_status_breakdown": review_counts,
        }

        if price_values:
            stats["price_range"] = {
                "min": round(min(price_values), 2),
                "max": round(max(price_values), 2),
                "avg": round(sum(price_values) / len(price_values), 2),
                "currency": products[0].get("currency") if products else None,
            }

        return {
            "catalog_id": catalog_id,
            "products": products,
            "stats": stats,
            "rate_limit_usage_pct": api_client.rate_limits.max_usage_pct,
        }

    except MetaAPIError:
        raise


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_product_sets(catalog_id: str) -> dict:
    """
    List product sets in a catalog with product counts and filter rules.

    Args:
        catalog_id: Product catalog ID.
    """
    api_client._ensure_initialized()

    try:
        result = api_client.graph_get(
            f"/{catalog_id}/product_sets",
            fields=["id", "name", "product_count", "filter"],
        )

        sets = result.get("data", [])

        # Flag empty sets
        for s in sets:
            s["is_empty"] = (s.get("product_count", 0) == 0)

        return {
            "catalog_id": catalog_id,
            "total": len(sets),
            "product_sets": sets,
            "empty_sets": sum(1 for s in sets if s.get("is_empty")),
            "rate_limit_usage_pct": api_client.rate_limits.max_usage_pct,
        }

    except MetaAPIError:
        raise


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def validate_catalog_connections(
    catalog_id: str,
    account_id: Optional[str] = None,
    pixel_id: Optional[str] = None,
    page_id: Optional[str] = None,
) -> dict:
    """
    Validate the catalog-pixel-account-page connection chain
    and run diagnostic health assessment.

    For DPA (Dynamic Product Ads) to work, the full chain must be connected:
    Catalog -> Pixel -> Ad Account -> Page

    Args:
        catalog_id: Product catalog ID.
        account_id: Optional ad account ID to verify connection.
        pixel_id: Optional pixel ID to verify catalog-pixel link.
        page_id: Optional page ID to verify catalog-page link.
    """
    api_client._ensure_initialized()

    issues: list[dict] = []
    connections = {
        "catalog_exists": False,
        "catalog_has_products": False,
        "pixel_connected": False,
        "has_product_sets": False,
        "feed_active": False,
        "products_approved": False,
    }

    # 1. Check catalog exists and has products
    try:
        catalog = api_client.graph_get(
            f"/{catalog_id}",
            fields=["id", "name", "product_count", "vertical"],
        )
        connections["catalog_exists"] = True
        product_count = catalog.get("product_count", 0)
        connections["catalog_has_products"] = product_count > 0

        # Check catalog name hygiene
        catalog_name = catalog.get("name", "")
        generic_names = ["service card catalog", "test catalog", "catalog", "untitled", "default"]
        if any(g in catalog_name.lower() for g in generic_names):
            issues.append({
                "severity": SEVERITY_LOW,
                "check": "catalog_name_hygiene",
                "message": f"Catalog name '{catalog_name}' appears generic or auto-generated",
                "fix": "Rename the catalog to something descriptive (e.g., 'Example E-Shop Products') in Commerce Manager.",
            })

        if product_count == 0:
            issues.append({
                "severity": SEVERITY_CRITICAL,
                "check": "catalog_products",
                "message": "Catalog has 0 products",
                "fix": "Add products to the catalog via Commerce Manager or product feed.",
            })
        elif product_count < 4:
            issues.append({
                "severity": SEVERITY_LOW,
                "check": "catalog_product_count",
                "message": f"Catalog has only {product_count} products (< 4 for carousel)",
                "fix": "Consider adding more products for carousel ad format.",
            })

    except MetaAPIError as e:
        issues.append({
            "severity": SEVERITY_CRITICAL,
            "check": "catalog_exists",
            "message": f"Cannot access catalog {catalog_id}: {e}",
            "fix": "Verify catalog ID is correct and accessible by this business.",
        })
        return {
            "catalog_id": catalog_id,
            "health": "missing",
            "connections": connections,
            "issues": issues,
        }

    # 2. Check pixel connection
    try:
        event_sources = api_client.graph_get(
            f"/{catalog_id}/external_event_sources",
            fields=["id", "name"],
        )
        connected_pixels = event_sources.get("data", [])
        connected_pixel_ids = [p.get("id") for p in connected_pixels]

        if not connected_pixels:
            issues.append({
                "severity": SEVERITY_CRITICAL,
                "check": "pixel_connected",
                "message": "No pixel connected to this catalog. Catalog is NOT DPA-ready - Dynamic Product Ads cannot run without pixel-catalog linkage.",
                "fix": "Connect the pixel to this catalog in Commerce Manager > Data Sources. Until connected, DPA campaigns (like retargeting) will not function.",
            })
        else:
            connections["pixel_connected"] = True
            if pixel_id and pixel_id not in connected_pixel_ids:
                issues.append({
                    "severity": SEVERITY_HIGH,
                    "check": "specific_pixel_connected",
                    "message": f"Pixel {pixel_id} is not connected to this catalog. Connected: {connected_pixel_ids}",
                    "fix": f"Connect pixel {pixel_id} to catalog {catalog_id} in Commerce Manager.",
                })

    except MetaAPIError:
        issues.append({
            "severity": SEVERITY_MEDIUM,
            "check": "pixel_connection_check",
            "message": "Could not verify pixel-catalog connection via API",
            "fix": "Check catalog data sources in Commerce Manager manually.",
        })

    # 3. Check product sets
    try:
        sets_result = api_client.graph_get(
            f"/{catalog_id}/product_sets",
            fields=["id", "name", "product_count"],
        )
        product_sets = sets_result.get("data", [])
        connections["has_product_sets"] = len(product_sets) > 0

        empty_sets = [s for s in product_sets if s.get("product_count", 0) == 0]
        if empty_sets:
            names = [s.get("name", s.get("id")) for s in empty_sets]
            issues.append({
                "severity": SEVERITY_MEDIUM,
                "check": "empty_product_sets",
                "message": f"Empty product sets: {', '.join(names)}",
                "fix": "Update filter rules or remove empty product sets.",
            })

        # Check product set coverage vs total catalog
        if product_sets and product_count > 0:
            total_set_products = sum(s.get("product_count", 0) for s in product_sets)
            max_set_products = max(s.get("product_count", 0) for s in product_sets)
            if max_set_products < product_count * 0.5:
                issues.append({
                    "severity": SEVERITY_MEDIUM,
                    "check": "product_set_coverage",
                    "message": f"Largest product set covers {max_set_products}/{product_count} products ({max_set_products*100//product_count}%). {product_count - max_set_products} products are not in the primary set.",
                    "fix": "Review product set filters. Products outside all sets will not appear in DPA ads.",
                })

        if not product_sets:
            issues.append({
                "severity": SEVERITY_MEDIUM,
                "check": "product_sets_exist",
                "message": "No product sets defined. DPA ad sets require a product set.",
                "fix": "Create product sets in Commerce Manager for targeting.",
            })

    except MetaAPIError:
        pass

    # 4. Check product health (sample)
    try:
        products = api_client.graph_get(
            f"/{catalog_id}/products",
            fields=["id", "availability", "review_status"],
            params={"limit": "50"},
        )
        product_list = products.get("data", [])

        out_of_stock = sum(1 for p in product_list if p.get("availability") != "in stock")
        rejected = sum(1 for p in product_list if p.get("review_status") in ("rejected", "disapproved"))

        if product_list:
            connections["products_approved"] = rejected == 0

        if rejected > 0:
            issues.append({
                "severity": SEVERITY_HIGH,
                "check": "rejected_products",
                "message": f"{rejected} product(s) rejected/disapproved out of {len(product_list)} sampled",
                "fix": "Review rejected products in Commerce Manager and fix violations.",
            })

        if out_of_stock > 0:
            issues.append({
                "severity": SEVERITY_LOW,
                "check": "out_of_stock",
                "message": f"{out_of_stock} product(s) out of stock out of {len(product_list)} sampled",
                "fix": "Update product availability or exclude out-of-stock items from DPA.",
            })

    except MetaAPIError:
        pass

    # 5. Check feeds
    try:
        feeds = api_client.graph_get(
            f"/{catalog_id}/product_feeds",
            fields=["id", "name", "product_count", "latest_upload", "schedule"],
        )
        feed_list = feeds.get("data", [])
        connections["feed_active"] = len(feed_list) > 0

        if not feed_list:
            issues.append({
                "severity": SEVERITY_INFO,
                "check": "feed_exists",
                "message": "No product feed detected via API. Catalog may be manually managed or managed via another path (Commerce Manager, Shops, or partner integration).",
                "fix": "If product updates are needed at scale, consider adding an automated product feed.",
            })

    except MetaAPIError:
        pass

    # 6. Classify overall health
    critical_count = sum(1 for i in issues if i["severity"] == SEVERITY_CRITICAL)
    high_count = sum(1 for i in issues if i["severity"] == SEVERITY_HIGH)

    if critical_count > 0:
        health = "degraded"
    elif high_count > 0:
        health = "partial"
    elif issues:
        health = "healthy_with_warnings"
    else:
        health = "healthy"

    # Sort issues by severity
    severity_order = {SEVERITY_CRITICAL: 0, SEVERITY_HIGH: 1, SEVERITY_MEDIUM: 2, SEVERITY_LOW: 3, SEVERITY_INFO: 4}
    issues.sort(key=lambda x: severity_order.get(x["severity"], 5))

    # DPA readiness summary
    dpa_ready = (
        connections["catalog_exists"]
        and connections["catalog_has_products"]
        and connections["pixel_connected"]
        and connections["has_product_sets"]
        and connections["products_approved"]
    )
    dpa_blockers = []
    if not connections["pixel_connected"]:
        dpa_blockers.append("pixel not connected to catalog")
    if not connections["catalog_has_products"]:
        dpa_blockers.append("catalog has no products")
    if not connections["has_product_sets"]:
        dpa_blockers.append("no product sets defined")
    if not connections["products_approved"]:
        dpa_blockers.append("products have approval issues")

    return {
        "catalog_id": catalog_id,
        "catalog_name": catalog.get("name"),
        "product_count": catalog.get("product_count"),
        "vertical": catalog.get("vertical"),
        "health": health,
        "dpa_ready": dpa_ready,
        "dpa_blockers": dpa_blockers,
        "connections": connections,
        "issues": issues,
        "issue_count": len(issues),
        "critical_issues": critical_count,
        "rate_limit_usage_pct": api_client.rate_limits.max_usage_pct,
    }


# --- Convenience Gap: Product Set Create/Update ---

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def create_product_set(
    catalog_id: str,
    name: str,
    filter_json: str,
) -> dict:
    """
    Create a product set with filter rules for DPA targeting.

    Args:
        catalog_id: Product catalog ID.
        name: Product set name.
        filter_json: JSON string of filter rules.
            Example: '{"product_type":{"i_contains":"shoes"}}'
    """
    import json as _json

    if not name or not name.strip():
        return {"error": "name is required.", "blocked_at": "input_validation"}

    try:
        filters = _json.loads(filter_json)
        if not isinstance(filters, dict):
            return {"error": "filter_json must be a JSON object.", "blocked_at": "input_validation"}
    except _json.JSONDecodeError as e:
        return {"error": f"Malformed filter_json: {e}", "blocked_at": "input_validation"}

    api_client._ensure_initialized()

    try:
        result = api_client.graph_post(
            f"/{catalog_id}/product_sets",
            data={
                "name": name.strip(),
                "filter": _json.dumps(filters),
            },
        )
    except MetaAPIError as e:
        return {"error": f"Meta API error: {e}", "blocked_at": "api_call"}

    ps_id = result.get("id")
    return {
        "product_set_id": ps_id,
        "catalog_id": catalog_id,
        "name": name.strip(),
        "filter": filters,
        "rate_limit_usage_pct": api_client.rate_limits.max_usage_pct,
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True))
def update_product_set(
    product_set_id: str,
    name: Optional[str] = None,
    filter_json: Optional[str] = None,
) -> dict:
    """
    Update a product set name or filter rules.

    Args:
        product_set_id: Product set ID to update.
        name: New name.
        filter_json: New filter rules as JSON string.
    """
    import json as _json

    if name is None and filter_json is None:
        return {"error": "Provide name or filter_json.", "blocked_at": "input_validation"}

    if filter_json is not None:
        try:
            filters = _json.loads(filter_json)
            if not isinstance(filters, dict):
                return {"error": "filter_json must be a JSON object.", "blocked_at": "input_validation"}
        except _json.JSONDecodeError as e:
            return {"error": f"Malformed filter_json: {e}", "blocked_at": "input_validation"}

    api_client._ensure_initialized()

    payload = {}
    if name is not None:
        payload["name"] = name.strip()
    if filter_json is not None:
        payload["filter"] = _json.dumps(filters)

    try:
        api_client.graph_post(f"/{product_set_id}", data=payload)
    except MetaAPIError as e:
        return {"error": f"Meta API error: {e}", "blocked_at": "api_call"}

    return {
        "product_set_id": product_set_id,
        "updated_fields": list(payload.keys()),
        "rate_limit_usage_pct": api_client.rate_limits.max_usage_pct,
    }


# --- Feed health: upload sessions, upload errors, catalog diagnostics ---

# Operator heuristics for flagging, NOT Meta-published thresholds.
FEED_STALE_FACTOR = 2.0         # flag a scheduled feed after missing this many intervals
INVALID_RATIO_CRITICAL = 0.20   # >=20% of detected items invalid in the latest upload
INVALID_RATIO_HIGH = 0.05       # >=5%
ITEM_DROP_HIGH_PCT = 20.0       # persisted items fell this much vs the previous upload
MAX_UPLOADS = 10
MAX_ERROR_SAMPLES = 3           # sample rows/products kept per error

SCHEDULE_INTERVAL_HOURS = {"HOURLY": 1, "DAILY": 24, "WEEKLY": 168, "MONTHLY": 720}

FEED_FIELDS_RICH = [
    "id", "name", "file_name", "ingestion_source_type", "item_count",
    "product_count", "schedule", "update_schedule", "latest_upload",
]
FEED_FIELDS_BASIC = ["id", "name", "product_count", "latest_upload", "schedule"]
UPLOAD_FIELDS = [
    "id", "start_time", "end_time", "error_count", "warning_count",
    "num_detected_items", "num_invalid_items", "num_persisted_items",
    "num_deleted_items", "input_method", "filename",
]
DIAGNOSTIC_FIELDS = [
    "type", "severity", "title", "subtitle", "number_of_affected_items",
    "number_of_affected_entities", "affected_channels", "affected_entity",
    "affected_features", "error_code",
]
DIAGNOSTIC_FIELDS_BASIC = ["type", "severity", "title", "number_of_affected_items"]

_SEVERITY_ORDER = {SEVERITY_CRITICAL: 0, SEVERITY_HIGH: 1, SEVERITY_MEDIUM: 2, SEVERITY_LOW: 3, SEVERITY_INFO: 4}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_meta_time(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("+0000", "+00:00").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _schedule_interval_hours(schedule: Any) -> Optional[float]:
    """Expected hours between fetches for a scheduled feed, or None (manual / API feed)."""
    if not isinstance(schedule, dict):
        return None
    base = SCHEDULE_INTERVAL_HOURS.get(str(schedule.get("interval", "")).upper())
    if base is None:
        return None
    count = _int(schedule.get("interval_count")) or 1
    return float(base * max(count, 1))


def _normalize_upload(raw: dict) -> dict:
    detected = _int(raw.get("num_detected_items"))
    invalid = _int(raw.get("num_invalid_items"))
    return {
        "id": raw.get("id"),
        "start_time": raw.get("start_time"),
        "end_time": raw.get("end_time"),
        "completed": bool(raw.get("end_time")),
        "input_method": raw.get("input_method"),
        "filename": raw.get("filename"),
        "items_detected": detected,
        "items_persisted": _int(raw.get("num_persisted_items")),
        "items_invalid": invalid,
        "items_deleted": _int(raw.get("num_deleted_items")),
        "error_count": _int(raw.get("error_count")),
        "warning_count": _int(raw.get("warning_count")),
        "invalid_ratio": round(invalid / detected, 4) if invalid is not None and detected else None,
    }


def _assess_feed(feed: dict, uploads: list[dict], now: datetime) -> list[dict]:
    """Severity-ranked issues for one feed from its schedule and recent uploads (newest first)."""
    issues: list[dict] = []
    label = feed.get("name") or feed.get("id")

    def add(severity: str, check: str, message: str, fix: str) -> None:
        issues.append({"severity": severity, "check": check, "feed_id": feed.get("id"),
                       "message": f"{label}: {message}", "fix": fix})

    if not uploads:
        add(SEVERITY_HIGH, "feed_never_uploaded", "no upload sessions found",
            "Check the feed URL/schedule in Commerce Manager and trigger a manual upload.")
        return issues

    latest = uploads[0]
    started = _parse_meta_time(latest["start_time"])
    ended = _parse_meta_time(latest["end_time"])

    # Staleness: scheduled feeds only; a manual/API feed has no expected cadence.
    interval = _schedule_interval_hours(feed.get("schedule"))
    reference = ended or started
    if interval and reference:
        age_h = (now - reference).total_seconds() / 3600
        if age_h > interval * FEED_STALE_FACTOR:
            add(SEVERITY_HIGH, "feed_stale",
                f"last upload {age_h:.0f}h ago, schedule expects one every {interval:.0f}h",
                "Confirm the feed URL is reachable and returns the file; check fetch errors in Commerce Manager.")

    if not latest["completed"] and started and (now - started).total_seconds() > 2 * 3600:
        add(SEVERITY_MEDIUM, "upload_not_finished",
            "latest upload started over 2h ago and has not finished",
            "Very large or slow feed file; check the feed host's response time.")

    detected, persisted = latest["items_detected"], latest["items_persisted"]
    if latest["completed"] and detected and persisted == 0:
        add(SEVERITY_CRITICAL, "no_items_persisted",
            f"latest upload detected {detected} items but none were accepted",
            "Fix the fatal feed errors below; no products from this upload reached the catalog.")

    ratio = latest["invalid_ratio"]
    if ratio is not None and ratio > 0 and persisted != 0:
        pct = f"{ratio:.0%}"
        detail = f"{pct} of items in the latest upload are invalid ({latest['items_invalid']} of {detected})"
        fix = "Fix the fatal errors listed for this upload; invalid items are not created or updated."
        if ratio >= INVALID_RATIO_CRITICAL:
            add(SEVERITY_CRITICAL, "invalid_items", detail, fix)
        elif ratio >= INVALID_RATIO_HIGH:
            add(SEVERITY_HIGH, "invalid_items", detail, fix)
        else:
            add(SEVERITY_MEDIUM, "invalid_items", detail, fix)

    # Catalog shrinkage vs the previous completed upload (replace feeds delete missing items).
    previous = next((u for u in uploads[1:] if u["completed"] and u["items_persisted"]), None)
    if latest["completed"] and previous and persisted is not None:
        drop = (previous["items_persisted"] - persisted) / previous["items_persisted"] * 100
        if drop >= ITEM_DROP_HIGH_PCT:
            add(SEVERITY_HIGH, "item_count_drop",
                f"accepted items fell {drop:.0f}% ({previous['items_persisted']} -> {persisted}) vs the previous upload",
                "Check whether the feed file was truncated or products were filtered out upstream.")

    warnings = latest["warning_count"]
    if warnings and not issues:
        add(SEVERITY_LOW, "upload_warnings", f"latest upload has {warnings} warning(s)",
            "Warnings omit malformed optional fields; review the sampled errors below.")
    return issues


def _normalize_error(raw: dict) -> dict:
    samples = ((raw.get("samples") or {}).get("data")) or []
    return {
        "id": raw.get("id"),
        "severity": str(raw.get("severity", "")).lower() or None,
        "summary": raw.get("summary"),
        "description": raw.get("description"),
        "samples": [
            {"row_number": x.get("row_number"), "retailer_id": x.get("retailer_id"), "product_id": x.get("id")}
            for x in samples[:MAX_ERROR_SAMPLES] if isinstance(x, dict)
        ],
    }


def _graph_with_fallback(endpoint: str, rich: list[str], basic: Optional[list[str]], params: Optional[dict] = None) -> dict:
    """GET with `rich` fields; if Meta rejects one (code 100) retry with `basic` (None = no fields)."""
    try:
        return api_client.graph_get(endpoint, fields=rich, params=params)
    except MetaAPIError as e:
        if e.error_code != 100:
            raise
        return api_client.graph_get(endpoint, fields=basic, params=params)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_catalog_feed_health(
    catalog_id: str,
    feed_id: Optional[str] = None,
    upload_limit: int = 5,
    include_errors: bool = True,
    include_diagnostics: bool = True,
) -> dict:
    """
    Check the health of a product catalog's feeds: recent upload sessions (accepted vs
    invalid items, item-count drops, staleness against the schedule), a sample of the
    latest upload's errors and warnings, and Meta's catalog-level diagnostics
    (MUST_FIX issues such as missing/invalid attributes, image quality, policy violations).

    Read-only. Needs the catalog_management permission and access to the catalog.
    Flag thresholds are operator heuristics; error and diagnostic text comes from Meta.

    Args:
        catalog_id: Product catalog ID.
        feed_id: Only check this feed. Default: every feed on the catalog.
        upload_limit: Recent upload sessions to inspect per feed (default 5, max 10).
        include_errors: Include Meta's error/warning sample for each feed's latest upload.
        include_diagnostics: Include Meta's catalog-level diagnostic groups.
    """
    api_client._ensure_initialized()
    catalog_id = str(catalog_id).strip()
    upload_limit = max(1, min(int(upload_limit), MAX_UPLOADS))
    now = _utcnow()

    response: dict[str, Any] = {"catalog_id": catalog_id}
    errors: dict[str, str] = {}
    issues: list[dict] = []

    # --- Feeds ---
    feeds: list[dict] = []
    try:
        if feed_id:
            feeds = [_graph_with_fallback(f"/{str(feed_id).strip()}", FEED_FIELDS_RICH, FEED_FIELDS_BASIC)]
        else:
            raw = _graph_with_fallback(f"/{catalog_id}/product_feeds", FEED_FIELDS_RICH, FEED_FIELDS_BASIC)
            feeds = [f for f in raw.get("data", []) if isinstance(f, dict)]
    except MetaAPIError as e:
        errors["feeds"] = str(e)

    feed_reports: list[dict] = []
    for feed in feeds:
        fid = feed.get("id")
        report: dict[str, Any] = {
            "id": fid,
            "name": feed.get("name"),
            "file_name": feed.get("file_name"),
            "ingestion_source_type": feed.get("ingestion_source_type"),
            "schedule": feed.get("schedule"),
            "product_count": feed.get("product_count", feed.get("item_count")),
        }

        raw_uploads: list[dict] = []
        try:
            res = _graph_with_fallback(f"/{fid}/uploads", UPLOAD_FIELDS, None, params={"limit": str(upload_limit)})
            raw_uploads = [u for u in res.get("data", []) if isinstance(u, dict)]
        except MetaAPIError as e:
            errors[f"uploads:{fid}"] = str(e)
        if not raw_uploads and isinstance(feed.get("latest_upload"), dict) and feed["latest_upload"].get("id"):
            raw_uploads = [feed["latest_upload"]]  # fall back to the summary embedded on the feed

        uploads = sorted(
            (_normalize_upload(u) for u in raw_uploads),
            key=lambda u: _parse_meta_time(u["start_time"]) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )[:upload_limit]
        report["recent_uploads"] = uploads
        if "uploads:" + str(fid) not in errors:
            feed_issues = _assess_feed(feed, uploads, now)
            report["issues"] = feed_issues
            issues.extend(feed_issues)

        if include_errors and uploads:
            try:
                res = api_client.graph_get(f"/{uploads[0]['id']}/errors", params={"limit": "25"})
                sampled = [_normalize_error(e) for e in res.get("data", []) if isinstance(e, dict)]
                sampled.sort(key=lambda e: 0 if e["severity"] == "fatal" else 1)
                report["latest_upload_errors"] = sampled
                total = (res.get("summary") or {}).get("total_count")
                if total is not None:
                    report["latest_upload_error_total"] = total
            except MetaAPIError as e:
                errors[f"errors:{fid}"] = str(e)
        feed_reports.append(report)

    response["feeds"] = feed_reports
    if not feeds and "feeds" not in errors:
        issues.append({
            "severity": SEVERITY_INFO, "check": "feed_exists", "feed_id": None,
            "message": "No product feed on this catalog. It may be managed manually, via Shops, or a partner integration.",
            "fix": "Nothing to check here; use get_catalog_products for item-level status.",
        })

    # --- Catalog diagnostics ---
    if include_diagnostics:
        try:
            res = _graph_with_fallback(f"/{catalog_id}/diagnostics", DIAGNOSTIC_FIELDS, DIAGNOSTIC_FIELDS_BASIC)
            groups = [
                {
                    "type": g.get("type"),
                    "severity": g.get("severity"),
                    "title": g.get("title"),
                    "subtitle": g.get("subtitle"),
                    "affected_items": _int(g.get("number_of_affected_items")),
                    "affected_entity": g.get("affected_entity"),
                    "affected_channels": g.get("affected_channels"),
                    "affected_features": g.get("affected_features"),
                }
                for g in res.get("data", []) if isinstance(g, dict)
            ]
            groups.sort(key=lambda g: (0 if g["severity"] == "MUST_FIX" else 1, -(g["affected_items"] or 0)))
            response["diagnostics"] = groups
            for g in groups:
                must_fix = g["severity"] == "MUST_FIX"
                issues.append({
                    "severity": SEVERITY_HIGH if must_fix else SEVERITY_LOW,
                    "check": "catalog_diagnostic",
                    "feed_id": None,
                    "message": f"{g['title'] or g['type']}"
                               + (f" ({g['affected_items']} items)" if g["affected_items"] else ""),
                    "fix": g["subtitle"] or "See Commerce Manager > Catalog > Diagnostics for the affected items.",
                })
        except MetaAPIError as e:
            errors["diagnostics"] = str(e)

    issues.sort(key=lambda i: _SEVERITY_ORDER.get(i["severity"], 5))
    critical = sum(1 for i in issues if i["severity"] == SEVERITY_CRITICAL)
    high = sum(1 for i in issues if i["severity"] == SEVERITY_HIGH)
    actionable = [i for i in issues if i["severity"] != SEVERITY_INFO]
    response["health"] = (
        "degraded" if critical else "partial" if high
        else "healthy_with_warnings" if actionable else "healthy"
    )
    response["issue_count"] = len(issues)
    response["issues"] = issues

    if errors:
        response["errors"] = errors
        response["hint"] = (
            "Meta rejected part of this request. Feed health needs the catalog_management permission and "
            "access to this catalog (Commerce Manager > Catalog > Settings > Partners / People)."
        )
        if response["health"] == "healthy":
            response["health"] = "unknown"  # nothing was flagged, but part of the data could not be read
    response["rate_limit_usage_pct"] = api_client.rate_limits.max_usage_pct
    return response
