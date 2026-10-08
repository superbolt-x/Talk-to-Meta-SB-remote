"""
Pixel and event diagnostic tools.

Provides pixel health checks, event inspection, diagnostic reports,
and Test Events API integration.

Diagnostic-first: outputs classify tracking health and suggest fixes,
not just raw API payloads.

Phase: v1.1 (Read Operations)
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from meta_ads_mcp.server import mcp
from mcp.types import ToolAnnotations
from meta_ads_mcp.core.api import api_client, MetaAPIError
from meta_ads_mcp.core.utils import ensure_account_id_format

logger = logging.getLogger("meta-ads-mcp.pixels")

# --- Archetype event requirements ---

REQUIRED_EVENTS = {
    "ecommerce": {
        "critical": ["Purchase"],
        "important": ["AddToCart", "InitiateCheckout", "ViewContent"],
        "optional": ["ViewCategory", "Search", "AddPaymentInfo"],
    },
    "lead_gen": {
        "critical": ["Lead"],
        "important": ["SubmitApplication", "Contact"],
        "optional": ["ViewContent", "Schedule"],
    },
    "awareness": {
        "critical": [],
        "important": ["PageView"],
        "optional": ["ViewContent"],
    },
    "traffic": {
        "critical": [],
        "important": ["PageView", "ViewContent"],
        "optional": ["Lead"],
    },
    "hybrid": {
        "critical": ["Purchase", "Lead"],
        "important": ["AddToCart", "ViewContent", "InitiateCheckout"],
        "optional": ["SubmitApplication", "Contact"],
    },
    "messages": {
        "critical": [],
        "important": ["PageView"],
        "optional": ["Lead", "Contact"],
    },
}

# Events that require value/currency parameters
VALUE_REQUIRED_EVENTS = ["Purchase"]

# Severity levels
SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_HIGH = "HIGH"
SEVERITY_MEDIUM = "MEDIUM"
SEVERITY_LOW = "LOW"
SEVERITY_INFO = "INFO"


def _classify_pixel_health(
    pixel_info: dict,
    events_found: list[str],
    archetype: str,
    diagnostics: list[dict],
) -> dict:
    """
    Classify overall pixel health based on info, events, and archetype requirements.

    Returns health classification with severity-ranked issues.
    """
    issues = []
    last_fired = pixel_info.get("last_fired_time")
    is_unavailable = pixel_info.get("is_unavailable", False)

    # Check pixel existence and availability
    if is_unavailable:
        issues.append({
            "severity": SEVERITY_CRITICAL,
            "check": "pixel_available",
            "message": "Pixel is marked as unavailable",
            "fix": "Check pixel configuration in Events Manager. May need re-creation.",
        })

    # Check last fired time
    if not last_fired:
        issues.append({
            "severity": SEVERITY_CRITICAL,
            "check": "pixel_ever_fired",
            "message": "Pixel has never fired",
            "fix": "Install pixel on website. Verify base code is on all pages.",
        })
        health = "never_fired"
    else:
        # Parse last fired and check recency
        try:
            fired_dt = datetime.fromisoformat(last_fired.replace("+0000", "+00:00").replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            hours_since = (now - fired_dt).total_seconds() / 3600

            if hours_since > 48:
                issues.append({
                    "severity": SEVERITY_HIGH,
                    "check": "pixel_recency",
                    "message": f"Pixel last fired {hours_since:.0f}h ago (> 48h)",
                    "fix": "Verify website is up and pixel code is still installed.",
                })
        except (ValueError, TypeError):
            pass

        health = "healthy"  # Will be downgraded below if needed

    # Check required events by archetype
    reqs = REQUIRED_EVENTS.get(archetype, REQUIRED_EVENTS["hybrid"])
    events_lower = [e.lower() for e in events_found]

    missing_critical = []
    for ev in reqs["critical"]:
        if ev.lower() not in events_lower:
            missing_critical.append(ev)

    missing_important = []
    for ev in reqs["important"]:
        if ev.lower() not in events_lower:
            missing_important.append(ev)

    if missing_critical:
        issues.append({
            "severity": SEVERITY_CRITICAL,
            "check": "required_events",
            "message": f"Missing critical events for {archetype}: {', '.join(missing_critical)}",
            "fix": f"Install {', '.join(missing_critical)} event(s) on the website. For ecommerce, ensure purchase event fires on order confirmation page with value and currency params.",
        })
        if health != "never_fired":
            health = "degraded"

    if missing_important:
        issues.append({
            "severity": SEVERITY_MEDIUM,
            "check": "important_events",
            "message": f"Missing important events for {archetype}: {', '.join(missing_important)}",
            "fix": f"Add {', '.join(missing_important)} event(s) for better optimization data.",
        })
        if health == "healthy":
            health = "partial"

    # Check diagnostics from da_checks
    for diag in diagnostics:
        result = diag.get("result", "")
        if result == "failed":
            issues.append({
                "severity": SEVERITY_HIGH,
                "check": f"da_check_{diag.get('key', 'unknown')}",
                "message": diag.get("description", diag.get("title", "Diagnostic check failed")),
                "fix": diag.get("action_uri", "Check Events Manager for details."),
            })
            if health == "healthy":
                health = "degraded"

    # If no events at all but pixel fired, it's degraded
    if not events_found and last_fired:
        if health == "healthy":
            health = "degraded"

    # Sort issues by severity
    severity_order = {SEVERITY_CRITICAL: 0, SEVERITY_HIGH: 1, SEVERITY_MEDIUM: 2, SEVERITY_LOW: 3, SEVERITY_INFO: 4}
    issues.sort(key=lambda x: severity_order.get(x["severity"], 5))

    return {
        "health": health,
        "issues": issues,
        "issue_count": len(issues),
        "critical_count": sum(1 for i in issues if i["severity"] == SEVERITY_CRITICAL),
        "events_detected": events_found,
        "archetype": archetype,
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_pixel_info(pixel_id: str) -> dict:
    """
    Get pixel status, connections, last fired time, and availability.

    Args:
        pixel_id: Pixel ID (numeric string).
    """
    api_client._ensure_initialized()

    try:
        result = api_client.graph_get(
            f"/{pixel_id}",
            fields=[
                "id", "name", "creation_time", "last_fired_time",
                "is_unavailable", "is_created_by_business",
            ],
        )

        result["rate_limit_usage_pct"] = api_client.rate_limits.max_usage_pct
        return result

    except MetaAPIError:
        raise


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_pixel_events(pixel_id: str) -> dict:
    """
    Get events received by a pixel in the last 24 hours,
    broken down by event type and hourly counts.

    Args:
        pixel_id: Pixel ID (numeric string).
    """
    api_client._ensure_initialized()

    try:
        result = api_client.graph_get(
            f"/{pixel_id}/stats",
            params={"aggregation": "event"},
        )

        raw_data = result.get("data", [])

        # Aggregate event counts across all time buckets
        event_totals: dict[str, int] = {}
        hourly_buckets = 0
        for bucket in raw_data:
            hourly_buckets += 1
            for event_entry in bucket.get("data", []):
                event_name = event_entry.get("value", "Unknown")
                count = event_entry.get("count", 0)
                event_totals[event_name] = event_totals.get(event_name, 0) + count

        # Sort by count descending
        sorted_events = sorted(event_totals.items(), key=lambda x: x[1], reverse=True)

        return {
            "pixel_id": pixel_id,
            "time_window": "last_24h",
            "hourly_buckets": hourly_buckets,
            "event_count": len(sorted_events),
            "events": [{"event": name, "count": count} for name, count in sorted_events],
            "event_names": [name for name, _ in sorted_events],
            "total_fires": sum(count for _, count in sorted_events),
            "raw_buckets": len(raw_data),
            "rate_limit_usage_pct": api_client.rate_limits.max_usage_pct,
        }

    except MetaAPIError:
        raise


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_event_stats(
    pixel_id: str,
    archetype: str = "hybrid",
) -> dict:
    """
    Get event statistics with archetype-aware diagnostic analysis.

    Checks event coverage, parameter completeness, diagnostic flags,
    and classifies overall tracking health.

    Args:
        pixel_id: Pixel ID (numeric string).
        archetype: Account archetype for requirement matching:
            'ecommerce', 'lead_gen', 'awareness', 'traffic', 'hybrid', 'messages'.
    """
    api_client._ensure_initialized()

    # 1. Get pixel info
    try:
        pixel_info = api_client.graph_get(
            f"/{pixel_id}",
            fields=["id", "name", "last_fired_time", "is_unavailable", "creation_time"],
        )
    except MetaAPIError as e:
        return {
            "pixel_id": pixel_id,
            "error": f"Could not read pixel: {e}",
            "health": "missing",
        }

    # 2. Get events (last 24h)
    events_found: list[str] = []
    event_counts: dict[str, int] = {}
    try:
        stats_result = api_client.graph_get(
            f"/{pixel_id}/stats",
            params={"aggregation": "event"},
        )
        for bucket in stats_result.get("data", []):
            for entry in bucket.get("data", []):
                name = entry.get("value", "Unknown")
                count = entry.get("count", 0)
                event_counts[name] = event_counts.get(name, 0) + count
                if name not in events_found:
                    events_found.append(name)
    except MetaAPIError:
        pass

    # 3. Get diagnostics (da_checks)
    diagnostics: list[dict] = []
    try:
        diag_result = api_client.graph_get(f"/{pixel_id}/da_checks")
        diagnostics = diag_result.get("data", [])
    except MetaAPIError:
        pass

    # 4. Classify health
    classification = _classify_pixel_health(pixel_info, events_found, archetype, diagnostics)

    # 5. Check value parameter coverage for purchase events
    value_coverage = None
    if "Purchase" in events_found or "purchase" in [e.lower() for e in events_found]:
        # We can't check individual event params via stats API alone,
        # but we can flag it as needing verification
        value_coverage = {
            "event": "Purchase",
            "has_value_param": "unknown_from_stats_api",
            "note": "Verify via Test Events or Events Manager that Purchase events include value and currency params.",
        }
        # Check if any da_check flags missing params
        for diag in diagnostics:
            if "missing_param" in diag.get("key", ""):
                value_coverage["has_value_param"] = "likely_missing"
                value_coverage["diagnostic"] = diag.get("description")

    # 6. Build summary
    diagnostic_summary = {
        "pixel_id": pixel_id,
        "pixel_name": pixel_info.get("name"),
        "last_fired": pixel_info.get("last_fired_time"),
        "is_unavailable": pixel_info.get("is_unavailable", False),
        "health": classification["health"],
        "archetype": archetype,
        "events_detected": events_found,
        "event_counts": dict(sorted(event_counts.items(), key=lambda x: x[1], reverse=True)),
        "total_events_24h": sum(event_counts.values()),
        "issues": classification["issues"],
        "issue_count": classification["issue_count"],
        "critical_issues": classification["critical_count"],
        "diagnostics_checked": len(diagnostics),
        "diagnostics_failed": sum(1 for d in diagnostics if d.get("result") == "failed"),
        "value_coverage": value_coverage,
        "rate_limit_usage_pct": api_client.rate_limits.max_usage_pct,
    }

    return diagnostic_summary


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def send_test_event(
    pixel_id: str,
    event_name: str = "PageView",
    test_event_code: Optional[str] = None,
    custom_data: Optional[str] = None,
) -> dict:
    """
    Send a test event via the Conversions API Test Events endpoint.

    Args:
        pixel_id: Pixel ID (numeric string).
        event_name: Event name to send (default 'PageView').
        test_event_code: Test event code from Events Manager.
            If not provided, generates a temporary one.
        custom_data: Optional JSON string of custom_data params
            (e.g., '{"value": 10.00, "currency": "EUR"}').
    """
    api_client._ensure_initialized()
    import json
    import time
    import hashlib

    # Build the event payload per Conversions API spec
    now = int(time.time())
    test_code = test_event_code or f"TEST{now}"

    event_data: dict[str, Any] = {
        "event_name": event_name,
        "event_time": now,
        "action_source": "website",
        "event_source_url": "https://test.example.com/test-event",
        "user_data": {
            "client_ip_address": "0.0.0.0",
            "client_user_agent": "Mozilla/5.0 (Meta Ads MCP Test Event)",
            "em": [hashlib.sha256(f"test_{now}@example.com".encode()).hexdigest()],
        },
    }

    if custom_data:
        try:
            event_data["custom_data"] = json.loads(custom_data)
        except json.JSONDecodeError:
            return {"error": f"Invalid custom_data JSON: {custom_data}"}

    payload = {
        "data": [event_data],
        "test_event_code": test_code,
    }

    try:
        result = api_client.graph_post(
            f"/{pixel_id}/events",
            json_body=payload,
        )

        return {
            "pixel_id": pixel_id,
            "event_name": event_name,
            "test_event_code": test_code,
            "events_received": result.get("events_received"),
            "messages": result.get("messages", []),
            "fbtrace_id": result.get("fbtrace_id"),
            "status": "sent",
            "note": f"Check Events Manager > Test Events tab with code '{test_code}' to verify receipt.",
            "rate_limit_usage_pct": api_client.rate_limits.max_usage_pct,
        }

    except MetaAPIError as e:
        return {
            "pixel_id": pixel_id,
            "event_name": event_name,
            "test_event_code": test_code,
            "status": "failed",
            "error": str(e),
        }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def run_tracking_diagnostic(
    account_id: str,
    archetype: str = "hybrid",
) -> dict:
    """
    Run comprehensive tracking diagnostic for an ad account.

    Checks all connected pixels, event coverage, parameter completeness,
    and Meta diagnostic flags. Returns archetype-aware health classification
    with severity-ranked issues and fix suggestions.

    Args:
        account_id: Ad account ID (e.g., 'act_123456789').
        archetype: Account archetype for requirement matching.
    """
    api_client._ensure_initialized()
    account_id = ensure_account_id_format(account_id)

    # 1. Get connected pixels
    try:
        pixel_result = api_client.graph_get(
            f"/{account_id}/adspixels",
            fields=["id", "name", "last_fired_time", "is_unavailable", "creation_time"],
        )
        pixels = pixel_result.get("data", [])
    except MetaAPIError as e:
        return {
            "account_id": account_id,
            "error": f"Could not read pixels: {e}",
            "health": "missing",
            "pixels": [],
        }

    if not pixels:
        severity = SEVERITY_CRITICAL if archetype in ("ecommerce", "lead_gen") else SEVERITY_MEDIUM
        return {
            "account_id": account_id,
            "archetype": archetype,
            "health": "missing",
            "pixels": [],
            "pixel_count": 0,
            "issues": [{
                "severity": severity,
                "check": "pixel_exists",
                "message": "No pixel connected to this ad account",
                "fix": "Create a pixel in Events Manager and connect it to this ad account.",
            }],
            "rate_limit_usage_pct": api_client.rate_limits.max_usage_pct,
        }

    # 2. Diagnose each pixel
    pixel_reports = []
    worst_health = "healthy"
    all_issues = []

    for pixel in pixels:
        pid = pixel.get("id")
        report = get_event_stats(pid, archetype=archetype)
        pixel_reports.append(report)
        all_issues.extend(report.get("issues", []))

        # Track worst health
        health = report.get("health", "unknown")
        health_order = {"healthy": 0, "partial": 1, "degraded": 2, "never_fired": 3, "missing": 4}
        if health_order.get(health, 5) > health_order.get(worst_health, 0):
            worst_health = health

    # 3. Objective-tracking alignment check
    # Get active campaigns to check objective vs tracking
    try:
        camp_result = api_client.graph_get(
            f"/{account_id}/campaigns",
            fields=["id", "name", "objective", "effective_status"],
            params={
                "limit": "20",
                "filtering": '[{"field":"effective_status","operator":"IN","value":["ACTIVE"]}]',
            },
        )
        active_campaigns = camp_result.get("data", [])
    except MetaAPIError:
        active_campaigns = []

    # Check for mismatches
    all_detected_events = set()
    for pr in pixel_reports:
        all_detected_events.update(pr.get("events_detected", []))

    alignment_warnings = []
    for camp in active_campaigns:
        obj = camp.get("objective", "")
        name = camp.get("name", "")
        if obj == "OUTCOME_SALES" and "Purchase" not in all_detected_events:
            alignment_warnings.append({
                "severity": SEVERITY_HIGH,
                "check": "objective_tracking_alignment",
                "message": f"Campaign '{name}' has OUTCOME_SALES objective but no Purchase events detected on pixel",
                "fix": "Install Purchase event on order confirmation page, or change campaign objective.",
            })
        elif obj == "OUTCOME_LEADS" and "Lead" not in all_detected_events:
            alignment_warnings.append({
                "severity": SEVERITY_HIGH,
                "check": "objective_tracking_alignment",
                "message": f"Campaign '{name}' has OUTCOME_LEADS objective but no Lead events detected on pixel",
                "fix": "Install Lead event on form submission, or use instant forms.",
            })

    all_issues.extend(alignment_warnings)

    # Sort all issues by severity
    severity_order = {SEVERITY_CRITICAL: 0, SEVERITY_HIGH: 1, SEVERITY_MEDIUM: 2, SEVERITY_LOW: 3, SEVERITY_INFO: 4}
    all_issues.sort(key=lambda x: severity_order.get(x["severity"], 5))

    return {
        "account_id": account_id,
        "archetype": archetype,
        "health": worst_health,
        "pixel_count": len(pixels),
        "pixels": pixel_reports,
        "active_campaigns": len(active_campaigns),
        "all_detected_events": sorted(all_detected_events),
        "issues": all_issues,
        "issue_count": len(all_issues),
        "critical_issues": sum(1 for i in all_issues if i["severity"] == SEVERITY_CRITICAL),
        "alignment_warnings": len(alignment_warnings),
        "rate_limit_usage_pct": api_client.rate_limits.max_usage_pct,
    }


# --- Dataset Quality API (event match quality, dedupe, coverage, freshness) ---

# Operator heuristics for flagging, NOT Meta-published thresholds. Meta's own targets
# (e.g. event coverage goal_percentage) are read from the API response where available.
EMQ_HIGH_BELOW = 4.0      # < 4/10: weak matching, attribution and optimization suffer
EMQ_MEDIUM_BELOW = 6.0    # < 6/10: room to improve with more customer-info parameters
DEDUPE_KEY_MIN_PCT = 90.0  # event_id should be on (nearly) every browser and server event

# One entry per metric group so a rejected field in one group cannot hide the rest.
_DQ_GROUPS: dict[str, list[str]] = {
    "event_match_quality": [
        "event_match_quality{composite_score,match_key_feedback{identifier,coverage{percentage}}}",
    ],
    "diagnostics": ["event_match_quality{diagnostics}"],
    "acr": ["acr{percentage,description}"],
    "event_coverage": ["event_coverage{percentage,goal_percentage,description}"],
    # Meta's docs spell this `dedupe_key_feedback` in queries and `dedup_key_feedback` in
    # the field table; try the query spelling first, then the table spelling.
    "dedupe": [
        "dedupe_key_feedback{dedupe_key,browser_events_with_dedupe_key{percentage},"
        "server_events_with_dedupe_key{percentage},overall_browser_coverage_from_dedupe_key{percentage}}",
        "dedup_key_feedback{dedupe_key,browser_events_with_dedupe_key{percentage},"
        "server_events_with_dedupe_key{percentage},overall_browser_coverage_from_dedupe_key{percentage}}",
    ],
    "data_freshness": ["data_freshness{upload_frequency,description}"],
}


# Everything in one request (the happy path). EMQ and its diagnostics share one selection.
_DQ_COMBINED: list[str] = [
    "event_match_quality{composite_score,match_key_feedback{identifier,coverage{percentage}},diagnostics}",
    _DQ_GROUPS["acr"][0],
    _DQ_GROUPS["event_coverage"][0],
    _DQ_GROUPS["dedupe"][0],
    _DQ_GROUPS["data_freshness"][0],
]


def _dq_query(dataset_id: str, selections: list[str], agent_name: Optional[str]) -> list[dict]:
    """Run one /dataset_quality request and return its `web` array."""
    params = {"dataset_id": dataset_id}
    if agent_name:
        params["agent_name"] = agent_name
    fields = "web{" + ",".join(selections + ["event_name"]) + "}"
    result = api_client.graph_get("/dataset_quality", params=params, fields=[fields])
    web = result.get("web", [])
    return web if isinstance(web, list) else []


def _merge_events(target: dict[str, dict], web: list[dict]) -> None:
    for entry in web:
        name = entry.get("event_name")
        if not name:
            continue
        merged = target.setdefault(name, {"event_name": name})
        for key, value in entry.items():
            if key == "event_name":
                continue
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)  # e.g. event_match_quality from two groups
            else:
                merged[key] = value


def _assess_event(event: dict) -> list[dict]:
    """Turn one event's raw quality data into severity-ranked issues."""
    issues: list[dict] = []
    name = event["event_name"]

    emq = event.get("event_match_quality") or {}
    score = emq.get("composite_score")
    if isinstance(score, (int, float)):
        if score < EMQ_MEDIUM_BELOW:
            issues.append({
                "severity": SEVERITY_HIGH if score < EMQ_HIGH_BELOW else SEVERITY_MEDIUM,
                "check": "event_match_quality",
                "event": name,
                "message": f"{name}: event match quality {score}/10",
                "fix": (
                    "Send more customer-information parameters through the Conversions API "
                    "(hashed email, phone, external_id, client IP and user agent, fbp/fbc)."
                ),
            })

    coverage = event.get("event_coverage") or {}
    pct, goal = coverage.get("percentage"), coverage.get("goal_percentage")
    if isinstance(pct, (int, float)) and isinstance(goal, (int, float)) and pct < goal:
        issues.append({
            "severity": SEVERITY_MEDIUM,
            "check": "event_coverage",
            "event": name,
            "message": f"{name}: Conversions API covers {pct}% of Pixel events (Meta goal {goal}%)",
            "fix": "Send the same events from the server with a shared event_id so they deduplicate against the Pixel.",
        })

    dedupe_rows = event.get("dedupe_key_feedback") or event.get("dedup_key_feedback") or []
    for row in dedupe_rows if isinstance(dedupe_rows, list) else []:
        if row.get("dedupe_key") != "event_id":
            continue
        browser = (row.get("browser_events_with_dedupe_key") or {}).get("percentage")
        server = (row.get("server_events_with_dedupe_key") or {}).get("percentage")
        for side, value in (("browser", browser), ("server", server)):
            if isinstance(value, (int, float)) and value < DEDUPE_KEY_MIN_PCT:
                issues.append({
                    "severity": SEVERITY_MEDIUM,
                    "check": "dedupe_event_id",
                    "event": name,
                    "message": f"{name}: only {value}% of {side} events carry event_id",
                    "fix": "Pass the same event_id from the browser Pixel and the server event so Meta can deduplicate.",
                })

    freshness = (event.get("data_freshness") or {}).get("upload_frequency")
    if freshness and str(freshness).lower() not in ("real_time", "realtime"):
        issues.append({
            "severity": SEVERITY_LOW,
            "check": "data_freshness",
            "event": name,
            "message": f"{name}: events arrive {freshness}, not in real time",
            "fix": "Send server events as close to real time as possible.",
        })

    return issues


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_dataset_quality(
    pixel_id: str,
    event_name: Optional[str] = None,
    agent_name: Optional[str] = None,
) -> dict:
    """
    Get Meta's Dataset Quality for a pixel/dataset: event match quality (EMQ, 0-10) with
    per-parameter match-key coverage, Conversions API event coverage, deduplication-key
    coverage, additional conversions reported (ACR), data freshness, and Meta's own
    diagnostics. Web events only.

    Needs a token whose user has "Use events dataset" access on the pixel. Meta
    recommends a long-lived system user token; client system user tokens are not supported.

    Args:
        pixel_id: Pixel / dataset ID (numeric string).
        event_name: Only return this event (e.g., 'Purchase'). Default: all events.
        agent_name: Only count events sent with this partner_agent. Normally leave unset.
    """
    api_client._ensure_initialized()
    pixel_id = str(pixel_id).strip()
    if not pixel_id.isdigit():
        return {
            "error": "pixel_id must be a numeric pixel/dataset ID",
            "blocked_at": "input_validation",
        }

    events: dict[str, dict] = {}
    unavailable: dict[str, str] = {}
    mode = "combined"

    try:
        _merge_events(events, _dq_query(pixel_id, _DQ_COMBINED, agent_name))
    except MetaAPIError as e:
        if e.error_code != 100:  # 100 = invalid field/param; anything else won't be fixed by retrying per group
            return _dataset_quality_error(pixel_id, e)
        # A field in the combined request was rejected: query each group on its own.
        mode = "per_metric_fallback"
        events.clear()
        last_error: Optional[MetaAPIError] = None
        for group, variants in _DQ_GROUPS.items():
            for selection in variants:
                try:
                    _merge_events(events, _dq_query(pixel_id, [selection], agent_name))
                    unavailable.pop(group, None)
                    break
                except MetaAPIError as inner:
                    last_error = inner
                    unavailable[group] = str(inner)
        if not events and last_error is not None:
            return _dataset_quality_error(pixel_id, last_error)

    event_list = sorted(events.values(), key=lambda ev: ev["event_name"])
    if event_name:
        event_list = [ev for ev in event_list if ev["event_name"].lower() == event_name.lower()]

    issues: list[dict] = []
    for ev in event_list:
        ev["issues"] = _assess_event(ev)
        issues.extend(ev["issues"])
    severity_order = {SEVERITY_CRITICAL: 0, SEVERITY_HIGH: 1, SEVERITY_MEDIUM: 2, SEVERITY_LOW: 3, SEVERITY_INFO: 4}
    issues.sort(key=lambda i: severity_order.get(i["severity"], 5))

    scores = [
        (ev["event_name"], ev["event_match_quality"]["composite_score"])
        for ev in event_list
        if isinstance((ev.get("event_match_quality") or {}).get("composite_score"), (int, float))
    ]
    response: dict[str, Any] = {
        "pixel_id": pixel_id,
        "event_count": len(event_list),
        "lowest_match_quality": (
            {"event": min(scores, key=lambda s: s[1])[0], "score": min(s[1] for s in scores)}
            if scores else None
        ),
        "issue_count": len(issues),
        "issues": issues,
        "events": event_list,
        "query_mode": mode,
        "rate_limit_usage_pct": api_client.rate_limits.max_usage_pct,
    }
    if unavailable:
        response["unavailable_metrics"] = unavailable
    if not event_list:
        response["note"] = (
            "Meta returned no quality data. EMQ needs Conversions API events (web) for this dataset; "
            "pixel-only datasets, or datasets with no recent server events, return nothing."
        )
    return response


