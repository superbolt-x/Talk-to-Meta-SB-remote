"""
Tracking validation checks (Category C).

Validates pixel connection, event presence, parameter completeness,
launch-blocking diagnostics, and catalog connections.

Note: Hard enforcement lives in engine/tracking_gate.py. This module provides
advisory checks from the accounts.yaml configuration layer (no live API calls).
"""
import logging
import os
from typing import Optional

import yaml

logger = logging.getLogger("meta-ads-mcp.validators.tracking")

# Required pixel events per archetype (minimum viable tracking)
ARCHETYPE_REQUIRED_EVENTS = {
    "ecommerce": ["PageView", "Purchase"],
    "lead_gen": ["PageView", "Lead"],
    "hybrid": ["PageView"],
    "local": ["PageView"],
    "saas": ["PageView", "Lead"],
}

# Pixel health states that block launch
BLOCKING_PIXEL_HEALTH = {"missing", "critical", "disabled"}

# Pixel health states that generate warnings
WARN_PIXEL_HEALTH = {"degraded", "unknown", "stale"}

CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "config", "accounts.yaml"
)


def _load_account_config(account_id: str) -> Optional[dict]:
    """Load account entry from accounts.yaml by account_id."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        clients = config.get("clients", {})
        for slug, data in clients.items():
            if data.get("account_id") == account_id:
                return data
    except (FileNotFoundError, yaml.YAMLError) as e:
        logger.warning("Could not load accounts.yaml: %s", e)
    return None


def validate_pixel_readiness(account_id: str, archetype: str) -> dict:
    """
    Validate pixel and event tracking readiness for an account.

    Reads from accounts.yaml configuration. Does NOT make live API calls.
    For live pixel diagnostics use engine/tracking_gate.py.

    Args:
        account_id: The ad account ID (e.g. "act_12345").
        archetype: Account archetype ("ecommerce", "lead_gen", "hybrid", etc.).

    Returns dict with: valid (bool), issues (list), warnings (list),
    pixel_id (str or None), events_found (list), events_required (list).
    """
    issues = []
    warnings = []

    # --- Load account config ---
    account = _load_account_config(account_id)

    if account is None:
        warnings.append(
            f"Account '{account_id}' not found in accounts.yaml - "
            "pixel readiness cannot be verified from config"
        )
        return {
            "valid": True,  # Don't block - account may not be in config yet
            "issues": issues,
            "warnings": warnings,
            "pixel_id": None,
            "events_found": [],
            "events_required": ARCHETYPE_REQUIRED_EVENTS.get(archetype.lower(), ["PageView"]),
        }

    pixel_id = account.get("pixel_id")
    pixel_status = account.get("pixel_status", {})
    archetype_key = archetype.lower() if archetype else ""

    # --- Pixel ID present ---
    if not pixel_id:
        issues.append(
            f"No pixel_id configured for account '{account_id}'. "
            "Add pixel_id to accounts.yaml before launching campaigns."
        )
        return {
            "valid": False,
            "issues": issues,
            "warnings": warnings,
            "pixel_id": None,
            "events_found": [],
            "events_required": ARCHETYPE_REQUIRED_EVENTS.get(archetype_key, ["PageView"]),
        }

    # --- Pixel health ---
    pixel_health = pixel_status.get("health", "unknown")
    if pixel_health in BLOCKING_PIXEL_HEALTH:
        issues.append(
            f"Pixel {pixel_id} health is '{pixel_health}' - "
            "fix pixel before launching campaigns"
        )
    elif pixel_health in WARN_PIXEL_HEALTH:
        warnings.append(
            f"Pixel {pixel_id} health is '{pixel_health}' - "
            "verify pixel is firing correctly before launch"
        )

    # --- Required events check ---
    events_found = pixel_status.get("events", [])
    events_required = ARCHETYPE_REQUIRED_EVENTS.get(archetype_key, ["PageView"])

    missing_events = [e for e in events_required if e not in events_found]
    if missing_events:
        if pixel_health in BLOCKING_PIXEL_HEALTH:
            issues.append(
                f"Missing required events for '{archetype}' archetype: {missing_events}"
            )
        else:
            warnings.append(
                f"Required events for '{archetype}' archetype not confirmed in config: "
                f"{missing_events}. Verify these events are firing via Events Manager."
            )

    # --- Catalog check for ecommerce ---
    if archetype_key == "ecommerce":
        catalog_id = account.get("catalog_id")
        if not catalog_id:
            warnings.append(
                "Ecommerce archetype detected but no catalog_id in accounts.yaml. "
                "DPA campaigns require a connected catalog."
            )

    # --- Launch readiness blockers from config ---
    readiness = account.get("readiness", {})
    blockers = readiness.get("blockers", [])
    if blockers:
        for blocker in blockers:
            issues.append(f"Launch blocker in config: {blocker}")

    config_warnings = readiness.get("warnings", [])
    for w in config_warnings:
        warnings.append(f"Config warning: {w}")

    return {
        "valid": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
        "pixel_id": pixel_id,
        "events_found": events_found,
        "events_required": events_required,
    }
