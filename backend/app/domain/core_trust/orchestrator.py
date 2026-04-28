from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from app.shared.database import execute_query, fetch_all, fetch_one
from app.shared.logger import get_logger
from app.shared.time_utils import utcnow_iso
from app.domain.core_trust.pipeline import TrustDecisionPipeline

log = get_logger("service.decision_orchestrator")


DECISION_ORCHESTRATOR_DDL = """
CREATE TABLE IF NOT EXISTS decision_registry (
    decision_id TEXT PRIMARY KEY,
    decision_type TEXT NOT NULL,
    decision_outcome TEXT,
    module_name TEXT NOT NULL,
    source_endpoint TEXT,
    status TEXT NOT NULL DEFAULT 'RECORDED',
    actor TEXT NOT NULL DEFAULT 'system',
    portfolio_snapshot_ref TEXT,
    market_snapshot_ref TEXT,
    policy_snapshot_ref TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dr_type ON decision_registry(decision_type);
CREATE INDEX IF NOT EXISTS idx_dr_status ON decision_registry(status);
CREATE INDEX IF NOT EXISTS idx_dr_created ON decision_registry(created_at DESC);

CREATE TABLE IF NOT EXISTS decision_explanations (
    decision_id TEXT PRIMARY KEY,
    schema_version TEXT,
    explanation_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_de_updated ON decision_explanations(updated_at DESC);

CREATE TABLE IF NOT EXISTS decision_override_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    required_approvals INTEGER NOT NULL DEFAULT 2,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
    resolution_notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_dor_decision ON decision_override_requests(decision_id);
CREATE INDEX IF NOT EXISTS idx_dor_status ON decision_override_requests(status);

CREATE TABLE IF NOT EXISTS decision_override_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    override_request_id INTEGER NOT NULL,
    decision_id TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approval_note TEXT,
    approved_at TEXT NOT NULL,
    UNIQUE(override_request_id, approved_by)
);

CREATE INDEX IF NOT EXISTS idx_doa_request ON decision_override_approvals(override_request_id);
CREATE INDEX IF NOT EXISTS idx_doa_decision ON decision_override_approvals(decision_id);
"""


