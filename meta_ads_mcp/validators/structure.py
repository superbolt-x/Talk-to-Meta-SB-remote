"""
Campaign structure validation checks (Category B).

Validates objective-archetype alignment, hierarchy completeness,
naming conventions, account/page/IG mappings, and budget reasonability.
"""
import logging
import re

logger = logging.getLogger("meta-ads-mcp.validators.structure")

# Naming convention tokens (must match naming.py)
VALID_OBJECTIVES = ["Sales", "Traffic", "Leads", "Awareness", "Engagement"]
VALID_FUNNELS = ["TOFU", "MOFU", "BOFU", "RT"]
VALID_BUDGET_MODELS = ["ABO", "CBO"]
VALID_FORMATS = ["REEL", "VID", "IMG", "REEL+FEED"]

# Objective-archetype alignment rules
# Maps archetype -> objectives that make sense for it
ARCHETYPE_OBJECTIVES = {
    "ecommerce": ["Sales", "Traffic", "Awareness"],
    "lead_gen": ["Leads", "Traffic", "Awareness"],
    "hybrid": ["Sales", "Leads", "Traffic", "Awareness", "Engagement"],
    "local": ["Traffic", "Awareness", "Leads", "Engagement"],
    "saas": ["Leads", "Traffic", "Awareness"],
}

# Budget sanity thresholds (EUR/day minimum per objective)
MIN_DAILY_BUDGET = {
    "Sales": 5.0,
    "Traffic": 3.0,
    "Leads": 5.0,
    "Awareness": 3.0,
    "Engagement": 2.0,
}

# Campaign naming pattern: "Objective | Product | Funnel | BudgetModel"
CAMPAIGN_NAME_PATTERN = re.compile(
    r'^([A-Za-z]+)\s*\|\s*(.+?)\s*\|\s*(TOFU|MOFU|BOFU|RT)\s*\|\s*(ABO|CBO)$'
)

# Ad set naming pattern: "AudienceType | AgeRange | Geo | ExclusionFlag"
ADSET_NAME_PATTERN = re.compile(
    r'^(.+?)\s*\|\s*(\d{2}-\d{2}|All)\s*\|\s*([A-Z]{2}(?:,[A-Z]{2})*)\s*\|\s*(.+)$'
)

# Ad naming pattern: "Hook | Format | Version"
AD_NAME_PATTERN = re.compile(
    r'^(.+?)\s*\|\s*(REEL|VID|IMG|REEL\+FEED)\s*\|\s*(V\d+)$'
)


def validate_campaign_structure(
    objective: str,
    archetype: str,
    budget: float,
    currency: str = "EUR",
) -> dict:
    """
    Validate campaign structure against archetype requirements.

    Checks:
    - Objective is a recognized Meta objective
    - Objective is appropriate for the account archetype
    - Budget meets minimum threshold for the objective

    Returns dict with: valid (bool), issues (list), warnings (list).
    """
    issues = []
    warnings = []

    # --- Objective validation ---
    if objective not in VALID_OBJECTIVES:
        issues.append(
            f"Objective '{objective}' is not valid. "
            f"Valid options: {VALID_OBJECTIVES}"
        )
        return {"valid": False, "issues": issues, "warnings": warnings}

    # --- Archetype-objective alignment ---
    archetype_lower = archetype.lower() if archetype else ""
    allowed = ARCHETYPE_OBJECTIVES.get(archetype_lower)
    if allowed and objective not in allowed:
        warnings.append(
            f"Objective '{objective}' is unusual for '{archetype}' archetype. "
            f"Typical objectives: {allowed}"
        )

    # --- Budget sanity check ---
    if budget is not None and budget > 0:
        min_budget = MIN_DAILY_BUDGET.get(objective, 3.0)
        if budget < min_budget:
            warnings.append(
                f"Daily budget {budget} {currency} is below recommended minimum "
                f"{min_budget} {currency} for '{objective}' objective"
            )
        elif budget > 500:
            warnings.append(
                f"Daily budget {budget} {currency} is unusually high - "
                "confirm this is intentional"
            )

    return {
        "valid": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
    }


def validate_naming_convention(name: str, object_type: str = "campaign") -> dict:
    """
    Validate a name against the account naming convention.

    Args:
        name: The name to validate.
        object_type: 'campaign', 'adset', or 'ad'.

    Returns dict with: valid (bool), issues (list), warnings (list),
    parsed (dict if successfully parsed).
    """
    issues = []
    warnings = []
    parsed = None

    if not name or not name.strip():
        issues.append("Name cannot be empty")
        return {"valid": False, "issues": issues, "warnings": warnings, "parsed": None}

    object_type = object_type.lower()

    if object_type == "campaign":
        match = CAMPAIGN_NAME_PATTERN.match(name.strip())
        if not match:
            warnings.append(
                f"Campaign name '{name}' does not match pattern "
                "'Objective | Product | Funnel | BudgetModel'. "
                "Example: 'Sales | ProductName | TOFU | CBO'"
            )
        else:
            objective, product, funnel, budget_model = match.groups()
            parsed = {
                "objective": objective,
                "product": product,
                "funnel": funnel,
                "budget_model": budget_model,
            }
            if objective not in VALID_OBJECTIVES:
                issues.append(
                    f"Objective '{objective}' in name is not valid. "
                    f"Valid: {VALID_OBJECTIVES}"
                )

    elif object_type == "adset":
        match = ADSET_NAME_PATTERN.match(name.strip())
        if not match:
            warnings.append(
                f"Ad set name '{name}' does not match pattern "
                "'AudienceType | AgeRange | Geo | ExclusionFlag'. "
                "Example: 'Broad | 24-55 | GR | None'"
            )
        else:
            audience, age_range, geo, exclusion = match.groups()
            parsed = {
                "audience_type": audience,
                "age_range": age_range,
                "geo": geo,
                "exclusion_flag": exclusion,
            }

    elif object_type == "ad":
        match = AD_NAME_PATTERN.match(name.strip())
        if not match:
            warnings.append(
                f"Ad name '{name}' does not match pattern "
                "'Hook | Format | Version'. "
                "Example: 'social-proof | REEL | V1'"
            )
        else:
            hook, fmt, version = match.groups()
            parsed = {"hook": hook, "format": fmt, "version": version}

    # Check for [OLD] suffix - informational
    if "[OLD]" in name:
        warnings.append(
            f"Name contains [OLD] suffix - this object is deprecated and should not be modified"
        )

    return {
        "valid": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
        "parsed": parsed,
    }
