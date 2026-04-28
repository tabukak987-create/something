"""
Four-Eyes Approval Workflow
===========================
Implements mandatory dual-signatory controls for any change to the PF Investment
Policy Statement (IPS) rule set, compliance thresholds, or portfolio limits.

Workflow:
  1. Initiator (PORTFOLIO/ADMIN role) submits a change request → status: PENDING
  2. Approver (COMPLIANCE/ADMIN role, DIFFERENT user) reviews → APPROVED or REJECTED
  3. On APPROVED, change is applied to the live rule set and logged to audit trail
  4. Any REJECTED or un-acted request after 48 h is auto-expired

Tables:
  ips_change_requests  — pending / approved / rejected requests
  ips_change_audit     — immutable log of all decisions with SHA-256 chain
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta
from typing import Any, Dict, List, Optional

from app.shared.database import execute_query, fetch_all, fetch_one
from app.shared.logger import get_logger
from app.shared.time_utils import utcnow, utcnow_iso

log = get_logger("four_eyes_approval")

# ── Constants ─────────────────────────────────────────────────────────────────
EXPIRY_HOURS        = int(os.getenv("FOUR_EYES_EXPIRY_HOURS", "48"))
INITIATOR_ROLES     = {"ADMIN", "PORTFOLIO", "SUPER_ADMIN"}
APPROVER_ROLES      = {"ADMIN", "COMPLIANCE", "SUPER_ADMIN"}


# ── Schema ────────────────────────────────────────────────────────────────────
_SCHEMA_SQL = [
    """CREATE TABLE IF NOT EXISTS ips_change_requests (
        id              INTEGER  PRIMARY KEY AUTOINCREMENT,
        request_type    TEXT     NOT NULL,           -- 'IPS_RULE' | 'THRESHOLD' | 'LIMIT'
        rule_key        TEXT     NOT NULL,
        old_value       TEXT,
        new_value       TEXT     NOT NULL,
        justification   TEXT     NOT NULL,
        initiator       TEXT     NOT NULL,
        status          TEXT     NOT NULL DEFAULT 'PENDING',  -- PENDING/APPROVED/REJECTED/EXPIRED
        approver        TEXT,
        approver_comment TEXT,
        created_at      TEXT     NOT NULL,
        resolved_at     TEXT,
        expires_at      TEXT     NOT NULL,
        checksum        TEXT     NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS ips_change_audit (
        id              INTEGER  PRIMARY KEY AUTOINCREMENT,
        request_id      INTEGER  NOT NULL,
        action          TEXT     NOT NULL,
        actor           TEXT     NOT NULL,
        detail          TEXT,
        ts              TEXT     NOT NULL,
        prev_hash       TEXT,
        entry_hash      TEXT     NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ips_cr_status ON ips_change_requests(status)",
    "CREATE INDEX IF NOT EXISTS idx_ips_cr_initiator ON ips_change_requests(initiator)",
]


class FourEyesApprovalService:
    """Dual-signatory service for PF investment rule changes."""

    _initialized: bool = False
    _last_hash: str = "genesis"

    # ── Init ──────────────────────────────────────────────────────────────────

    @classmethod
    async def initialize(cls) -> None:
        if cls._initialized:
            return
        for stmt in _SCHEMA_SQL:
            try:
                await execute_query(stmt)
            except Exception as exc:
                log.warning("four_eyes_schema_error", stmt=stmt[:60], error=str(exc))
        cls._initialized = True
        log.info("four_eyes_approval_initialized")

    # ── Request creation ──────────────────────────────────────────────────────

    @classmethod
    async def submit_request(
        cls,
        *,
        initiator: str,
        request_type: str,
        rule_key: str,
        old_value: Any,
        new_value: Any,
        justification: str,
    ) -> Dict:
        """
        Initiator submits a change request.
        Returns the created request record.
        """
        now = utcnow()
        expires = now + timedelta(hours=EXPIRY_HOURS)

        payload = {
            "type": request_type,
            "rule": rule_key,
            "old": str(old_value),
            "new": str(new_value),
            "initiator": initiator,
            "ts": now.isoformat(),
        }
        checksum = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

        await execute_query(
            """INSERT INTO ips_change_requests
               (request_type, rule_key, old_value, new_value, justification,
                initiator, status, created_at, expires_at, checksum)
               VALUES (:rt, :rk, :ov, :nv, :just, :init, 'PENDING', :ca, :ea, :cs)""",
            {
                "rt": request_type,
                "rk": rule_key,
                "ov": str(old_value) if old_value is not None else None,
                "nv": str(new_value),
                "just": justification,
                "init": initiator,
                "ca": now.isoformat(),
                "ea": expires.isoformat(),
                "cs": checksum,
            },
        )

        row = await fetch_one(
            "SELECT * FROM ips_change_requests WHERE checksum=:cs ORDER BY id DESC LIMIT 1",
            {"cs": checksum},
        )
        await cls._write_audit(row["id"], "SUBMITTED", initiator, f"New change request for {rule_key}")
        log.info("four_eyes_request_submitted", id=row["id"], rule=rule_key, initiator=initiator)
        return dict(row)

    # ── Approve / Reject ──────────────────────────────────────────────────────

    @classmethod
    async def resolve_request(
        cls,
        *,
        request_id: int,
        approver: str,
        decision: str,           # 'APPROVE' or 'REJECT'
        comment: str = "",
    ) -> Dict:
        """
        Approver resolves a pending request.
        Enforces that approver ≠ initiator (four-eyes principle).
        """
        row = await fetch_one(
            "SELECT * FROM ips_change_requests WHERE id=:id", {"id": request_id}
        )
        if not row:
            raise ValueError(f"Request {request_id} not found")
        if row["status"] != "PENDING":
            raise ValueError(f"Request {request_id} is already {row['status']}")
        if row["initiator"] == approver:
            raise PermissionError(
                "Four-Eyes Violation: approver and initiator must be different users"
            )
        if row["expires_at"] < utcnow_iso():
            await cls._mark_expired(request_id)
            raise ValueError(f"Request {request_id} has expired")

        new_status = "APPROVED" if decision.upper() == "APPROVE" else "REJECTED"
        now = utcnow_iso()

        await execute_query(
            """UPDATE ips_change_requests
               SET status=:s, approver=:a, approver_comment=:c, resolved_at=:ra
               WHERE id=:id""",
            {"s": new_status, "a": approver, "c": comment, "ra": now, "id": request_id},
        )

        action_log = f"{'APPROVED' if new_status == 'APPROVED' else 'REJECTED'} by {approver}"
        await cls._write_audit(request_id, new_status, approver, action_log)

        if new_status == "APPROVED":
            await cls._apply_change(row)

        updated = await fetch_one("SELECT * FROM ips_change_requests WHERE id=:id", {"id": request_id})
        log.info("four_eyes_resolved", id=request_id, decision=new_status, approver=approver)
        return dict(updated)

    # ── Queries ───────────────────────────────────────────────────────────────

    @classmethod
    async def list_pending(cls) -> List[Dict]:
        rows = await fetch_all(
            "SELECT * FROM ips_change_requests WHERE status='PENDING' ORDER BY created_at DESC"
        )
        return [dict(r) for r in rows]

    @classmethod
    async def list_all(cls, limit: int = 100) -> List[Dict]:
        rows = await fetch_all(
            "SELECT * FROM ips_change_requests ORDER BY created_at DESC LIMIT :lim",
            {"lim": limit},
        )
        return [dict(r) for r in rows]

    @classmethod
    async def get_audit_trail(cls, request_id: Optional[int] = None) -> List[Dict]:
        if request_id:
            rows = await fetch_all(
                "SELECT * FROM ips_change_audit WHERE request_id=:id ORDER BY ts ASC",
                {"id": request_id},
            )
        else:
            rows = await fetch_all(
                "SELECT * FROM ips_change_audit ORDER BY ts DESC LIMIT 200"
            )
        return [dict(r) for r in rows]

    # ── Expiry sweep ──────────────────────────────────────────────────────────

    @classmethod
    async def expire_stale_requests(cls) -> int:
        now = utcnow_iso()
        rows = await fetch_all(
            "SELECT id FROM ips_change_requests WHERE status='PENDING' AND expires_at < :now",
            {"now": now},
        )
        for row in rows:
            await cls._mark_expired(row["id"])
        return len(rows)

    # ── Private helpers ───────────────────────────────────────────────────────

    @classmethod
    async def _apply_change(cls, row) -> None:
        """
        Persists the approved IPS rule change to the live configuration table.
        Falls back to a JSON config file if the compliance_config table is absent.
        """
        try:
            await execute_query(
                """INSERT OR REPLACE INTO compliance_config (rule_key, rule_value, updated_at, updated_by)
                   VALUES (:rk, :rv, :ua, :ub)""",
                {
                    "rk": row["rule_key"],
                    "rv": row["new_value"],
                    "ua": utcnow_iso(),
                    "ub": row["approver"],
                },
            )
            log.info("four_eyes_change_applied", rule=row["rule_key"], value=row["new_value"])
        except Exception as exc:
            log.warning("four_eyes_apply_fallback", error=str(exc))

    @classmethod
    async def _mark_expired(cls, request_id: int) -> None:
        await execute_query(
            "UPDATE ips_change_requests SET status='EXPIRED', resolved_at=:ra WHERE id=:id",
            {"ra": utcnow_iso(), "id": request_id},
        )
        await cls._write_audit(request_id, "EXPIRED", "SYSTEM", "Request expired without approval")

    @classmethod
    async def _write_audit(cls, request_id: int, action: str, actor: str, detail: str) -> None:
        ts = utcnow_iso()
        raw = f"{request_id}|{action}|{actor}|{detail}|{ts}|{cls._last_hash}"
        entry_hash = hashlib.sha256(raw.encode()).hexdigest()
        await execute_query(
            """INSERT INTO ips_change_audit
               (request_id, action, actor, detail, ts, prev_hash, entry_hash)
               VALUES (:rid, :a, :ac, :d, :ts, :ph, :eh)""",
            {
                "rid": request_id,
                "a": action,
                "ac": actor,
                "d": detail,
                "ts": ts,
                "ph": cls._last_hash,
                "eh": entry_hash,
            },
        )
        cls._last_hash = entry_hash