class DecisionOrchestrator:
    _initialized: bool = False

    @classmethod
    async def initialize(cls) -> None:
        if cls._initialized:
            return
        try:
            for stmt in DECISION_ORCHESTRATOR_DDL.split(";"):
                sql = stmt.strip()
                if not sql:
                    continue
                await execute_query(sql + ";")
            await TrustDecisionPipeline.initialize()
            log.info("decision_orchestrator_initialized")
            cls._initialized = True
        except Exception as exc:
            log.error("decision_orchestrator_init_failed", error=str(exc))

    @staticmethod
    def _to_json(value: Any) -> str:
        try:
            return json.dumps(value, default=str)
        except Exception:
            return json.dumps({"unserializable": str(value)})

    @staticmethod
    def _from_json(value: Any, default: Any) -> Any:
        if value is None:
            return default
        if isinstance(value, (dict, list)):
            return value
        if isinstance(value, (bytes, bytearray, memoryview)):
            try:
                value = bytes(value).decode("utf-8")
            except Exception:
                return default
        if not isinstance(value, str):
            return default
        raw = value.strip()
        if not raw:
            return default
        try:
            return json.loads(raw)
        except Exception:
            return default

    @staticmethod
    def _actor(user: Any) -> str:
        if isinstance(user, dict):
            return str(
                user.get("username")
                or user.get("sub")
                or user.get("id")
                or user.get("user_id")
                or "system"
            )
        if user is None:
            return "system"
        return str(user)

    @staticmethod
    def _coerce_payload(payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            return dict(payload)
        return {"value": payload}

    @classmethod
    async def record_decision(
        cls,
        *,
        payload: Any,
        decision_type: str,
        decision_outcome: str,
        module_name: str,
        source_endpoint: str,
        user: Any = None,
        status: str = "RECORDED",
        decision_id: Optional[str] = None,
        input_payload: Optional[dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        policy_version_id: Optional[str] = None,
        data_snapshot_ids: Optional[list[str]] = None,
        calculation_version_id: Optional[str] = None,
        alternatives: Optional[list[dict[str, Any]]] = None,
        portfolio_snapshot_ref: Optional[str] = None,
        market_snapshot_ref: Optional[str] = None,
        policy_snapshot_ref: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        await cls.initialize()
        return await TrustDecisionPipeline.record_decision_fail_closed(
            payload=payload,
            decision_type=decision_type,
            decision_outcome=decision_outcome,
            module_name=module_name,
            source_endpoint=source_endpoint,
            user=user,
            status=status,
            decision_id=decision_id,
            input_payload=input_payload,
            idempotency_key=idempotency_key,
            policy_version_id=policy_version_id,
            data_snapshot_ids=data_snapshot_ids,
            calculation_version_id=calculation_version_id,
            alternatives=alternatives,
            portfolio_snapshot_ref=portfolio_snapshot_ref,
            market_snapshot_ref=market_snapshot_ref,
            policy_snapshot_ref=policy_snapshot_ref,
            metadata=metadata,
        )

    @classmethod
    async def list_decisions(
        cls,
        *,
        limit: int = 100,
        decision_type: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        await cls.initialize()
        clauses = ["1=1"]
        params: dict[str, Any] = {"limit": max(1, min(limit, 500))}

        if decision_type:
            clauses.append("decision_type = :decision_type")
            params["decision_type"] = decision_type
        if status:
            clauses.append("status = :status")
            params["status"] = status

        try:
            rows = await fetch_all(
                f"""
                SELECT *
                FROM decision_registry
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC
                LIMIT :limit
                """,
                params,
            )
        except Exception as exc:
            log.warning("decision_registry_list_failed", error=str(exc))
            return []

        return [
            {
                **dict(row),
                "metadata": cls._from_json(row.get("metadata_json"), {}),
                "data_snapshot_ids": cls._from_json(row.get("data_snapshot_ids_json"), []),
            }
            for row in rows
        ]

    @classmethod
    async def get_explanation(cls, decision_id: str) -> Optional[dict[str, Any]]:
        await cls.initialize()
        try:
            row = await fetch_one(
                "SELECT decision_id, schema_version, explanation_json, updated_at FROM decision_explanations WHERE decision_id = :decision_id",
                {"decision_id": decision_id},
            )
        except Exception as exc:
            log.warning("decision_explanation_lookup_failed", decision_id=decision_id, error=str(exc))
            return None
        if not row:
            return None

        payload = cls._from_json(row.get("explanation_json"), {})
        if not isinstance(payload, dict):
            payload = {"summary": str(payload)}
        payload.setdefault("decision_id", decision_id)
        payload.setdefault("schema_version", row.get("schema_version") or payload.get("schema_version"))
        return payload

    @classmethod
    async def get_decision(cls, decision_id: str) -> Optional[dict[str, Any]]:
        await cls.initialize()
        try:
            row = await fetch_one(
                "SELECT * FROM decision_registry WHERE decision_id = :decision_id",
                {"decision_id": decision_id},
            )
        except Exception as exc:
            log.warning("decision_lookup_failed", decision_id=decision_id, error=str(exc))
            return None
        if not row:
            return None

        explanation = await cls.get_explanation(decision_id)
        alternatives = await TrustDecisionPipeline.get_decision_alternatives(decision_id)
        trace_graph = await TrustDecisionPipeline.get_trace_graph(decision_id)
        intent = await TrustDecisionPipeline.get_intent_for_decision(decision_id)
        requests = await fetch_all(
            """
            SELECT *
            FROM decision_override_requests
            WHERE decision_id = :decision_id
            ORDER BY created_at DESC, id DESC
            """,
            {"decision_id": decision_id},
        )
        approvals = await fetch_all(
            """
            SELECT *
            FROM decision_override_approvals
            WHERE decision_id = :decision_id
            ORDER BY approved_at DESC, id DESC
            """,
            {"decision_id": decision_id},
        )

        return {
            **dict(row),
            "metadata": cls._from_json(row.get("metadata_json"), {}),
            "data_snapshot_ids": cls._from_json(row.get("data_snapshot_ids_json"), []),
            "explanation": explanation,
            "alternatives": alternatives,
            "trace_graph": trace_graph,
            "intent": intent,
            "override_requests": [dict(item) for item in requests],
            "override_approvals": [dict(item) for item in approvals],
        }

    @classmethod
    async def create_override_request(
        cls,
        *,
        decision_id: str,
        requested_by: str,
        reason: str,
        required_approvals: int = 2,
    ) -> dict[str, Any]:
        await cls.initialize()
        decision = await cls.get_decision(decision_id)
        if not decision:
            raise ValueError(f"Decision not found: {decision_id}")

        now = utcnow_iso()
        min_approvals = max(2, int(required_approvals or 2))

        await execute_query(
            """
            INSERT INTO decision_override_requests (
                decision_id,
                requested_by,
                reason,
                status,
                required_approvals,
                created_at
            ) VALUES (
                :decision_id,
                :requested_by,
                :reason,
                'PENDING',
                :required_approvals,
                :created_at
            )
            """,
            {
                "decision_id": decision_id,
                "requested_by": requested_by,
                "reason": reason,
                "required_approvals": min_approvals,
                "created_at": now,
            },
        )

        row = await fetch_one(
            """
            SELECT *
            FROM decision_override_requests
            WHERE decision_id = :decision_id
            ORDER BY id DESC
            LIMIT 1
            """,
            {"decision_id": decision_id},
        )

        return dict(row or {})

    @classmethod
    async def approve_override(
        cls,
        *,
        decision_id: str,
        override_request_id: int,
        approved_by: str,
        approval_note: str = "",
    ) -> dict[str, Any]:
        await cls.initialize()
        req = await fetch_one(
            """
            SELECT *
            FROM decision_override_requests
            WHERE id = :id AND decision_id = :decision_id
            """,
            {"id": override_request_id, "decision_id": decision_id},
        )
        if not req:
            raise ValueError(f"Override request not found: {override_request_id}")

        current_status = str(req.get("status") or "").upper()
        if current_status in {"REJECTED", "CANCELLED"}:
            raise ValueError(f"Override request {override_request_id} is {current_status}")

        requester = str(req.get("requested_by") or "")
        if requester and requester == approved_by:
            raise ValueError("Requester cannot self-approve an override request")

        now = utcnow_iso()
        try:
            await execute_query(
                """
                INSERT INTO decision_override_approvals (
                    override_request_id,
                    decision_id,
                    approved_by,
                    approval_note,
                    approved_at
                ) VALUES (
                    :override_request_id,
                    :decision_id,
                    :approved_by,
                    :approval_note,
                    :approved_at
                )
                """,
                {
                    "override_request_id": override_request_id,
                    "decision_id": decision_id,
                    "approved_by": approved_by,
                    "approval_note": approval_note,
                    "approved_at": now,
                },
            )
        except Exception:
            # Duplicate approvals are ignored via UNIQUE constraint.
            pass

        count_row = await fetch_one(
            "SELECT COUNT(*) AS cnt FROM decision_override_approvals WHERE override_request_id = :id",
            {"id": override_request_id},
        )
        approvals_count = int((count_row or {}).get("cnt") or 0)
        required_approvals = max(2, int(req.get("required_approvals") or 2))

        if approvals_count >= required_approvals:
            request_status = "APPROVED"
            await execute_query(
                """
                UPDATE decision_override_requests
                SET status = :status,
                    resolved_at = :resolved_at,
                    resolved_by = :resolved_by,
                    resolution_notes = :resolution_notes
                WHERE id = :id
                """,
                {
                    "status": request_status,
                    "resolved_at": now,
                    "resolved_by": approved_by,
                    "resolution_notes": approval_note,
                    "id": override_request_id,
                },
            )
            await execute_query(
                """
                UPDATE decision_registry
                SET status = 'OVERRIDDEN', updated_at = :updated_at
                WHERE decision_id = :decision_id
                """,
                {"updated_at": now, "decision_id": decision_id},
            )
        else:
            request_status = "PARTIALLY_APPROVED"
            await execute_query(
                "UPDATE decision_override_requests SET status = :status WHERE id = :id",
                {"status": request_status, "id": override_request_id},
            )

        return {
            "decision_id": decision_id,
            "override_request_id": override_request_id,
            "status": request_status,
            "required_approvals": required_approvals,
            "approvals_count": approvals_count,
            "remaining_approvals": max(required_approvals - approvals_count, 0),
        }

    @classmethod
    async def get_audit_pack(cls, decision_id: str) -> Optional[dict[str, Any]]:
        await cls.initialize()
        decision = await cls.get_decision(decision_id)
        if not decision:
            return None

        institutional_audit = await fetch_all(
            """
            SELECT *
            FROM institutional_audit_log
            WHERE entity_id = :decision_id
            ORDER BY timestamp_utc DESC, id DESC
            LIMIT 500
            """,
            {"decision_id": decision_id},
        )
        canonical_audit = await fetch_all(
            """
            SELECT *
            FROM audit_logs
            WHERE entity_id = :decision_id
            ORDER BY timestamp DESC
            LIMIT 500
            """,
            {"decision_id": decision_id},
        )

        return {
            "decision": decision,
            "institutional_audit": [dict(row) for row in institutional_audit],
            "canonical_audit": [dict(row) for row in canonical_audit],
            "generated_at": utcnow_iso(),
        }

    @classmethod
    async def get_decision_evidence(cls, decision_id: str) -> Optional[dict[str, Any]]:
        await cls.initialize()
        decision = await cls.get_decision(decision_id)
        if not decision:
            return None

        snapshots = await TrustDecisionPipeline.get_decision_snapshots(decision_id)
        data_snapshot_ids = list(decision.get("data_snapshot_ids") or [])
        catalog_snapshots = await TrustDecisionPipeline.get_catalog_snapshots(data_snapshot_ids)
        policy = await TrustDecisionPipeline.get_policy_version(str(decision.get("policy_version_id") or ""))
        replay_runs = await TrustDecisionPipeline.list_replay_runs(decision_id, limit=200)
        replay_timeline = await cls.get_replay_timeline(decision_id)
        audit_pack = await cls.get_audit_pack(decision_id)

        return {
            "decision_id": decision_id,
            "decision": decision,
            "policy": policy,
            "snapshot_catalog": catalog_snapshots,
            "decision_snapshots": snapshots,
            "alternatives": decision.get("alternatives") or [],
            "override_requests": decision.get("override_requests") or [],
            "override_approvals": decision.get("override_approvals") or [],
            "replay_timeline": replay_timeline,
            "replay_runs": replay_runs,
            "audit_pack": audit_pack,
            "override_workflow": {
                "request_endpoint": "/api/v1/decisions/overrides/request",
                "approve_endpoint": "/api/v1/decisions/overrides/approve",
                "direct_override_enabled": False,
            },
            "generated_at": utcnow_iso(),
        }

    @classmethod
    async def get_command_center_snapshot(cls, *, limit: int = 100) -> dict[str, Any]:
        await cls.initialize()

        lim = max(1, min(int(limit or 100), 500))

        pending_overrides: list[dict[str, Any]] = []
        flagged_decisions: list[dict[str, Any]] = []
        compliance_audit: list[dict[str, Any]] = []
        ips_breaches: list[dict[str, Any]] = []
        recent_alerts: list[dict[str, Any]] = []
        feed_status: dict[str, dict[str, Any]] = {}

        try:
            rows = await fetch_all(
                """
                SELECT *
                FROM decision_override_requests
                WHERE status IN ('PENDING', 'PARTIALLY_APPROVED')
                ORDER BY created_at DESC, id DESC
                LIMIT :limit
                """,
                {"limit": lim},
            )
            pending_overrides = [dict(row) for row in rows]
            feed_status["override_requests"] = {"status": "OK", "count": len(pending_overrides)}
        except Exception as exc:
            log.warning("command_center_pending_overrides_failed", error=str(exc))
            feed_status["override_requests"] = {"status": "ERROR", "error": str(exc)}

        try:
            rows = await fetch_all(
                """
                SELECT *
                FROM decision_registry
                WHERE UPPER(COALESCE(decision_outcome, '')) IN ('FAIL', 'WARN', 'ERROR')
                ORDER BY updated_at DESC
                LIMIT :limit
                """,
                {"limit": lim},
            )
            flagged_decisions = [
                {
                    **dict(row),
                    "metadata": cls._from_json(row.get("metadata_json"), {}),
                }
                for row in rows
            ]
            feed_status["decision_registry"] = {"status": "OK", "count": len(flagged_decisions)}
        except Exception as exc:
            log.warning("command_center_flagged_decisions_failed", error=str(exc))
            feed_status["decision_registry"] = {"status": "ERROR", "error": str(exc)}

        try:
            rows = await fetch_all(
                """
                SELECT *
                FROM unified_compliance_audit
                ORDER BY checked_at DESC, id DESC
                LIMIT :limit
                """,
                {"limit": lim},
            )
            compliance_audit = [dict(row) for row in rows]
            feed_status["unified_compliance_audit"] = {"status": "OK", "count": len(compliance_audit)}
        except Exception as exc:
            compliance_audit = []
            feed_status["unified_compliance_audit"] = {"status": "ERROR", "error": str(exc)}

        try:
            rows = await fetch_all(
                """
                SELECT *
                FROM ips_breach_log
                ORDER BY detected_at DESC, id DESC
                LIMIT :limit
                """,
                {"limit": lim},
            )
            ips_breaches = [dict(row) for row in rows]
            feed_status["ips_breach_log"] = {"status": "OK", "count": len(ips_breaches)}
        except Exception as exc:
            ips_breaches = []
            feed_status["ips_breach_log"] = {"status": "ERROR", "error": str(exc)}

        try:
            rows = await fetch_all(
                """
                SELECT *
                FROM alerts
                ORDER BY created_at DESC, id DESC
                LIMIT :limit
                """,
                {"limit": lim},
            )
            recent_alerts = [dict(row) for row in rows]
            feed_status["alerts"] = {"status": "OK", "count": len(recent_alerts)}
        except Exception as exc:
            recent_alerts = []
            feed_status["alerts"] = {"status": "ERROR", "error": str(exc)}

        try:
            ops_status = await TrustDecisionPipeline.command_center_ops_status()
            feed_status["decision_intent"] = {"status": "OK"}
        except Exception as exc:
            ops_status = {
                "intent_backlog": None,
                "intent_failures": None,
                "generated_at": utcnow_iso(),
            }
            feed_status["decision_intent"] = {"status": "ERROR", "error": str(exc)}

        feed_failures = [name for name, info in feed_status.items() if str(info.get("status") or "") == "ERROR"]

        return {
            "generated_at": utcnow_iso(),
            "overall_status": "OK" if not feed_failures else "ERROR",
            "summary": {
                "pending_overrides": len(pending_overrides),
                "flagged_decisions": len(flagged_decisions),
                "recent_compliance_checks": len(compliance_audit),
                "recent_ips_breaches": len(ips_breaches),
                "recent_alerts": len(recent_alerts),
                "feed_failures": len(feed_failures),
                "intent_backlog": int(ops_status.get("intent_backlog") or 0),
                "intent_failures": int(ops_status.get("intent_failures") or 0),
            },
            "pending_overrides": pending_overrides,
            "flagged_decisions": flagged_decisions,
            "recent_compliance_checks": compliance_audit,
            "recent_ips_breaches": ips_breaches,
            "recent_alerts": recent_alerts,
            "ops_status": ops_status,
            "feed_status": feed_status,
            "feed_failures": feed_failures,
        }

    @classmethod
    async def get_replay_timeline(cls, decision_id: str) -> Optional[dict[str, Any]]:
        decision = await cls.get_decision(decision_id)
        if not decision:
            return None

        timeline: list[dict[str, Any]] = []
        timeline.append(
            {
                "timestamp": decision.get("created_at"),
                "event": "DECISION_RECORDED",
                "decision_id": decision_id,
                "details": {
                    "decision_type": decision.get("decision_type"),
                    "decision_outcome": decision.get("decision_outcome"),
                    "status": decision.get("status"),
                },
            }
        )

        explanation = decision.get("explanation")
        if isinstance(explanation, dict):
            timeline.append(
                {
                    "timestamp": decision.get("updated_at"),
                    "event": "EXPLANATION_RECORDED",
                    "decision_id": decision_id,
                    "details": {
                        "schema_version": explanation.get("schema_version"),
                        "summary": explanation.get("summary"),
                    },
                }
            )

        for req in decision.get("override_requests") or []:
            timeline.append(
                {
                    "timestamp": req.get("created_at"),
                    "event": "OVERRIDE_REQUESTED",
                    "decision_id": decision_id,
                    "details": dict(req),
                }
            )
            if req.get("resolved_at"):
                timeline.append(
                    {
                        "timestamp": req.get("resolved_at"),
                        "event": "OVERRIDE_RESOLVED",
                        "decision_id": decision_id,
                        "details": {
                            "status": req.get("status"),
                            "resolved_by": req.get("resolved_by"),
                            "resolution_notes": req.get("resolution_notes"),
                        },
                    }
                )

        for approval in decision.get("override_approvals") or []:
            timeline.append(
                {
                    "timestamp": approval.get("approved_at"),
                    "event": "OVERRIDE_APPROVED",
                    "decision_id": decision_id,
                    "details": dict(approval),
                }
            )

        try:
            audit_rows = await fetch_all(
                """
                SELECT id, timestamp_utc, action_type, description, severity
                FROM institutional_audit_log
                WHERE entity_id = :decision_id
                ORDER BY timestamp_utc ASC, id ASC
                LIMIT 500
                """,
                {"decision_id": decision_id},
            )
            for row in audit_rows:
                timeline.append(
                    {
                        "timestamp": row.get("timestamp_utc"),
                        "event": "AUDIT_EVENT",
                        "decision_id": decision_id,
                        "details": dict(row),
                    }
                )
        except Exception:
            pass

        replay_rows = await fetch_all(
            """
            SELECT replay_status, expected_output_hash, actual_output_hash, run_at
            FROM decision_replay_runs
            WHERE decision_id = :decision_id
            ORDER BY run_at ASC, id ASC
            LIMIT 500
            """,
            {"decision_id": decision_id},
        )
        for row in replay_rows:
            timeline.append(
                {
                    "timestamp": row.get("run_at"),
                    "event": "REPLAY_EXECUTED",
                    "decision_id": decision_id,
                    "details": dict(row),
                }
            )

        timeline.sort(key=lambda item: str(item.get("timestamp") or ""))

        return {
            "decision_id": decision_id,
            "decision_type": decision.get("decision_type"),
            "decision_outcome": decision.get("decision_outcome"),
            "timeline": timeline,
            "generated_at": utcnow_iso(),
        }

    @classmethod
    async def build_trust_packet(cls, decision_id: str) -> Optional[dict[str, Any]]:
        audit_pack = await cls.get_audit_pack(decision_id)
        replay = await cls.get_replay_timeline(decision_id)
        if not audit_pack or not replay:
            return None

        decision = audit_pack.get("decision") or {}
        packet = {
            "decision_id": decision_id,
            "decision_type": decision.get("decision_type"),
            "decision_outcome": decision.get("decision_outcome"),
            "status": decision.get("status"),
            "trace": {
                "intent_id": decision.get("intent_id"),
                "policy_version_id": decision.get("policy_version_id"),
                "data_snapshot_ids": decision.get("data_snapshot_ids") or [],
                "calculation_version_id": decision.get("calculation_version_id"),
                "input_hash": decision.get("input_hash"),
                "output_hash": decision.get("output_hash"),
                "trace_timestamp": decision.get("trace_timestamp"),
            },
            "snapshot_refs": {
                "portfolio_snapshot_ref": decision.get("portfolio_snapshot_ref"),
                "market_snapshot_ref": decision.get("market_snapshot_ref"),
                "policy_snapshot_ref": decision.get("policy_snapshot_ref"),
            },
            "explanation": decision.get("explanation"),
            "alternatives": decision.get("alternatives") or [],
            "trace_graph": decision.get("trace_graph"),
            "override_requests": decision.get("override_requests") or [],
            "override_approvals": decision.get("override_approvals") or [],
            "audit_pack": audit_pack,
            "replay": replay,
            "generated_at": utcnow_iso(),
        }

        canonical = cls._to_json(packet)
        packet_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        packet["packet_hash_sha256"] = packet_hash
        return packet
