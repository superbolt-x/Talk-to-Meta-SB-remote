"""
Duplicate prevention for creative and campaign objects.

Checks manifest layer and vault memory before creating new objects.
Prevents accidental duplicate ads.

Two layers:
- Layer 1: Local manifest files (fast, no API)
- Layer 2: Vault creative-intelligence.md (text search)
"""
import json
import logging
import os
from typing import Optional

logger = logging.getLogger("meta-ads-mcp.safety.duplicate_checker")

# Vault base path (same resolution as engine/storage.py)
VAULT_BASE = os.environ.get("VAULT_PATH", os.path.expanduser("~/marketing-vault"))


def check_for_duplicate(
    logical_creative_id: str,
    client_slug: str,
    campaign_id: Optional[str] = None,
    adset_id: Optional[str] = None,
) -> dict:
    """
    Check all available layers for duplicate creatives before ad creation.

    Args:
        logical_creative_id: The logical creative identifier to check.
        client_slug: Client slug for vault path resolution.
        campaign_id: Optional campaign ID (Layer 3 - not yet implemented).
        adset_id: Optional ad set ID (Layer 3 - not yet implemented).

    Returns dict with: duplicate_found (bool), layers_checked (list),
    matches (list of location dicts), recommendation (str).
    """
    matches = []
    layers_checked = []

    # --- Layer 1: Local manifest files ---
    layers_checked.append("local_manifests")
    manifest_matches = _check_manifest_layer(logical_creative_id)
    matches.extend(manifest_matches)

    # --- Layer 2: Vault creative-intelligence.md ---
    layers_checked.append("vault")
    vault_matches = _check_vault_layer(logical_creative_id, client_slug)
    matches.extend(vault_matches)

    # --- Layer 3: API campaign check - not implemented (requires live token) ---
    if campaign_id or adset_id:
        logger.debug(
            "Layer 3 (API campaign check) not implemented - "
            "skipping for campaign_id=%s adset_id=%s",
            campaign_id,
            adset_id,
        )

    duplicate_found = len(matches) > 0

    if duplicate_found:
        recommendation = (
            f"Logical creative '{logical_creative_id}' already exists in "
            f"{len(matches)} location(s). Review existing creatives before "
            "creating a new one to avoid duplication."
        )
    else:
        recommendation = (
            f"No duplicate found for '{logical_creative_id}' - safe to proceed."
        )

    return {
        "status": "ok",
        "duplicate_found": duplicate_found,
        "layers_checked": layers_checked,
        "matches": matches,
        "recommendation": recommendation,
    }


def _check_manifest_layer(logical_creative_id: str) -> list:
    """Search local manifest JSON files for the given logical_creative_id."""
    matches = []
    search_root = os.getcwd()

    for root, dirs, files in os.walk(search_root):
        # Skip hidden dirs and irrelevant dirs
        dirs[:] = [
            d for d in dirs
            if not d.startswith(".")
            and d not in {"node_modules", "__pycache__", ".git", "dist", ".venv", "venv"}
        ]
        for fname in files:
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if "creatives" not in data or "manifest_version" not in data:
                    continue
                for creative in data.get("creatives", []):
                    if creative.get("logical_creative_id") == logical_creative_id:
                        matches.append({
                            "layer": "manifest",
                            "path": fpath,
                            "client_slug": data.get("client_slug", "unknown"),
                            "creative_group_id": data.get("creative_group_id"),
                        })
            except (json.JSONDecodeError, OSError):
                continue

    return matches


def _check_vault_layer(logical_creative_id: str, client_slug: str) -> list:
    """Search vault creative-intelligence.md for the logical_creative_id."""
    matches = []

    # Primary location: 01_CLIENTS/{slug}/creative-intelligence.md
    vault_path = os.path.join(
        VAULT_BASE, "01_CLIENTS", client_slug, "creative-intelligence.md"
    )

    # Fallback: 06_INTELLIGENCE/Video Analysis Cache.md
    fallback_paths = [
        os.path.join(VAULT_BASE, "06_INTELLIGENCE", "Video Analysis Cache.md"),
        os.path.join(VAULT_BASE, "06_INTELLIGENCE", "Performance Intelligence.md"),
    ]

    all_paths = [vault_path] + fallback_paths

    for path in all_paths:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            if logical_creative_id in content:
                matches.append({
                    "layer": "vault",
                    "path": path,
                    "note": f"logical_creative_id '{logical_creative_id}' found in vault file",
                })
        except OSError:
            continue

    return matches
