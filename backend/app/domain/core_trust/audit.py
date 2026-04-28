"""
Institutional Audit Trail Service
===================================
Immutable, append-only audit logging for all governance-critical operations.

Covers:
  - Portfolio edits (trade execution, holding changes)
  - Compliance threshold changes (rule config updates)
  - Rating updates (manual overrides, feed changes)
  - Data overrides (valuation, pricing adjustments)
  - User role changes (RBAC mutations)
  - System configuration changes

Log Structure (per SEBI/EPFO audit requirements):
  - user_id          — Authenticated actor
  - timestamp_utc    — ISO-8601 UTC, immutable
  - action_type      — Enum category (PORTFOLIO_EDIT, COMPLIANCE_CHANGE, etc.)
  - entity_type      — Bond, Portfolio, User, Rule, Config
  - entity_id        — Primary key of affected entity
  - old_value        — JSON snapshot of prior state
  - new_value        — JSON snapshot of new state
  - affected_module  — Service module originating the action
  - ip_address       — Client IP for forensic tracing
  - session_id       — JWT session identifier
  - severity         — INFO, WARNING, CRITICAL
  - compliance_flag  — Whether action has compliance implications

All logs are append-only. No UPDATE or DELETE operations permitted.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from enum import Enum
from typing import Any, Optional

from app.shared.deployment import strict_audit_logging_enabled
from app.shared.database import execute_query, fetch_all, fetch_one
from app.shared.logger import get_logger
from app.shared.time_utils import utcnow, utcnow_iso

log = get_logger("service.audit")


# ── Action Type Enum ──────────────────────────────────────────────────────────
class AuditAction(str, Enum):
    # Portfolio operations
    PORTFOLIO_VIEW = "PORTFOLIO_VIEW"
    PORTFOLIO_TRADE = "PORTFOLIO_TRADE"
    PORTFOLIO_EDIT = "PORTFOLIO_EDIT"
    PORTFOLIO_DELETE = "PORTFOLIO_DELETE"
    HOLDING_ADJUSTMENT = "HOLDING_ADJUSTMENT"
    PORTFOLIO_UPLOAD = "PORTFOLIO_UPLOAD"
    PORTFOLIO_VERSION_CREATED = "PORTFOLIO_VERSION_CREATED"
    ALLOCATION_RUN = "ALLOCATION_RUN"

    # Compliance operations
    COMPLIANCE_RULE_CHANGE = "COMPLIANCE_RULE_CHANGE"
    COMPLIANCE_THRESHOLD_CHANGE = "COMPLIANCE_THRESHOLD_CHANGE"
    COMPLIANCE_OVERRIDE = "COMPLIANCE_OVERRIDE"
    COMPLIANCE_BREACH = "COMPLIANCE_BREACH"
    COMPLIANCE_BREACH_RESOLUTION = "COMPLIANCE_BREACH_RESOLUTION"

    # Rating operations
    RATING_UPDATE = "RATING_UPDATE"
    RATING_OVERRIDE = "RATING_OVERRIDE"
    RATING_DOWNGRADE = "RATING_DOWNGRADE"

    # Data operations
    DATA_OVERRIDE = "DATA_OVERRIDE"
    VALUATION_OVERRIDE = "VALUATION_OVERRIDE"
    PRICE_CORRECTION = "PRICE_CORRECTION"
    PRICE_UPDATE = "PRICE_UPDATE"
    DATA_FEED_FAILURE = "DATA_FEED_FAILURE"
    RECOMMENDATION_GENERATED = "RECOMMENDATION_GENERATED"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"

    # User management
    USER_CREATED = "USER_CREATED"
    USER_ROLE_CHANGE = "USER_ROLE_CHANGE"
    USER_DEACTIVATED = "USER_DEACTIVATED"
    USER_LOGIN = "USER_LOGIN"
    USER_LOGIN_FAILED = "USER_LOGIN_FAILED"
    USER_LOGOUT = "USER_LOGOUT"
    USER_LOGOUT_ALL = "USER_LOGOUT_ALL"
    USER_PASSWORD_CHANGE = "USER_PASSWORD_CHANGE"

    # System config
    CONFIG_CHANGE = "CONFIG_CHANGE"
    SYSTEM_STARTUP = "SYSTEM_STARTUP"
    SYSTEM_SHUTDOWN = "SYSTEM_SHUTDOWN"

    # Report generation
    REPORT_GENERATED = "REPORT_GENERATED"
    REPORT_EXPORTED = "REPORT_EXPORTED"


class AuditSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class EntityType(str, Enum):
    BOND = "BOND"
    PORTFOLIO = "PORTFOLIO"
    USER = "USER"
    COMPLIANCE_RULE = "COMPLIANCE_RULE"
    SYSTEM_CONFIG = "SYSTEM_CONFIG"
    REPORT = "REPORT"
    TRADE = "TRADE"
    ALERT = "ALERT"
    MARKET_DATA = "MARKET_DATA"


# ── Schema DDL ────────────────────────────────────────────────────────────────
AUDIT_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS institutional_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    username TEXT NOT NULL,
    user_role TEXT NOT NULL,
    timestamp_utc TEXT NOT NULL,
    action_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    old_value TEXT,
    new_value TEXT,
    affected_module TEXT NOT NULL,
    ip_address TEXT,
    session_id TEXT,
    severity TEXT NOT NULL DEFAULT 'INFO',
    compliance_flag INTEGER NOT NULL DEFAULT 0,
    description TEXT,
    metadata_json TEXT,
    checksum TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON institutional_audit_log(timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_audit_action ON institutional_audit_log(action_type);
CREATE INDEX IF NOT EXISTS idx_audit_user ON institutional_audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON institutional_audit_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_severity ON institutional_audit_log(severity);
CREATE INDEX IF NOT EXISTS idx_audit_compliance ON institutional_audit_log(compliance_flag);
"""

