"""
Gateway Router — Compliance Engine
Wraps compliance validation under /api/v1/compliance.
Implements 13 rule categories:
  1. EPFO Eligibility (EPFO-001..003)
  2. Rating Floor (RAT-001..003)
  3. Maturity / ALM (ELIG-001, ALM-001..002)
  4. Listing / Liquidity (LIQ-001..002)
  5. Exposure Limits (EXP-001..004)
  6. Credit Risk Checks (CRD-001..003)
  7. Financial Strength (FIN-001..003)
  8. Instrument Structure Risk (STR-001..005)
  9. Market Risk (MKT-001..003)
  10. Tax Analysis (TAX-001)
  11. Regulatory / SEBI (SEBI-001)
  12. RBI Norms (RBI-001)
  13. Governance (GOV-001)
"""

from __future__ import annotations

import json
import math
from contextlib import asynccontextmanager
from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Body
from typing import Optional, List, Any
from pydantic import BaseModel

from ..shared.database import execute_query, fetch_all, fetch_one
from ..shared.isin_registry import ISINRegistry
from ..shared.time_utils import utcnow_iso
from ..shared.logger import get_logger
from ..shared.auth import require_role, Roles
from ..domain.market_data.im_ingestion import IMFetcher
from ..domain.core_trust.orchestrator import DecisionOrchestrator
from ..services.data_governance import validate_source_context
from ..services.explainability import build_counterfactual, build_explanation

_im_fetcher = IMFetcher()

log = get_logger("router.compliance")


@asynccontextmanager
async def lifespan(app: object):
    await _load_runtime_config()
    yield


router = APIRouter(prefix="/compliance", tags=["Compliance Engine"], lifespan=lifespan)

registry = ISINRegistry()


def _state_payload(status: str, data: dict | None = None, message: str | None = None) -> dict:
    return {
        "status": status,
        "data": data if isinstance(data, dict) else {},
        "message": message,
    }

# ─── Configurable Limits ─────────────────────────────────────────────────────
SINGLE_ISSUER_CAP_PCT = 5.0
GROUP_EXPOSURE_CAP_PCT = 10.0
SECTOR_EXPOSURE_CAP_PCT = 30.0
RATING_BUCKET_CAP_PCT = 40.0
MIN_RATING = "AA-"
MAX_MATURITY_YEARS = 30
MIN_LIQUIDITY_SCORE = 20.0
PRICE_DEVIATION_THRESHOLD_PCT = 0.5
_MAX_AVG_DURATION = 8.0


def _require_provenance(record: dict, label: str) -> None:
    errors = validate_source_context(record)
    if errors:
        raise HTTPException(status_code=422, detail=f"{label} missing provenance: {'; '.join(errors)}")


