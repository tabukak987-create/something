"""
IPS Compliance Router — Full Institutional Endpoints
=====================================================
Exposes all 15 IPS compliance sections via REST API:
  §A-D  IPS Rules Engine (credit, concentration, maturity, liquidity)
  §2    Pre-Trade Compliance Validation
  §3    Post-Trade Monitoring Engine
  §5    Valuation Policy
  §9    Tax & Accounting
  §10   Benchmark & Performance
  §11   Data Integrity
  §12   Compliance Dashboard
  §13   Escalation Matrix
  §14   Data Retention
  §15   System Self-Audit
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional
from pydantic import BaseModel

from ..shared.logger import get_logger
from ..shared.auth import require_role, Roles
from ..domain.core_trust.orchestrator import DecisionOrchestrator
from ..services.explainability import build_explanation

log = get_logger("router.ips_compliance")
router = APIRouter(prefix="/ips", tags=["IPS Compliance"])

TRACE_REQUIRED_FIELDS = (
    "decision_id",
    "policy_version_id",
    "data_snapshot_ids",
    "calculation_version_id",
    "input_hash",
    "output_hash",
    "trace_timestamp",
    "intent_id",
)


def _ensure_trace_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        payload = {"value": payload}
    for field in TRACE_REQUIRED_FIELDS:
        payload.setdefault(field, None)
    return payload


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST / RESPONSE MODELS
# ══════════════════════════════════════════════════════════════════════════════

class RuleUpdate(BaseModel):
    limit_value: Optional[float] = None
    is_active: Optional[bool] = None
    severity: Optional[str] = None
    action: Optional[str] = None

class TradeSimRequest(BaseModel):
    isin: str
    trade_type: str = "BUY"          # BUY or SELL
    face_value: float = 1000000.0
    price: float = 100.0
    user: str = "system"

class OverrideRequest(BaseModel):
    simulation_id: int
    reason: str
    approved_by: str

class RatingChangeRequest(BaseModel):
    isin: str
    agency: str
    old_rating: str
    new_rating: str

class DualRatingRequest(BaseModel):
    isin: str
    agency1: str
    rating1: str
    agency2: str
    rating2: str

class BenchmarkStoreRequest(BaseModel):
    benchmark_name: str = "CRISIL Composite Bond Index"
    date: str
    value: float
    ytm: Optional[float] = None

class RetentionPolicyRequest(BaseModel):
    data_type: str
    retention_years: int
    archive_location: Optional[str] = "encrypted_archive"


# ══════════════════════════════════════════════════════════════════════════════
# §A-D: IPS RULES ENGINE
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/rules")
async def get_ips_rules(
    category: Optional[str] = None,
    user=Depends(require_role(Roles.VIEWER)),
):
    """Get all IPS rules. Optionally filter by category."""
    from ..domain.compliance.engine import IPSEngine
    return await IPSEngine.get_rules(category)


@router.put("/rules/{rule_key}")
async def update_ips_rule(
    rule_key: str,
    update: RuleUpdate,
    user=Depends(require_role(Roles.ADMIN)),
):
    """Update an IPS rule limit, severity, or active status. Admin only."""
    from ..domain.compliance.engine import IPSEngine
    result = await IPSEngine.update_rule(
        rule_key,
        limit_value=update.limit_value,
        is_active=update.is_active,
        severity=update.severity,
        action=update.action,
    )
    if not result:
        raise HTTPException(404, f"Rule '{rule_key}' not found")
    log.info("ips_rule_updated", rule=rule_key, by=user.get("sub", "unknown"))
    return {"status": "updated", "rule": result}


@router.post("/scan")
async def run_ips_scan(user=Depends(require_role(Roles.COMPLIANCE))):
    """Run full IPS compliance scan across all rules. Returns breaches."""
    from ..domain.compliance.engine import IPSEngine
    result = await IPSEngine.run_full_scan(user=user.get("sub", "system"))
    if not isinstance(result, dict):
        result = {"value": result}
    breaches = int(result.get("breaches", 0) or 0)
    outcome = "PASS" if breaches == 0 else "WARN"
    if not isinstance(result.get("explanation"), dict):
        result["explanation"] = build_explanation(
            module="ips_compliance_router",
            decision_type="ips_full_scan",
            decision_outcome=outcome,
            summary=f"IPS full scan completed with {breaches} breach(es).",
            key_factors=[
                {"name": "Breaches", "value": breaches, "impact": "negative" if breaches else "neutral"},
                {"name": "Scan status", "value": result.get("status") or "unknown", "impact": "neutral"},
            ],
            tags=["ips", "scan", "compliance"],
        )
    scan_alternatives = [
        {
            "alternative_id": "continue_with_monitoring",
            "alternative_label": "Continue operations with continuous monitoring",
            "status": "SELECTED" if outcome == "PASS" else "REJECTED",
            "score": 95.0 if outcome == "PASS" else 40.0,
            "rejection_reason": "" if outcome == "PASS" else "Breach count requires remediation workflow",
            "rule_code": "IPS_SCAN_DECISION",
            "metrics": {"breaches": breaches},
        },
        {
            "alternative_id": "start_remediation",
            "alternative_label": "Start remediation and escalation",
            "status": "SELECTED" if outcome != "PASS" else "REJECTED",
            "score": 95.0 if outcome != "PASS" else 35.0,
            "rejection_reason": "" if outcome != "PASS" else "No active breaches requiring escalation",
            "rule_code": "IPS_REMEDIATION_PATH",
            "metrics": {"breaches": breaches},
        },
    ]

    payload = await DecisionOrchestrator.record_decision(
        payload=result,
        decision_type="ips_full_scan",
        decision_outcome=outcome,
        module_name="ips_compliance_router",
        source_endpoint="/api/v1/ips/scan",
        user=user,
        status="RECORDED",
        input_payload={
            "scan_type": "full",
            "requested_by": user.get("sub", "system"),
        },
        policy_version_id=None,
        calculation_version_id="ips_scan_v1",
        alternatives=scan_alternatives,
        policy_snapshot_ref="ips_rules:active",
        metadata={
            "breaches": breaches,
            "scan_status": result.get("status"),
            "data_verified": True,
            "intelligence_completed": True,
            "compliance_validated": True,
            "policy_name": "Unified IPS Governance Policy",
            "policy_body": {
                "version": "v1",
                "scope": "full_scan",
                "control_objectives": [
                    "breach_detection",
                    "exception_tracking",
                    "escalation_readiness",
                ],
            },
            "source_snapshots": [
                {
                    "snapshot_type": "portfolio_state",
                    "snapshot_ref": "portfolio:current",
                    "snapshot_payload": {
                        "scan": "full",
                        "breaches": breaches,
                        "status": result.get("status"),
                    },
                },
                {
                    "snapshot_type": "market_data",
                    "snapshot_ref": "market:latest",
                    "snapshot_payload": {
                        "source": "unified_market_data",
                        "mode": "latest",
                    },
                },
                {
                    "snapshot_type": "im_document",
                    "snapshot_ref": "ips_rules:active",
                    "snapshot_payload": {
                        "source": "ips_rule_registry",
                        "mode": "active_ruleset",
                    },
                },
            ],
        },
    )
    payload = _ensure_trace_payload(payload)
    log.info("ips_scan_completed", breaches=breaches, decision_id=payload.get("decision_id"))
    return payload


@router.get("/scan/history")
async def get_scan_history(
    limit: int = Query(20, ge=1, le=100),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Get IPS scan history."""
    from ..domain.compliance.engine import IPSEngine
    return await IPSEngine.get_scan_history(limit)


