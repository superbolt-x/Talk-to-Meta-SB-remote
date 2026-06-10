"""
Compliance and risk validation checks (Category D).

Validates creative compliance flags, copy-creative alignment,
special ad categories, and mutation risk classification.
"""
import logging

logger = logging.getLogger("meta-ads-mcp.validators.compliance")

# Valid special ad categories (Meta Graph API v25.0+)
VALID_SPECIAL_AD_CATEGORIES = {
    "FINANCIAL_PRODUCTS_SERVICES",
    "EMPLOYMENT",
    "HOUSING",
    "ISSUES_ELECTIONS_POLITICS",
    "ONLINE_GAMBLING_AND_GAMING",
}

# Mutation risk classification by field
HIGH_RISK_FIELDS = {"status", "budget_remaining", "daily_budget", "lifetime_budget"}
MEDIUM_RISK_FIELDS = {"targeting", "bid_amount", "bid_strategy", "billing_event"}
LOW_RISK_FIELDS = {"name", "special_ad_categories"}


def validate_compliance(payload: dict, creative_profile: dict = None) -> dict:
    """
    Validate compliance requirements for ad creation.

    Checks:
    - Special ad categories are valid Meta-recognized values
    - No conflicting budget fields
    - Mutation risk classification

    Returns dict with keys: valid (bool), issues (list), warnings (list),
    risk_level (str: low/medium/high), sac_flags (list).
    """
    issues = []
    warnings = []

    # --- SAC validation ---
    sac_raw = payload.get("special_ad_categories", "")
    sac_flags = []
    if sac_raw:
        if isinstance(sac_raw, str):
            candidates = [s.strip().upper() for s in sac_raw.split(",") if s.strip()]
        elif isinstance(sac_raw, list):
            candidates = [s.strip().upper() for s in sac_raw]
        else:
            candidates = []

        for cat in candidates:
            if cat not in VALID_SPECIAL_AD_CATEGORIES:
                issues.append(
                    f"Invalid special_ad_category '{cat}'. "
                    f"Valid values: {sorted(VALID_SPECIAL_AD_CATEGORIES)}"
                )
            else:
                sac_flags.append(cat)

    # --- Conflicting budget fields ---
    has_daily = payload.get("daily_budget") is not None
    has_lifetime = payload.get("lifetime_budget") is not None
    if has_daily and has_lifetime:
        issues.append(
            "Cannot set both daily_budget and lifetime_budget simultaneously"
        )

    # --- Mutation risk classification ---
    payload_fields = set(payload.keys())
    if payload_fields & HIGH_RISK_FIELDS:
        risk_level = "high"
        if "status" in payload_fields and payload.get("status") == "ACTIVE":
            warnings.append(
                "Setting status=ACTIVE is a high-risk mutation - activation gate required"
            )
    elif payload_fields & MEDIUM_RISK_FIELDS:
        risk_level = "medium"
    else:
        risk_level = "low"

    # --- SAC with active status is elevated risk ---
    if sac_flags and payload.get("status") == "ACTIVE":
        risk_level = "high"
        warnings.append(
            f"Creating ACTIVE ad with special ad categories {sac_flags} - "
            "ensure compliance review is complete"
        )

    return {
        "valid": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
        "risk_level": risk_level,
        "sac_flags": sac_flags,
    }
