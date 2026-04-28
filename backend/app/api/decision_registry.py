from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..domain.core_trust.audit import AuditAction, AuditService, AuditSeverity, EntityType
from ..domain.core_trust.orchestrator import DecisionOrchestrator
from ..domain.core_trust.pipeline import TrustDecisionPipeline
from ..shared.auth import Roles, require_role
from ..shared.logger import get_logger

log = get_logger("router.decision_registry")
router = APIRouter(prefix="/decisions", tags=["Decision Registry"])


class OverrideRequestBody(BaseModel):
    decision_id: str = Field(..., min_length=8)
    reason: str = Field(..., min_length=8)
    required_approvals: int = Field(default=2, ge=2, le=5)


class OverrideApprovalBody(BaseModel):
    decision_id: str = Field(..., min_length=8)
    override_request_id: int = Field(..., ge=1)
    approval_note: str = ""


class PolicyRegisterBody(BaseModel):
    policy_version_id: str = Field(..., min_length=3)
    policy_name: str = Field(..., min_length=3)
    policy_body: dict


class SnapshotRegisterBody(BaseModel):
    snapshot_type: str = Field(..., min_length=3)
    snapshot_payload: dict = Field(default_factory=dict)
    snapshot_ref: Optional[str] = None
    snapshot_hash: Optional[str] = None
    snapshot_id: Optional[str] = None
    captured_at: Optional[str] = None


def _audit_user(user: dict) -> dict:
    return {
        "id": str(user.get("id") or user.get("sub") or user.get("username") or "system"),
        "username": user.get("username") or user.get("sub") or "system",
        "role": user.get("role") or Roles.VIEWER,
    }


@router.get("/")
async def list_decisions(
    limit: int = Query(default=100, ge=1, le=500),
    decision_type: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    user: dict = Depends(require_role(Roles.VIEWER)),
):
    rows = await DecisionOrchestrator.list_decisions(
        limit=limit,
        decision_type=decision_type,
        status=status,
    )
    return {
        "items": rows,
        "count": len(rows),
        "filters": {
            "decision_type": decision_type,
            "status": status,
        },
    }


@router.get("/workbench/summary")
async def get_workbench_summary(
    user: dict = Depends(require_role(Roles.VIEWER)),
):
    recent = await DecisionOrchestrator.list_decisions(limit=50)
    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_outcome: dict[str, int] = {}

    for item in recent:
        dt = str(item.get("decision_type") or "UNKNOWN")
        st = str(item.get("status") or "UNKNOWN")
        oc = str(item.get("decision_outcome") or "UNKNOWN")
        by_type[dt] = by_type.get(dt, 0) + 1
        by_status[st] = by_status.get(st, 0) + 1
        by_outcome[oc] = by_outcome.get(oc, 0) + 1

    return {
        "generated_at": recent[0].get("updated_at") if recent else None,
        "recent_count": len(recent),
        "decision_type_breakdown": by_type,
        "status_breakdown": by_status,
        "outcome_breakdown": by_outcome,
        "recent_items": recent[:20],
    }


@router.get("/intents/pending")
async def list_pending_intents(
    limit: int = Query(default=100, ge=1, le=500),
    user: dict = Depends(require_role(Roles.COMPLIANCE)),
):
    rows = await TrustDecisionPipeline.list_pending_intents(limit=limit)
    return {
        "items": rows,
        "count": len(rows),
    }


@router.post("/intents/reconcile")
async def reconcile_pending_intents(
    limit: int = Query(default=50, ge=1, le=500),
    user: dict = Depends(require_role(Roles.ADMIN)),
):
    summary = await TrustDecisionPipeline.reconcile_incomplete_intents(limit=limit)
    return summary


@router.get("/command-center")
async def get_compliance_command_center(
    limit: int = Query(default=100, ge=1, le=500),
    user: dict = Depends(require_role(Roles.COMPLIANCE)),
):
    return await DecisionOrchestrator.get_command_center_snapshot(limit=limit)