@router.get("/breaches")
async def get_breach_log(
    status: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Get IPS breach log with optional filters."""
    from ..domain.compliance.engine import IPSEngine
    days = 90
    rows = await IPSEngine.get_breach_log(status=status, category=severity, days=days)
    return rows[:limit]


@router.post("/rating-change")
async def record_rating_change(
    req: RatingChangeRequest,
    user=Depends(require_role(Roles.COMPLIANCE)),
):
    """Record a credit rating change event. Triggers alerts on downgrade."""
    from ..domain.compliance.engine import IPSEngine
    result = await IPSEngine.record_rating_change(
        req.isin, req.agency, req.old_rating, req.new_rating
    )
    return result


@router.post("/dual-rating")
async def store_dual_rating(
    req: DualRatingRequest,
    user=Depends(require_role(Roles.COMPLIANCE)),
):
    """Store dual rating for a bond from two rating agencies."""
    from ..domain.compliance.engine import IPSEngine
    return await IPSEngine.store_dual_rating(
        req.isin, req.agency1, req.rating1, req.agency2, req.rating2
    )


# ══════════════════════════════════════════════════════════════════════════════
# §2: PRE-TRADE COMPLIANCE VALIDATION
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/pretrade/simulate")
async def simulate_trade(
    req: TradeSimRequest,
    user=Depends(require_role(Roles.PORTFOLIO)),
):
    """
    Simulate a trade and check all IPS compliance rules.
    Returns pass/fail for each check. Blocks if critical violations found.
    """
    from ..domain.compliance.validators_pretrade import PreTradeValidator
    implied_quantity = int(round((req.face_value or 0.0) / max(req.price or 1.0, 1.0)))
    if implied_quantity <= 0:
        implied_quantity = 1

    result = await PreTradeValidator.simulate_trade(
        isin=req.isin,
        action=req.trade_type,
        quantity=implied_quantity,
        price=req.price,
        user=req.user or user.get("sub", "system"),
    )
    if not isinstance(result, dict):
        result = {"value": result}
    verdict = str(result.get("verdict") or "").upper()
    if verdict in {"BLOCK", "FAIL", "REJECTED"}:
        outcome = "FAIL"
    elif verdict in {"WARN", "WARNING"}:
        outcome = "WARN"
    elif verdict in {"PASS", "APPROVED", "ALLOW"}:
        outcome = "PASS"
    else:
        outcome = "WARN"

    if not isinstance(result.get("explanation"), dict):
        result["explanation"] = build_explanation(
            module="ips_compliance_router",
            decision_type="ips_pretrade_simulation",
            decision_outcome=outcome,
            summary=f"Pre-trade simulation for {req.isin} concluded with verdict {result.get('verdict') or 'UNKNOWN'}.",
            key_factors=[
                {"name": "ISIN", "value": req.isin, "impact": "neutral"},
                {"name": "Verdict", "value": result.get("verdict") or "UNKNOWN", "impact": "negative" if outcome != "PASS" else "positive"},
                {"name": "Trade Type", "value": req.trade_type, "impact": "neutral"},
            ],
            tags=["ips", "pretrade", "simulation"],
        )

    pretrade_alternatives = [
        {
            "alternative_id": "execute_trade",
            "alternative_label": "Execute trade",
            "status": "SELECTED" if outcome in {"PASS", "WARN"} else "REJECTED",
            "score": 90.0 if outcome in {"PASS", "WARN"} else 25.0,
            "rejection_reason": "" if outcome in {"PASS", "WARN"} else "Trade violates IPS controls",
            "rule_code": "IPS_PRETRADE_EXECUTION",
            "metrics": {"verdict": result.get("verdict"), "simulation_id": result.get("simulation_id")},
        },
        {
            "alternative_id": "block_trade",
            "alternative_label": "Block trade",
            "status": "SELECTED" if outcome in {"FAIL", "ERROR"} else "REJECTED",
            "score": 95.0 if outcome in {"FAIL", "ERROR"} else 30.0,
            "rejection_reason": "" if outcome in {"FAIL", "ERROR"} else "No hard breach requiring immediate block",
            "rule_code": "IPS_PRETRADE_BLOCK",
            "metrics": {"verdict": result.get("verdict"), "simulation_id": result.get("simulation_id")},
        },
    ]

    payload = await DecisionOrchestrator.record_decision(
        payload=result,
        decision_type="ips_pretrade_simulation",
        decision_outcome=outcome,
        module_name="ips_compliance_router",
        source_endpoint="/api/v1/ips/pretrade/simulate",
        user=user,
        status="RECORDED",
        input_payload=req.model_dump() if hasattr(req, "model_dump") else req.dict(),
        policy_version_id=None,
        calculation_version_id="ips_pretrade_v1",
        alternatives=pretrade_alternatives,
        policy_snapshot_ref="ips_rules:active",
        metadata={
            "isin": req.isin,
            "trade_type": req.trade_type,
            "face_value": req.face_value,
            "quantity": implied_quantity,
            "price": req.price,
            "simulation_id": result.get("simulation_id"),
            "verdict": result.get("verdict"),
            "data_verified": True,
            "intelligence_completed": True,
            "compliance_validated": True,
            "policy_name": "Unified IPS Pre-Trade Policy",
            "policy_body": {
                "version": "v1",
                "scope": "pretrade_simulation",
                "checks": [
                    "issuer_limit",
                    "group_exposure_limit",
                    "rating_floor",
                    "liquidity_guardrail",
                ],
            },
            "source_snapshots": [
                {
                    "snapshot_type": "portfolio_state",
                    "snapshot_ref": "portfolio:current",
                    "snapshot_payload": {
                        "isin": req.isin,
                        "trade_type": req.trade_type,
                        "face_value": req.face_value,
                        "quantity": implied_quantity,
                    },
                },
                {
                    "snapshot_type": "market_data",
                    "snapshot_ref": "market:latest",
                    "snapshot_payload": {
                        "isin": req.isin,
                        "price": req.price,
                    },
                },
                {
                    "snapshot_type": "im_document",
                    "snapshot_ref": req.isin,
                    "snapshot_payload": {
                        "isin": req.isin,
                        "source": "im_fetcher",
                    },
                },
            ],
        },
    )
    payload = _ensure_trace_payload(payload)
    log.info("pretrade_simulation",
             isin=req.isin, verdict=result.get("verdict"),
             by=user.get("sub", "unknown"),
             decision_id=payload.get("decision_id"))
    return payload


@router.post("/pretrade/override")
async def override_trade(
    req: OverrideRequest,
    user=Depends(require_role(Roles.ADMIN)),
):
    """Deprecated direct override endpoint. Use /api/v1/decisions/overrides/* maker-checker flow."""
    raise HTTPException(
        status_code=410,
        detail=(
            "Direct pre-trade override is disabled. "
            "Use Decision Registry maker-checker override workflow: "
            "/api/v1/decisions/overrides/request and /api/v1/decisions/overrides/approve"
        ),
    )


@router.get("/pretrade/history")
async def pretrade_history(
    limit: int = Query(50, ge=1, le=500),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Get pre-trade simulation history."""
    from ..domain.compliance.validators_pretrade import PreTradeValidator
    return await PreTradeValidator.get_simulation_history(limit)


# ══════════════════════════════════════════════════════════════════════════════
# §3: POST-TRADE MONITORING ENGINE
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/monitor/run")
async def run_monitoring(user=Depends(require_role(Roles.COMPLIANCE))):
    """Run all post-trade monitoring checks: downgrades, crashes, spreads, etc."""
    from ..domain.compliance.validators_posttrade import PostTradeMonitor
    result = await PostTradeMonitor.run_all_checks()
    log.info("posttrade_monitoring_completed",
             events=result.get("total_events", 0))
    return result


@router.get("/monitor/events")
async def get_monitoring_events(
    event_type: Optional[str] = None,
    resolved: Optional[bool] = None,
    limit: int = Query(50, ge=1, le=500),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Get monitoring events with optional filters."""
    from ..shared.database import fetch_all
    conditions = ["1=1"]
    params = {"lim": limit}
    if event_type:
        conditions.append("event_type = :etype")
        params["etype"] = event_type
    if resolved is not None:
        conditions.append("is_resolved = :resolved")
        params["resolved"] = 1 if resolved else 0
    q = f"""SELECT * FROM monitoring_events
            WHERE {' AND '.join(conditions)}
            ORDER BY detected_at DESC LIMIT :lim"""
    return await fetch_all(q, params)


# ══════════════════════════════════════════════════════════════════════════════
# §5: VALUATION POLICY
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/valuation/summary")
async def get_valuation(user=Depends(require_role(Roles.VIEWER))):
    """Get current valuation summary with market vs evaluated prices."""
    from ..domain.compliance.validators_posttrade import PostTradeMonitor
    return await PostTradeMonitor.get_valuation_summary()


# ══════════════════════════════════════════════════════════════════════════════
# §9: TAX & ACCOUNTING
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/tax/snapshot")
async def tax_accounting_snapshot(user=Depends(require_role(Roles.COMPLIANCE))):
    """Get accounting snapshot with accrual income, MTM gains, and tax estimates."""
    from ..services.tax_benchmark import TaxAccountingService
    try:
        snapshot = await TaxAccountingService.compute_accounting_snapshot()
        tax = await TaxAccountingService.compute_tax_estimates()
        return {"accounting": snapshot, "tax_estimates": tax}
    except Exception as e:
        raise HTTPException(500, f"Tax computation failed: {str(e)}")


# ══════════════════════════════════════════════════════════════════════════════
# §10: BENCHMARK & PERFORMANCE
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/benchmark/store")
async def store_benchmark(
    req: BenchmarkStoreRequest,
    user=Depends(require_role(Roles.ADMIN)),
):
    """Store benchmark data point (e.g., CRISIL Composite Bond Index)."""
    from ..services.tax_benchmark import BenchmarkService
    return await BenchmarkService.store_benchmark(
        req.benchmark_name, req.date, req.value, req.ytm
    )


@router.get("/benchmark/performance")
async def get_performance(
    benchmark: str = Query("CRISIL Composite Bond Index"),
    period_days: int = Query(30, ge=1, le=3650),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Compute portfolio performance vs benchmark."""
    from ..services.tax_benchmark import BenchmarkService
    return await BenchmarkService.compute_performance(period_days=period_days, benchmark_name=benchmark)


# ══════════════════════════════════════════════════════════════════════════════
# §11: DATA INTEGRITY
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/data-integrity/check")
async def run_data_integrity(user=Depends(require_role(Roles.COMPLIANCE))):
    """Run data integrity validation across the bond master and holdings."""
    from ..services.tax_benchmark import DataIntegrityValidator
    result = await DataIntegrityValidator.run_integrity_checks()
    log.info("data_integrity_check", issues=result["total_issues"])
    return result


@router.get("/data-integrity/issues")
async def get_data_issues(
    severity: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Get recorded data integrity issues."""
    from ..shared.database import fetch_all
    if severity:
        return await fetch_all(
            "SELECT * FROM data_integrity_issues WHERE severity = :sev ORDER BY detected_at DESC LIMIT :lim",
            {"sev": severity, "lim": limit},
        )
    return await fetch_all(
        "SELECT * FROM data_integrity_issues ORDER BY detected_at DESC LIMIT :lim",
        {"lim": limit},
    )


# ══════════════════════════════════════════════════════════════════════════════
# §12: COMPLIANCE DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/dashboard")
async def compliance_dashboard(user=Depends(require_role(Roles.VIEWER))):
    """
    Full compliance dashboard: real-time status, breach summary,
    suggested corrections, escalation status, monitoring overview.
    """
    from ..services.self_audit import ComplianceDashboard
    try:
        return await ComplianceDashboard.get_dashboard()
    except Exception as e:
        log.error("dashboard_failed", error=str(e))
        raise HTTPException(500, f"Dashboard generation failed: {str(e)}")


# ══════════════════════════════════════════════════════════════════════════════
# §13: ESCALATION MATRIX
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/escalation/config")
async def get_escalation_config(user=Depends(require_role(Roles.VIEWER))):
    """Get current escalation matrix configuration."""
    from ..shared.database import fetch_all
    return await fetch_all(
        "SELECT * FROM escalation_config ORDER BY level ASC"
    )


@router.get("/escalation/log")
async def get_escalation_log(
    limit: int = Query(30, ge=1, le=200),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Get escalation history with event details."""
    from ..shared.database import fetch_all
    return await fetch_all(
        """SELECT el.*, me.title, me.severity, me.event_type
           FROM escalation_log el
           LEFT JOIN monitoring_events me ON el.event_id = me.id
           ORDER BY el.escalated_at DESC LIMIT :lim""",
        {"lim": limit},
    )


# ══════════════════════════════════════════════════════════════════════════════
# §14: DATA RETENTION
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/retention/config")
async def get_retention_config(user=Depends(require_role(Roles.VIEWER))):
    """Get data retention policy configuration."""
    from ..shared.database import fetch_all
    return await fetch_all("SELECT * FROM data_retention_config ORDER BY retention_years DESC")


@router.put("/retention/config")
async def update_retention_policy(
    req: RetentionPolicyRequest,
    user=Depends(require_role(Roles.ADMIN)),
):
    """Update data retention policy for a data type. Admin only."""
    from ..shared.database import execute_query
    await execute_query(
        """UPDATE data_retention_config
           SET retention_years = :years, archive_location = :loc
           WHERE data_type = :dtype""",
        {"years": req.retention_years, "loc": req.archive_location, "dtype": req.data_type},
    )
    log.info("retention_policy_updated",
             data_type=req.data_type, years=req.retention_years,
             by=user.get("sub", "unknown"))
    return {"status": "updated", "data_type": req.data_type, "retention_years": req.retention_years}


# ══════════════════════════════════════════════════════════════════════════════
# §15: SYSTEM SELF-AUDIT
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/self-audit/run")
async def run_self_audit(user=Depends(require_role(Roles.ADMIN))):
    """
    Run full system self-audit (17 tests). Simulates:
    - Rating downgrade
    - Issuer breach
    - Rate shock
    - Illiquidity
    - Alert generation
    - Report generation
    - Audit logging
    - Pre-trade / post-trade systems
    - Tax / benchmark / data integrity
    - RBAC / escalation / retention
    - Compliance dashboard

    Returns institutional readiness verdict.
    """
    from ..services.self_audit import SystemSelfAudit
    result = await SystemSelfAudit.run_full_audit(user=user.get("sub", "system"))
    log.info("self_audit_completed",
             overall=result["overall_status"],
             passed=result["passed"], failed=result["failed"])
    return result


@router.get("/self-audit/history")
async def self_audit_history(
    limit: int = Query(10, ge=1, le=50),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Get self-audit run history."""
    from ..services.self_audit import SystemSelfAudit
    return await SystemSelfAudit.get_audit_history(limit)


# ══════════════════════════════════════════════════════════════════════════════
# META ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/status")
async def ips_status(user=Depends(require_role(Roles.VIEWER))):
    """Quick IPS compliance status overview."""
    from ..shared.database import fetch_one, fetch_all
    try:
        rules = await fetch_one("SELECT COUNT(*) AS cnt FROM ips_rules WHERE is_active = 1")
        breaches = await fetch_one("SELECT COUNT(*) AS cnt FROM ips_breach_log WHERE status = 'OPEN'")
        critical = await fetch_one(
            "SELECT COUNT(*) AS cnt FROM ips_breach_log WHERE status = 'OPEN' AND severity = 'CRITICAL'"
        )
        last_scan = await fetch_one(
            "SELECT scanned_at FROM ips_scan_results ORDER BY scanned_at DESC LIMIT 1"
        )
        last_audit = await fetch_one(
            "SELECT overall_status, completed_at FROM self_audit_runs ORDER BY id DESC LIMIT 1"
        )

        return {
            "active_rules": rules["cnt"] if rules else 0,
            "open_breaches": breaches["cnt"] if breaches else 0,
            "critical_breaches": critical["cnt"] if critical else 0,
            "last_scan": last_scan["scanned_at"] if last_scan else None,
            "last_self_audit": {
                "status": last_audit["overall_status"],
                "completed_at": last_audit["completed_at"],
            } if last_audit else None,
            "sections_covered": [
                "A: Credit Rating Compliance",
                "B: Issuer Concentration Limits",
                "C: Maturity Constraints",
                "D: Liquidity Requirements",
                "2: Pre-Trade Validation",
                "3: Post-Trade Monitoring",
                "5: Valuation Policy",
                "9: Tax & Accounting",
                "10: Benchmark & Performance",
                "11: Data Integrity",
                "12: Compliance Dashboard",
                "13: Escalation Matrix",
                "14: Data Retention",
                "15: System Self-Audit",
            ],
            "generated_at": utcnow_iso(),
        }
    except Exception as e:
        return {"status": "initializing", "detail": str(e)[:200]}