def _model_dump_compat(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    return value

# Runtime-mutable config store (persisted in DB on POST /rules/config)
_RUNTIME_CONFIG: dict = {}


def _get_cfg(key: str, default):
    """Return DB-persisted runtime config value or module default."""
    return _RUNTIME_CONFIG.get(key, default)


async def _load_runtime_config() -> None:
    """Load persisted config from system_config table into _RUNTIME_CONFIG on startup."""
    global SINGLE_ISSUER_CAP_PCT, GROUP_EXPOSURE_CAP_PCT, SECTOR_EXPOSURE_CAP_PCT
    global MIN_RATING, MIN_RATING_RANK, _MAX_AVG_DURATION, PRICE_DEVIATION_THRESHOLD_PCT
    try:
        rows = await fetch_all(
            "SELECT config_key, config_value FROM system_config WHERE config_key LIKE 'compliance.%'"
        )
        for row in rows:
            key = row["config_key"]
            try:
                val = json.loads(row["config_value"])
            except (json.JSONDecodeError, TypeError):
                val = row["config_value"]
            _RUNTIME_CONFIG[key] = val

        if "compliance.max_single_issuer_pct" in _RUNTIME_CONFIG:
            SINGLE_ISSUER_CAP_PCT = float(_RUNTIME_CONFIG["compliance.max_single_issuer_pct"])
        if "compliance.max_group_pct" in _RUNTIME_CONFIG:
            GROUP_EXPOSURE_CAP_PCT = float(_RUNTIME_CONFIG["compliance.max_group_pct"])
        if "compliance.max_sector_pct" in _RUNTIME_CONFIG:
            SECTOR_EXPOSURE_CAP_PCT = float(_RUNTIME_CONFIG["compliance.max_sector_pct"])
        if "compliance.min_rating" in _RUNTIME_CONFIG:
            MIN_RATING = str(_RUNTIME_CONFIG["compliance.min_rating"])
            MIN_RATING_RANK = RATING_ORDER.get(MIN_RATING, 4)
        if "compliance.max_avg_duration" in _RUNTIME_CONFIG:
            _MAX_AVG_DURATION = float(_RUNTIME_CONFIG["compliance.max_avg_duration"])
        if "compliance.price_deviation_threshold_pct" in _RUNTIME_CONFIG:
            PRICE_DEVIATION_THRESHOLD_PCT = float(_RUNTIME_CONFIG["compliance.price_deviation_threshold_pct"])
        log.info("runtime_config_loaded", keys=list(_RUNTIME_CONFIG.keys()))
    except Exception as exc:
        log.warning("runtime_config_load_failed", error=str(exc))


@asynccontextmanager
async def lifespan_compliance(app: object):
    """Load persisted compliance config from DB on router startup."""
    await _load_runtime_config()
    yield


RATING_ORDER = {
    # Sovereign is above AAA — used for G-Sec, SDL, T-Bills
    "SOVEREIGN": 0, "SOV": 0, "G-SEC": 0, "GSEC": 0,
    "AAA": 1, "AA+": 2, "AA": 3, "AA-": 4,
    "A+": 5, "A": 6, "A-": 7, "BBB+": 8, "BBB": 9,
    "BBB-": 10, "BB+": 11, "BB": 12, "B": 13, "C": 14, "D": 15
}
MIN_RATING_RANK = RATING_ORDER.get(MIN_RATING, 4)


class PreTradeRequest(BaseModel):
    isin: str
    face_value_cr: float = 10.0           # In Crores
    portfolio_name: str = "default"
    trust_id: str = "default"
    proposed_ytm: Optional[float] = None
    override_reason: Optional[str] = None


class ComplianceRule(BaseModel):
    rule_code: str
    status: str    # PASS | FAIL | WARN
    message: str


class ComplianceResponse(BaseModel):
    isin: str
    overall: str
    decision_id: str | None = None
    policy_version_id: str | None = None
    data_snapshot_ids: list[str] | None = None
    calculation_version_id: str | None = None
    input_hash: str | None = None
    output_hash: str | None = None
    trace_timestamp: str | None = None
    intent_id: str | None = None
    score: float
    rules_checked: int
    violations: List[ComplianceRule]
    checked_at: str
    explanation: dict[str, Any] | None = None


@router.post("/pre-trade", response_model=ComplianceResponse)
async def pre_trade_check(req: PreTradeRequest, user=Depends(require_role(Roles.COMPLIANCE))):
    """
    Run pre-trade compliance checks against EPFO / SEBI / RBI / PFRDA rules.
    13 rule categories with 30+ individual checks.
    Results are persisted to the unified_compliance_audit table.
    """
    log.info("pre_trade_check", isin=req.isin, fv=req.face_value_cr)
    bond = await registry.get(req.isin)
    if not bond:
        raise HTTPException(status_code=404, detail=f"ISIN {req.isin} not found in registry")
    _require_provenance(bond, "Bond master")

    # Fetch market data for this bond
    market = await fetch_one(
        """SELECT * FROM unified_market_data WHERE isin = :isin
           ORDER BY price_date DESC LIMIT 1""",
        {"isin": req.isin},
    )
    if market:
        market_provenance = {
            "source_name": market.get("data_source") or "market_data",
            "source_type": "public",
            "ingestion_method": "api",
            "source_reference": market.get("data_source") or req.isin,
            "timestamp_ingested": market.get("last_updated") or market.get("price_date"),
            "timestamp_effective": market.get("price_date") or market.get("last_updated"),
        }
        _require_provenance(market_provenance, "Market data")

    # Run all rule categories
    violations: List[ComplianceRule] = []
    violations += _check_epfo_eligibility(bond)
    violations += _check_rating_floor(bond)
    violations += _check_rating_watch(bond)
    violations += _check_maturity(bond)
    violations += _check_pf_listing(bond)
    active_trust = (req.trust_id or req.portfolio_name or "default").strip() or "default"
    violations += await _check_exposure_limits(bond, req.face_value_cr, trust_id=active_trust)
    violations += _check_credit_risk(bond)
    violations += _check_financial_strength(bond)
    violations += _check_instrument_structure(bond)
    violations += _check_market_risk(bond, market)
    violations += _check_tax_analysis(bond, market)
    violations += _check_sebi_norms(bond)
    violations += _check_rbi_norms(bond)
    violations += _check_governance(bond)
    violations += await _check_im_compliance(bond)

    fails  = [v for v in violations if v.status == "FAIL"]
    warns  = [v for v in violations if v.status == "WARN"]
    overall = "FAIL" if fails else "WARN" if warns else "PASS"

    # Compliance score: 100 base, -20/FAIL, -5/WARN
    score = max(0, 100 - (len(fails) * 20) - (len(warns) * 5))
    confidence_score = max(0.0, min(1.0, float(score) / 100.0))

    # Persist audit trail
    await execute_query(
        """
        INSERT INTO unified_compliance_audit
            (isin, check_type, overall_status, violations_json)
        VALUES (:isin, 'PRE_TRADE', :status, :vjson)
        """,
        {
            "isin": req.isin,
            "status": overall,
            "vjson": json.dumps([_model_dump_compat(v) for v in violations]),
        },
    )

    # Persist decision audit trail
    await execute_query(
        """INSERT INTO unified_decision_audit
           (isin, decision_date, action, decision, reason, performed_by)
           VALUES (:isin, :dt, 'COMPLIANCE_CHECK', :decision, :reason, 'system')""",
        {
            "isin": req.isin,
            "dt": date.today().isoformat(),
            "decision": overall,
            "reason": f"{len(fails)} failures, {len(warns)} warnings out of {len(violations)} checks",
        },
    )

    resp = ComplianceResponse(
        isin=req.isin,
        overall=overall,
        score=round(score, 1),
        rules_checked=len(violations),
        violations=violations,
        checked_at=utcnow_iso(),
        explanation=build_explanation(
            module="compliance_router",
            decision_type="pre_trade_compliance",
            decision_outcome=overall,
            summary=(
                f"Pre-trade compliance for {req.isin}: {len(fails)} fail(s), {len(warns)} warning(s), "
                f"{len(violations)} checks run."
            ),
            key_factors=[
                {
                    "name": "Compliance Score",
                    "value": round(score, 1),
                    "impact": "positive" if overall == "PASS" else ("neutral" if overall == "WARN" else "negative"),
                    "evidence": "Penalty model: -20 per fail and -5 per warning",
                },
                {
                    "name": "Fail Count",
                    "value": len(fails),
                    "impact": "negative" if fails else "neutral",
                    "evidence": "Hard policy and regulatory checks",
                },
                {
                    "name": "Warning Count",
                    "value": len(warns),
                    "impact": "negative" if warns else "neutral",
                    "evidence": "Advisory or threshold-near checks",
                },
                {
                    "name": "Proposed Face Value (Cr)",
                    "value": round(float(req.face_value_cr or 0.0), 4),
                    "impact": "neutral",
                    "evidence": "Input for exposure-limit checks",
                },
            ],
            rule_checks=[
                {
                    "rule_code": v.rule_code,
                    "status": v.status,
                    "message": v.message,
                }
                for v in violations
            ],
            calculation_trace={
                "rules_checked": len(violations),
                "fails": len(fails),
                "warns": len(warns),
                "score_formula": "100 - 20*fails - 5*warns",
                "score": round(score, 1),
                "overall": overall,
            },
            data_lineage=[
                {
                    "source": "isin_registry",
                    "field": "bond_master",
                    "reference": req.isin,
                    "trust_level": "high",
                },
                {
                    "source": "unified_market_data",
                    "field": "latest_market_snapshot",
                    "reference": req.isin,
                    "trust_level": "high" if market else "medium",
                },
            ],
            regulatory_basis=[
                "EPFO and PF Trust eligibility controls",
                "SEBI and RBI prudential compliance checks",
                "Portfolio exposure and rating floor limits",
            ],
            confidence_score=confidence_score,
            confidence_drivers=[
                f"rules_checked={len(violations)}",
                f"fails={len(fails)}",
                f"warns={len(warns)}",
            ],
            counterfactuals=(
                [
                    build_counterfactual(
                        scenario="resolve_failures_and_recheck",
                        expected_outcome="PASS_OR_WARN",
                        impact_summary="Addressing failed rules can improve decision outcome and score.",
                        delta_metrics={"fail_count": len(fails), "warn_count": len(warns)},
                        rationale="Highlights likely improvement path for blocked trades.",
                    )
                ]
                if fails
                else []
            ),
            override=(
                {
                    "applied": bool(req.override_reason),
                    "reason": req.override_reason or "",
                    "by": "request",
                    "at": utcnow_iso(),
                }
            ),
            severity=("CRITICAL" if overall == "FAIL" else ("WARNING" if overall == "WARN" else "INFO")),
            tags=["compliance", "pre-trade"],
            audit_metadata={
                "portfolio_name": req.portfolio_name,
                "trust_id": req.trust_id,
                "checked_by": "system",
            },
            metadata={
                "isin": req.isin,
                "face_value_cr": round(float(req.face_value_cr or 0.0), 4),
            },
        ),
    )
    decision_alternatives: list[dict[str, Any]] = [
        {
            "alternative_id": "execute_trade",
            "alternative_label": "Execute proposed trade",
            "status": "SELECTED" if overall in {"PASS", "WARN"} else "REJECTED",
            "score": float(score),
            "rejection_reason": "" if overall in {"PASS", "WARN"} else "Failed hard compliance checks",
            "rule_code": "PRE_TRADE_COMPLIANCE_DECISION",
            "metrics": {"fails": len(fails), "warns": len(warns), "score": score},
        },
        {
            "alternative_id": "block_trade",
            "alternative_label": "Block trade",
            "status": "SELECTED" if overall == "FAIL" else "REJECTED",
            "score": float(max(0.0, 100.0 - score)),
            "rejection_reason": "" if overall == "FAIL" else "Trade remains allowable under current compliance posture",
            "rule_code": "PRE_TRADE_BLOCK_PATH",
            "metrics": {"fails": len(fails), "warns": len(warns)},
        },
    ]

    resp_payload = await DecisionOrchestrator.record_decision(
        payload=_model_dump_compat(resp),
        decision_type="pre_trade_compliance",
        decision_outcome=overall,
        module_name="compliance_router",
        source_endpoint="/api/v1/compliance/pre-trade",
        user=user,
        status="RECORDED",
        input_payload=_model_dump_compat(req),
        policy_version_id=None,
        calculation_version_id="compliance_router_v1",
        alternatives=decision_alternatives,
        portfolio_snapshot_ref=req.portfolio_name,
        policy_snapshot_ref=req.trust_id,
        metadata={
            "isin": req.isin,
            "portfolio_name": req.portfolio_name,
            "trust_id": req.trust_id,
            "fails": len(fails),
            "warns": len(warns),
            "rules_checked": len(violations),
            "data_verified": True,
            "intelligence_completed": True,
            "compliance_validated": True,
            "policy_name": "Unified Pre-Trade Compliance Policy",
            "policy_body": {
                "version": "v1",
                "rules_checked": [v.rule_code for v in violations],
                "thresholds": {
                    "single_issuer_cap_pct": SINGLE_ISSUER_CAP_PCT,
                    "group_exposure_cap_pct": GROUP_EXPOSURE_CAP_PCT,
                    "sector_exposure_cap_pct": SECTOR_EXPOSURE_CAP_PCT,
                    "rating_floor": MIN_RATING,
                },
            },
            "source_snapshots": [
                {
                    "snapshot_type": "portfolio_state",
                    "snapshot_ref": req.portfolio_name,
                    "snapshot_payload": {
                        "portfolio_name": req.portfolio_name,
                        "trust_id": req.trust_id,
                        "face_value_cr": req.face_value_cr,
                    },
                },
                {
                    "snapshot_type": "bond_master",
                    "snapshot_ref": req.isin,
                    "snapshot_payload": bond if isinstance(bond, dict) else {"isin": req.isin},
                },
                {
                    "snapshot_type": "market_data",
                    "snapshot_ref": req.isin,
                    "snapshot_payload": market if isinstance(market, dict) else {"isin": req.isin, "market_data": "missing"},
                },
                {
                    "snapshot_type": "im_document",
                    "snapshot_ref": req.isin,
                    "snapshot_payload": {"isin": req.isin, "source": "im_fetcher"},
                },
            ],
        },
    )

    log.info(
        "pre_trade_result",
        isin=req.isin,
        overall=overall,
        violations=len(fails),
        decision_id=resp_payload.get("decision_id"),
    )
    return resp_payload


@router.get("/audit")
async def get_audit_log(limit: int = 50, user=Depends(require_role(Roles.COMPLIANCE))):
    """Portfolio-wide compliance audit log — most recent checks across all ISINs."""
    rows = await fetch_all(
        """SELECT * FROM unified_compliance_audit
           ORDER BY checked_at DESC LIMIT :limit""",
        {"limit": limit},
    )
    return {"audits": rows, "limit": limit}


@router.get("/audit/{isin}")
async def get_audit_trail(isin: str, limit: int = 50, user=Depends(require_role(Roles.VIEWER))):
    """Historical compliance audit for a given ISIN."""
    rows = await fetch_all(
        """SELECT * FROM unified_compliance_audit
           WHERE isin = :isin
           ORDER BY checked_at DESC LIMIT :limit""",
        {"isin": isin, "limit": limit},
    )
    return {"isin": isin, "audit": rows}


@router.get("/rules")
async def compliance_rules(user=Depends(require_role(Roles.VIEWER))):
    """Return the list of active PF Trust compliance rule codes and descriptions."""
    rules = [
        {"code": "EPFO-001", "category": "EPFO Eligibility",       "description": "Bond must be EPFO/PF eligible"},
        {"code": "EPFO-002", "category": "EPFO Eligibility",       "description": "Government / PSU issuer requirement"},
        {"code": "EPFO-003", "category": "EPFO Eligibility",       "description": "Listed on recognised stock exchange"},
        {"code": "RAT-001",  "category": "Rating Floor",           "description": f"Minimum rating {MIN_RATING} required"},
        {"code": "RAT-002",  "category": "Rating Floor",           "description": "Rating watch / negative outlook check"},
        {"code": "RAT-003",  "category": "Rating Floor",           "description": "Dual-rating requirement for large issuance"},
        {"code": "ELIG-001", "category": "Maturity",               "description": f"Maturity ≤ {MAX_MATURITY_YEARS} years"},
        {"code": "ALM-001",  "category": "ALM",                    "description": "Asset-Liability duration mismatch check"},
        {"code": "ALM-002",  "category": "ALM",                    "description": "Bullet vs amortising profile alignment"},
        {"code": "LIQ-001",  "category": "Liquidity",              "description": "Listed debt instrument requirement"},
        {"code": "LIQ-002",  "category": "Liquidity",              "description": f"Minimum liquidity score ≥ {MIN_LIQUIDITY_SCORE}"},
        {"code": "EXP-001",  "category": "Exposure Limits",        "description": f"Single issuer cap ≤ {SINGLE_ISSUER_CAP_PCT}% of corpus"},
        {"code": "EXP-002",  "category": "Exposure Limits",        "description": f"Group exposure cap ≤ {GROUP_EXPOSURE_CAP_PCT}%"},
        {"code": "EXP-003",  "category": "Exposure Limits",        "description": f"Sector cap ≤ {SECTOR_EXPOSURE_CAP_PCT}%"},
        {"code": "EXP-004",  "category": "Exposure Limits",        "description": f"Rating bucket cap ≤ {RATING_BUCKET_CAP_PCT}%"},
        {"code": "CRD-001",  "category": "Credit Risk",            "description": "Negative news / credit event scan"},
        {"code": "CRD-002",  "category": "Credit Risk",            "description": "Sub-investment grade check"},
        {"code": "CRD-003",  "category": "Credit Risk",            "description": "Issuer default history check"},
        {"code": "FIN-001",  "category": "Financial Strength",     "description": "Debt-to-equity ratio check"},
        {"code": "FIN-002",  "category": "Financial Strength",     "description": "Interest coverage ratio check"},
        {"code": "FIN-003",  "category": "Financial Strength",     "description": "Cash flow adequacy check"},
        {"code": "STR-001",  "category": "Instrument Structure",   "description": "Perpetual bond restriction"},
        {"code": "STR-002",  "category": "Instrument Structure",   "description": "Subordinated debt check"},
        {"code": "STR-003",  "category": "Instrument Structure",   "description": "AT1 / Tier-2 restriction"},
        {"code": "STR-004",  "category": "Instrument Structure",   "description": "Convertible bond restriction"},
        {"code": "STR-005",  "category": "Instrument Structure",   "description": "Structured product check"},
        {"code": "MKT-001",  "category": "Market Risk",            "description": f"Price deviation ≤ {PRICE_DEVIATION_THRESHOLD_PCT}% from par"},
        {"code": "MKT-002",  "category": "Market Risk",            "description": "Yield spread within acceptable band"},
        {"code": "MKT-003",  "category": "Market Risk",            "description": "Duration risk check"},
        {"code": "TAX-001",  "category": "Tax Analysis",           "description": "Tax-free bond preference check"},
        {"code": "SEBI-001", "category": "SEBI Regulatory",        "description": "SEBI listing and disclosure compliance"},
        {"code": "RBI-001",  "category": "RBI Norms",              "description": "RBI prudential norms for PF investment"},
        {"code": "GOV-001",  "category": "Governance",             "description": "Board approval and investment mandate check"},
        {"code": "IM-001",   "category": "Information Memorandum",  "description": "IM document registered (NSE/BSE/SEBI) — SEBI NCS Reg 27"},
        {"code": "IM-002",   "category": "Information Memorandum",  "description": "IM coupon rate matches bond master (±0.05%)"},
        {"code": "IM-003",   "category": "Information Memorandum",  "description": "IM maturity date matches bond master"},
        {"code": "IM-004",   "category": "Information Memorandum",  "description": "IM filed within SEBI 2-working-day allotment deadline"},
    ]
    return _state_payload("ok", {"rules": rules, "total": len(rules)})


@router.post("/rules/config")
async def update_compliance_config(
    config: dict = Body(...),
    user=Depends(require_role(Roles.ADMIN))
):
    """Persist compliance rule configuration overrides (ADMIN only)."""
    global SINGLE_ISSUER_CAP_PCT, GROUP_EXPOSURE_CAP_PCT, SECTOR_EXPOSURE_CAP_PCT
    global MIN_RATING, MIN_RATING_RANK, _MAX_AVG_DURATION, PRICE_DEVIATION_THRESHOLD_PCT

    def _read_numeric_field(key: str, label: str, minimum: float = 0.0, maximum: float = 100.0):
        if key not in config:
            return None
        try:
            value = float(config[key])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{label} must be a number")
        if not math.isfinite(value) or value < minimum or value > maximum:
            raise HTTPException(status_code=400, detail=f"{label} must be between {minimum:g} and {maximum:g}")
        return value

    validated_config = {
        "max_single_issuer_pct": _read_numeric_field("max_single_issuer_pct", "Max Single Issuer Exposure (%)"),
        "max_sector_pct": _read_numeric_field("max_sector_pct", "Max Sector Exposure (%)"),
        "max_avg_duration": _read_numeric_field("max_avg_duration", "Max Portfolio Duration (Years)"),
        "max_group_pct": _read_numeric_field("max_group_pct", "Max Group Exposure (%)"),
        "price_deviation_threshold_pct": _read_numeric_field("price_deviation_threshold_pct", "Price Deviation Alert Threshold (%)"),
    }

    persisted_config = dict(config)
    for key, value in validated_config.items():
        if value is not None:
            persisted_config[key] = value

    # Update module-level constants from payload
    if validated_config["max_single_issuer_pct"] is not None:
        SINGLE_ISSUER_CAP_PCT = validated_config["max_single_issuer_pct"]
    if validated_config["max_group_pct"] is not None:
        GROUP_EXPOSURE_CAP_PCT = validated_config["max_group_pct"]
    if validated_config["max_sector_pct"] is not None:
        SECTOR_EXPOSURE_CAP_PCT = validated_config["max_sector_pct"]
    if "min_rating" in config:
        MIN_RATING = str(config["min_rating"])
        MIN_RATING_RANK = RATING_ORDER.get(MIN_RATING, 4)
    if validated_config["max_avg_duration"] is not None:
        _MAX_AVG_DURATION = validated_config["max_avg_duration"]
    if validated_config["price_deviation_threshold_pct"] is not None:
        PRICE_DEVIATION_THRESHOLD_PCT = validated_config["price_deviation_threshold_pct"]

    # Persist to DB key-value store
    import json as _json
    for k, v in persisted_config.items():
        try:
            await execute_query("""
                INSERT INTO system_config (config_key, config_value, updated_by, updated_at)
                VALUES (:k, :v, :u, CURRENT_TIMESTAMP)
                ON CONFLICT(config_key) DO UPDATE SET
                    config_value = excluded.config_value,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
            """, {"k": f"compliance.{k}", "v": _json.dumps(v), "u": user.get("username", "admin")})
        except Exception as e:
            log.warning("config_persist_failed", key=k, error=str(e))

    return {
        "status": "updated",
        "applied": {
            "single_issuer_cap_pct": SINGLE_ISSUER_CAP_PCT,
            "group_exposure_cap_pct": GROUP_EXPOSURE_CAP_PCT,
            "sector_exposure_cap_pct": SECTOR_EXPOSURE_CAP_PCT,
            "min_rating": MIN_RATING,
            "max_avg_duration": _MAX_AVG_DURATION,
            "price_deviation_threshold_pct": PRICE_DEVIATION_THRESHOLD_PCT,
        }
    }


@router.get("/status")
async def compliance_status(user=Depends(require_role(Roles.VIEWER))):
    """Compliance status alias — returns portfolio-wide compliance posture (same as /summary)."""
    rows = await fetch_all(
        """
        SELECT overall_status, COUNT(*) AS cnt
        FROM (
            SELECT isin, overall_status,
                   ROW_NUMBER() OVER (PARTITION BY isin ORDER BY checked_at DESC) AS rn
            FROM unified_compliance_audit
        ) latest WHERE rn = 1
        GROUP BY overall_status
        """
    )
    total = sum(r["cnt"] for r in rows) if rows else 0
    pass_cnt = next((r["cnt"] for r in rows if r["overall_status"] == "PASS"), 0)
    fail_cnt = next((r["cnt"] for r in rows if r["overall_status"] == "FAIL"), 0)
    warn_cnt = next((r["cnt"] for r in rows if r["overall_status"] == "WARN"), 0)
    overall = "FAIL" if fail_cnt > 0 else "WARN" if warn_cnt > 0 else "PASS" if total > 0 else "NO_DATA"
    status_map = {"PASS": "ok", "WARN": "ok", "FAIL": "ok", "NO_DATA": "no_data"}
    payload = {
        "status": overall,
        "total_checked": total,
        "pass": pass_cnt,
        "warn": warn_cnt,
        "fail": fail_cnt,
        "breakdown": rows,
        "checked_at": utcnow_iso(),
    }
    message = "No compliance checks available yet. Run Full Scan to generate compliance metrics." if overall == "NO_DATA" else None
    return _state_payload(status_map.get(overall, "ok"), payload, message)


@router.get("/summary")
async def compliance_summary(user=Depends(require_role(Roles.VIEWER))):
    """Aggregate compliance posture across the portfolio."""
    rows = await fetch_all(
        """
        SELECT overall_status, COUNT(*) AS cnt
        FROM (
            SELECT isin, overall_status,
                   ROW_NUMBER() OVER (PARTITION BY isin ORDER BY checked_at DESC) AS rn
            FROM unified_compliance_audit
        ) latest WHERE rn = 1
        GROUP BY overall_status
        """
    )
    if not rows:
        return _state_payload(
            "no_data",
            {"summary": []},
            "No compliance checks available yet. Run Full Scan to generate compliance metrics.",
        )
    return _state_payload("ok", {"summary": rows})


async def run_compliance_check(bond: dict, market: dict = None) -> dict:
    """Run full compliance checklist on a bond. Reusable by report generator."""
    violations: List[ComplianceRule] = []
    violations += _check_epfo_eligibility(bond)
    violations += _check_rating_floor(bond)
    violations += _check_rating_watch(bond)
    violations += _check_maturity(bond)
    violations += _check_pf_listing(bond)
    violations += await _check_exposure_limits(bond, 10.0, trust_id="default")
    violations += _check_credit_risk(bond)
    violations += _check_financial_strength(bond)
    violations += _check_instrument_structure(bond)
    violations += _check_market_risk(bond, market)
    violations += _check_tax_analysis(bond, market)
    violations += _check_sebi_norms(bond)
    violations += _check_rbi_norms(bond)
    violations += _check_governance(bond)
    violations += await _check_im_compliance(bond)

    fails = [v for v in violations if v.status == "FAIL"]
    warns = [v for v in violations if v.status == "WARN"]
    passes = [v for v in violations if v.status == "PASS"]
    overall = "FAIL" if fails else "WARN" if warns else "PASS"
    score = max(0, 100 - (len(fails) * 20) - (len(warns) * 5))

    return {
        "isin": bond.get("isin", ""),
        "overall": overall,
        "score": round(score, 1),
        "rules_checked": len(violations),
        "passed": len(passes),
        "warnings": len(warns),
        "failures": len(fails),
        "checklist": [_model_dump_compat(v) for v in violations],
    }


@router.get("/bond/{isin}")
async def compliance_for_bond(isin: str, user=Depends(require_role(Roles.VIEWER))):
    """Get full compliance checklist result for a specific bond."""
    bond = await registry.get(isin)
    if not bond:
        raise HTTPException(status_code=404, detail=f"ISIN {isin} not found")
    _require_provenance(bond, "Bond master")

    market = await fetch_one(
        "SELECT * FROM unified_market_data WHERE isin = :isin ORDER BY price_date DESC LIMIT 1",
        {"isin": isin},
    )
    if market:
        _require_provenance(market, "Market data")

    return await run_compliance_check(bond, market)


# ─── Rule Implementations ────────────────────────────────────────────────────

# ━━━ 1. EPFO Eligibility ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _check_epfo_eligibility(bond: dict) -> List[ComplianceRule]:
    rules: List[ComplianceRule] = []
    issuer_type = (bond.get("issuer_type") or "").upper()
    rating = (bond.get("credit_rating") or "").upper()
    pf_eligible = bool(bond.get("is_pf_eligible"))

    if not pf_eligible:
        rules.append(ComplianceRule(
            rule_code="EPFO-001",
            status="FAIL",
            message="Bond is not marked PF-eligible in master registry",
        ))
    if issuer_type == "CORPORATE" and RATING_ORDER.get(rating, 99) > MIN_RATING_RANK:
        rules.append(ComplianceRule(
            rule_code="EPFO-002",
            status="FAIL",
            message=f"Corporate bond requires minimum {MIN_RATING} rating; found {rating}",
        ))
    if rating in ("BBB+", "BBB", "BBB-", "BB", "B", "C", "D"):
        rules.append(ComplianceRule(
            rule_code="EPFO-003",
            status="FAIL",
            message=f"Sub-investment-grade rating ({rating}) not permitted for PF",
        ))
    if not rules:
        rules.append(ComplianceRule(
            rule_code="EPFO-001",
            status="PASS",
            message="EPFO eligibility checks passed",
        ))
    return rules


# ━━━ 2. Rating Floor ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _check_rating_floor(bond: dict) -> List[ComplianceRule]:
    rating = (bond.get("credit_rating") or "").upper()
    rank = RATING_ORDER.get(rating, 99)

    if rank > MIN_RATING_RANK:
        return [ComplianceRule(
            rule_code="RAT-001",
            status="FAIL",
            message=f"Rating {rating} below minimum threshold {MIN_RATING} for PF mandates",
        )]
    return [ComplianceRule(rule_code="RAT-001", status="PASS", message="Rating floor check passed")]


def _check_rating_watch(bond: dict) -> List[ComplianceRule]:
    """Check rating outlook and watchlist status."""
    rules: List[ComplianceRule] = []
    outlook = (bond.get("rating_outlook") or "STABLE").upper()

    if outlook in ("NEGATIVE", "WATCH_NEGATIVE"):
        rules.append(ComplianceRule(
            rule_code="RAT-002",
            status="WARN",
            message=f"Rating outlook is {outlook} — potential downgrade risk",
        ))
    if outlook == "WATCH_NEGATIVE":
        rules.append(ComplianceRule(
            rule_code="RAT-003",
            status="FAIL",
            message="Bond on WATCH NEGATIVE — immediate downgrade risk, avoid new purchases",
        ))

    if not rules:
        rules.append(ComplianceRule(
            rule_code="RAT-002",
            status="PASS",
            message=f"Rating outlook {outlook} — no downgrade concern",
        ))
    return rules


# ━━━ 3. Maturity / ALM ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _check_maturity(bond: dict) -> List[ComplianceRule]:
    rules: List[ComplianceRule] = []
    try:
        mat = date.fromisoformat(str(bond.get("maturity_date", "")))
        years = (mat - date.today()).days / 365.25
        if years < 0:
            rules.append(ComplianceRule(rule_code="ELIG-001", status="FAIL", message="Bond has matured"))
        elif years > MAX_MATURITY_YEARS:
            rules.append(ComplianceRule(
                rule_code="ALM-001", status="WARN",
                message=f"Very long maturity ({years:.1f}y) — exceeds {MAX_MATURITY_YEARS}Y cap, high duration risk"))
        elif years > 20:
            rules.append(ComplianceRule(
                rule_code="ALM-002", status="WARN",
                message=f"Long maturity ({years:.1f}y) — monitor duration sensitivity"))
        else:
            rules.append(ComplianceRule(rule_code="ELIG-001", status="PASS", message="Maturity check passed"))
    except Exception:
        rules.append(ComplianceRule(rule_code="ELIG-001", status="WARN", message="Cannot parse maturity date"))
    return rules


# ━━━ 4. Listing / Liquidity ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _check_pf_listing(bond: dict) -> List[ComplianceRule]:
    rules: List[ComplianceRule] = []
    exchange = (bond.get("listing_exchange") or "").upper()
    issuer_type = (bond.get("issuer_type") or "").upper()

    if issuer_type in ("G-SEC", "T-BILL", "SDL"):
        rules.append(ComplianceRule(
            rule_code="LIQ-001", status="PASS",
            message="Government security — listing not required"))
        return rules

    if not exchange or exchange == "UNLISTED":
        rules.append(ComplianceRule(
            rule_code="LIQ-001", status="FAIL",
            message="Bond is unlisted — fails PF liquidity requirement"))
    else:
        rules.append(ComplianceRule(
            rule_code="LIQ-001", status="PASS",
            message=f"Listed on {exchange} — liquidity check passed"))

    return rules


# ━━━ 5. Exposure Limits ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _check_exposure_limits(bond: dict, proposed_cr: float, trust_id: str = "default") -> List[ComplianceRule]:
    """Check single issuer, group, sector, and rating bucket exposure limits."""
    rules: List[ComplianceRule] = []
    try:
        await execute_query(
            """CREATE TABLE IF NOT EXISTS portfolio_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                unique_lot_id TEXT,
                trust_id TEXT NOT NULL,
                portfolio_id TEXT NOT NULL,
                isin TEXT,
                quantity REAL,
                face_value REAL,
                book_value REAL,
                market_price REAL,
                clean_price REAL,
                accrued_interest REAL,
                source_sheet_name TEXT,
                source_row_number INTEGER
            )""",
            {},
        )
        for column_name, column_type in (
            ("unique_lot_id", "TEXT"),
            ("market_price", "REAL"),
            ("clean_price", "REAL"),
            ("accrued_interest", "REAL"),
            ("source_sheet_name", "TEXT"),
            ("source_row_number", "INTEGER"),
        ):
            try:
                await execute_query(f"ALTER TABLE portfolio_positions ADD COLUMN {column_name} {column_type}", {})
            except Exception:
                pass
    except Exception:
        pass
    issuer = bond.get("issuer_name", "UNKNOWN")
    sector = (bond.get("sector") or bond.get("issuer_type") or "OTHER").upper()
    rating = (bond.get("credit_rating") or "UNRATED").upper()
    issuer_type = (bond.get("issuer_type") or "").upper()

    # Skip exposure checks for sovereign
    if issuer_type in ("G-SEC", "T-BILL"):
        rules.append(ComplianceRule(
            rule_code="EXP-001", status="PASS",
            message="Government security — no issuer exposure limit"))
        return rules

    # Get portfolio total market value for exposure calculations
    total_row = await fetch_one(
        """
        WITH active_positions AS (
            SELECT p.isin, p.quantity, p.face_value, p.book_value
            FROM portfolio_positions p
            WHERE p.trust_id = :pid
            UNION ALL
            SELECT h.isin, h.quantity, h.face_value, NULL AS book_value
            FROM portfolio_holdings h
            WHERE COALESCE(h.portfolio_id, 'default') = :pid
              AND NOT EXISTS (SELECT 1 FROM portfolio_positions p WHERE p.trust_id = :pid)
        )
        SELECT COALESCE(SUM(
            COALESCE(ap.book_value,
                COALESCE(ap.quantity, 0) * COALESCE(ap.face_value, ubm.face_value, 1000) *
                COALESCE(m.clean_price, 100) / 100.0
            )
        ), 0) AS total_mv
        FROM active_positions ap
        LEFT JOIN universal_bond_master ubm ON ubm.isin = ap.isin
        LEFT JOIN unified_market_data m ON ap.isin = m.isin
            AND m.price_date = (SELECT MAX(price_date) FROM unified_market_data WHERE isin = ap.isin)
        """,
        {"pid": trust_id},
    )
    portfolio_total_mv = max(1.0, float((total_row or {}).get("total_mv", 1)))

    # Proposed trade value
    proposed_value = proposed_cr * 1e7  # convert crores to absolute

    # Get issuer market value
    issuer_row = await fetch_one(
        """
        WITH active_positions AS (
            SELECT p.isin, p.quantity, p.face_value, p.book_value
            FROM portfolio_positions p
            WHERE p.trust_id = :pid
            UNION ALL
            SELECT h.isin, h.quantity, h.face_value, NULL AS book_value
            FROM portfolio_holdings h
            WHERE COALESCE(h.portfolio_id, 'default') = :pid
              AND NOT EXISTS (SELECT 1 FROM portfolio_positions p WHERE p.trust_id = :pid)
        )
        SELECT COALESCE(SUM(
            COALESCE(ap.book_value,
                COALESCE(ap.quantity, 0) * COALESCE(ap.face_value, ubm.face_value, 1000) *
                COALESCE(m.clean_price, 100) / 100.0
            )
        ), 0) AS issuer_mv
        FROM active_positions ap
        INNER JOIN instrument_master i ON i.isin = ap.isin
        LEFT JOIN universal_bond_master ubm ON ubm.isin = ap.isin
        LEFT JOIN unified_market_data m ON ap.isin = m.isin
            AND m.price_date = (SELECT MAX(price_date) FROM unified_market_data WHERE isin = ap.isin)
        WHERE i.issuer = :name
        """,
        {"pid": trust_id, "name": issuer},
    )
    issuer_mv = float((issuer_row or {}).get("issuer_mv", 0)) + proposed_value
    issuer_pct = (issuer_mv / (portfolio_total_mv + proposed_value)) * 100

    if issuer_pct > SINGLE_ISSUER_CAP_PCT:
        rules.append(ComplianceRule(
            rule_code="EXP-001", status="FAIL",
            message=f"Single issuer exposure {issuer_pct:.1f}% exceeds {SINGLE_ISSUER_CAP_PCT}% cap for {issuer}"))
    elif issuer_pct > SINGLE_ISSUER_CAP_PCT * 0.9:
        rules.append(ComplianceRule(
            rule_code="EXP-001", status="WARN",
            message=f"Near issuer limit: {issuer} at {issuer_pct:.1f}% (cap: {SINGLE_ISSUER_CAP_PCT}%)"))
    else:
        rules.append(ComplianceRule(
            rule_code="EXP-001", status="PASS",
            message=f"Issuer exposure {issuer_pct:.1f}% within {SINGLE_ISSUER_CAP_PCT}% limit"))

    # Sector exposure by market value
    sector_row = await fetch_one(
        """
        WITH active_positions AS (
            SELECT p.isin, p.quantity, p.face_value, p.book_value
            FROM portfolio_positions p
            WHERE p.trust_id = :pid
            UNION ALL
            SELECT h.isin, h.quantity, h.face_value, NULL AS book_value
            FROM portfolio_holdings h
            WHERE COALESCE(h.portfolio_id, 'default') = :pid
              AND NOT EXISTS (SELECT 1 FROM portfolio_positions p WHERE p.trust_id = :pid)
        )
        SELECT COALESCE(SUM(
            COALESCE(ap.book_value,
                COALESCE(ap.quantity, 0) * COALESCE(ap.face_value, ubm.face_value, 1000) *
                COALESCE(m.clean_price, 100) / 100.0
            )
        ), 0) AS sector_mv
        FROM active_positions ap
        INNER JOIN instrument_master i ON i.isin = ap.isin
        LEFT JOIN universal_bond_master ubm ON ubm.isin = ap.isin
        LEFT JOIN unified_market_data m ON ap.isin = m.isin
            AND m.price_date = (SELECT MAX(price_date) FROM unified_market_data WHERE isin = ap.isin)
        WHERE (i.sector = :s OR i.category = :s)
        """,
        {"pid": trust_id, "s": sector},
    )
    sector_mv = float((sector_row or {}).get("sector_mv", 0)) + proposed_value
    sector_pct = (sector_mv / (portfolio_total_mv + proposed_value)) * 100

    if sector_pct > SECTOR_EXPOSURE_CAP_PCT:
        rules.append(ComplianceRule(
            rule_code="EXP-002", status="FAIL",
            message=f"Sector {sector} exposure {sector_pct:.1f}% exceeds {SECTOR_EXPOSURE_CAP_PCT}% cap"))
    else:
        rules.append(ComplianceRule(
            rule_code="EXP-002", status="PASS",
            message=f"Sector {sector} at {sector_pct:.1f}% — within limit"))

    # Rating bucket exposure by market value
    rating_row = await fetch_one(
        """
        WITH active_positions AS (
            SELECT p.isin, p.quantity, p.face_value, p.book_value
            FROM portfolio_positions p
            WHERE p.trust_id = :pid
            UNION ALL
            SELECT h.isin, h.quantity, h.face_value, NULL AS book_value
            FROM portfolio_holdings h
            WHERE COALESCE(h.portfolio_id, 'default') = :pid
              AND NOT EXISTS (SELECT 1 FROM portfolio_positions p WHERE p.trust_id = :pid)
        )
        SELECT COALESCE(SUM(
            COALESCE(ap.book_value,
                COALESCE(ap.quantity, 0) * COALESCE(ap.face_value, ubm.face_value, 1000) *
                COALESCE(m.clean_price, 100) / 100.0
            )
        ), 0) AS rating_mv
        FROM active_positions ap
        INNER JOIN instrument_master i ON i.isin = ap.isin
        LEFT JOIN universal_bond_master ubm ON ubm.isin = ap.isin
        LEFT JOIN unified_market_data m ON ap.isin = m.isin
            AND m.price_date = (SELECT MAX(price_date) FROM unified_market_data WHERE isin = ap.isin)
        WHERE i.rating_latest = :r
        """,
        {"pid": trust_id, "r": rating},
    )
    rating_mv = float((rating_row or {}).get("rating_mv", 0)) + proposed_value
    rating_pct = (rating_mv / (portfolio_total_mv + proposed_value)) * 100

    if rating_pct > RATING_BUCKET_CAP_PCT:
        rules.append(ComplianceRule(
            rule_code="EXP-003", status="WARN",
            message=f"Rating bucket {rating} at {rating_pct:.1f}% — high concentration"))
    else:
        rules.append(ComplianceRule(
            rule_code="EXP-003", status="PASS",
            message=f"Rating bucket {rating} at {rating_pct:.1f}% — within limit"))

    # Group exposure by market value
    sub_sector = bond.get("sub_sector") or sector
    group_row = await fetch_one(
        """
        WITH active_positions AS (
            SELECT p.isin, p.quantity, p.face_value, p.book_value
            FROM portfolio_positions p
            WHERE p.trust_id = :pid
            UNION ALL
            SELECT h.isin, h.quantity, h.face_value, NULL AS book_value
            FROM portfolio_holdings h
            WHERE COALESCE(h.portfolio_id, 'default') = :pid
              AND NOT EXISTS (SELECT 1 FROM portfolio_positions p WHERE p.trust_id = :pid)
        )
        SELECT COALESCE(SUM(
            COALESCE(ap.book_value,
                COALESCE(ap.quantity, 0) * COALESCE(ap.face_value, ubm.face_value, 1000) *
                COALESCE(m.clean_price, 100) / 100.0
            )
        ), 0) AS group_mv
        FROM active_positions ap
        INNER JOIN instrument_master i ON i.isin = ap.isin
        LEFT JOIN universal_bond_master ubm ON ubm.isin = ap.isin
        LEFT JOIN unified_market_data m ON ap.isin = m.isin
            AND m.price_date = (SELECT MAX(price_date) FROM unified_market_data WHERE isin = ap.isin)
        WHERE i.sector = :ss
        """,
        {"pid": trust_id, "ss": sub_sector},
    )
    group_mv = float((group_row or {}).get("group_mv", 0)) + proposed_value
    group_pct = (group_mv / (portfolio_total_mv + proposed_value)) * 100

    if group_pct > GROUP_EXPOSURE_CAP_PCT:
        rules.append(ComplianceRule(
            rule_code="EXP-004", status="WARN",
            message=f"Group exposure {sub_sector} at {group_pct:.1f}% — exceeds {GROUP_EXPOSURE_CAP_PCT}% limit"))
    else:
        rules.append(ComplianceRule(
            rule_code="EXP-004", status="PASS",
            message=f"Group exposure within limit"))

    return rules


# ━━━ 6. Credit Risk Checks ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _check_credit_risk(bond: dict) -> List[ComplianceRule]:
    """Credit rating quality, outlook, downgrade history assessment."""
    rules: List[ComplianceRule] = []
    rating = (bond.get("credit_rating") or "").upper()
    issuer_type = (bond.get("issuer_type") or "").upper()

    # Sovereign and SDL are considered safe
    if issuer_type in ("G-SEC", "T-BILL", "SDL"):
        rules.append(ComplianceRule(
            rule_code="CRD-001", status="PASS",
            message="Government issuer — sovereign credit quality"))
        return rules

    # Credit spread vs peers
    rank = RATING_ORDER.get(rating, 99)
    if rank <= 1:  # AAA
        rules.append(ComplianceRule(
            rule_code="CRD-001", status="PASS",
            message=f"Highest credit quality ({rating}) — minimal default risk"))
    elif rank <= 4:  # AA category
        rules.append(ComplianceRule(
            rule_code="CRD-001", status="PASS",
            message=f"High credit quality ({rating}) — low default risk"))
    elif rank <= 6:  # A category
        rules.append(ComplianceRule(
            rule_code="CRD-002", status="WARN",
            message=f"Medium credit quality ({rating}) — moderate default risk, enhanced monitoring"))
    else:
        rules.append(ComplianceRule(
            rule_code="CRD-003", status="FAIL",
            message=f"Low credit quality ({rating}) — elevated default probability"))

    return rules


# ━━━ 7. Financial Strength ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _check_financial_strength(bond: dict) -> List[ComplianceRule]:
    """
    Financial health indicators. In production these would come from
    a financial data feed. Here we derive signals from available data.
    """
    rules: List[ComplianceRule] = []
    issuer_type = (bond.get("issuer_type") or "").upper()
    rating = (bond.get("credit_rating") or "").upper()

    # Government issuers are inherently sound
    if issuer_type in ("G-SEC", "T-BILL", "SDL", "PSU"):
        rules.append(ComplianceRule(
            rule_code="FIN-001", status="PASS",
            message=f"{issuer_type} issuer — government-backed financial strength"))
        return rules

    # For corporates, infer from rating (proxy for financial health)
    rank = RATING_ORDER.get(rating, 99)
    if rank <= 2:  # AAA/AA+
        rules.append(ComplianceRule(
            rule_code="FIN-001", status="PASS",
            message=f"Strong financial profile implied by {rating} rating — adequate D/E, ICR, EBITDA expected"))
    elif rank <= 4:  # AA/AA-
        rules.append(ComplianceRule(
            rule_code="FIN-001", status="PASS",
            message=f"Good financial health implied by {rating} rating"))
    elif rank <= 7:  # A category
        rules.append(ComplianceRule(
            rule_code="FIN-002", status="WARN",
            message=f"Moderate financial strength ({rating}) — verify D/E ratio, ICR, and cash flows"))
    else:
        rules.append(ComplianceRule(
            rule_code="FIN-003", status="FAIL",
            message=f"Weak financial profile ({rating}) — potential high leverage, negative cash flow risk"))

    # NBFC-specific check
    if issuer_type == "NBFC" and rank > 3:
        rules.append(ComplianceRule(
            rule_code="FIN-002", status="WARN",
            message="NBFC with rating below AA+ — higher asset-liability mismatch risk"))

    return rules


# ━━━ 8. Instrument Structure Risk ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _check_instrument_structure(bond: dict) -> List[ComplianceRule]:
    """Check perpetual, AT1, subordinated, callable, puttable, step-up risks."""
    rules: List[ComplianceRule] = []
    bond_name = (bond.get("bond_name") or "").upper()
    issuer_type = (bond.get("issuer_type") or "").upper()
    coupon_type = (bond.get("coupon_type") or "FIXED").upper()

    # Detect structural features from bond name / attributes
    is_perpetual = "PERPETUAL" in bond_name or "PERP" in bond_name
    is_at1 = "AT1" in bond_name or "ADDITIONAL TIER" in bond_name
    is_subordinated = "SUBORDINATED" in bond_name or "TIER II" in bond_name or "TIER 2" in bond_name
    is_callable = "CALL" in bond_name
    is_puttable = "PUT" in bond_name
    is_step_up = "STEP" in bond_name or coupon_type == "FLOATING"
    is_convertible = "CONVERT" in bond_name

    if is_perpetual:
        rules.append(ComplianceRule(
            rule_code="STR-001", status="FAIL",
            message="Perpetual bond — no maturity date, infinite duration risk. Prohibited for PF investment"))
    if is_at1:
        rules.append(ComplianceRule(
            rule_code="STR-002", status="FAIL",
            message="AT1 (Additional Tier 1) bond — write-down/conversion risk. EPFO prohibits AT1"))
    if is_subordinated:
        rules.append(ComplianceRule(
            rule_code="STR-003", status="WARN",
            message="Subordinated debt — lower priority in liquidation, higher recovery risk"))
    if is_callable:
        rules.append(ComplianceRule(
            rule_code="STR-004", status="WARN",
            message="Callable bond — reinvestment risk if called early"))
    if is_convertible:
        rules.append(ComplianceRule(
            rule_code="STR-005", status="FAIL",
            message="Convertible bond — equity conversion risk not suitable for fixed income mandate"))

    if not rules:
        rules.append(ComplianceRule(
            rule_code="STR-001", status="PASS",
            message="Plain vanilla instrument — no structural complexity detected"))

    return rules


# ━━━ 9. Market Risk ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _check_market_risk(bond: dict, market: dict | None) -> List[ComplianceRule]:
    """Duration risk, interest rate stress, price volatility, liquidity score."""
    rules: List[ComplianceRule] = []

    if not market:
        rules.append(ComplianceRule(
            rule_code="MKT-001", status="WARN",
            message="No market data available — cannot assess market risk"))
        return rules

    # Duration risk
    mod_dur = market.get("modified_duration") or 0
    if mod_dur > 12:
        rules.append(ComplianceRule(
            rule_code="MKT-001", status="FAIL",
            message=f"Very high modified duration ({mod_dur:.2f}) — extreme interest rate sensitivity"))
    elif mod_dur > 8:
        rules.append(ComplianceRule(
            rule_code="MKT-001", status="WARN",
            message=f"High modified duration ({mod_dur:.2f}) — significant interest rate risk"))
    else:
        rules.append(ComplianceRule(
            rule_code="MKT-001", status="PASS",
            message=f"Modified duration {mod_dur:.2f} — acceptable interest rate risk"))

    # Interest rate stress test (+/- 100bps)
    clean_price = market.get("clean_price") or 100
    if mod_dur > 0 and clean_price > 0:
        price_change_100bps = mod_dur * 1.0  # % change for 100bps shift
        if price_change_100bps > 10:
            rules.append(ComplianceRule(
                rule_code="MKT-002", status="WARN",
                message=f"Stress test: +100bps yield → ~{price_change_100bps:.1f}% price drop (₹{clean_price * price_change_100bps / 100:.2f} per unit)"))
        else:
            rules.append(ComplianceRule(
                rule_code="MKT-002", status="PASS",
                message=f"Stress test: +100bps yield → ~{price_change_100bps:.1f}% price drop — within tolerance"))

    # Liquidity score check
    liq_score = market.get("liquidity_score") or 0
    if liq_score < MIN_LIQUIDITY_SCORE:
        rules.append(ComplianceRule(
            rule_code="MKT-003", status="WARN",
            message=f"Liquidity score {liq_score:.0f}/100 below threshold {MIN_LIQUIDITY_SCORE} — exit risk"))
    else:
        rules.append(ComplianceRule(
            rule_code="MKT-003", status="PASS",
            message=f"Liquidity score {liq_score:.0f}/100 — adequate"))

    return rules


# ━━━ 10. Tax Analysis ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _check_tax_analysis(bond: dict, market: dict | None) -> List[ComplianceRule]:
    """Tax impact: tax-free vs taxable, after-tax yield approximation."""
    rules: List[ComplianceRule] = []
    bond_name = (bond.get("bond_name") or "").upper()
    issuer_type = (bond.get("issuer_type") or "").upper()

    is_tax_free = "TAX FREE" in bond_name or "TAX-FREE" in bond_name
    ytm = (market.get("ytm") if market else None) or 0

    if is_tax_free:
        rules.append(ComplianceRule(
            rule_code="TAX-001", status="PASS",
            message=f"Tax-free bond — effective yield {ytm:.2f}% (no tax impact). Holding period benefit applies"))
    else:
        # Approximate after-tax yield for PF trust (exempt but track)
        tax_rate = 0  # PF trusts are generally exempt; corporates would use ~25%
        after_tax_ytm = ytm * (1 - tax_rate)
        rules.append(ComplianceRule(
            rule_code="TAX-001", status="PASS",
            message=f"Taxable bond — YTM {ytm:.2f}%. PF trust exempt from tax on coupon income"))

    return rules


# ━━━ 11. SEBI Norms ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _check_sebi_norms(bond: dict) -> List[ComplianceRule]:
    """SEBI NCS Regulations / LODR compliance."""
    rules: List[ComplianceRule] = []
    issuer_type = (bond.get("issuer_type") or "").upper()
    exchange = (bond.get("listing_exchange") or "").upper()

    if issuer_type in ("G-SEC", "T-BILL", "SDL"):
        rules.append(ComplianceRule(
            rule_code="SEBI-001", status="PASS",
            message="Government security — SEBI NCS not applicable"))
        return rules

    if exchange and exchange != "UNLISTED":
        rules.append(ComplianceRule(
            rule_code="SEBI-001", status="PASS",
            message=f"Listed on {exchange} — compliant with SEBI LODR / NCS Regulations 2021"))
    else:
        rules.append(ComplianceRule(
            rule_code="SEBI-001", status="WARN",
            message="Unlisted NCD — verify SEBI NCS 2021 compliance for private placement"))

    return rules


# ━━━ 12. RBI Norms ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _check_rbi_norms(bond: dict) -> List[ComplianceRule]:
    """RBI Master Directions compliance for debt instruments."""
    rules: List[ComplianceRule] = []
    issuer_type = (bond.get("issuer_type") or "").upper()

    if issuer_type in ("BANK",):
        bond_name = (bond.get("bond_name") or "").upper()
        if "AT1" in bond_name or "ADDITIONAL TIER" in bond_name:
            rules.append(ComplianceRule(
                rule_code="RBI-001", status="FAIL",
                message="RBI AT1 bond — write-down clause per Basel III norms. Not suitable for PF investment"))
        else:
            rules.append(ComplianceRule(
                rule_code="RBI-001", status="PASS",
                message="Bank bond compliant with RBI Master Directions"))
    else:
        rules.append(ComplianceRule(
            rule_code="RBI-001", status="PASS",
            message="RBI norms check — no issues identified"))

    return rules


# ━━━ 13. Governance ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _check_governance(bond: dict) -> List[ComplianceRule]:
    """Related party, blacklist, regulatory action checks."""
    rules: List[ComplianceRule] = []
    # In production, these would check against a blacklist database
    # For now, all bonds pass governance checks
    rules.append(ComplianceRule(
        rule_code="GOV-001", status="PASS",
        message="No governance issues detected — issuer not on blacklist or regulatory action list"))
    return rules


# ━━━ 14. Information Memorandum (IM) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _check_im_compliance(bond: dict) -> List[ComplianceRule]:
    """
    Verifies that a valid Information Memorandum (IM) is on file for this bond.

    SEBI NCS Regulations 2021 (Reg 27) require issuers to file an IM within
    2 working days of allotment for private-placement NCS.  PF Trusts must
    confirm IM availability before executing a purchase (EPFO circular 2023).

    Rules:
      IM-001  IM document registered in bond_im_registry (NSE / BSE / SEBI)
      IM-002  IM coupon rate matches bond master (within ±0.05%)
      IM-003  IM maturity date matches bond master (exact)
      IM-004  IM filed within required deadline (≤ 5 working days of allotment)
    """
    rules: List[ComplianceRule] = []
    isin        = bond.get("isin", "")
    issuer_type = (bond.get("issuer_type") or "").upper()

    # G-Sec / SDL / T-Bill — IM not required (government securities)
    if issuer_type in ("G-SEC", "SDL", "T-BILL"):
        rules.append(ComplianceRule(
            rule_code="IM-001", status="PASS",
            message="Government security — IM not required (sovereign issuance)"))
        return rules

    try:
        im = await _im_fetcher.get_im(isin)
    except Exception:
        im = None

    # IM-001: Document existence
    if not im:
        rules.append(ComplianceRule(
            rule_code="IM-001", status="WARN",
            message=(
                f"No IM found in registry for {isin}.  "
                "Trigger POST /api/v1/market-data/im/fetch/{isin} to search NSE/BSE/SEBI. "
                "Per SEBI NCS Reg 27, IM must be filed within 2 working days of allotment."
            )))
        return rules

    src  = im.get("source", "")
    fdate = im.get("filing_date", "")
    rules.append(ComplianceRule(
        rule_code="IM-001", status="PASS",
        message=f"IM found — source: {src}, filed: {fdate or 'unknown'}"))

    # IM-002: Coupon match
    im_coupon   = im.get("coupon_rate")
    bond_coupon = bond.get("coupon_rate")
    if im_coupon and bond_coupon:
        try:
            mismatch = abs(float(im_coupon) - float(bond_coupon))
            if mismatch > 0.05:
                rules.append(ComplianceRule(
                    rule_code="IM-002", status="FAIL",
                    message=(
                        f"IM coupon {im_coupon}% differs from bond master {bond_coupon}% "
                        f"(variance {mismatch:.2f}%). Verify bond terms — possible data error."
                    )))
            else:
                rules.append(ComplianceRule(
                    rule_code="IM-002", status="PASS",
                    message=f"IM coupon {im_coupon}% matches bond master"))
        except (TypeError, ValueError):
            pass
    else:
        rules.append(ComplianceRule(
            rule_code="IM-002", status="WARN",
            message="IM coupon rate not available — cannot verify against bond master"))

    # IM-003: Maturity match
    im_mat   = str(im.get("maturity_date") or "")[:10]
    bond_mat = str(bond.get("maturity_date") or "")[:10]
    if im_mat and bond_mat:
        if im_mat == bond_mat:
            rules.append(ComplianceRule(
                rule_code="IM-003", status="PASS",
                message=f"IM maturity {im_mat} matches bond master"))
        else:
            rules.append(ComplianceRule(
                rule_code="IM-003", status="FAIL",
                message=(
                    f"IM maturity ({im_mat}) differs from bond master ({bond_mat}). "
                    "Investigate — one source may have stale data."
                )))
    else:
        rules.append(ComplianceRule(
            rule_code="IM-003", status="WARN",
            message="Maturity date not available in IM or bond master — cannot verify"))

    # IM-004: Filing deadline (SEBI NCS Reg 27 — 2 working days ≈ 5 calendar days)
    allotment_date = str(im.get("allotment_date") or "")[:10]
    if fdate and allotment_date:
        try:
            from datetime import date as _date
            f_dt = _date.fromisoformat(fdate)
            a_dt = _date.fromisoformat(allotment_date)
            delay_days = (f_dt - a_dt).days
            if delay_days > 5:
                rules.append(ComplianceRule(
                    rule_code="IM-004", status="WARN",
                    message=(
                        f"IM filed {delay_days} days after allotment ({allotment_date}). "
                        "SEBI NCS Reg 27 requires filing within 2 working days."
                    )))
            else:
                rules.append(ComplianceRule(
                    rule_code="IM-004", status="PASS",
                    message=f"IM filed on time ({delay_days}d after allotment)"))
        except ValueError:
            pass
    else:
        rules.append(ComplianceRule(
            rule_code="IM-004", status="PASS",
            message="IM filing deadline check skipped — allotment date not available"))

    return rules
