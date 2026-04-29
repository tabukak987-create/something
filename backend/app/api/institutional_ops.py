"""
Institutional Operations Router
==================================
Combines hardened compliance, data integrity, and math validation endpoints
into a unified institutional operations layer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from ..shared.auth import require_role, Roles, get_current_user
from ..shared.database import fetch_all, fetch_one
from ..shared.logger import get_logger
from ..domain.compliance.rules_hardened import ComplianceHardeningService, BreachType
from ..domain.core_trust.data_integrity import DataIntegrityService
from ..domain.core_trust.math_validation import run_all_validations
from ..domain.core_trust.audit import AuditService, AuditAction, AuditSeverity, EntityType

log = get_logger("router.institutional_ops")
router = APIRouter(prefix="/institutional", tags=["Institutional Operations"])


# ══════════════════════════════════════════════════════════════════════════════
# COMPLIANCE HARDENING ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/compliance/limits")
async def get_compliance_limits(
    limit_type: Optional[str] = None,
    user: dict = Depends(require_role(Roles.COMPLIANCE)),
):
    """Get all active compliance limits with optional type filter."""
    return await ComplianceHardeningService.get_limits(limit_type)


class LimitUpdate(BaseModel):
    limit_id: int
    new_value: float
    warning_threshold: float = 0.8


@router.put("/compliance/limits")
async def update_compliance_limit(
    request: Request,
    payload: LimitUpdate,
    user: dict = Depends(require_role(Roles.ADMIN)),
):
    """Update a compliance limit. ADMIN only. Fully audit-trailed."""
    old_limit = await fetch_one(
        "SELECT * FROM compliance_limits WHERE id = :id", {"id": payload.limit_id}
    )
    if not old_limit:
        raise HTTPException(404, "Limit not found")

    result = await ComplianceHardeningService.update_limit(
        payload.limit_id, payload.new_value, user, payload.warning_threshold
    )

    # Audit trail
    ip = request.client.host if request.client else None
    from ..domain.core_trust.audit import audit_compliance_change
    await audit_compliance_change(
        user,
        f"{old_limit['limit_type']}:{old_limit['limit_key']}",
        {"value": old_limit["limit_value"]},
        {"value": payload.new_value},
        ip,
    )

    return result


@router.post("/compliance/scan")
async def run_compliance_scan(
    trust_id: str = Query("default", description="Trust identifier"),
    user: dict = Depends(require_role(Roles.COMPLIANCE)),
):
    """Run full compliance scan against current portfolio."""
    try:
        from ..domain.portfolio.analytics import compute_full_portfolio_analytics
        portfolio = await compute_full_portfolio_analytics(portfolio_id=trust_id)
        result = await ComplianceHardeningService.run_compliance_scan(portfolio)
        return result
    except Exception as e:
        log.error("compliance_scan_failed", error=str(e))
        raise HTTPException(500, f"Compliance scan failed: {str(e)}")


@router.get("/compliance/breaches")
async def get_breach_history(
    breach_type: Optional[str] = None,
    status: Optional[str] = None,
    days: int = Query(default=90, le=365),
    limit: int = Query(default=100, le=500),
    user: dict = Depends(require_role(Roles.COMPLIANCE)),
):
    """Get historical compliance breaches with filtering."""
    return await ComplianceHardeningService.get_breach_history(
        breach_type, status, days, limit
    )


class BreachAcknowledge(BaseModel):
    breach_id: int
    notes: str = ""


@router.put("/compliance/breaches/acknowledge")
async def acknowledge_breach(
    payload: BreachAcknowledge,
    user: dict = Depends(require_role(Roles.COMPLIANCE)),
):
    """Acknowledge a compliance breach."""
    return await ComplianceHardeningService.acknowledge_breach(
        payload.breach_id, user, payload.notes
    )


class BreachResolve(BaseModel):
    breach_id: int
    resolution_notes: str


@router.put("/compliance/breaches/resolve")
async def resolve_breach(
    payload: BreachResolve,
    user: dict = Depends(require_role(Roles.COMPLIANCE)),
):
    """Resolve a compliance breach with documented resolution."""
    return await ComplianceHardeningService.resolve_breach(
        payload.breach_id, user, payload.resolution_notes
    )


@router.get("/compliance/breach-report")
async def get_breach_report(
    days: int = Query(default=30, le=365),
    user: dict = Depends(require_role(Roles.TRUSTEE)),
):
    """Generate board-ready compliance breach summary report."""
    return await ComplianceHardeningService.generate_breach_report(days)


# ══════════════════════════════════════════════════════════════════════════════
# DATA INTEGRITY ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/data/freshness")
async def check_data_freshness(
    user: dict = Depends(require_role(Roles.VIEWER)),
):
    """Check freshness of all portfolio pricing data."""
    return await DataIntegrityService.check_data_freshness()


@router.get("/data/sources")
async def get_data_sources(
    user: dict = Depends(require_role(Roles.VIEWER)),
):
    """Data source transparency panel — shows all feed status."""
    return await DataIntegrityService.get_data_source_status()


@router.get("/data/anomalies")
async def run_anomaly_detection(
    user: dict = Depends(require_role(Roles.COMPLIANCE)),
):
    """Run batch anomaly detection across portfolio."""
    return await DataIntegrityService.run_anomaly_detection()


class PriceValidation(BaseModel):
    isin: str
    price: float
    source: str = "MANUAL"


@router.post("/data/validate-price")
async def validate_price(
    payload: PriceValidation,
    user: dict = Depends(require_role(Roles.PORTFOLIO)),
):
    """Validate a price against anomaly detection rules before ingestion."""
    return await DataIntegrityService.validate_price(
        payload.isin, payload.price, payload.source
    )


@router.get("/data/fallback/{isin}")
async def get_fallback_price(
    isin: str,
    user: dict = Depends(require_role(Roles.VIEWER)),
):
    """Get fallback price for a bond using priority waterfall."""
    return await DataIntegrityService.apply_fallback(isin)


# ══════════════════════════════════════════════════════════════════════════════
# MATH VALIDATION ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/math/validation")
async def run_math_validation(
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    """
    Run complete mathematical validation suite.
    Tests all financial formulas against known edge cases.
    Returns pass/fail for each test with detailed results.
    """
    return run_all_validations()


@router.get("/math/validation/summary")
async def math_validation_summary(
    user: dict = Depends(require_role(Roles.VIEWER)),
):
    """Summary of mathematical validation — pass rate and any flagged issues."""
    result = run_all_validations()
    return {
        "total_tests": result["total_tests"],
        "passed": result["passed"],
        "failed": result["failed"],
        "pass_rate": result["pass_rate"],
        "flagged_inconsistencies": result["flagged_inconsistencies"],
        "validation_date": result["validation_date"],
    }


# ══════════════════════════════════════════════════════════════════════════════
# STRESS TESTING ENDPOINTS (Phase 8)
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/stress-test/run")
async def run_stress_tests(
    user: dict = Depends(require_role(Roles.ADMIN)),
):
    """Execute full institutional stress test suite (10 scenarios)."""
    from ..domain.risk.stress_test import StressTestingService
    try:
        result = await StressTestingService.run_full_stress_suite()

        # Audit the stress test execution
        await AuditService.record(
            action=AuditAction.COMPLIANCE_CHECK,
            user_id=user.get("user_id", "system"),
            entity_type=EntityType.PORTFOLIO,
            entity_id="stress_test_suite",
            details={
                "type": "stress_test_full_suite",
                "scenarios": result["summary"]["total_scenarios"],
                "passed": result["summary"]["passed"],
                "resilience_score": result["summary"]["overall_resilience_score"],
            },
            severity=AuditSeverity.INFO,
        )

        return result
    except Exception as e:
        log.error("stress_test_failed", error=str(e))
        raise HTTPException(500, f"Stress test execution failed: {str(e)}")

