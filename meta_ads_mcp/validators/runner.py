"""
Validation runner - orchestrates all validation categories.

Runs the appropriate validation checks for a given action class
and produces a structured ValidationResult.

Phase: v1.0 (Foundation) - operational and Greek text checks.
Other categories will be activated as their modules are implemented.
"""
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from meta_ads_mcp.validators.greek_text import validate_payload_greek_text, contains_greek

logger = logging.getLogger("meta-ads-mcp.validators.runner")


class ActionClass(str, Enum):
    """Classification of actions that require validation."""
    CREATE = "CREATE"
    ACTIVATE = "ACTIVATE"
    MODIFY_ACTIVE = "MODIFY_ACTIVE"
    CONNECT = "CONNECT"
    BULK = "BULK"


class CheckStatus(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class ValidationVerdict(str, Enum):
    PASS = "pass"
    PASS_WITH_WARNINGS = "pass_with_warnings"
    FAIL = "fail"
    REQUIRES_CONFIRMATION = "requires_confirmation"


@dataclass
class CheckResult:
    """Result of a single validation check."""
    category: str  # A-F
    check_name: str
    status: CheckStatus
    message: str
    remediation: Optional[str] = None


@dataclass
class ValidationResult:
    """Complete validation result for an action."""
    validation_id: str
    timestamp: str
    action_class: ActionClass
    target_account_id: str
    target_object_type: str
    target_object_id: Optional[str]
    verdict: ValidationVerdict = ValidationVerdict.PASS
    checks: list[CheckResult] = field(default_factory=list)
    blocking_issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    confirmation_required: bool = False
    confirmation_reason: Optional[str] = None

    def to_dict(self) -> dict:
        """Serialize to dict for MCP tool output."""
        return {
            "validation_id": self.validation_id,
            "timestamp": self.timestamp,
            "action_class": self.action_class.value,
            "target": {
                "account_id": self.target_account_id,
                "object_type": self.target_object_type,
                "object_id": self.target_object_id,
            },
            "result": self.verdict.value,
            "checks": [
                {
                    "category": c.category,
                    "check_name": c.check_name,
                    "status": c.status.value,
                    "message": c.message,
                    "remediation": c.remediation,
                }
                for c in self.checks
            ],
            "blocking_issues": self.blocking_issues,
            "warnings": self.warnings,
            "confirmation_required": self.confirmation_required,
            "confirmation_reason": self.confirmation_reason,
        }

    def to_log_entry(self) -> str:
        """Format as a markdown entry for vault mutation-log.md."""
        passed = sum(1 for c in self.checks if c.status == CheckStatus.PASS)
        total = len(self.checks)
        warnings_str = ", ".join(self.warnings[:3]) if self.warnings else "none"

        return (
            f"### [{self.timestamp}] Validation: {self.action_class.value} "
            f"{self.target_object_type}\n"
            f"- **Result:** {self.verdict.value}\n"
            f"- **Checks passed:** {passed}/{total}\n"
            f"- **Warnings:** {warnings_str}\n"
            f"- **Blocking issues:** {len(self.blocking_issues)}\n"
        )


# Map of which validation categories run for each action class
VALIDATION_MATRIX: dict[ActionClass, list[str]] = {
    ActionClass.CREATE: ["B", "C", "E", "F"],       # + A for ad creation
    ActionClass.ACTIVATE: ["A", "B", "C", "D", "E", "F"],  # Full validation
    ActionClass.MODIFY_ACTIVE: ["B", "D", "E", "F"],
    ActionClass.CONNECT: ["C", "E"],
    ActionClass.BULK: ["E"],
}


def run_validation(
    action_class: ActionClass,
    target_account_id: str,
    target_object_type: str,
    target_object_id: Optional[str] = None,
    payload: Optional[dict] = None,
    safety_tier: int = 3,
    is_ad_creation: bool = False,
    manifest_ref: Optional[str] = None,
) -> ValidationResult:
    """
    Run validation checks for a given action.

    This is the main entry point for the validation system.
    It determines which categories to run based on the action class,
    executes all applicable checks, and produces a structured result.

    Args:
        action_class: Type of action being validated.
        target_account_id: The ad account ID.
        target_object_type: 'campaign', 'adset', or 'ad'.
        target_object_id: ID of existing object (None for creates).
        payload: The parameters that will be sent to Meta API.
        safety_tier: Safety tier (1, 2, or 3) from tier classification.
        is_ad_creation: True if this is ad creation (adds creative validation).
        manifest_ref: Path to creative manifest (for ad creation).

    Returns:
        ValidationResult with verdict and detailed check results.
    """
    result = ValidationResult(
        validation_id=str(uuid.uuid4())[:8],
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
        action_class=action_class,
        target_account_id=target_account_id,
        target_object_type=target_object_type,
        target_object_id=target_object_id,
    )

    categories = list(VALIDATION_MATRIX.get(action_class, []))

    # Add creative validation for ad creation
    if is_ad_creation and "A" not in categories:
        categories.insert(0, "A")

    payload = payload or {}

    # --- Category A: Creative Validation ---
    if "A" in categories:
        _run_creative_checks(result, manifest_ref, payload)

    # --- Category B: Campaign Structure Validation ---
    if "B" in categories:
        _run_structure_checks(result, target_object_type, payload, action_class)

    # --- Category C: Tracking Validation ---
    if "C" in categories:
        _run_tracking_checks(result, target_account_id, payload)

    # --- Category D: Compliance / Risk Validation ---
    if "D" in categories:
        _run_compliance_checks(result, payload, safety_tier)

    # --- Category E: Operational Validation ---
    if "E" in categories:
        _run_operational_checks(result, action_class, safety_tier)

    # --- Category F: Greek Unicode Integrity ---
    if "F" in categories:
        _run_greek_text_checks(result, payload)

    # --- Determine final verdict ---
    _compute_verdict(result, safety_tier)

    logger.info(
        "Validation %s for %s %s: %s (%d checks, %d blocking, %d warnings)",
        result.validation_id,
        action_class.value,
        target_object_type,
        result.verdict.value,
        len(result.checks),
        len(result.blocking_issues),
        len(result.warnings),
    )

    return result


def _run_creative_checks(result: ValidationResult, manifest_ref: Optional[str], payload: dict):
    """Category A: Creative validation."""
    from meta_ads_mcp.validators.creative import validate_manifest, validate_no_duplicate_creative

    if not manifest_ref:
        result.checks.append(CheckResult(
            category="A",
            check_name="manifest_present",
            status=CheckStatus.WARN,
            message="No manifest_ref provided - skipping creative validation. "
                    "Corridor-level checks still apply.",
        ))
        result.warnings.append("Category A (Creative): no manifest_ref provided")
        return

    # A1: Manifest structure validation
    manifest_result = validate_manifest(manifest_ref)
    if manifest_result["valid"]:
        result.checks.append(CheckResult(
            category="A",
            check_name="manifest_valid",
            status=CheckStatus.PASS,
            message=(
                f"Manifest validated: {manifest_result['creative_count']} creative(s), "
                f"{manifest_result['variant_count']} variant(s)"
            ),
        ))
    else:
        for issue in manifest_result["issues"]:
            result.checks.append(CheckResult(
                category="A",
                check_name="manifest_valid",
                status=CheckStatus.FAIL,
                message=issue,
                remediation="Fix the manifest file before proceeding.",
            ))
            result.blocking_issues.append(f"Manifest issue: {issue}")

    for warning in manifest_result.get("warnings", []):
        result.warnings.append(f"Manifest: {warning}")

    # A2: Duplicate creative check (only if manifest loaded OK)
    if manifest_result["valid"]:
        try:
            import json
            with open(manifest_ref, "r", encoding="utf-8") as f:
                manifest_data = json.load(f)
            for creative in manifest_data.get("creatives", []):
                lcid = creative.get("logical_creative_id", "")
                if not lcid:
                    continue
                dup_result = validate_no_duplicate_creative(lcid)
                if dup_result["duplicate_found"]:
                    result.checks.append(CheckResult(
                        category="A",
                        check_name=f"duplicate_check_{lcid}",
                        status=CheckStatus.WARN,
                        message=(
                            f"Creative '{lcid}' already exists in "
                            f"{len(dup_result['locations'])} location(s)"
                        ),
                        remediation="Review existing creatives - this may be an accidental duplicate.",
                    ))
                    result.warnings.append(f"Possible duplicate creative: '{lcid}'")
                else:
                    result.checks.append(CheckResult(
                        category="A",
                        check_name=f"duplicate_check_{lcid}",
                        status=CheckStatus.PASS,
                        message=f"No duplicate found for creative '{lcid}'",
                    ))
        except (OSError, Exception) as e:
            result.checks.append(CheckResult(
                category="A",
                check_name="duplicate_check",
                status=CheckStatus.WARN,
                message=f"Duplicate check could not complete: {e}",
            ))
            result.warnings.append("Duplicate check skipped - error reading manifest")


def _run_structure_checks(result: ValidationResult, object_type: str, payload: dict, action_class: Optional[ActionClass] = None):
    """Category B: Campaign structure validation checks."""
    from meta_ads_mcp.validators.structure import validate_campaign_structure, validate_naming_convention

    # Check that PAUSED status is set for creates.
    # Skip for ACTIVATE actions - setting status=ACTIVE is the entire point.
    if action_class == ActionClass.ACTIVATE:
        result.checks.append(CheckResult(
            category="B",
            check_name="created_as_paused",
            status=CheckStatus.PASS,
            message="ACTIVATE action - status=ACTIVE is expected",
        ))
    else:
        status = payload.get("status", "PAUSED")
        if status == "PAUSED":
            result.checks.append(CheckResult(
                category="B",
                check_name="created_as_paused",
                status=CheckStatus.PASS,
                message="Object will be created as PAUSED",
            ))
        elif status == "ACTIVE":
            result.checks.append(CheckResult(
                category="B",
                check_name="created_as_paused",
                status=CheckStatus.FAIL,
                message="Object is being created as ACTIVE - this bypasses the activation gate",
                remediation="Set status to PAUSED. Activation requires separate user confirmation.",
            ))
            result.blocking_issues.append("Cannot create objects as ACTIVE - must be PAUSED first")

    # Campaign-level structure checks
    if object_type == "campaign" and payload:
        objective = payload.get("objective")
        archetype = payload.get("archetype", "hybrid")
        budget = payload.get("daily_budget") or payload.get("lifetime_budget")

        if objective:
            struct = validate_campaign_structure(
                objective=objective,
                archetype=archetype,
                budget=budget or 0,
            )
            if struct["valid"]:
                result.checks.append(CheckResult(
                    category="B",
                    check_name="campaign_structure",
                    status=CheckStatus.PASS if not struct["warnings"] else CheckStatus.WARN,
                    message="Campaign structure valid" if not struct["warnings"] else struct["warnings"][0],
                ))
            else:
                for issue in struct["issues"]:
                    result.checks.append(CheckResult(
                        category="B",
                        check_name="campaign_structure",
                        status=CheckStatus.FAIL,
                        message=issue,
                        remediation="Fix campaign structure before proceeding.",
                    ))
                    result.blocking_issues.append(f"Structure: {issue}")
            for w in struct.get("warnings", []):
                result.warnings.append(f"Structure: {w}")

    # Naming convention check
    name = payload.get("name")
    if name:
        naming = validate_naming_convention(name, object_type)
        if not naming["valid"]:
            for issue in naming["issues"]:
                result.checks.append(CheckResult(
                    category="B",
                    check_name="naming_convention",
                    status=CheckStatus.FAIL,
                    message=issue,
                    remediation="Fix name before proceeding.",
                ))
                result.blocking_issues.append(f"Naming: {issue}")
        elif naming.get("warnings"):
            result.checks.append(CheckResult(
                category="B",
                check_name="naming_convention",
                status=CheckStatus.WARN,
                message=naming["warnings"][0],
            ))
            for w in naming["warnings"]:
                result.warnings.append(f"Naming: {w}")
        else:
            result.checks.append(CheckResult(
                category="B",
                check_name="naming_convention",
                status=CheckStatus.PASS,
                message=f"Name follows convention: '{name}'",
            ))


def _run_tracking_checks(result: ValidationResult, account_id: str, payload: dict):
    """Category C: Tracking validation - NOT IMPLEMENTED in validator framework.

    Pixel/event tracking validation is not implemented here. However, the tracking_gate
    (engine/tracking_gate.py) provides HARD BLOCK enforcement in create_adset and
    activation corridors. This validator category is skipped because the real enforcement
    lives in the corridor gates.
    """
    result.checks.append(CheckResult(
        category="C",
        check_name="tracking_validation",
        status=CheckStatus.WARN,
        message="SKIPPED: Tracking validation not in validator framework. Hard enforcement via tracking_gate in create_adset/activation corridors.",
    ))
    result.warnings.append("Category C (Tracking) validation skipped - enforcement is in corridor gates")


def _run_compliance_checks(result: ValidationResult, payload: dict, safety_tier: int):
    """Category D: Compliance validation."""
    from meta_ads_mcp.validators.compliance import validate_compliance

    compliance = validate_compliance(payload)

    if compliance["valid"]:
        risk = compliance.get("risk_level", "low")
        sac = compliance.get("sac_flags", [])
        msg = f"Compliance OK - risk level: {risk}"
        if sac:
            msg += f", SAC flags: {sac}"
        result.checks.append(CheckResult(
            category="D",
            check_name="compliance_check",
            status=CheckStatus.PASS,
            message=msg,
        ))
    else:
        for issue in compliance["issues"]:
            result.checks.append(CheckResult(
                category="D",
                check_name="compliance_check",
                status=CheckStatus.FAIL,
                message=issue,
                remediation="Fix compliance issue before proceeding.",
            ))
            result.blocking_issues.append(f"Compliance: {issue}")

    for warning in compliance.get("warnings", []):
        result.checks.append(CheckResult(
            category="D",
            check_name="compliance_warning",
            status=CheckStatus.WARN,
            message=warning,
        ))
        result.warnings.append(f"Compliance: {warning}")

    # Safety tier confirmation enforcement
    if safety_tier == 1:
        result.confirmation_required = True
        result.confirmation_reason = "Tier 1 action requires explicit user confirmation"

    # High-risk mutation also requires confirmation
    risk_level = compliance.get("risk_level", "low")
    if risk_level == "high" and not result.confirmation_required:
        result.confirmation_required = True
        result.confirmation_reason = (
            result.confirmation_reason or
            "High-risk mutation requires explicit user confirmation"
        )


def _run_operational_checks(result: ValidationResult, action_class: ActionClass, safety_tier: int):
    """
    Category E: Operational validation checks.

    Phase v1.0: Fully implemented.
    """
    # E1: Rollback readiness (required for Tier 1 actions)
    if safety_tier == 1:
        # In v1.0, we flag that rollback snapshot should be taken
        # Actual snapshot is taken by the operator before execution
        result.checks.append(CheckResult(
            category="E",
            check_name="rollback_readiness",
            status=CheckStatus.PASS,
            message="Rollback snapshot will be captured before execution (Tier 1 requirement)",
        ))
    else:
        result.checks.append(CheckResult(
            category="E",
            check_name="rollback_readiness",
            status=CheckStatus.PASS,
            message=f"Rollback snapshot optional for Tier {safety_tier}",
        ))

    # E2: Confirmation triggers identified
    if action_class == ActionClass.ACTIVATE:
        result.checks.append(CheckResult(
            category="E",
            check_name="activation_gate",
            status=CheckStatus.PASS,
            message="Activation requires user confirmation - gate is active",
        ))
        result.confirmation_required = True
        result.confirmation_reason = "PAUSED -> ACTIVE requires explicit user confirmation"

    # E3: Rate limit headroom
    from meta_ads_mcp.core.api import api_client
    usage = api_client.rate_limits.max_usage_pct
    if usage >= 95:
        result.checks.append(CheckResult(
            category="E",
            check_name="rate_limit_headroom",
            status=CheckStatus.FAIL,
            message=f"Rate limit usage at {usage:.1f}% - too high to proceed safely",
            remediation="Wait for rate limit to decrease before continuing operations.",
        ))
        result.blocking_issues.append(f"Rate limit at {usage:.1f}% - operations blocked")
    elif usage >= 80:
        result.checks.append(CheckResult(
            category="E",
            check_name="rate_limit_headroom",
            status=CheckStatus.WARN,
            message=f"Rate limit usage at {usage:.1f}% - approaching limit",
        ))
        result.warnings.append(f"Rate limit at {usage:.1f}%")
    else:
        result.checks.append(CheckResult(
            category="E",
            check_name="rate_limit_headroom",
            status=CheckStatus.PASS,
            message=f"Rate limit usage at {usage:.1f}% - healthy",
        ))


def _run_greek_text_checks(result: ValidationResult, payload: dict):
    """
    Category F: Greek Unicode integrity checks.

    Phase v1.0: Fully implemented.
    """
    text_results = validate_payload_greek_text(payload, context="pre-write validation")

    if not text_results:
        result.checks.append(CheckResult(
            category="F",
            check_name="greek_text_presence",
            status=CheckStatus.PASS,
            message="No Greek text detected in payload",
        ))
        return

    all_safe = True
    for tr in text_results:
        if tr.has_critical:
            all_safe = False
            for issue in tr.issues:
                if issue.severity.value == "critical":
                    result.checks.append(CheckResult(
                        category="F",
                        check_name=f"greek_text_{tr.field_name}",
                        status=CheckStatus.FAIL,
                        message=f"{issue.message} in field '{tr.field_name}'",
                        remediation="Fix the text encoding before proceeding. Ensure source text is valid UTF-8.",
                    ))
                    result.blocking_issues.append(
                        f"Greek text corruption in '{tr.field_name}': {issue.message}"
                    )
        elif tr.has_high:
            for issue in tr.issues:
                if issue.severity.value == "high":
                    result.checks.append(CheckResult(
                        category="F",
                        check_name=f"greek_text_{tr.field_name}",
                        status=CheckStatus.WARN,
                        message=f"{issue.message} in field '{tr.field_name}'",
                        remediation="Review the text for encoding issues.",
                    ))
                    result.warnings.append(f"Greek text issue in '{tr.field_name}': {issue.message}")
        else:
            result.checks.append(CheckResult(
                category="F",
                check_name=f"greek_text_{tr.field_name}",
                status=CheckStatus.PASS,
                message=f"Greek text in '{tr.field_name}' validated successfully (NFC normalized)",
            ))

    if all_safe:
        result.checks.append(CheckResult(
            category="F",
            check_name="greek_text_overall",
            status=CheckStatus.PASS,
            message=f"All {len(text_results)} Greek text fields validated successfully",
        ))


def _compute_verdict(result: ValidationResult, safety_tier: int):
    """Compute the final validation verdict from all check results."""
    has_fail = any(c.status == CheckStatus.FAIL for c in result.checks)
    has_warn = any(c.status == CheckStatus.WARN for c in result.checks)

    if has_fail:
        result.verdict = ValidationVerdict.FAIL
    elif result.confirmation_required:
        result.verdict = ValidationVerdict.REQUIRES_CONFIRMATION
    elif has_warn:
        # Tier 1 with warnings requires confirmation
        if safety_tier == 1:
            result.verdict = ValidationVerdict.REQUIRES_CONFIRMATION
            result.confirmation_required = True
            result.confirmation_reason = (
                result.confirmation_reason or
                "Tier 1 action with warnings requires explicit confirmation"
            )
        else:
            result.verdict = ValidationVerdict.PASS_WITH_WARNINGS
    else:
        result.verdict = ValidationVerdict.PASS