@router.get("/policies")
async def list_policy_versions(
    limit: int = Query(default=200, ge=1, le=1000),
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    items = await TrustDecisionPipeline.list_policy_versions(limit=limit)
    return {"items": items, "count": len(items)}


@router.post("/policies/register")
async def register_policy_version(
    body: PolicyRegisterBody,
    user: dict = Depends(require_role(Roles.ADMIN)),
):
    actor = str(user.get("username") or user.get("sub") or "system")
    result = await TrustDecisionPipeline.register_policy_version(
        policy_version_id=body.policy_version_id,
        policy_name=body.policy_name,
        policy_body=body.policy_body,
        created_by=actor,
    )
    return result


@router.get("/policies/{policy_version_id}")
async def get_policy_version(
    policy_version_id: str,
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    payload = await TrustDecisionPipeline.get_policy_version(policy_version_id)
    if not payload:
        raise HTTPException(status_code=404, detail=f"Policy version not found: {policy_version_id}")
    return payload


@router.get("/snapshots")
async def list_snapshot_catalog(
    snapshot_type: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    items = await TrustDecisionPipeline.list_snapshot_catalog_entries(snapshot_type=snapshot_type, limit=limit)
    return {"items": items, "count": len(items), "snapshot_type": snapshot_type}


@router.post("/snapshots/register")
async def register_snapshot_catalog(
    body: SnapshotRegisterBody,
    user: dict = Depends(require_role(Roles.ADMIN)),
):
    result = await TrustDecisionPipeline.register_snapshot_catalog_entry(
        snapshot_type=body.snapshot_type,
        snapshot_payload=body.snapshot_payload,
        snapshot_ref=body.snapshot_ref,
        snapshot_hash=body.snapshot_hash,
        snapshot_id=body.snapshot_id,
        captured_at=body.captured_at,
    )
    return result


@router.get("/snapshots/{snapshot_id}")
async def get_snapshot_catalog(
    snapshot_id: str,
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    payload = await TrustDecisionPipeline.get_snapshot_catalog_entry(snapshot_id)
    if not payload:
        raise HTTPException(status_code=404, detail=f"Snapshot not found: {snapshot_id}")
    return payload


@router.get("/{decision_id}")
async def get_decision(
    decision_id: str,
    user: dict = Depends(require_role(Roles.VIEWER)),
):
    data = await DecisionOrchestrator.get_decision(decision_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Decision not found: {decision_id}")
    return data


@router.get("/{decision_id}/evidence")
async def get_decision_evidence(
    decision_id: str,
    user: dict = Depends(require_role(Roles.VIEWER)),
):
    payload = await DecisionOrchestrator.get_decision_evidence(decision_id)
    if not payload:
        raise HTTPException(status_code=404, detail=f"Decision not found: {decision_id}")
    return payload


@router.get("/{decision_id}/explanation")
async def get_decision_explanation(
    decision_id: str,
    user: dict = Depends(require_role(Roles.VIEWER)),
):
    payload = await DecisionOrchestrator.get_explanation(decision_id)
    if not payload:
        raise HTTPException(
            status_code=412,
            detail=f"Fail-closed explainability contract violated: explanation missing for decision {decision_id}",
        )
    return payload


@router.get("/{decision_id}/audit-pack")
async def get_decision_audit_pack(
    decision_id: str,
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    pack = await DecisionOrchestrator.get_audit_pack(decision_id)
    if not pack:
        raise HTTPException(status_code=404, detail=f"Decision not found: {decision_id}")

    await AuditService.record(
        user=_audit_user(user),
        action=AuditAction.REPORT_EXPORTED,
        entity_type=EntityType.REPORT,
        entity_id=decision_id,
        old_value=None,
        new_value={"type": "decision_audit_pack", "decision_id": decision_id},
        affected_module="decision_registry.audit_pack",
        severity=AuditSeverity.INFO,
        compliance_flag=True,
        description=f"Decision audit pack exported for {decision_id}",
    )

    return pack


@router.get("/{decision_id}/replay")
async def replay_decision_timeline(
    decision_id: str,
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    timeline = await DecisionOrchestrator.get_replay_timeline(decision_id)
    if not timeline:
        raise HTTPException(status_code=404, detail=f"Decision not found: {decision_id}")
    return timeline


@router.post("/{decision_id}/replay/execute")
async def replay_decision_execute(
    decision_id: str,
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    try:
        result = await TrustDecisionPipeline.replay_decision(decision_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    await AuditService.record(
        user=_audit_user(user),
        action=AuditAction.REPORT_GENERATED,
        entity_type=EntityType.REPORT,
        entity_id=decision_id,
        old_value=None,
        new_value={
            "type": "decision_replay_execute",
            "decision_id": decision_id,
            "replay_status": result.get("replay_status"),
            "expected_output_hash": result.get("expected_output_hash"),
            "actual_output_hash": result.get("actual_output_hash"),
        },
        affected_module="decision_registry.replay_execute",
        severity=AuditSeverity.INFO,
        compliance_flag=True,
        description=f"Decision replay executed for {decision_id}",
    )
    return result


@router.get("/{decision_id}/trust-packet")
async def get_decision_trust_packet(
    decision_id: str,
    user: dict = Depends(require_role(Roles.TRUSTEE)),
):
    packet = await DecisionOrchestrator.build_trust_packet(decision_id)
    if not packet:
        raise HTTPException(status_code=404, detail=f"Decision not found: {decision_id}")

    await AuditService.record(
        user=_audit_user(user),
        action=AuditAction.REPORT_EXPORTED,
        entity_type=EntityType.REPORT,
        entity_id=decision_id,
        old_value=None,
        new_value={
            "type": "decision_trust_packet",
            "decision_id": decision_id,
            "packet_hash_sha256": packet.get("packet_hash_sha256"),
        },
        affected_module="decision_registry.trust_packet",
        severity=AuditSeverity.INFO,
        compliance_flag=True,
        description=f"Decision trust packet exported for {decision_id}",
    )
    return packet


@router.post("/overrides/request")
async def request_override(
    request: Request,
    body: OverrideRequestBody,
    user: dict = Depends(require_role(Roles.COMPLIANCE)),
):
    actor = str(user.get("username") or user.get("sub") or "system")
    try:
        row = await DecisionOrchestrator.create_override_request(
            decision_id=body.decision_id,
            requested_by=actor,
            reason=body.reason,
            required_approvals=body.required_approvals,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    await AuditService.record(
        user=_audit_user(user),
        action=AuditAction.COMPLIANCE_OVERRIDE,
        entity_type=EntityType.TRADE,
        entity_id=body.decision_id,
        old_value=None,
        new_value={
            "override_request_id": row.get("id"),
            "decision_id": body.decision_id,
            "reason": body.reason,
            "required_approvals": body.required_approvals,
        },
        affected_module="decision_registry.override_request",
        ip_address=request.client.host if request.client else None,
        severity=AuditSeverity.WARNING,
        compliance_flag=True,
        description=f"Override request created for decision {body.decision_id}",
    )

    return {
        "status": "PENDING",
        "override_request": row,
    }


@router.post("/overrides/approve")
async def approve_override(
    request: Request,
    body: OverrideApprovalBody,
    user: dict = Depends(require_role(Roles.TRUSTEE)),
):
    approver = str(user.get("username") or user.get("sub") or "system")
    try:
        result = await DecisionOrchestrator.approve_override(
            decision_id=body.decision_id,
            override_request_id=body.override_request_id,
            approved_by=approver,
            approval_note=body.approval_note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await AuditService.record(
        user=_audit_user(user),
        action=AuditAction.MANUAL_OVERRIDE,
        entity_type=EntityType.TRADE,
        entity_id=body.decision_id,
        old_value=None,
        new_value={
            "override_request_id": body.override_request_id,
            "decision_id": body.decision_id,
            "status": result.get("status"),
            "approvals_count": result.get("approvals_count"),
            "required_approvals": result.get("required_approvals"),
            "remaining_approvals": result.get("remaining_approvals"),
            "approval_note": body.approval_note,
        },
        affected_module="decision_registry.override_approve",
        ip_address=request.client.host if request.client else None,
        severity=AuditSeverity.WARNING,
        compliance_flag=True,
        description=(
            f"Override approval recorded for decision {body.decision_id} "
            f"({result.get('approvals_count')}/{result.get('required_approvals')})"
        ),
    )

    log.info(
        "decision_override_approved",
        decision_id=body.decision_id,
        override_request_id=body.override_request_id,
        status=result.get("status"),
        approvals=result.get("approvals_count"),
    )
    return result
