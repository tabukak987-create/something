from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..domain.core_trust.orchestrator import DecisionOrchestrator
from ..domain.core_trust.pipeline import TrustDecisionPipeline
from ..shared.auth import Roles, require_role

router = APIRouter(prefix="/explain", tags=["Explainability"])


def _decision_type(bundle: dict) -> str:
    decision = bundle.get("decision") if isinstance(bundle, dict) else {}
    return str((decision or {}).get("decision_type") or "").strip().lower()


async def _load_bundle_or_fail(decision_id: str) -> dict:
    bundle = await TrustDecisionPipeline.build_explain_bundle(decision_id)
    if bundle:
        return bundle

    decision = await DecisionOrchestrator.get_decision(decision_id)
    if not decision:
        raise HTTPException(status_code=404, detail=f"Decision not found: {decision_id}")
    raise HTTPException(
        status_code=412,
        detail=f"Fail-closed explainability contract violated: explanation missing for decision {decision_id}",
    )


@router.get("/decision/{decision_id}")
async def explain_decision(
    decision_id: str,
    user: dict = Depends(require_role(Roles.VIEWER)),
):
    bundle = await _load_bundle_or_fail(decision_id)
    return {
        "decision_id": decision_id,
        "decision_type": _decision_type(bundle),
        "explanation": bundle.get("explanation"),
        "alternatives": bundle.get("alternatives") or [],
        "trace_graph": bundle.get("trace_graph") or {},
        "decision": bundle.get("decision") or {},
    }


@router.get("/compliance/{decision_id}")
async def explain_compliance_decision(
    decision_id: str,
    user: dict = Depends(require_role(Roles.VIEWER)),
):
    bundle = await _load_bundle_or_fail(decision_id)
    dtype = _decision_type(bundle)
    allowed = (
        "pre_trade_compliance",
        "ips_pretrade_simulation",
        "ips_full_scan",
    )
    if dtype not in allowed and "compliance" not in dtype and "ips" not in dtype:
        raise HTTPException(
            status_code=422,
            detail=f"Decision {decision_id} is not a compliance decision (decision_type={dtype})",
        )
    return {
        "decision_id": decision_id,
        "domain": "compliance",
        "decision_type": dtype,
        "explanation": bundle.get("explanation"),
        "alternatives": bundle.get("alternatives") or [],
        "trace_graph": bundle.get("trace_graph") or {},
        "decision": bundle.get("decision") or {},
    }


@router.get("/risk/{decision_id}")
async def explain_risk_decision(
    decision_id: str,
    user: dict = Depends(require_role(Roles.VIEWER)),
):
    bundle = await _load_bundle_or_fail(decision_id)
    dtype = _decision_type(bundle)
    allowed = {
        "full_risk_report",
        "rate_sensitivity",
        "spread_simulation",
        "scenario_analysis",
        "var_estimation",
        "yield_curve_projection",
    }
    if dtype not in allowed and "risk" not in dtype:
        raise HTTPException(
            status_code=422,
            detail=f"Decision {decision_id} is not a risk decision (decision_type={dtype})",
        )
    return {
        "decision_id": decision_id,
        "domain": "risk",
        "decision_type": dtype,
        "explanation": bundle.get("explanation"),
        "alternatives": bundle.get("alternatives") or [],
        "trace_graph": bundle.get("trace_graph") or {},
        "decision": bundle.get("decision") or {},
    }