def _dataset_quality_error(pixel_id: str, error: MetaAPIError) -> dict:
    hint = (
        "Dataset Quality needs a user/system user with 'Use events dataset' access on this pixel and an "
        "app with ads_read plus ads_management or business_management. Client system user tokens are "
        "not supported."
    )
    if error.error_code in (190, 102):
        hint = "The access token is expired or invalid."
    return {
        "pixel_id": pixel_id,
        "error": str(error),
        "error_code": error.error_code,
        "hint": hint,
    }


# --- Event volume history (/{pixel}/stats) ---

# Meta keeps about 7 days of pixel stats ("seven days from the request time"), and today is a partial
# day, so 6 complete days is the most that can be requested. (Meta's own connector advertises 28 days;
# the public stats service documents 7.)
MAX_STATS_DAYS = 6
MAX_STATS_PAGES = 10
# Operator heuristics for flagging, NOT Meta-published thresholds.
MIN_DAILY_EVENTS_FOR_DROP = 20  # below this, day-to-day swings are noise
DROP_MEDIUM_PCT = 50.0          # last complete day this far below the prior days' average
DROP_HIGH_PCT = 80.0
MIN_EVENTS_FOR_SPLIT_FLAG = 100  # window total before a missing web/server channel is worth mentioning
CORE_EVENTS = ("Purchase", "Lead")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _bucket_date(bucket: dict) -> Optional[str]:
    raw = bucket.get("start_time")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("+0000", "+00:00").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).date().isoformat()


