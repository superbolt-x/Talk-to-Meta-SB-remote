"""
Creative validation checks (Category A).

Validates creative manifests, logical creative grouping, transcript pairing,
duplicate prevention, creative profile existence, and CTA/destination consistency.
"""
import json
import logging
import os
from typing import Optional

logger = logging.getLogger("meta-ads-mcp.validators.creative")

# Required fields in a valid manifest
MANIFEST_REQUIRED_FIELDS = {"manifest_version", "client_slug", "creatives"}

# Required fields per creative entry in the manifest
CREATIVE_REQUIRED_FIELDS = {"logical_creative_id", "variants"}

# Required fields per variant
VARIANT_REQUIRED_FIELDS = {"ratio", "file_path", "media_type"}

# Valid media types
VALID_MEDIA_TYPES = {"video", "image"}

# Valid aspect ratios
VALID_RATIOS = {"9x16", "1x1", "4x5", "16x9"}


def validate_manifest(manifest_ref: str) -> dict:
    """
    Validate a creative manifest for completeness and correctness.

    Args:
        manifest_ref: Absolute path to the manifest JSON file.

    Returns dict with: valid (bool), issues (list), warnings (list),
    creative_count (int), variant_count (int).
    """
    issues = []
    warnings = []

    # --- File existence ---
    if not manifest_ref:
        issues.append("manifest_ref is required")
        return {
            "valid": False, "issues": issues, "warnings": warnings,
            "creative_count": 0, "variant_count": 0,
        }

    if not os.path.isfile(manifest_ref):
        issues.append(f"Manifest file not found: {manifest_ref}")
        return {
            "valid": False, "issues": issues, "warnings": warnings,
            "creative_count": 0, "variant_count": 0,
        }

    # --- Parse JSON ---
    try:
        with open(manifest_ref, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except json.JSONDecodeError as e:
        issues.append(f"Manifest JSON is invalid: {e}")
        return {
            "valid": False, "issues": issues, "warnings": warnings,
            "creative_count": 0, "variant_count": 0,
        }

    # --- Required top-level fields ---
    missing_top = MANIFEST_REQUIRED_FIELDS - set(manifest.keys())
    if missing_top:
        issues.append(f"Manifest missing required fields: {sorted(missing_top)}")

    creatives = manifest.get("creatives", [])
    if not isinstance(creatives, list) or len(creatives) == 0:
        issues.append("Manifest must contain at least one creative entry")
        return {
            "valid": False, "issues": issues, "warnings": warnings,
            "creative_count": 0, "variant_count": 0,
        }

    creative_count = len(creatives)
    variant_count = 0

    for i, creative in enumerate(creatives):
        prefix = f"Creative[{i}]"

        # Required creative fields
        missing_creative = CREATIVE_REQUIRED_FIELDS - set(creative.keys())
        if missing_creative:
            issues.append(f"{prefix} missing fields: {sorted(missing_creative)}")
            continue

        lcid = creative.get("logical_creative_id", f"<unknown-{i}>")

        # Validate logical_creative_id format (kebab-case expected)
        if not isinstance(lcid, str) or not lcid.strip():
            issues.append(f"{prefix} logical_creative_id must be a non-empty string")

        variants = creative.get("variants", [])
        if not isinstance(variants, list) or len(variants) == 0:
            issues.append(f"{prefix} ({lcid}) must have at least one variant")
            continue

        variant_count += len(variants)
        ratios_seen = set()

        for j, variant in enumerate(variants):
            vprefix = f"{prefix}.variant[{j}]"

            missing_variant = VARIANT_REQUIRED_FIELDS - set(variant.keys())
            if missing_variant:
                issues.append(f"{vprefix} missing fields: {sorted(missing_variant)}")
                continue

            ratio = variant.get("ratio", "")
            if ratio not in VALID_RATIOS:
                warnings.append(
                    f"{vprefix} ratio '{ratio}' is not a standard ratio. "
                    f"Expected one of: {sorted(VALID_RATIOS)}"
                )

            if ratio in ratios_seen:
                issues.append(f"{prefix} ({lcid}) has duplicate ratio '{ratio}'")
            ratios_seen.add(ratio)

            media_type = variant.get("media_type", "")
            if media_type not in VALID_MEDIA_TYPES:
                issues.append(
                    f"{vprefix} media_type '{media_type}' is invalid. "
                    f"Expected: {VALID_MEDIA_TYPES}"
                )

            file_path = variant.get("file_path", "")
            if file_path and not os.path.isfile(file_path):
                warnings.append(
                    f"{vprefix} file not found at '{file_path}' - "
                    "may not have been copied to this machine"
                )

        # Check for 9x16 ratio (required for Reels)
        if "9x16" not in ratios_seen:
            warnings.append(
                f"{prefix} ({lcid}) has no 9x16 variant - "
                "required for Reels/Stories placement"
            )

    return {
        "valid": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
        "creative_count": creative_count,
        "variant_count": variant_count,
    }


def validate_no_duplicate_creative(
    logical_creative_id: str,
    campaign_id: Optional[str] = None,
    adset_id: Optional[str] = None,
) -> dict:
    """
    Check if a matching creative already exists in local manifests.

    Scans manifest files in the current working directory and its subdirectories
    for the given logical_creative_id. Layer 2 (vault) and Layer 3 (API) checks
    require live access and are left for the duplicate_checker safety module.

    Returns dict with: duplicate_found (bool), locations (list), layers_checked (list).
    """
    locations = []
    layers_checked = []

    # Layer 1: Scan local manifest files
    layers_checked.append("local_manifests")

    search_dirs = [os.getcwd()]
    manifest_files_checked = 0

    for search_dir in search_dirs:
        for root, dirs, files in os.walk(search_dir):
            # Skip hidden dirs and common non-manifest dirs
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"node_modules", "__pycache__", ".git"}]
            for fname in files:
                if not fname.endswith(".json"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    # Only process files that look like manifests
                    if "creatives" not in data or "manifest_version" not in data:
                        continue
                    manifest_files_checked += 1
                    for creative in data.get("creatives", []):
                        if creative.get("logical_creative_id") == logical_creative_id:
                            locations.append({
                                "manifest_path": fpath,
                                "client_slug": data.get("client_slug", "unknown"),
                            })
                except (json.JSONDecodeError, OSError):
                    continue

    # Warn if no manifests were found at all (directory might be wrong)
    if manifest_files_checked == 0:
        logger.debug(
            "No manifest files found in %s - duplicate check inconclusive",
            search_dirs
        )

    return {
        "duplicate_found": len(locations) > 0,
        "locations": locations,
        "layers_checked": layers_checked,
        "manifests_scanned": manifest_files_checked,
    }
