from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional
from uuid import uuid4

from app.shared.database import IS_SQLITE, execute_ddl, execute_query, fetch_all, fetch_one, transactional_session
from app.shared.logger import get_logger
from app.shared.time_utils import utcnow, utcnow_iso

log = get_logger("service.trust_decision_pipeline")


TRUST_DECISION_PIPELINE_DDL = """
CREATE TABLE IF NOT EXISTS decision_intent (
    intent_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE,
    idempotency_key TEXT UNIQUE,
    decision_type TEXT NOT NULL,
    module_name TEXT NOT NULL,
    source_endpoint TEXT,
    status TEXT NOT NULL DEFAULT 'INTENT_LOGGED',
    request_payload_json TEXT NOT NULL,
    execution_payload_json TEXT,
    context_json TEXT,
    error_text TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_di_status ON decision_intent(status);
CREATE INDEX IF NOT EXISTS idx_di_updated ON decision_intent(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_di_next_retry ON decision_intent(next_retry_at);

CREATE TABLE IF NOT EXISTS decision_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL,
    snapshot_role TEXT NOT NULL,
    snapshot_ref TEXT,
    snapshot_hash TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(decision_id, snapshot_role)
);

CREATE INDEX IF NOT EXISTS idx_ds_decision ON decision_snapshots(decision_id);
CREATE INDEX IF NOT EXISTS idx_ds_role ON decision_snapshots(snapshot_role);

CREATE TABLE IF NOT EXISTS decision_alternatives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL,
    rank_order INTEGER NOT NULL,
    alternative_id TEXT NOT NULL,
    alternative_label TEXT,
    status TEXT NOT NULL,
    score REAL,
    rejection_reason TEXT,
    rule_code TEXT,
    metrics_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(decision_id, rank_order)
);

CREATE INDEX IF NOT EXISTS idx_da_decision ON decision_alternatives(decision_id);
CREATE INDEX IF NOT EXISTS idx_da_status ON decision_alternatives(status);

CREATE TABLE IF NOT EXISTS decision_trace_graph (
    decision_id TEXT PRIMARY KEY,
    graph_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decision_replay_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL,
    replay_status TEXT NOT NULL,
    expected_output_hash TEXT,
    actual_output_hash TEXT,
    details_json TEXT,
    run_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drr_decision ON decision_replay_runs(decision_id);
CREATE INDEX IF NOT EXISTS idx_drr_run_at ON decision_replay_runs(run_at DESC);

CREATE TABLE IF NOT EXISTS policy_versions (
    policy_version_id TEXT PRIMARY KEY,
    policy_name TEXT NOT NULL,
    policy_body_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS data_snapshot_catalog (
    snapshot_id TEXT PRIMARY KEY,
    snapshot_type TEXT NOT NULL,
    snapshot_ref TEXT,
    snapshot_hash TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dsc_type ON data_snapshot_catalog(snapshot_type);
CREATE INDEX IF NOT EXISTS idx_dsc_created ON data_snapshot_catalog(created_at DESC);

CREATE TABLE IF NOT EXISTS calculation_versions (
    calculation_version_id TEXT PRIMARY KEY,
    module_name TEXT NOT NULL,
    build_ref TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL
);
"""