def _stats_buckets(pixel_id: str, params: dict) -> list[dict]:
    """All hourly stats buckets for a request, following cursors."""
    buckets: list[dict] = []
    params = dict(params)
    for _ in range(MAX_STATS_PAGES):
        res = api_client.graph_get(f"/{pixel_id}/stats", params=params)
        buckets.extend(b for b in res.get("data", []) if isinstance(b, dict))
        paging = res.get("paging") or {}
        cursor = (paging.get("cursors") or {}).get("after")
        if not paging.get("next") or not cursor:
            break
        params["after"] = cursor
    return buckets


def _daily_event_counts(buckets: list[dict]) -> dict[str, dict[str, int]]:
    """{event: {YYYY-MM-DD (UTC): count}} from hourly buckets."""
    out: dict[str, dict[str, int]] = {}
    for bucket in buckets:
        day = _bucket_date(bucket)
        if not day:
            continue
        for item in bucket.get("data", []) or []:
            name = item.get("value")
            try:
                count = int(float(item.get("count", 0)))
            except (TypeError, ValueError):
                continue
            if name:
                per_day = out.setdefault(str(name), {})
                per_day[day] = per_day.get(day, 0) + count
    return out


def _assess_event_volume(name: str, per_day: dict[str, int], days: list[str], split: Optional[dict]) -> list[dict]:
    issues: list[dict] = []
    last, prior = per_day[days[-1]], [per_day[d] for d in days[:-1]]
    if len(prior) >= 3:
        prior_avg = sum(prior) / len(prior)
        if prior_avg >= MIN_DAILY_EVENTS_FOR_DROP:
            drop = (prior_avg - last) / prior_avg * 100
            if drop >= DROP_MEDIUM_PCT:
                issues.append({
                    "severity": SEVERITY_HIGH if drop >= DROP_HIGH_PCT else SEVERITY_MEDIUM,
                    "check": "volume_drop", "event": name,
                    "message": f"{name}: {last} events on {days[-1]} vs {prior_avg:.0f}/day over the {len(prior)} days before ({-drop:.0f}%)",
                    "fix": "Check the pixel and Conversions API sending this event: a site change, a consent banner or a broken integration are the usual causes.",
                })
    if split:
        web, server = split["web"], split["server"]
        if web >= MIN_EVENTS_FOR_SPLIT_FLAG and server == 0:
            issues.append({
                "severity": SEVERITY_MEDIUM if name in CORE_EVENTS else SEVERITY_INFO,
                "check": "no_server_events", "event": name,
                "message": f"{name}: {web} browser events and no server (Conversions API) events in the window",
                "fix": "Send this event through the Conversions API too, with a shared event_id so Meta can deduplicate it.",
            })
        elif server >= MIN_EVENTS_FOR_SPLIT_FLAG and web == 0:
            issues.append({
                "severity": SEVERITY_INFO, "check": "no_browser_events", "event": name,
                "message": f"{name}: {server} server events and no browser events in the window",
                "fix": "Fine for a server-only setup. Otherwise check that the browser pixel fires this event.",
            })
    return issues


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_dataset_stats(
    pixel_id: str,
    days: int = MAX_STATS_DAYS,
    events: Optional[str] = None,
    top_n: int = 15,
    include_source_split: bool = True,
) -> dict:
    """
    Event volume received by a pixel/dataset over the last few complete days, per event and per day,
    with a browser (pixel) vs server (Conversions API) split and flags for sudden drops.

    Meta keeps about 7 days of pixel stats, so at most 6 complete days (today, a partial day, is
    reported separately). Days are UTC. Counts are events received from each source; use
    get_dataset_quality for deduplication and match quality.

    Args:
        pixel_id: Pixel / dataset ID (numeric string).
        days: Complete days to include, 1-6 (default 6).
        events: Comma-separated event names to show (e.g. 'Purchase,Lead'). Default: the top events by volume.
        top_n: How many events to show when `events` is not given (default 15, max 50).
        include_source_split: Also split each event into browser vs server counts (2 extra calls).
    """
    api_client._ensure_initialized()
    pixel_id = str(pixel_id).strip()
    if not pixel_id.isdigit():
        return {"error": "pixel_id must be a numeric pixel/dataset ID", "blocked_at": "input_validation"}
    if not 1 <= int(days) <= MAX_STATS_DAYS:
        return {"error": f"days must be between 1 and {MAX_STATS_DAYS}: Meta keeps about 7 days of pixel stats",
                "blocked_at": "input_validation"}
    days = int(days)
    top_n = max(1, min(int(top_n), 50))

    now = _utc_now()
    today = now.date()
    day_list = [(today - timedelta(days=i)).isoformat() for i in range(days, 0, -1)]  # oldest first, complete days
    start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc) - timedelta(days=days)
    base = {"aggregation": "event", "start_time": str(int(start.timestamp())), "end_time": str(int(now.timestamp()))}

    try:
        counts = _daily_event_counts(_stats_buckets(pixel_id, base))
    except MetaAPIError as e:
        return {
            "pixel_id": pixel_id, "error": str(e), "error_code": e.error_code,
            "hint": "Reading pixel stats needs ads_read and access to this pixel; Meta returns only about the last 7 days.",
        }

    notes: list[str] = []
    split_totals: dict[str, dict[str, int]] = {}
    if include_source_split and counts:
        sources = {}
        try:
            for label, source in (("web", "WEB_ONLY"), ("server", "SERVER_ONLY")):
                per_event = _daily_event_counts(_stats_buckets(pixel_id, {**base, "event_source": source}))
                sources[label] = {n: sum(d.get(day, 0) for day in day_list) for n, d in per_event.items()}
            for name in counts:
                split_totals[name] = {"web": sources["web"].get(name, 0), "server": sources["server"].get(name, 0)}
        except MetaAPIError as e:
            notes.append(f"Browser vs server split unavailable: {e}")

    wanted = [e.strip().lower() for e in events.split(",") if e.strip()] if events else None
    shown = []
    for name, per_day_raw in counts.items():
        if wanted is not None and name.lower() not in wanted:
            continue
        per_day = {d: per_day_raw.get(d, 0) for d in day_list}
        shown.append((name, per_day, per_day_raw.get(today.isoformat(), 0)))
    shown.sort(key=lambda item: -sum(item[1].values()))
    if wanted is None:
        shown = shown[:top_n]

    issues: list[dict] = []
    event_reports = []
    for name, per_day, today_so_far in shown:
        total = sum(per_day.values())
        split = split_totals.get(name)
        entry: dict[str, Any] = {
            "event": name, "total": total, "avg_per_day": round(total / days, 1),
            "per_day": per_day, "today_so_far": today_so_far,
        }
        if split:
            entry["by_source"] = {**split, "server_share_pct": (round(split["server"] / (split["web"] + split["server"]) * 100, 1)
                                                                 if split["web"] + split["server"] else None)}
        entry_issues = _assess_event_volume(name, per_day, day_list, split)
        issues.extend(entry_issues)
        event_reports.append(entry)

    order = {SEVERITY_CRITICAL: 0, SEVERITY_HIGH: 1, SEVERITY_MEDIUM: 2, SEVERITY_LOW: 3, SEVERITY_INFO: 4}
    issues.sort(key=lambda i: order.get(i["severity"], 5))
    response: dict[str, Any] = {
        "pixel_id": pixel_id,
        "window": {"from": day_list[0], "to": day_list[-1], "days": days, "timezone": "UTC", "today_partial": today.isoformat()},
        "event_count": len(event_reports),
        "issue_count": len(issues),
        "issues": issues,
        "events": event_reports,
        "rate_limit_usage_pct": api_client.rate_limits.max_usage_pct,
    }
    if notes:
        response["notes"] = notes
    if not counts:
        response["note"] = ("Meta returned no events for this window. The pixel may be new or inactive, "
                            "or the token may not have access to it.")
    elif wanted is not None and not event_reports:
        response["note"] = f"None of the requested events were received: {events}"
    return response