# ── Immutability Triggers (prevent UPDATE/DELETE on audit log) ────────────────
AUDIT_IMMUTABILITY_DDL = """
CREATE TRIGGER IF NOT EXISTS prevent_audit_update
    BEFORE UPDATE ON institutional_audit_log
    BEGIN
        SELECT RAISE(ABORT, 'IMMUTABILITY VIOLATION: UPDATE on institutional_audit_log is prohibited. Audit records are append-only.');
    END;

CREATE TRIGGER IF NOT EXISTS prevent_audit_delete
    BEFORE DELETE ON institutional_audit_log
    BEGIN
        SELECT RAISE(ABORT, 'IMMUTABILITY VIOLATION: DELETE on institutional_audit_log is prohibited. Audit records are append-only and must be retained for regulatory compliance.');
    END;
"""


# ── Canonical Trust Audit Schema (exact trustee export fields) ───────────────
TRUST_AUDIT_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS audit_logs (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    user_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    old_value TEXT,
    new_value TEXT,
    ip_address TEXT,
    remarks TEXT,
    prev_hash TEXT,
    entry_hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trust_audit_timestamp ON audit_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_trust_audit_action ON audit_logs(action_type);
CREATE INDEX IF NOT EXISTS idx_trust_audit_user ON audit_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_trust_audit_entity ON audit_logs(entity_type, entity_id);
"""

TRUST_AUDIT_IMMUTABILITY_DDL = """
CREATE TRIGGER IF NOT EXISTS prevent_trust_audit_update
    BEFORE UPDATE ON audit_logs
    BEGIN
        SELECT RAISE(ABORT, 'IMMUTABILITY VIOLATION: UPDATE on audit_logs is prohibited.');
    END;

CREATE TRIGGER IF NOT EXISTS prevent_trust_audit_delete
    BEFORE DELETE ON audit_logs
    BEGIN
        SELECT RAISE(ABORT, 'IMMUTABILITY VIOLATION: DELETE on audit_logs is prohibited.');
    END;
"""

TRUST_CHAIN_ANCHOR_DDL = """
CREATE TABLE IF NOT EXISTS trust_chain_anchors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    anchored_at TEXT NOT NULL,
    last_entry_id TEXT NOT NULL,
    last_entry_hash TEXT NOT NULL,
    entry_count INTEGER NOT NULL,
    anchor_hash TEXT NOT NULL,
    anchor_provider TEXT NOT NULL DEFAULT 'internal_notary',
    provider_reference TEXT,
    metadata_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_tca_anchored_at ON trust_chain_anchors(anchored_at DESC);
CREATE INDEX IF NOT EXISTS idx_tca_provider ON trust_chain_anchors(anchor_provider);
"""


# ── Core Audit Service ────────────────────────────────────────────────────────
class AuditService:
    """
    Immutable audit trail for institutional governance.
    All entries are append-only — no update or delete operations.
    """

    @staticmethod
    def _compute_checksum(data: dict) -> str:
        """SHA-256 checksum for tamper detection."""
        import hashlib
        canonical = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()[:32]

    @staticmethod
    def _compute_chain_hash(data: dict) -> str:
        """SHA-256 chain hash for canonical trust audit entries."""
        import hashlib

        canonical = json.dumps(data, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _serialize_value(value: Any) -> Optional[str]:
        """Safely serialize a value to JSON string."""
        if value is None:
            return None
        try:
            return json.dumps(value, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)

    @classmethod
    async def _record_trust_log(
        cls,
        *,
        user_id: str,
        action_type: str,
        entity_type: str,
        entity_id: Optional[str],
        old_value: Any,
        new_value: Any,
        ip_address: Optional[str],
        remarks: Optional[str],
        timestamp: str,
    ) -> str:
        """
        Record canonical audit_logs entry using chain hashing.

        This table matches trustee export fields exactly and appends chain metadata
        (`prev_hash`, `entry_hash`) for tamper evidence.
        """
        entry_id = str(uuid.uuid4())
        prev_row = await fetch_one(
            "SELECT entry_hash FROM audit_logs ORDER BY timestamp DESC, id DESC LIMIT 1"
        )
        prev_hash = (prev_row or {}).get("entry_hash") or ""

        old_serialized = cls._serialize_value(old_value)
        new_serialized = cls._serialize_value(new_value)
        safe_remarks = (remarks or "").strip() or f"{action_type} {entity_type}"

        hash_payload = {
            "id": entry_id,
            "timestamp": timestamp,
            "user_id": user_id,
            "action_type": action_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "old_value": old_serialized,
            "new_value": new_serialized,
            "ip_address": ip_address,
            "remarks": safe_remarks,
            "prev_hash": prev_hash,
        }
        entry_hash = cls._compute_chain_hash(hash_payload)

        await execute_query(
            """INSERT INTO audit_logs
               (id, timestamp, user_id, action_type, entity_type, entity_id,
                old_value, new_value, ip_address, remarks, prev_hash, entry_hash)
               VALUES (:id, :ts, :uid, :action, :etype, :eid,
                       :old_value, :new_value, :ip, :remarks, :prev_hash, :entry_hash)""",
            {
                "id": entry_id,
                "ts": timestamp,
                "uid": user_id,
                "action": action_type,
                "etype": entity_type,
                "eid": entity_id,
                "old_value": old_serialized,
                "new_value": new_serialized,
                "ip": ip_address,
                "remarks": safe_remarks,
                "prev_hash": prev_hash,
                "entry_hash": entry_hash,
            },
        )
        return entry_id

    @classmethod
    async def initialize(cls):
        """Create audit table if not exists and install immutability triggers. Called on startup."""
        try:
            for stmt in AUDIT_SCHEMA_DDL.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    await execute_query(stmt + ";")
            # Install immutability triggers (prevent UPDATE/DELETE)
            for stmt in AUDIT_IMMUTABILITY_DDL.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    try:
                        await execute_query(stmt + ";")
                    except Exception:
                        pass  # Trigger may already exist

            # Canonical trustee-facing audit table (required export schema)
            for stmt in TRUST_AUDIT_SCHEMA_DDL.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    await execute_query(stmt + ";")

            for stmt in TRUST_AUDIT_IMMUTABILITY_DDL.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    try:
                        await execute_query(stmt + ";")
                    except Exception:
                        pass

            for stmt in TRUST_CHAIN_ANCHOR_DDL.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    await execute_query(stmt + ";")

            log.info("audit_table_initialized", immutability="ENFORCED")
        except Exception as e:
            log.error("audit_table_init_failed", error=str(e))

    @classmethod
    async def record(
        cls,
        user: dict,
        action: AuditAction,
        entity_type: EntityType,
        entity_id: Optional[str] = None,
        old_value: Any = None,
        new_value: Any = None,
        affected_module: str = "gateway",
        ip_address: Optional[str] = None,
        session_id: Optional[str] = None,
        severity: AuditSeverity = AuditSeverity.INFO,
        compliance_flag: bool = False,
        description: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> int:
        """
        Record an immutable audit entry.

        Parameters:
            user            — Authenticated user dict (from get_current_user)
            action          — Type of action performed
            entity_type     — Category of affected entity
            entity_id       — Primary key of affected entity
            old_value       — Previous state (auto-serialized to JSON)
            new_value       — New state (auto-serialized to JSON)
            affected_module — Service/module originating the action
            ip_address      — Client IP address
            session_id      — JWT session identifier
            severity        — INFO / WARNING / CRITICAL
            compliance_flag — True if action has compliance implications
            description     — Human-readable description
            metadata        — Additional context (JSON)

        Returns:
            Audit log entry ID
        """
        timestamp = utcnow_iso()
        user_id = str(user.get("id", "system"))
        username = user.get("username", "system")
        user_role = user.get("role", "SYSTEM")

        # Build checksum payload
        checksum_data = {
            "user_id": user_id,
            "timestamp": timestamp,
            "action": action.value,
            "entity_type": entity_type.value,
            "entity_id": entity_id,
            "old_value": cls._serialize_value(old_value),
            "new_value": cls._serialize_value(new_value),
        }
        checksum = cls._compute_checksum(checksum_data)

        try:
            await execute_query(
                """INSERT INTO institutional_audit_log
                   (user_id, username, user_role, timestamp_utc, action_type,
                    entity_type, entity_id, old_value, new_value,
                    affected_module, ip_address, session_id, severity,
                    compliance_flag, description, metadata_json, checksum)
                   VALUES (:uid, :uname, :urole, :ts, :action,
                           :etype, :eid, :oval, :nval,
                           :mod, :ip, :sid, :sev,
                           :cflag, :desc, :meta, :chk)""",
                {
                    "uid": user_id,
                    "uname": username,
                    "urole": user_role,
                    "ts": timestamp,
                    "action": action.value,
                    "etype": entity_type.value,
                    "eid": entity_id,
                    "oval": cls._serialize_value(old_value),
                    "nval": cls._serialize_value(new_value),
                    "mod": affected_module,
                    "ip": ip_address,
                    "sid": session_id,
                    "sev": severity.value,
                    "cflag": 1 if compliance_flag else 0,
                    "desc": description,
                    "meta": cls._serialize_value(metadata),
                    "chk": checksum,
                },
            )
            log.info(
                "audit_recorded",
                action=action.value,
                entity=f"{entity_type.value}:{entity_id}",
                user=username,
                severity=severity.value,
            )

            # Mirror to canonical trustee export schema (audit_logs).
            try:
                await cls._record_trust_log(
                    user_id=user_id,
                    action_type=action.value,
                    entity_type=entity_type.value,
                    entity_id=entity_id,
                    old_value=old_value,
                    new_value=new_value,
                    ip_address=ip_address,
                    remarks=description,
                    timestamp=timestamp,
                )
            except Exception as trust_exc:
                log.error(
                    "trust_audit_record_failed",
                    error=str(trust_exc),
                    action=action.value,
                    strict=strict_audit_logging_enabled(),
                )
                if strict_audit_logging_enabled():
                    raise

            # Get the inserted ID
            result = await fetch_one(
                "SELECT MAX(id) AS last_id FROM institutional_audit_log"
            )
            return result["last_id"] if result else 0

        except Exception as e:
            log.error("audit_record_failed", error=str(e), action=action.value)
            raise

    @classmethod
    async def query_logs(
        cls,
        action_type: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        user_id: Optional[str] = None,
        severity: Optional[str] = None,
        compliance_only: bool = False,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """
        Query audit logs with flexible filtering.
        Returns paginated results with metadata.
        """
        conditions = []
        params: dict = {}

        if action_type:
            conditions.append("action_type = :action")
            params["action"] = action_type
        if entity_type:
            conditions.append("entity_type = :etype")
            params["etype"] = entity_type
        if entity_id:
            conditions.append("entity_id = :eid")
            params["eid"] = entity_id
        if user_id:
            conditions.append("user_id = :uid")
            params["uid"] = user_id
        if severity:
            conditions.append("severity = :sev")
            params["sev"] = severity
        if compliance_only:
            conditions.append("compliance_flag = 1")
        if start_date:
            conditions.append("timestamp_utc >= :start")
            params["start"] = start_date
        if end_date:
            conditions.append("timestamp_utc <= :end")
            params["end"] = end_date

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        # Count total
        count_row = await fetch_one(
            f"SELECT COUNT(*) AS total FROM institutional_audit_log WHERE {where_clause}",
            params,
        )
        total = count_row["total"] if count_row else 0

        # Paginated results
        rows = await fetch_all(
            f"""SELECT * FROM institutional_audit_log
                WHERE {where_clause}
                ORDER BY timestamp_utc DESC
                LIMIT :lim OFFSET :off""",
            {**params, "lim": limit, "off": offset},
        )

        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "entries": rows,
        }

    @classmethod
    async def get_summary(
        cls,
        days: int = 30,
    ) -> dict:
        """
        Generate audit summary statistics for the last N days.
        Suitable for trustee/board reporting.
        """
        cutoff = (utcnow() - timedelta(days=days)).isoformat()

        # Action distribution
        action_dist = await fetch_all(
            """SELECT action_type, COUNT(*) AS count
               FROM institutional_audit_log
               WHERE timestamp_utc >= :cutoff
               GROUP BY action_type ORDER BY count DESC""",
            {"cutoff": cutoff},
        )

        # Severity distribution
        severity_dist = await fetch_all(
            """SELECT severity, COUNT(*) AS count
               FROM institutional_audit_log
               WHERE timestamp_utc >= :cutoff
               GROUP BY severity ORDER BY count DESC""",
            {"cutoff": cutoff},
        )

        # Active users
        active_users = await fetch_all(
            """SELECT username, user_role, COUNT(*) AS actions
               FROM institutional_audit_log
               WHERE timestamp_utc >= :cutoff
               GROUP BY username, user_role ORDER BY actions DESC""",
            {"cutoff": cutoff},
        )

        # Compliance-flagged actions
        compliance_count = await fetch_one(
            """SELECT COUNT(*) AS count
               FROM institutional_audit_log
               WHERE timestamp_utc >= :cutoff AND compliance_flag = 1""",
            {"cutoff": cutoff},
        )

        # Critical events
        critical_events = await fetch_all(
            """SELECT * FROM institutional_audit_log
               WHERE timestamp_utc >= :cutoff AND severity = 'CRITICAL'
               ORDER BY timestamp_utc DESC LIMIT 50""",
            {"cutoff": cutoff},
        )

        total = await fetch_one(
            "SELECT COUNT(*) AS count FROM institutional_audit_log WHERE timestamp_utc >= :cutoff",
            {"cutoff": cutoff},
        )

        return {
            "period_days": days,
            "total_entries": total["count"] if total else 0,
            "action_distribution": action_dist,
            "severity_distribution": severity_dist,
            "active_users": active_users,
            "compliance_flagged_count": compliance_count["count"] if compliance_count else 0,
            "critical_events": critical_events,
            "generated_at": utcnow_iso(),
        }

    @classmethod
    async def verify_integrity(cls, last_n: int = 1000) -> dict:
        """
        Verify checksum integrity of the last N audit entries.
        Detects any tampering with historical records.
        """
        rows = await fetch_all(
            """SELECT * FROM institutional_audit_log
               ORDER BY id DESC LIMIT :n""",
            {"n": last_n},
        )

        total = len(rows)
        valid = 0
        tampered = []

        for row in rows:
            checksum_data = {
                "user_id": row["user_id"],
                "timestamp": row["timestamp_utc"],
                "action": row["action_type"],
                "entity_type": row["entity_type"],
                "entity_id": row.get("entity_id"),
                "old_value": row.get("old_value"),
                "new_value": row.get("new_value"),
            }
            expected = cls._compute_checksum(checksum_data)
            if expected == row.get("checksum"):
                valid += 1
            else:
                tampered.append({
                    "id": row["id"],
                    "timestamp": row["timestamp_utc"],
                    "action": row["action_type"],
                    "expected_checksum": expected,
                    "stored_checksum": row.get("checksum"),
                })

        return {
            "total_checked": total,
            "valid": valid,
            "tampered": len(tampered),
            "integrity_score": round((valid / total * 100) if total else 100, 2),
            "tampered_entries": tampered[:20],  # Limit output
            "verified_at": utcnow_iso(),
        }

    @classmethod
    async def export_csv(cls, start_date: Optional[str] = None,
                         end_date: Optional[str] = None,
                         action_type: Optional[str] = None) -> str:
        """
        Export audit log as CSV string for regulatory submission.
        Returns CSV content as a string (caller writes to file/response).
        """
        import csv
        import io

        where_parts = ["1=1"]
        params: dict = {}
        if start_date:
            where_parts.append("timestamp_utc >= :start")
            params["start"] = start_date
        if end_date:
            where_parts.append("timestamp_utc <= :end")
            params["end"] = end_date
        if action_type:
            where_parts.append("action_type = :action")
            params["action"] = action_type

        where_clause = " AND ".join(where_parts)
        rows = await fetch_all(
            f"""SELECT id, user_id, username, user_role, timestamp_utc,
                       action_type, entity_type, entity_id,
                       old_value, new_value, affected_module,
                       ip_address, severity, compliance_flag,
                       description, checksum
                FROM institutional_audit_log
                WHERE {where_clause}
                ORDER BY timestamp_utc ASC""",
            params,
        )

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "ID", "User ID", "Username", "Role", "Timestamp (UTC)",
            "Action", "Entity Type", "Entity ID", "Old Value", "New Value",
            "Module", "IP Address", "Severity", "Compliance Flag",
            "Description", "Checksum"
        ])
        for row in rows:
            writer.writerow([
                row.get("id"), row.get("user_id"), row.get("username"),
                row.get("user_role"), row.get("timestamp_utc"),
                row.get("action_type"), row.get("entity_type"),
                row.get("entity_id"), row.get("old_value"),
                row.get("new_value"), row.get("affected_module"),
                row.get("ip_address"), row.get("severity"),
                row.get("compliance_flag"), row.get("description"),
                row.get("checksum"),
            ])

        csv_content = output.getvalue()
        output.close()

        # Log the export itself as an audit event
        await cls.record(
            user={"id": "SYSTEM", "username": "csv_export", "role": "SYSTEM"},
            action=AuditAction.REPORT_EXPORTED,
            entity_type=EntityType.REPORT,
            entity_id="audit_csv_export",
            old_value=None,
            new_value={"rows_exported": len(rows), "filters": params},
            affected_module="audit",
            severity=AuditSeverity.INFO,
            compliance_flag=True,
            description=f"Audit log exported as CSV ({len(rows)} rows)",
        )

        return csv_content

    @classmethod
    async def query_trust_logs(
        cls,
        action_type: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        user_id: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """Query canonical trustee-facing audit logs (audit_logs table)."""
        conditions = []
        params: dict = {}

        if action_type:
            conditions.append("action_type = :action")
            params["action"] = action_type
        if entity_type:
            conditions.append("entity_type = :etype")
            params["etype"] = entity_type
        if entity_id:
            conditions.append("entity_id = :eid")
            params["eid"] = entity_id
        if user_id:
            conditions.append("user_id = :uid")
            params["uid"] = user_id
        if start_date:
            conditions.append("timestamp >= :start")
            params["start"] = start_date
        if end_date:
            conditions.append("timestamp <= :end")
            params["end"] = end_date

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        count_row = await fetch_one(
            f"SELECT COUNT(*) AS total FROM audit_logs WHERE {where_clause}",
            params,
        )
        total = int((count_row or {}).get("total") or 0)

        rows = await fetch_all(
            f"""SELECT id, timestamp, user_id, action_type, entity_type, entity_id,
                       old_value, new_value, ip_address, remarks, prev_hash, entry_hash
                FROM audit_logs
                WHERE {where_clause}
                ORDER BY timestamp DESC, id DESC
                LIMIT :lim OFFSET :off""",
            {**params, "lim": limit, "off": offset},
        )
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "entries": rows,
        }

    @classmethod
    async def verify_trust_chain(cls, last_n: int = 5000) -> dict:
        """Verify hash-chain integrity of canonical audit_logs entries."""
        recent = await fetch_all(
            """SELECT id, timestamp, user_id, action_type, entity_type, entity_id,
                      old_value, new_value, ip_address, remarks, prev_hash, entry_hash
               FROM audit_logs
               ORDER BY timestamp DESC, id DESC
               LIMIT :n""",
            {"n": last_n},
        )
        rows = list(reversed(recent))

        previous_hash = ""
        broken: list[dict[str, Any]] = []

        for row in rows:
            payload = {
                "id": row.get("id"),
                "timestamp": row.get("timestamp"),
                "user_id": row.get("user_id"),
                "action_type": row.get("action_type"),
                "entity_type": row.get("entity_type"),
                "entity_id": row.get("entity_id"),
                "old_value": row.get("old_value"),
                "new_value": row.get("new_value"),
                "ip_address": row.get("ip_address"),
                "remarks": row.get("remarks"),
                "prev_hash": previous_hash,
            }
            expected_hash = cls._compute_chain_hash(payload)
            stored_prev_hash = row.get("prev_hash") or ""
            stored_hash = row.get("entry_hash") or ""

            if stored_prev_hash != previous_hash or stored_hash != expected_hash:
                broken.append(
                    {
                        "id": row.get("id"),
                        "timestamp": row.get("timestamp"),
                        "stored_prev_hash": stored_prev_hash,
                        "expected_prev_hash": previous_hash,
                        "stored_hash": stored_hash,
                        "expected_hash": expected_hash,
                    }
                )

            previous_hash = stored_hash

        total = len(rows)
        valid = total - len(broken)
        return {
            "total_checked": total,
            "valid": valid,
            "broken": len(broken),
            "integrity_score": round((valid / total * 100) if total else 100, 2),
            "broken_entries": broken[:20],
            "verified_at": utcnow_iso(),
        }

    @classmethod
    async def create_trust_anchor(
        cls,
        *,
        anchor_provider: str = "internal_notary",
        provider_reference: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Create a chain-state anchor for external notarization workflows."""
        latest = await fetch_one(
            """
            SELECT id, entry_hash
            FROM audit_logs
            ORDER BY timestamp DESC, id DESC
            LIMIT 1
            """
        )
        if not latest:
            return {
                "anchored": False,
                "reason": "no_audit_entries",
                "anchored_at": utcnow_iso(),
            }

        count_row = await fetch_one("SELECT COUNT(*) AS cnt FROM audit_logs")
        entry_count = int((count_row or {}).get("cnt") or 0)
        anchored_at = utcnow_iso()

        payload = {
            "anchored_at": anchored_at,
            "last_entry_id": latest.get("id"),
            "last_entry_hash": latest.get("entry_hash"),
            "entry_count": entry_count,
            "anchor_provider": anchor_provider,
            "provider_reference": provider_reference or "",
        }
        anchor_hash = cls._compute_chain_hash(payload)

        await execute_query(
            """
            INSERT INTO trust_chain_anchors (
                anchored_at,
                last_entry_id,
                last_entry_hash,
                entry_count,
                anchor_hash,
                anchor_provider,
                provider_reference,
                metadata_json
            ) VALUES (
                :anchored_at,
                :last_entry_id,
                :last_entry_hash,
                :entry_count,
                :anchor_hash,
                :anchor_provider,
                :provider_reference,
                :metadata_json
            )
            """,
            {
                "anchored_at": anchored_at,
                "last_entry_id": latest.get("id"),
                "last_entry_hash": latest.get("entry_hash"),
                "entry_count": entry_count,
                "anchor_hash": anchor_hash,
                "anchor_provider": anchor_provider,
                "provider_reference": provider_reference,
                "metadata_json": cls._serialize_value(metadata or {}),
            },
        )

        return {
            "anchored": True,
            "anchored_at": anchored_at,
            "entry_count": entry_count,
            "last_entry_id": latest.get("id"),
            "last_entry_hash": latest.get("entry_hash"),
            "anchor_hash": anchor_hash,
            "anchor_provider": anchor_provider,
            "provider_reference": provider_reference,
        }

    @classmethod
    async def get_latest_trust_anchor(cls) -> Optional[dict[str, Any]]:
        row = await fetch_one(
            """
            SELECT *
            FROM trust_chain_anchors
            ORDER BY anchored_at DESC, id DESC
            LIMIT 1
            """
        )
        if not row:
            return None
        payload = dict(row)
        try:
            payload["metadata"] = json.loads(payload.get("metadata_json") or "{}")
        except Exception:
            payload["metadata"] = {}
        return payload

    @classmethod
    async def export_trust_csv(
        cls,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        action_type: Optional[str] = None,
    ) -> str:
        """Export canonical audit_logs table in trustee-required schema."""
        import csv
        import io

        where_parts = ["1=1"]
        params: dict = {}
        if start_date:
            where_parts.append("timestamp >= :start")
            params["start"] = start_date
        if end_date:
            where_parts.append("timestamp <= :end")
            params["end"] = end_date
        if action_type:
            where_parts.append("action_type = :action")
            params["action"] = action_type

        rows = await fetch_all(
            f"""SELECT id, timestamp, user_id, action_type, entity_type, entity_id,
                       old_value, new_value, ip_address, remarks, prev_hash, entry_hash
                FROM audit_logs
                WHERE {' AND '.join(where_parts)}
                ORDER BY timestamp ASC, id ASC""",
            params,
        )

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "id",
                "timestamp",
                "user_id",
                "action_type",
                "entity_type",
                "entity_id",
                "old_value",
                "new_value",
                "ip_address",
                "remarks",
                "prev_hash",
                "entry_hash",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.get("id"),
                    row.get("timestamp"),
                    row.get("user_id"),
                    row.get("action_type"),
                    row.get("entity_type"),
                    row.get("entity_id"),
                    row.get("old_value"),
                    row.get("new_value"),
                    row.get("ip_address"),
                    row.get("remarks"),
                    row.get("prev_hash"),
                    row.get("entry_hash"),
                ]
            )
        csv_content = output.getvalue()
        output.close()
        return csv_content

    @classmethod
    async def export_trust_pdf(
        cls,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        action_type: Optional[str] = None,
        limit: int = 1000,
    ) -> bytes:
        """Export canonical audit logs as a compact PDF for board/audit review."""
        import io

        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        where_parts = ["1=1"]
        params: dict = {"lim": max(1, min(limit, 5000))}
        if start_date:
            where_parts.append("timestamp >= :start")
            params["start"] = start_date
        if end_date:
            where_parts.append("timestamp <= :end")
            params["end"] = end_date
        if action_type:
            where_parts.append("action_type = :action")
            params["action"] = action_type

        rows = await fetch_all(
            f"""SELECT id, timestamp, user_id, action_type, entity_type, entity_id,
                       ip_address, remarks, entry_hash
                FROM audit_logs
                WHERE {' AND '.join(where_parts)}
                ORDER BY timestamp ASC, id ASC
                LIMIT :lim""",
            params,
        )

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), leftMargin=18, rightMargin=18, topMargin=18, bottomMargin=18)
        styles = getSampleStyleSheet()

        story = [
            Paragraph("Trust Audit Log Export", styles["Title"]),
            Spacer(1, 6),
            Paragraph(f"Generated: {utcnow_iso()} UTC", styles["Normal"]),
            Paragraph(f"Rows: {len(rows)}", styles["Normal"]),
            Spacer(1, 10),
        ]

        table_data = [["Timestamp", "User", "Action", "Entity", "Entity ID", "IP", "Remarks", "Entry Hash"]]
        for row in rows:
            table_data.append(
                [
                    str(row.get("timestamp") or "")[:19],
                    str(row.get("user_id") or ""),
                    str(row.get("action_type") or ""),
                    str(row.get("entity_type") or ""),
                    str(row.get("entity_id") or ""),
                    str(row.get("ip_address") or ""),
                    str(row.get("remarks") or "")[:80],
                    str(row.get("entry_hash") or "")[:24],
                ]
            )

        table = Table(table_data, repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#9ca3af")),
                    ("FONTSIZE", (0, 0), (-1, -1), 7),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9fafb")]),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        story.append(table)
        doc.build(story)
        return buffer.getvalue()


# ── Convenience helpers for common audit actions ──────────────────────────────
async def audit_trade(user: dict, trade_data: dict, ip: Optional[str] = None):
    """Log a portfolio trade execution."""
    return await AuditService.record(
        user=user,
        action=AuditAction.PORTFOLIO_TRADE,
        entity_type=EntityType.TRADE,
        entity_id=trade_data.get("isin", ""),
        new_value=trade_data,
        affected_module="portfolio",
        ip_address=ip,
        severity=AuditSeverity.INFO,
        compliance_flag=True,
        description=f"{trade_data.get('action', 'TRADE')} {trade_data.get('isin', '')} "
                    f"qty={trade_data.get('quantity', 0)} @ {trade_data.get('price', 0)}",
    )


async def audit_compliance_change(user: dict, rule_id: str, old_config: dict, new_config: dict, ip: Optional[str] = None):
    """Log a compliance threshold/rule configuration change."""
    return await AuditService.record(
        user=user,
        action=AuditAction.COMPLIANCE_THRESHOLD_CHANGE,
        entity_type=EntityType.COMPLIANCE_RULE,
        entity_id=rule_id,
        old_value=old_config,
        new_value=new_config,
        affected_module="compliance",
        ip_address=ip,
        severity=AuditSeverity.WARNING,
        compliance_flag=True,
        description=f"Compliance rule '{rule_id}' threshold modified",
    )


async def audit_rating_update(user: dict, isin: str, old_rating: str, new_rating: str, ip: Optional[str] = None):
    """Log a bond rating change."""
    severity = AuditSeverity.CRITICAL if _is_downgrade(old_rating, new_rating) else AuditSeverity.INFO
    return await AuditService.record(
        user=user,
        action=AuditAction.RATING_UPDATE if not _is_downgrade(old_rating, new_rating) else AuditAction.RATING_DOWNGRADE,
        entity_type=EntityType.BOND,
        entity_id=isin,
        old_value={"rating": old_rating},
        new_value={"rating": new_rating},
        affected_module="ratings",
        ip_address=ip,
        severity=severity,
        compliance_flag=True,
        description=f"Rating change for {isin}: {old_rating} → {new_rating}",
    )


async def audit_user_role_change(admin_user: dict, target_user: dict, old_role: str, new_role: str, ip: Optional[str] = None):
    """Log a user role modification."""
    return await AuditService.record(
        user=admin_user,
        action=AuditAction.USER_ROLE_CHANGE,
        entity_type=EntityType.USER,
        entity_id=str(target_user.get("id", "")),
        old_value={"role": old_role, "username": target_user.get("username")},
        new_value={"role": new_role, "username": target_user.get("username")},
        affected_module="auth",
        ip_address=ip,
        severity=AuditSeverity.WARNING,
        compliance_flag=True,
        description=f"Role change for {target_user.get('username')}: {old_role} → {new_role}",
    )


async def audit_data_override(user: dict, entity_id: str, field: str, old_val: Any, new_val: Any, ip: Optional[str] = None):
    """Log a manual data override (valuation, pricing, etc.)."""
    return await AuditService.record(
        user=user,
        action=AuditAction.DATA_OVERRIDE,
        entity_type=EntityType.MARKET_DATA,
        entity_id=entity_id,
        old_value={field: old_val},
        new_value={field: new_val},
        affected_module="market_data",
        ip_address=ip,
        severity=AuditSeverity.WARNING,
        compliance_flag=True,
        description=f"Manual override of {field} for {entity_id}: {old_val} → {new_val}",
    )


def _is_downgrade(old_rating: str, new_rating: str) -> bool:
    """Check if a rating change is a downgrade."""
    RATING_ORDER = {
        "SOV": 0, "SOVEREIGN": 0, "AAA": 1, "AA+": 2, "AA": 3, "AA-": 4,
        "A+": 5, "A": 6, "A-": 7, "BBB+": 8, "BBB": 9, "BBB-": 10,
        "BB+": 11, "BB": 12, "BB-": 13, "B+": 14, "B": 15,
    }
    old_rank = RATING_ORDER.get(old_rating.upper().strip(), 99)
    new_rank = RATING_ORDER.get(new_rating.upper().strip(), 99)
    return new_rank > old_rank  # Higher number = worse rating