class TrustDecisionPipeline:
    _initialized: bool = False
    _replay_handlers: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]]] = {}
    _standard_handlers_registered: bool = False

    STANDARD_DECISION_TYPES = {
        "investment_recommendation",
        "pre_trade_compliance",
        "full_risk_report",
        "rate_sensitivity",
        "spread_simulation",
        "scenario_analysis",
        "value_at_risk",
        "yield_curve_position",
        "ips_full_scan",
        "ips_pretrade_simulation",
    }

    SNAPSHOT_TYPE_MAX_AGE_MINUTES = {
        "market_data": int(os.getenv("MARKET_SNAPSHOT_MAX_AGE_MINUTES", "240") or "240"),
        "im_document": int(os.getenv("IM_SNAPSHOT_MAX_AGE_MINUTES", "1440") or "1440"),
        "portfolio_state": int(os.getenv("PORTFOLIO_SNAPSHOT_MAX_AGE_MINUTES", "1440") or "1440"),
        "policy_state": int(os.getenv("POLICY_SNAPSHOT_MAX_AGE_MINUTES", "10080") or "10080"),
        "decision_input": int(os.getenv("DECISION_INPUT_SNAPSHOT_MAX_AGE_MINUTES", "60") or "60"),
        "decision_output": int(os.getenv("DECISION_OUTPUT_SNAPSHOT_MAX_AGE_MINUTES", "60") or "60"),
    }

    REQUIRED_SOURCE_SNAPSHOT_TYPES: dict[str, set[str]] = {
        "investment_recommendation": {"portfolio_state", "market_data", "im_document"},
        "pre_trade_compliance": {"portfolio_state", "market_data", "im_document"},
        "ips_full_scan": {"portfolio_state", "market_data", "im_document"},
        "ips_pretrade_simulation": {"portfolio_state", "market_data", "im_document"},
        "full_risk_report": {"portfolio_state", "market_data"},
        "rate_sensitivity": {"portfolio_state", "market_data"},
        "spread_simulation": {"portfolio_state", "market_data"},
        "scenario_analysis": {"portfolio_state", "market_data"},
        "value_at_risk": {"portfolio_state", "market_data"},
        "yield_curve_position": {"portfolio_state", "market_data"},
    }

    TRUST_CHAIN_ANCHOR_BATCH_SIZE = max(1, int(os.getenv("TRUST_CHAIN_ANCHOR_BATCH_SIZE", "50") or "50"))
    TRUST_CHAIN_ANCHOR_PROVIDER = os.getenv("TRUST_CHAIN_ANCHOR_PROVIDER", "internal_notary").strip() or "internal_notary"

    REQUIRED_TRACE_FIELDS = {
        "decision_id",
        "policy_version_id",
        "data_snapshot_ids",
        "calculation_version_id",
        "input_hash",
        "output_hash",
        "trace_timestamp",
        "intent_id",
    }

    @staticmethod
    def _parse_iso_utc(raw: Any) -> Optional[datetime]:
        text = str(raw or "").strip()
        if not text:
            return None
        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except Exception:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _snapshot_max_age_minutes(cls, snapshot_type: str) -> int:
        key = str(snapshot_type or "").strip().lower()
        return int(cls.SNAPSHOT_TYPE_MAX_AGE_MINUTES.get(key, int(os.getenv("GENERIC_SNAPSHOT_MAX_AGE_MINUTES", "1440") or "1440")))

    @classmethod
    def _required_source_snapshot_types_for_decision(cls, decision_type: str) -> set[str]:
        key = str(decision_type or "").strip().lower()
        configured = cls.REQUIRED_SOURCE_SNAPSHOT_TYPES.get(key)
        if configured:
            return set(configured)
        default_required = os.getenv("DEFAULT_REQUIRED_SOURCE_SNAPSHOT_TYPES", "portfolio_state,market_data,im_document")
        return {
            str(item or "").strip().lower()
            for item in default_required.split(",")
            if str(item or "").strip()
        }

    @classmethod
    def _deterministic_replay_signature(
        cls,
        *,
        decision_type: str,
        decision_outcome: str,
        policy_version_id: str,
        data_snapshot_ids: list[str],
        calculation_version_id: str,
        input_hash: str,
    ) -> str:
        payload = {
            "decision_type": str(decision_type or ""),
            "decision_outcome": str(decision_outcome or ""),
            "policy_version_id": str(policy_version_id or ""),
            "data_snapshot_ids": sorted([str(item or "") for item in data_snapshot_ids]),
            "calculation_version_id": str(calculation_version_id or ""),
            "input_hash": str(input_hash or ""),
        }
        return cls._hash_json(payload)

    @staticmethod
    def _to_json(value: Any) -> str:
        try:
            return json.dumps(value, default=str, ensure_ascii=False)
        except Exception:
            return json.dumps({"serialization_error": str(value)}, ensure_ascii=False)

    @staticmethod
    def _from_json(raw: Any, default: Any) -> Any:
        if raw is None:
            return default
        if isinstance(raw, (dict, list)):
            return raw
        if isinstance(raw, (bytes, bytearray, memoryview)):
            try:
                raw = bytes(raw).decode("utf-8")
            except Exception:
                return default
        if not isinstance(raw, str):
            return default
        try:
            return json.loads(raw)
        except Exception:
            return default

    @staticmethod
    def _hash_json(value: Any) -> str:
        canonical = json.dumps(value, default=str, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _actor(user: Any) -> str:
        if isinstance(user, dict):
            return str(user.get("username") or user.get("sub") or user.get("id") or "system")
        if user is None:
            return "system"
        return str(user)

    @staticmethod
    def _coerce_payload(payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            return dict(payload)
        if hasattr(payload, "model_dump"):
            return payload.model_dump()
        if hasattr(payload, "dict"):
            return payload.dict()
        return {"value": payload}

    @classmethod
    async def initialize(cls) -> None:
        if cls._initialized:
            return

        for stmt in TRUST_DECISION_PIPELINE_DDL.split(";"):
            sql = stmt.strip()
            if sql:
                await execute_query(sql + ";")

        await cls._ensure_decision_registry_columns()
        cls._register_standard_replay_handlers()
        cls._initialized = True
        log.info("trust_decision_pipeline_initialized")

    @classmethod
    async def _table_columns(cls, table_name: str) -> set[str]:
        if IS_SQLITE:
            rows = await fetch_all(f"PRAGMA table_info({table_name})")
            return {str(row.get("name") or "").lower() for row in rows}
        rows = await fetch_all(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = :table_name
            """,
            {"table_name": table_name},
        )
        return {str(row.get("column_name") or "").lower() for row in rows}

    @classmethod
    async def _ensure_column(cls, table: str, column: str, definition: str) -> None:
        existing = await cls._table_columns(table)
        if column.lower() in existing:
            return
        await execute_ddl(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @classmethod
    async def _ensure_decision_registry_columns(cls) -> None:
        await cls._ensure_column("decision_registry", "policy_version_id", "TEXT")
        await cls._ensure_column("decision_registry", "data_snapshot_ids_json", "TEXT")
        await cls._ensure_column("decision_registry", "calculation_version_id", "TEXT")
        await cls._ensure_column("decision_registry", "input_hash", "TEXT")
        await cls._ensure_column("decision_registry", "output_hash", "TEXT")
        await cls._ensure_column("decision_registry", "trace_timestamp", "TEXT")
        await cls._ensure_column("decision_registry", "intent_id", "TEXT")
        await cls._ensure_column("decision_registry", "idempotency_key", "TEXT")
        await cls._ensure_column("decision_registry", "explanation_required", "INTEGER NOT NULL DEFAULT 1")
        await cls._ensure_column("decision_registry", "pipeline_stage", "TEXT")

    @classmethod
    def _register_standard_replay_handlers(cls) -> None:
        if cls._standard_handlers_registered:
            return

        async def _deterministic_handler(envelope: dict[str, Any]) -> dict[str, Any]:
            decision = envelope.get("decision") if isinstance(envelope, dict) else {}
            input_hash = str((decision or {}).get("input_hash") or "")
            signature = cls._deterministic_replay_signature(
                decision_type=str((decision or {}).get("decision_type") or ""),
                decision_outcome=str((envelope.get("decision_outcome") if isinstance(envelope, dict) else "") or ""),
                policy_version_id=str((decision or {}).get("policy_version_id") or ""),
                data_snapshot_ids=list((envelope.get("data_snapshot_ids") if isinstance(envelope, dict) else []) or []),
                calculation_version_id=str((decision or {}).get("calculation_version_id") or ""),
                input_hash=input_hash,
            )
            return {"deterministic_replay_signature": signature}

        for decision_type in cls.STANDARD_DECISION_TYPES:
            cls.register_replay_handler(decision_type, _deterministic_handler)

        cls._standard_handlers_registered = True

    @classmethod
    async def _upsert_policy_version(
        cls,
        *,
        policy_version_id: str,
        policy_name: str,
        policy_body: dict[str, Any],
        created_by: str,
    ) -> None:
        now = utcnow_iso()
        await execute_query(
            """
            INSERT INTO policy_versions (
                policy_version_id,
                policy_name,
                policy_body_json,
                created_by,
                created_at
            ) VALUES (
                :policy_version_id,
                :policy_name,
                :policy_body_json,
                :created_by,
                :created_at
            )
            ON CONFLICT(policy_version_id) DO UPDATE SET
                policy_name = excluded.policy_name,
                policy_body_json = excluded.policy_body_json,
                created_by = excluded.created_by
            """,
            {
                "policy_version_id": policy_version_id,
                "policy_name": policy_name,
                "policy_body_json": cls._to_json(policy_body),
                "created_by": created_by,
                "created_at": now,
            },
        )

    @classmethod
    async def register_policy_version(
        cls,
        *,
        policy_version_id: str,
        policy_name: str,
        policy_body: dict[str, Any],
        created_by: str = "system",
    ) -> dict[str, Any]:
        await cls.initialize()
        resolved_id = str(policy_version_id or "").strip()
        if not resolved_id:
            raise ValueError("policy_version_id is required")
        resolved_name = str(policy_name or "").strip() or resolved_id
        resolved_body = policy_body if isinstance(policy_body, dict) else {"raw": policy_body}
        await cls._upsert_policy_version(
            policy_version_id=resolved_id,
            policy_name=resolved_name,
            policy_body=resolved_body,
            created_by=str(created_by or "system"),
        )
        return {
            "policy_version_id": resolved_id,
            "policy_name": resolved_name,
            "created_by": str(created_by or "system"),
            "registered_at": utcnow_iso(),
        }

    @classmethod
    async def get_policy_version(cls, policy_version_id: str) -> Optional[dict[str, Any]]:
        await cls.initialize()
        row = await fetch_one(
            "SELECT * FROM policy_versions WHERE policy_version_id = :policy_version_id",
            {"policy_version_id": policy_version_id},
        )
        if not row:
            return None
        payload = dict(row)
        payload["policy_body"] = cls._from_json(payload.get("policy_body_json"), {})
        return payload

    @classmethod
    async def list_policy_versions(cls, limit: int = 200) -> list[dict[str, Any]]:
        await cls.initialize()
        rows = await fetch_all(
            """
            SELECT policy_version_id, policy_name, created_by, created_at
            FROM policy_versions
            ORDER BY created_at DESC
            LIMIT :limit
            """,
            {"limit": max(1, min(limit, 1000))},
        )
        return [dict(row) for row in rows]

    @classmethod
    async def _ensure_policy_control(
        cls,
        *,
        policy_version_id: str,
        metadata: Optional[dict[str, Any]],
        actor: str,
    ) -> dict[str, Any]:
        existing = await cls.get_policy_version(policy_version_id)
        if existing:
            return existing

        meta = metadata or {}
        policy_body = meta.get("policy_body") or meta.get("policy_body_json")
        if not isinstance(policy_body, dict):
            raise RuntimeError(
                f"Fail-closed policy: policy_version_id '{policy_version_id}' is not registered in policy_versions"
            )
        policy_name = str(meta.get("policy_name") or policy_version_id)
        await cls._upsert_policy_version(
            policy_version_id=policy_version_id,
            policy_name=policy_name,
            policy_body=policy_body,
            created_by=actor,
        )
        registered = await cls.get_policy_version(policy_version_id)
        if not registered:
            raise RuntimeError(
                f"Fail-closed policy: unable to register policy_version_id '{policy_version_id}'"
            )
        return registered

    @classmethod
    async def _ensure_calculation_version(
        cls,
        *,
        calculation_version_id: str,
        module_name: str,
        metadata: Optional[dict[str, Any]],
    ) -> None:
        row = await fetch_one(
            "SELECT calculation_version_id FROM calculation_versions WHERE calculation_version_id = :calculation_version_id",
            {"calculation_version_id": calculation_version_id},
        )
        if row:
            return
        await execute_query(
            """
            INSERT INTO calculation_versions (
                calculation_version_id,
                module_name,
                build_ref,
                metadata_json,
                created_at
            ) VALUES (
                :calculation_version_id,
                :module_name,
                :build_ref,
                :metadata_json,
                :created_at
            )
            """,
            {
                "calculation_version_id": calculation_version_id,
                "module_name": module_name,
                "build_ref": str((metadata or {}).get("build_ref") or os.getenv("SOURCE_COMMIT_SHA", "") or "local"),
                "metadata_json": cls._to_json({"module": module_name, "metadata": metadata or {}}),
                "created_at": utcnow_iso(),
            },
        )

    @classmethod
    def _catalog_snapshot_id(
        cls,
        *,
        snapshot_type: str,
        snapshot_hash: str,
        snapshot_ref: Optional[str],
    ) -> str:
        seed = f"{snapshot_type}|{snapshot_hash}|{snapshot_ref or ''}"
        return "snap_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()

    @classmethod
    async def register_snapshot_catalog_entry(
        cls,
        *,
        snapshot_type: str,
        snapshot_payload: Optional[dict[str, Any]] = None,
        snapshot_ref: Optional[str] = None,
        snapshot_hash: Optional[str] = None,
        snapshot_id: Optional[str] = None,
        captured_at: Optional[str] = None,
    ) -> dict[str, Any]:
        await cls.initialize()
        if not str(snapshot_type or "").strip():
            raise ValueError("snapshot_type is required")
        payload = snapshot_payload if isinstance(snapshot_payload, dict) else {}
        resolved_hash = str(snapshot_hash or "").strip() or cls._hash_json(payload)
        resolved_snapshot_id = str(snapshot_id or "").strip() or cls._catalog_snapshot_id(
            snapshot_type=snapshot_type,
            snapshot_hash=resolved_hash,
            snapshot_ref=snapshot_ref,
        )
        resolved_created_at = str(captured_at or "").strip() or utcnow_iso()
        await execute_query(
            """
            INSERT INTO data_snapshot_catalog (
                snapshot_id,
                snapshot_type,
                snapshot_ref,
                snapshot_hash,
                snapshot_json,
                created_at
            ) VALUES (
                :snapshot_id,
                :snapshot_type,
                :snapshot_ref,
                :snapshot_hash,
                :snapshot_json,
                :created_at
            )
            ON CONFLICT(snapshot_id) DO UPDATE SET
                snapshot_type = excluded.snapshot_type,
                snapshot_ref = excluded.snapshot_ref,
                snapshot_hash = excluded.snapshot_hash,
                snapshot_json = excluded.snapshot_json,
                created_at = excluded.created_at
            """,
            {
                "snapshot_id": resolved_snapshot_id,
                "snapshot_type": str(snapshot_type),
                "snapshot_ref": str(snapshot_ref or ""),
                "snapshot_hash": resolved_hash,
                "snapshot_json": cls._to_json(payload),
                "created_at": resolved_created_at,
            },
        )
        return {
            "snapshot_id": resolved_snapshot_id,
            "snapshot_type": str(snapshot_type),
            "snapshot_ref": str(snapshot_ref or ""),
            "snapshot_hash": resolved_hash,
            "created_at": resolved_created_at,
        }

    @classmethod
    async def get_snapshot_catalog_entry(cls, snapshot_id: str) -> Optional[dict[str, Any]]:
        row = await fetch_one(
            "SELECT * FROM data_snapshot_catalog WHERE snapshot_id = :snapshot_id",
            {"snapshot_id": snapshot_id},
        )
        if not row:
            return None
        payload = dict(row)
        payload["snapshot_payload"] = cls._from_json(payload.get("snapshot_json"), {})
        return payload

    @classmethod
    async def list_snapshot_catalog_entries(
        cls,
        *,
        snapshot_type: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        await cls.initialize()
        where = ""
        params: dict[str, Any] = {"limit": max(1, min(limit, 1000))}
        if snapshot_type:
            where = "WHERE LOWER(snapshot_type) = :snapshot_type"
            params["snapshot_type"] = str(snapshot_type).lower()
        rows = await fetch_all(
            f"""
            SELECT snapshot_id, snapshot_type, snapshot_ref, snapshot_hash, created_at
            FROM data_snapshot_catalog
            {where}
            ORDER BY created_at DESC
            LIMIT :limit
            """,
            params,
        )
        return [dict(row) for row in rows]

    @classmethod
    async def _resolve_catalog_snapshot_ids(
        cls,
        *,
        decision_type: str,
        provided_snapshot_ids: Optional[list[str]],
        metadata: Optional[dict[str, Any]],
        request_payload: dict[str, Any],
        output_payload: dict[str, Any],
        source_endpoint: str,
    ) -> list[str]:
        ids: list[str] = []
        resolved_source_types: set[str] = set()
        meta = metadata or {}

        source_snapshots = meta.get("source_snapshots")
        if source_snapshots is None:
            source_snapshots = []
        if not isinstance(source_snapshots, list):
            raise RuntimeError("Fail-closed policy: metadata.source_snapshots must be a list")

        for item in source_snapshots:
            if not isinstance(item, dict):
                continue
            snapshot_type = str(item.get("snapshot_type") or "").strip()
            if not snapshot_type:
                continue
            resolved_source_types.add(snapshot_type.lower())
            entry = await cls.register_snapshot_catalog_entry(
                snapshot_type=snapshot_type,
                snapshot_payload=item.get("snapshot_payload") if isinstance(item.get("snapshot_payload"), dict) else {},
                snapshot_ref=item.get("snapshot_ref"),
                snapshot_hash=item.get("snapshot_hash"),
                snapshot_id=item.get("snapshot_id"),
                captured_at=item.get("captured_at"),
            )
            ids.append(str(entry.get("snapshot_id")))

        # Always catalog immutable input/output payload hashes for replay evidence.
        input_entry = await cls.register_snapshot_catalog_entry(
            snapshot_type="decision_input",
            snapshot_payload=request_payload,
            snapshot_ref=f"{source_endpoint}:input",
        )
        output_entry = await cls.register_snapshot_catalog_entry(
            snapshot_type="decision_output",
            snapshot_payload=output_payload,
            snapshot_ref=f"{source_endpoint}:output",
        )
        ids.extend([str(input_entry.get("snapshot_id")), str(output_entry.get("snapshot_id"))])

        for raw in provided_snapshot_ids or []:
            value = str(raw or "").strip()
            if not value:
                continue
            existing = await cls.get_snapshot_catalog_entry(value)
            if not existing:
                raise RuntimeError(
                    f"Fail-closed policy: data_snapshot_id '{value}' is missing from data_snapshot_catalog"
                )
            resolved_source_types.add(str(existing.get("snapshot_type") or "").strip().lower())
            ids.append(value)

        required_source_types = cls._required_source_snapshot_types_for_decision(decision_type)
        missing_source_types = sorted(
            snapshot_type
            for snapshot_type in required_source_types
            if snapshot_type not in resolved_source_types
        )
        if missing_source_types:
            raise RuntimeError(
                "Fail-closed policy: required source snapshots missing for "
                f"decision_type '{decision_type}' ({', '.join(missing_source_types)})"
            )

        uniq: list[str] = []
        seen: set[str] = set()
        for item in ids:
            if item and item not in seen:
                seen.add(item)
                uniq.append(item)

        if not uniq:
            raise RuntimeError("Fail-closed policy: no immutable snapshot IDs resolved for decision")
        return uniq

    @classmethod
    async def _assert_snapshot_freshness(cls, snapshot_ids: list[str]) -> None:
        now = datetime.now(timezone.utc)
        stale: list[str] = []
        for snapshot_id in snapshot_ids:
            row = await cls.get_snapshot_catalog_entry(snapshot_id)
            if not row:
                stale.append(f"{snapshot_id}:missing")
                continue
            snapshot_type = str(row.get("snapshot_type") or "").strip().lower()
            created_at = cls._parse_iso_utc(row.get("created_at"))
            if not created_at:
                stale.append(f"{snapshot_id}:invalid_timestamp")
                continue
            age_minutes = (now - created_at).total_seconds() / 60.0
            if age_minutes > cls._snapshot_max_age_minutes(snapshot_type):
                stale.append(f"{snapshot_id}:{snapshot_type}:stale")

        if stale:
            raise RuntimeError(
                "Fail-closed policy: stale or invalid data snapshots detected (" + ", ".join(stale[:10]) + ")"
            )

    @classmethod
    async def readiness_catalog_report(cls) -> dict[str, Any]:
        await cls.initialize()

        integrity_violations: list[dict[str, Any]] = []
        rows = await fetch_all(
            "SELECT decision_id, data_snapshot_ids_json FROM decision_registry ORDER BY updated_at DESC LIMIT 2000"
        )
        for row in rows:
            decision_id = row.get("decision_id")
            ids = cls._from_json(row.get("data_snapshot_ids_json"), [])
            if not isinstance(ids, list):
                integrity_violations.append({"decision_id": decision_id, "reason": "invalid_snapshot_id_list"})
                continue
            for snapshot_id in ids:
                found = await fetch_one(
                    "SELECT snapshot_id FROM data_snapshot_catalog WHERE snapshot_id = :snapshot_id",
                    {"snapshot_id": str(snapshot_id)},
                )
                if not found:
                    integrity_violations.append(
                        {
                            "decision_id": decision_id,
                            "reason": "missing_snapshot",
                            "snapshot_id": snapshot_id,
                        }
                    )

        freshness_status: dict[str, Any] = {}
        now = datetime.now(timezone.utc)
        required_types = ["market_data", "im_document", "portfolio_state"]
        stale_types: list[str] = []
        for snapshot_type in required_types:
            latest = await fetch_one(
                """
                SELECT snapshot_id, created_at
                FROM data_snapshot_catalog
                WHERE LOWER(snapshot_type) = :snapshot_type
                ORDER BY created_at DESC
                LIMIT 1
                """,
                {"snapshot_type": snapshot_type},
            )
            if not latest:
                freshness_status[snapshot_type] = {"status": "MISSING"}
                stale_types.append(snapshot_type)
                continue
            created_at = cls._parse_iso_utc(latest.get("created_at"))
            if not created_at:
                freshness_status[snapshot_type] = {"status": "INVALID_TIMESTAMP", "snapshot_id": latest.get("snapshot_id")}
                stale_types.append(snapshot_type)
                continue
            age_minutes = (now - created_at).total_seconds() / 60.0
            max_age = cls._snapshot_max_age_minutes(snapshot_type)
            status = "OK" if age_minutes <= max_age else "STALE"
            freshness_status[snapshot_type] = {
                "status": status,
                "snapshot_id": latest.get("snapshot_id"),
                "age_minutes": round(age_minutes, 2),
                "max_age_minutes": max_age,
            }
            if status != "OK":
                stale_types.append(snapshot_type)

        return {
            "status": "OK" if not integrity_violations and not stale_types else "ERROR",
            "integrity_violations": integrity_violations[:50],
            "integrity_violation_count": len(integrity_violations),
            "freshness": freshness_status,
            "stale_or_missing_types": stale_types,
            "checked_at": utcnow_iso(),
        }

    @classmethod
    def _derive_default_calc_version(cls, module_name: str) -> str:
        return (
            os.getenv("GATEWAY_CALCULATION_VERSION", "").strip()
            or os.getenv("SOURCE_COMMIT_SHA", "").strip()
            or f"{module_name}:local"
        )

    @classmethod
    def _normalize_data_snapshot_ids(
        cls,
        raw_ids: Any,
        *,
        portfolio_snapshot_ref: Optional[str],
        market_snapshot_ref: Optional[str],
        policy_snapshot_ref: Optional[str],
    ) -> list[str]:
        out: list[str] = []
        if isinstance(raw_ids, list):
            for item in raw_ids:
                text = str(item or "").strip()
                if text:
                    out.append(text)
        elif isinstance(raw_ids, str):
            text = raw_ids.strip()
            if text:
                out.append(text)

        uniq: list[str] = []
        seen: set[str] = set()
        for value in out:
            if value not in seen:
                seen.add(value)
                uniq.append(value)
        return uniq

    @classmethod
    def _normalize_alternatives(
        cls,
        *,
        decision_id: str,
        payload: dict[str, Any],
        alternatives: Optional[list[dict[str, Any]]],
        decision_outcome: str,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []

        explicit_alternatives = alternatives
        if explicit_alternatives is None:
            candidate = payload.get("evaluated_alternatives")
            if isinstance(candidate, list):
                explicit_alternatives = candidate

        if explicit_alternatives:
            for idx, alt in enumerate(explicit_alternatives, start=1):
                if not isinstance(alt, dict):
                    continue
                status = str(alt.get("status") or "REJECTED").upper()
                if status not in {"SELECTED", "REJECTED"}:
                    status = "REJECTED"
                rows.append(
                    {
                        "rank_order": idx,
                        "alternative_id": str(alt.get("alternative_id") or f"alt-{idx}"),
                        "alternative_label": str(alt.get("alternative_label") or alt.get("label") or f"Alternative {idx}"),
                        "status": status,
                        "score": float(alt.get("score") or 0.0),
                        "rejection_reason": str(alt.get("rejection_reason") or ""),
                        "rule_code": str(alt.get("rule_code") or ""),
                        "metrics_json": cls._to_json(alt.get("metrics") or {}),
                    }
                )

        if not rows:
            raise RuntimeError(
                f"Fail-closed policy: explicit alternatives ledger is mandatory for decision {decision_id}"
            )

        has_selected = any(str(item.get("status") or "").upper() == "SELECTED" for item in rows)
        if not has_selected:
            raise RuntimeError(
                f"Fail-closed policy: alternatives ledger must contain at least one SELECTED option for decision {decision_id}"
            )

        return rows

    @classmethod
    async def _idempotent_intent_lookup(cls, idempotency_key: Optional[str]) -> Optional[dict[str, Any]]:
        key = str(idempotency_key or "").strip()
        if not key:
            return None
        row = await fetch_one(
            "SELECT * FROM decision_intent WHERE idempotency_key = :key ORDER BY created_at DESC LIMIT 1",
            {"key": key},
        )
        return dict(row) if row else None

    @classmethod
    async def _create_intent(
        cls,
        *,
        decision_id: str,
        idempotency_key: str,
        decision_type: str,
        module_name: str,
        source_endpoint: str,
        request_payload: dict[str, Any],
        execution_payload: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        now = utcnow_iso()
        intent_id = str(uuid4())
        await execute_query(
            """
            INSERT INTO decision_intent (
                intent_id,
                decision_id,
                idempotency_key,
                decision_type,
                module_name,
                source_endpoint,
                status,
                request_payload_json,
                execution_payload_json,
                context_json,
                created_at,
                updated_at
            ) VALUES (
                :intent_id,
                :decision_id,
                :idempotency_key,
                :decision_type,
                :module_name,
                :source_endpoint,
                'INTENT_LOGGED',
                :request_payload_json,
                :execution_payload_json,
                :context_json,
                :created_at,
                :updated_at
            )
            """,
            {
                "intent_id": intent_id,
                "decision_id": decision_id,
                "idempotency_key": idempotency_key,
                "decision_type": decision_type,
                "module_name": module_name,
                "source_endpoint": source_endpoint,
                "request_payload_json": cls._to_json(request_payload),
                "execution_payload_json": cls._to_json(execution_payload),
                "context_json": cls._to_json(context),
                "created_at": now,
                "updated_at": now,
            },
        )
        return {
            "intent_id": intent_id,
            "decision_id": decision_id,
            "idempotency_key": idempotency_key,
            "status": "INTENT_LOGGED",
            "created_at": now,
            "updated_at": now,
        }

    @classmethod
    @transactional_session
    async def _persist_intent_atomic(
        cls,
        *,
        intent_id: str,
        decision_id: str,
        idempotency_key: str,
        decision_type: str,
        decision_outcome: str,
        module_name: str,
        source_endpoint: str,
        actor: str,
        status: str,
        portfolio_snapshot_ref: Optional[str],
        market_snapshot_ref: Optional[str],
        policy_snapshot_ref: Optional[str],
        policy_version_id: str,
        data_snapshot_ids: list[str],
        calculation_version_id: str,
        input_payload: dict[str, Any],
        output_payload: dict[str, Any],
        explanation: dict[str, Any],
        alternatives: list[dict[str, Any]],
        metadata: dict[str, Any],
        trace_timestamp: str,
        input_hash: str,
        output_hash: str,
    ) -> None:
        now = utcnow_iso()

        await execute_query(
            """
            UPDATE decision_intent
            SET status = 'PERSISTING', updated_at = :updated_at
            WHERE intent_id = :intent_id
            """,
            {"intent_id": intent_id, "updated_at": now},
        )

        await execute_query(
            """
            INSERT INTO decision_registry (
                decision_id,
                decision_type,
                decision_outcome,
                module_name,
                source_endpoint,
                status,
                actor,
                portfolio_snapshot_ref,
                market_snapshot_ref,
                policy_snapshot_ref,
                policy_version_id,
                data_snapshot_ids_json,
                calculation_version_id,
                input_hash,
                output_hash,
                trace_timestamp,
                intent_id,
                idempotency_key,
                explanation_required,
                pipeline_stage,
                metadata_json,
                created_at,
                updated_at
            ) VALUES (
                :decision_id,
                :decision_type,
                :decision_outcome,
                :module_name,
                :source_endpoint,
                :status,
                :actor,
                :portfolio_snapshot_ref,
                :market_snapshot_ref,
                :policy_snapshot_ref,
                :policy_version_id,
                :data_snapshot_ids_json,
                :calculation_version_id,
                :input_hash,
                :output_hash,
                :trace_timestamp,
                :intent_id,
                :idempotency_key,
                1,
                'ACTION_READY',
                :metadata_json,
                :created_at,
                :updated_at
            )
            ON CONFLICT(decision_id) DO UPDATE SET
                decision_type = excluded.decision_type,
                decision_outcome = excluded.decision_outcome,
                module_name = excluded.module_name,
                source_endpoint = excluded.source_endpoint,
                status = excluded.status,
                actor = excluded.actor,
                portfolio_snapshot_ref = excluded.portfolio_snapshot_ref,
                market_snapshot_ref = excluded.market_snapshot_ref,
                policy_snapshot_ref = excluded.policy_snapshot_ref,
                policy_version_id = excluded.policy_version_id,
                data_snapshot_ids_json = excluded.data_snapshot_ids_json,
                calculation_version_id = excluded.calculation_version_id,
                input_hash = excluded.input_hash,
                output_hash = excluded.output_hash,
                trace_timestamp = excluded.trace_timestamp,
                intent_id = excluded.intent_id,
                idempotency_key = excluded.idempotency_key,
                explanation_required = excluded.explanation_required,
                pipeline_stage = excluded.pipeline_stage,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            {
                "decision_id": decision_id,
                "decision_type": decision_type,
                "decision_outcome": decision_outcome,
                "module_name": module_name,
                "source_endpoint": source_endpoint,
                "status": status,
                "actor": actor,
                "portfolio_snapshot_ref": str(portfolio_snapshot_ref or ""),
                "market_snapshot_ref": str(market_snapshot_ref or ""),
                "policy_snapshot_ref": str(policy_snapshot_ref or ""),
                "policy_version_id": policy_version_id,
                "data_snapshot_ids_json": cls._to_json(data_snapshot_ids),
                "calculation_version_id": calculation_version_id,
                "input_hash": input_hash,
                "output_hash": output_hash,
                "trace_timestamp": trace_timestamp,
                "intent_id": intent_id,
                "idempotency_key": idempotency_key,
                "metadata_json": cls._to_json(metadata),
                "created_at": now,
                "updated_at": now,
            },
        )

        await execute_query(
            """
            INSERT INTO decision_explanations (
                decision_id,
                schema_version,
                explanation_json,
                created_at,
                updated_at
            ) VALUES (
                :decision_id,
                :schema_version,
                :explanation_json,
                :created_at,
                :updated_at
            )
            ON CONFLICT(decision_id) DO UPDATE SET
                schema_version = excluded.schema_version,
                explanation_json = excluded.explanation_json,
                updated_at = excluded.updated_at
            """,
            {
                "decision_id": decision_id,
                "schema_version": str(explanation.get("schema_version") or ""),
                "explanation_json": cls._to_json(explanation),
                "created_at": now,
                "updated_at": now,
            },
        )

        for role, snapshot, snapshot_ref in (
            ("INPUT", input_payload, f"intent:{intent_id}:input"),
            ("OUTPUT", output_payload, f"intent:{intent_id}:output"),
            ("EXPLANATION", explanation, f"intent:{intent_id}:explanation"),
        ):
            await execute_query(
                """
                INSERT INTO decision_snapshots (
                    decision_id,
                    snapshot_role,
                    snapshot_ref,
                    snapshot_hash,
                    snapshot_json,
                    created_at
                ) VALUES (
                    :decision_id,
                    :snapshot_role,
                    :snapshot_ref,
                    :snapshot_hash,
                    :snapshot_json,
                    :created_at
                )
                ON CONFLICT(decision_id, snapshot_role) DO UPDATE SET
                    snapshot_ref = excluded.snapshot_ref,
                    snapshot_hash = excluded.snapshot_hash,
                    snapshot_json = excluded.snapshot_json
                """,
                {
                    "decision_id": decision_id,
                    "snapshot_role": role,
                    "snapshot_ref": snapshot_ref,
                    "snapshot_hash": cls._hash_json(snapshot),
                    "snapshot_json": cls._to_json(snapshot),
                    "created_at": now,
                },
            )

        await execute_query(
            "DELETE FROM decision_alternatives WHERE decision_id = :decision_id",
            {"decision_id": decision_id},
        )
        for row in alternatives:
            await execute_query(
                """
                INSERT INTO decision_alternatives (
                    decision_id,
                    rank_order,
                    alternative_id,
                    alternative_label,
                    status,
                    score,
                    rejection_reason,
                    rule_code,
                    metrics_json,
                    created_at
                ) VALUES (
                    :decision_id,
                    :rank_order,
                    :alternative_id,
                    :alternative_label,
                    :status,
                    :score,
                    :rejection_reason,
                    :rule_code,
                    :metrics_json,
                    :created_at
                )
                """,
                {
                    "decision_id": decision_id,
                    "rank_order": int(row.get("rank_order") or 0),
                    "alternative_id": row.get("alternative_id"),
                    "alternative_label": row.get("alternative_label"),
                    "status": row.get("status"),
                    "score": row.get("score"),
                    "rejection_reason": row.get("rejection_reason"),
                    "rule_code": row.get("rule_code"),
                    "metrics_json": row.get("metrics_json") or cls._to_json({}),
                    "created_at": now,
                },
            )

        trace_graph = {
            "nodes": [
                {"id": f"intent:{intent_id}", "type": "intent", "label": "Decision Intent"},
                {"id": "input", "type": "snapshot", "label": "Input Snapshot", "hash": input_hash},
                {"id": "policy", "type": "policy", "label": policy_version_id},
                {"id": "calculation", "type": "calculation", "label": calculation_version_id},
                {"id": "decision", "type": "decision", "label": decision_type, "outcome": decision_outcome},
                {"id": "compliance", "type": "compliance", "label": "Compliance Validation"},
                {"id": "explanation", "type": "explanation", "label": "Explainability"},
                {"id": "output", "type": "snapshot", "label": "Output Snapshot", "hash": output_hash},
            ],
            "edges": [
                {"from": f"intent:{intent_id}", "to": "input"},
                {"from": "input", "to": "policy"},
                {"from": "policy", "to": "calculation"},
                {"from": "calculation", "to": "decision"},
                {"from": "decision", "to": "compliance"},
                {"from": "compliance", "to": "explanation"},
                {"from": "explanation", "to": "output"},
            ],
            "data_snapshot_ids": data_snapshot_ids,
            "generated_at": now,
        }

        await execute_query(
            """
            INSERT INTO decision_trace_graph (
                decision_id,
                graph_json,
                created_at,
                updated_at
            ) VALUES (
                :decision_id,
                :graph_json,
                :created_at,
                :updated_at
            )
            ON CONFLICT(decision_id) DO UPDATE SET
                graph_json = excluded.graph_json,
                updated_at = excluded.updated_at
            """,
            {
                "decision_id": decision_id,
                "graph_json": cls._to_json(trace_graph),
                "created_at": now,
                "updated_at": now,
            },
        )

        await execute_query(
            """
            UPDATE decision_intent
            SET status = 'COMPLETED',
                updated_at = :updated_at,
                completed_at = :completed_at,
                error_text = NULL
            WHERE intent_id = :intent_id
            """,
            {
                "intent_id": intent_id,
                "updated_at": now,
                "completed_at": now,
            },
        )

    @classmethod
    async def _mark_intent_failed(cls, intent_id: str, exc: Exception) -> None:
        now = utcnow_iso()
        next_retry = (utcnow() + timedelta(seconds=30)).isoformat()
        await execute_query(
            """
            UPDATE decision_intent
            SET status = 'FAILED',
                error_text = :error_text,
                retry_count = COALESCE(retry_count, 0) + 1,
                next_retry_at = :next_retry_at,
                updated_at = :updated_at
            WHERE intent_id = :intent_id
            """,
            {
                "intent_id": intent_id,
                "error_text": str(exc)[:2000],
                "next_retry_at": next_retry,
                "updated_at": now,
            },
        )

    @classmethod
    async def _maybe_anchor_batch_boundary(cls, decision_id: str) -> None:
        try:
            latest_anchor = await fetch_one(
                "SELECT anchored_at FROM trust_chain_anchors ORDER BY anchored_at DESC, id DESC LIMIT 1"
            )
        except Exception:
            latest_anchor = None

        where_clause = ""
        params: dict[str, Any] = {}
        if latest_anchor and latest_anchor.get("anchored_at"):
            where_clause = "WHERE created_at > :anchored_at"
            params["anchored_at"] = latest_anchor.get("anchored_at")

        count_row = await fetch_one(
            f"SELECT COUNT(*) AS cnt FROM decision_registry {where_clause}",
            params,
        )
        pending_since_anchor = int((count_row or {}).get("cnt") or 0)
        if pending_since_anchor < cls.TRUST_CHAIN_ANCHOR_BATCH_SIZE:
            return

        try:
            from app.domain.core_trust.audit import AuditService

            await AuditService.create_trust_anchor(
                anchor_provider=cls.TRUST_CHAIN_ANCHOR_PROVIDER,
                provider_reference=f"decision-batch:{decision_id}",
                metadata={
                    "reason": "decision_batch_boundary",
                    "pending_since_anchor": pending_since_anchor,
                    "boundary_decision_id": decision_id,
                },
            )
        except Exception as exc:
            log.warning("trust_chain_anchor_batch_failed", decision_id=decision_id, error=str(exc))

    @classmethod
    async def get_output_snapshot_payload(cls, decision_id: str) -> Optional[dict[str, Any]]:
        row = await fetch_one(
            """
            SELECT snapshot_json
            FROM decision_snapshots
            WHERE decision_id = :decision_id AND snapshot_role = 'OUTPUT'
            """,
            {"decision_id": decision_id},
        )
        if not row:
            return None
        payload = cls._from_json(row.get("snapshot_json"), {})
        return payload if isinstance(payload, dict) else {"value": payload}

    @classmethod
    async def record_decision_fail_closed(
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

        body = cls._coerce_payload(payload)
        explanation = body.get("explanation")
        if not isinstance(explanation, dict):
            raise RuntimeError("Fail-closed policy: decision explanation payload is mandatory")

        request_payload = cls._coerce_payload(input_payload)
        if not request_payload:
            raise RuntimeError("Fail-closed policy: input payload snapshot is mandatory")

        resolved_decision_id = (
            str(decision_id or "").strip()
            or str(body.get("decision_id") or "").strip()
            or str(explanation.get("decision_id") or "").strip()
            or str(uuid4())
        )
        explanation["decision_id"] = resolved_decision_id
        body["decision_id"] = resolved_decision_id
        body["explanation"] = explanation

        resolved_policy_version = (
            str(policy_version_id or "").strip()
            or str((metadata or {}).get("policy_version_id") or "").strip()
            or str(policy_snapshot_ref or "").strip()
        )
        if not resolved_policy_version:
            raise RuntimeError("Fail-closed policy: policy_version_id is mandatory")

        actor = cls._actor(user)
        await cls._ensure_policy_control(
            policy_version_id=resolved_policy_version,
            metadata=metadata,
            actor=actor,
        )

        provided_snapshot_ids = cls._normalize_data_snapshot_ids(
            data_snapshot_ids,
            portfolio_snapshot_ref=portfolio_snapshot_ref,
            market_snapshot_ref=market_snapshot_ref,
            policy_snapshot_ref=policy_snapshot_ref,
        )

        resolved_calc_version = str(calculation_version_id or "").strip() or cls._derive_default_calc_version(module_name)
        if not resolved_calc_version:
            raise RuntimeError("Fail-closed policy: calculation_version_id is mandatory")
        await cls._ensure_calculation_version(
            calculation_version_id=resolved_calc_version,
            module_name=module_name,
            metadata=metadata,
        )

        input_hash = cls._hash_json(request_payload)
        trace_timestamp = utcnow_iso()
        derived_idempotency = (
            str(idempotency_key or "").strip()
            or hashlib.sha256(
                f"{decision_type}|{source_endpoint}|{actor}|{input_hash}".encode("utf-8")
            ).hexdigest()
        )

        existing_intent = await cls._idempotent_intent_lookup(derived_idempotency)
        if existing_intent and str(existing_intent.get("status") or "").upper() == "COMPLETED":
            existing_decision_id = str(existing_intent.get("decision_id") or "").strip()
            existing_payload = await cls.get_output_snapshot_payload(existing_decision_id)
            if existing_payload:
                return existing_payload

        if existing_intent and str(existing_intent.get("status") or "").upper() in {
            "INTENT_LOGGED",
            "PERSISTING",
            "FAILED",
        }:
            raise RuntimeError(
                f"Fail-closed policy: decision intent already in-progress for idempotency_key {derived_idempotency}"
            )

        normalized_alternatives = cls._normalize_alternatives(
            decision_id=resolved_decision_id,
            payload=body,
            alternatives=alternatives,
            decision_outcome=decision_outcome,
        )

        for stage_flag in ("data_verified", "intelligence_completed", "compliance_validated"):
            if metadata is not None and stage_flag in metadata and not bool(metadata.get(stage_flag)):
                raise RuntimeError(
                    f"Fail-closed policy: pipeline stage '{stage_flag}' is not satisfied for decision {resolved_decision_id}"
                )

        resolved_data_snapshots = await cls._resolve_catalog_snapshot_ids(
            decision_type=decision_type,
            provided_snapshot_ids=provided_snapshot_ids,
            metadata=metadata,
            request_payload=request_payload,
            output_payload=body,
            source_endpoint=source_endpoint,
        )
        await cls._assert_snapshot_freshness(resolved_data_snapshots)

        deterministic_replay_signature = cls._deterministic_replay_signature(
            decision_type=decision_type,
            decision_outcome=decision_outcome,
            policy_version_id=resolved_policy_version,
            data_snapshot_ids=resolved_data_snapshots,
            calculation_version_id=resolved_calc_version,
            input_hash=input_hash,
        )
        body["deterministic_replay_signature"] = deterministic_replay_signature
        output_hash = cls._hash_json(body)

        metadata_payload = {
            "decision_type": decision_type,
            "decision_outcome": decision_outcome,
            "response_keys": sorted(list(body.keys())),
            "policy_version_id": resolved_policy_version,
            "data_snapshot_ids": resolved_data_snapshots,
            "calculation_version_id": resolved_calc_version,
            "deterministic_replay_signature": deterministic_replay_signature,
            "pipeline": {
                "data_verified": bool((metadata or {}).get("data_verified", True)),
                "intelligence_completed": bool((metadata or {}).get("intelligence_completed", True)),
                "compliance_validated": bool((metadata or {}).get("compliance_validated", True)),
                "explanation_committed": True,
            },
        }
        if metadata:
            metadata_payload.update(metadata)

        intent = await cls._create_intent(
            decision_id=resolved_decision_id,
            idempotency_key=derived_idempotency,
            decision_type=decision_type,
            module_name=module_name,
            source_endpoint=source_endpoint,
            request_payload=request_payload,
            execution_payload=body,
            context={
                "decision_outcome": decision_outcome,
                "status": status,
                "actor": actor,
                "portfolio_snapshot_ref": portfolio_snapshot_ref,
                "market_snapshot_ref": market_snapshot_ref,
                "policy_snapshot_ref": policy_snapshot_ref,
                "policy_version_id": resolved_policy_version,
                "data_snapshot_ids": resolved_data_snapshots,
                "calculation_version_id": resolved_calc_version,
                "metadata": metadata_payload,
                "alternatives": normalized_alternatives,
                "trace_timestamp": trace_timestamp,
                "input_hash": input_hash,
                "output_hash": output_hash,
            },
        )

        intent_id = str(intent.get("intent_id") or "")
        try:
            await cls._persist_intent_atomic(
                intent_id=intent_id,
                decision_id=resolved_decision_id,
                idempotency_key=derived_idempotency,
                decision_type=decision_type,
                decision_outcome=decision_outcome,
                module_name=module_name,
                source_endpoint=source_endpoint,
                actor=actor,
                status=status,
                portfolio_snapshot_ref=portfolio_snapshot_ref,
                market_snapshot_ref=market_snapshot_ref,
                policy_snapshot_ref=policy_snapshot_ref,
                policy_version_id=resolved_policy_version,
                data_snapshot_ids=resolved_data_snapshots,
                calculation_version_id=resolved_calc_version,
                input_payload=request_payload,
                output_payload=body,
                explanation=explanation,
                alternatives=normalized_alternatives,
                metadata=metadata_payload,
                trace_timestamp=trace_timestamp,
                input_hash=input_hash,
                output_hash=output_hash,
            )
        except Exception as exc:
            await cls._mark_intent_failed(intent_id, exc)
            raise RuntimeError(
                f"Fail-closed policy: decision persistence failed for decision_id={resolved_decision_id}. "
                f"No response emitted without committed storage."
            ) from exc

        await cls._maybe_anchor_batch_boundary(resolved_decision_id)

        body["policy_version_id"] = resolved_policy_version
        body["data_snapshot_ids"] = resolved_data_snapshots
        body["calculation_version_id"] = resolved_calc_version
        body["input_hash"] = input_hash
        body["output_hash"] = output_hash
        body["trace_timestamp"] = trace_timestamp
        body["intent_id"] = intent_id
        body["idempotency_key"] = derived_idempotency
        body["alternatives"] = [
            {
                "rank": row.get("rank_order"),
                "alternative_id": row.get("alternative_id"),
                "label": row.get("alternative_label"),
                "status": row.get("status"),
                "score": row.get("score"),
                "rejection_reason": row.get("rejection_reason"),
                "rule_code": row.get("rule_code"),
            }
            for row in normalized_alternatives
        ]
        return body

    @classmethod
    async def list_pending_intents(cls, limit: int = 100) -> list[dict[str, Any]]:
        await cls.initialize()
        rows = await fetch_all(
            """
            SELECT *
            FROM decision_intent
            WHERE status IN ('INTENT_LOGGED', 'PERSISTING', 'FAILED')
            ORDER BY updated_at DESC
            LIMIT :limit
            """,
            {"limit": max(1, min(limit, 500))},
        )
        return [dict(row) for row in rows]

    @classmethod
    async def _replay_intent_finalize(cls, row: dict[str, Any]) -> tuple[bool, str]:
        intent_id = str(row.get("intent_id") or "")
        decision_id = str(row.get("decision_id") or "")
        payload = cls._from_json(row.get("execution_payload_json"), {})
        request_payload = cls._from_json(row.get("request_payload_json"), {})
        context = cls._from_json(row.get("context_json"), {})
        if not isinstance(payload, dict) or not isinstance(request_payload, dict) or not isinstance(context, dict):
            return False, "intent payload/context is not replayable"

        explanation = payload.get("explanation")
        if not isinstance(explanation, dict):
            return False, "missing explanation payload"

        try:
            await cls._persist_intent_atomic(
                intent_id=intent_id,
                decision_id=decision_id,
                idempotency_key=str(row.get("idempotency_key") or ""),
                decision_type=str(row.get("decision_type") or context.get("decision_type") or "UNKNOWN"),
                decision_outcome=str(context.get("decision_outcome") or payload.get("decision_outcome") or "UNKNOWN"),
                module_name=str(row.get("module_name") or "unknown"),
                source_endpoint=str(row.get("source_endpoint") or ""),
                actor=str(context.get("actor") or "system"),
                status=str(context.get("status") or "RECORDED"),
                portfolio_snapshot_ref=context.get("portfolio_snapshot_ref"),
                market_snapshot_ref=context.get("market_snapshot_ref"),
                policy_snapshot_ref=context.get("policy_snapshot_ref"),
                policy_version_id=str(context.get("policy_version_id") or ""),
                data_snapshot_ids=list(context.get("data_snapshot_ids") or []),
                calculation_version_id=str(context.get("calculation_version_id") or ""),
                input_payload=request_payload,
                output_payload=payload,
                explanation=explanation,
                alternatives=list(context.get("alternatives") or []),
                metadata=dict(context.get("metadata") or {}),
                trace_timestamp=str(context.get("trace_timestamp") or utcnow_iso()),
                input_hash=str(context.get("input_hash") or cls._hash_json(request_payload)),
                output_hash=str(context.get("output_hash") or cls._hash_json(payload)),
            )
            return True, "reconciled"
        except Exception as exc:
            await cls._mark_intent_failed(intent_id, exc)
            return False, str(exc)

    @classmethod
    async def reconcile_incomplete_intents(cls, limit: int = 50) -> dict[str, Any]:
        await cls.initialize()
        now = utcnow_iso()
        rows = await fetch_all(
            """
            SELECT *
            FROM decision_intent
            WHERE status IN ('INTENT_LOGGED', 'PERSISTING', 'FAILED')
              AND (next_retry_at IS NULL OR next_retry_at <= :now)
              AND COALESCE(retry_count, 0) < 10
            ORDER BY updated_at ASC
            LIMIT :limit
            """,
            {"now": now, "limit": max(1, min(limit, 500))},
        )

        ok = 0
        failed = 0
        failures: list[dict[str, Any]] = []
        for row in rows:
            success, message = await cls._replay_intent_finalize(dict(row))
            if success:
                ok += 1
            else:
                failed += 1
                failures.append(
                    {
                        "intent_id": row.get("intent_id"),
                        "decision_id": row.get("decision_id"),
                        "error": message,
                    }
                )

        return {
            "scanned": len(rows),
            "reconciled": ok,
            "failed": failed,
            "failures": failures,
            "run_at": utcnow_iso(),
        }

    @classmethod
    async def get_decision_alternatives(cls, decision_id: str) -> list[dict[str, Any]]:
        await cls.initialize()
        rows = await fetch_all(
            """
            SELECT rank_order, alternative_id, alternative_label, status, score, rejection_reason, rule_code, metrics_json
            FROM decision_alternatives
            WHERE decision_id = :decision_id
            ORDER BY rank_order ASC
            """,
            {"decision_id": decision_id},
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            entry = dict(row)
            entry["metrics"] = cls._from_json(entry.get("metrics_json"), {})
            result.append(entry)
        return result

    @classmethod
    async def get_trace_graph(cls, decision_id: str) -> Optional[dict[str, Any]]:
        await cls.initialize()
        row = await fetch_one(
            "SELECT graph_json, updated_at FROM decision_trace_graph WHERE decision_id = :decision_id",
            {"decision_id": decision_id},
        )
        if not row:
            return None
        payload = cls._from_json(row.get("graph_json"), {})
        if not isinstance(payload, dict):
            return None
        payload.setdefault("updated_at", row.get("updated_at"))
        return payload

    @classmethod
    async def get_decision_snapshots(cls, decision_id: str) -> dict[str, Any]:
        await cls.initialize()
        rows = await fetch_all(
            """
            SELECT snapshot_role, snapshot_ref, snapshot_hash, snapshot_json, created_at
            FROM decision_snapshots
            WHERE decision_id = :decision_id
            ORDER BY id ASC
            """,
            {"decision_id": decision_id},
        )
        output: dict[str, Any] = {}
        for row in rows:
            role = str(row.get("snapshot_role") or "").upper()
            output[role] = {
                "snapshot_ref": row.get("snapshot_ref"),
                "snapshot_hash": row.get("snapshot_hash"),
                "snapshot_payload": cls._from_json(row.get("snapshot_json"), {}),
                "created_at": row.get("created_at"),
            }
        return output

    @classmethod
    async def get_catalog_snapshots(cls, snapshot_ids: list[str]) -> list[dict[str, Any]]:
        await cls.initialize()
        out: list[dict[str, Any]] = []
        for snapshot_id in snapshot_ids:
            row = await cls.get_snapshot_catalog_entry(str(snapshot_id))
            if row:
                out.append(row)
        return out

    @classmethod
    async def list_replay_runs(cls, decision_id: str, limit: int = 50) -> list[dict[str, Any]]:
        await cls.initialize()
        rows = await fetch_all(
            """
            SELECT replay_status, expected_output_hash, actual_output_hash, details_json, run_at
            FROM decision_replay_runs
            WHERE decision_id = :decision_id
            ORDER BY run_at DESC, id DESC
            LIMIT :limit
            """,
            {
                "decision_id": decision_id,
                "limit": max(1, min(limit, 200)),
            },
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["details"] = cls._from_json(item.get("details_json"), {})
            out.append(item)
        return out

    @classmethod
    async def get_intent_for_decision(cls, decision_id: str) -> Optional[dict[str, Any]]:
        row = await fetch_one(
            "SELECT * FROM decision_intent WHERE decision_id = :decision_id ORDER BY created_at DESC LIMIT 1",
            {"decision_id": decision_id},
        )
        return dict(row) if row else None

    @classmethod
    async def build_explain_bundle(cls, decision_id: str) -> Optional[dict[str, Any]]:
        decision = await fetch_one(
            "SELECT * FROM decision_registry WHERE decision_id = :decision_id",
            {"decision_id": decision_id},
        )
        if not decision:
            return None

        explanation_row = await fetch_one(
            "SELECT explanation_json, schema_version FROM decision_explanations WHERE decision_id = :decision_id",
            {"decision_id": decision_id},
        )
        explanation = cls._from_json((explanation_row or {}).get("explanation_json"), {})
        if not isinstance(explanation, dict) or not explanation:
            return None

        alternatives = await cls.get_decision_alternatives(decision_id)
        trace_graph = await cls.get_trace_graph(decision_id)

        return {
            "decision": dict(decision),
            "explanation": explanation,
            "alternatives": alternatives,
            "trace_graph": trace_graph,
        }

    @classmethod
    def register_replay_handler(
        cls,
        decision_type: str,
        handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]],
    ) -> None:
        cls._replay_handlers[str(decision_type).strip().lower()] = handler

    @classmethod
    async def replay_decision(cls, decision_id: str) -> dict[str, Any]:
        await cls.initialize()
        decision = await fetch_one(
            "SELECT decision_id, decision_type, decision_outcome, output_hash, input_hash, policy_version_id, calculation_version_id, data_snapshot_ids_json FROM decision_registry WHERE decision_id = :decision_id",
            {"decision_id": decision_id},
        )
        if not decision:
            raise ValueError(f"Decision not found: {decision_id}")

        input_snapshot = await fetch_one(
            "SELECT snapshot_json, snapshot_hash FROM decision_snapshots WHERE decision_id = :decision_id AND snapshot_role = 'INPUT'",
            {"decision_id": decision_id},
        )
        output_snapshot = await fetch_one(
            "SELECT snapshot_json, snapshot_hash FROM decision_snapshots WHERE decision_id = :decision_id AND snapshot_role = 'OUTPUT'",
            {"decision_id": decision_id},
        )
        if not input_snapshot or not output_snapshot:
            raise ValueError(f"Replay snapshots missing for decision: {decision_id}")

        input_payload = cls._from_json(input_snapshot.get("snapshot_json"), {})
        output_payload = cls._from_json(output_snapshot.get("snapshot_json"), {})

        expected_output_hash = str(decision.get("output_hash") or output_snapshot.get("snapshot_hash") or "")
        actual_output_hash = cls._hash_json(output_payload)
        replay_status = "MATCH" if expected_output_hash == actual_output_hash else "MISMATCH"
        mismatch_reason = "" if replay_status == "MATCH" else "output_hash_mismatch"

        handler_key = str(decision.get("decision_type") or "").strip().lower()
        handler = cls._replay_handlers.get(handler_key)
        if not handler:
            raise RuntimeError(
                f"Fail-closed replay policy: no deterministic replay handler registered for decision_type={handler_key}"
            )

        envelope = {
            "input_payload": input_payload if isinstance(input_payload, dict) else {"value": input_payload},
            "output_payload": output_payload if isinstance(output_payload, dict) else {"value": output_payload},
            "decision": dict(decision),
            "data_snapshot_ids": cls._from_json(decision.get("data_snapshot_ids_json"), []),
            "decision_outcome": decision.get("decision_outcome"),
        }
        maybe_result = handler(envelope)
        replay_payload = await maybe_result if hasattr(maybe_result, "__await__") else maybe_result
        replay_payload = replay_payload if isinstance(replay_payload, dict) else {"value": replay_payload}
        expected_signature = str((output_payload if isinstance(output_payload, dict) else {}).get("deterministic_replay_signature") or "")
        recomputed_signature = str(replay_payload.get("deterministic_replay_signature") or "")

        if not expected_signature or not recomputed_signature:
            replay_status = "MISMATCH"
            mismatch_reason = mismatch_reason or "missing_replay_signature"
        elif expected_signature != recomputed_signature:
            replay_status = "MISMATCH"
            mismatch_reason = mismatch_reason or "deterministic_signature_mismatch"

        await execute_query(
            """
            INSERT INTO decision_replay_runs (
                decision_id,
                replay_status,
                expected_output_hash,
                actual_output_hash,
                details_json,
                run_at
            ) VALUES (
                :decision_id,
                :replay_status,
                :expected_output_hash,
                :actual_output_hash,
                :details_json,
                :run_at
            )
            """,
            {
                "decision_id": decision_id,
                "replay_status": replay_status,
                "expected_output_hash": expected_output_hash,
                "actual_output_hash": actual_output_hash,
                "details_json": cls._to_json(
                    {
                        "policy_version_id": decision.get("policy_version_id"),
                        "calculation_version_id": decision.get("calculation_version_id"),
                        "data_snapshot_ids": cls._from_json(decision.get("data_snapshot_ids_json"), []),
                        "used_handler": True,
                        "snapshot_replay": True,
                        "expected_signature": expected_signature,
                        "recomputed_signature": recomputed_signature,
                        "mismatch_reason": mismatch_reason,
                    }
                ),
                "run_at": utcnow_iso(),
            },
        )

        if replay_status != "MATCH":
            raise RuntimeError(
                f"Fail-closed replay mismatch for decision {decision_id}: {mismatch_reason or 'unknown'}"
            )

        return {
            "decision_id": decision_id,
            "replay_status": replay_status,
            "expected_output_hash": expected_output_hash,
            "actual_output_hash": actual_output_hash,
            "policy_version_id": decision.get("policy_version_id"),
            "calculation_version_id": decision.get("calculation_version_id"),
            "data_snapshot_ids": cls._from_json(decision.get("data_snapshot_ids_json"), []),
            "input_hash": decision.get("input_hash") or input_snapshot.get("snapshot_hash"),
            "output_hash": decision.get("output_hash") or output_snapshot.get("snapshot_hash"),
            "deterministic_replay_signature": recomputed_signature,
            "replayed_at": utcnow_iso(),
        }

    @classmethod
    async def command_center_ops_status(cls) -> dict[str, Any]:
        await cls.initialize()
        pending = await fetch_one(
            "SELECT COUNT(*) AS cnt FROM decision_intent WHERE status IN ('INTENT_LOGGED', 'PERSISTING', 'FAILED')"
        )
        failed = await fetch_one(
            "SELECT COUNT(*) AS cnt FROM decision_intent WHERE status = 'FAILED'"
        )
        return {
            "intent_backlog": int((pending or {}).get("cnt") or 0),
            "intent_failures": int((failed or {}).get("cnt") or 0),
            "generated_at": utcnow_iso(),
        }
