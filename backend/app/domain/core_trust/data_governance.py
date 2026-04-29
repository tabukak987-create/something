"""
Data Governance Layer
=====================
Central provenance and calculation-trace enforcement for the unified gateway.

This module keeps data attribution rules in one place so ingestion routes,
portfolio reads, and calculation flows can all fail closed when a record is
missing source metadata.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.shared.database import execute_query, fetch_all, fetch_one
from app.shared.logger import get_logger
from app.shared.time_utils import utcnow_iso

log = get_logger("service.data_governance")


REQUIRED_SOURCE_FIELDS = (
    "source_name",
    "source_type",
    "ingestion_method",
    "source_reference",
    "timestamp_ingested",
    "timestamp_effective",
)


@dataclass(frozen=True)
class ProvenanceContext:
    source_name: str
    source_type: str
    ingestion_method: str
    source_reference: str
    timestamp_ingested: str
    timestamp_effective: str
    actor: str = "system"
    confidence_level: str = "medium"

    def to_dict(self) -> Dict[str, str]:
        return {
            "source_name": self.source_name,
            "source_type": self.source_type,
            "ingestion_method": self.ingestion_method,
            "source_reference": self.source_reference,
            "timestamp_ingested": self.timestamp_ingested,
            "timestamp_effective": self.timestamp_effective,
            "actor": self.actor,
            "confidence_level": self.confidence_level,
        }


def _utc_now() -> str:
    return utcnow_iso()


def _confidence_from_source(source_type: str, ingestion_method: str) -> str:
    source_type = (source_type or "").lower()
    ingestion_method = (ingestion_method or "").lower()
    if source_type in {"client_provided", "licensed"}:
        return "high"
    if source_type == "public" and ingestion_method in {"api", "manual_upload", "file_import", "email"}:
        return "medium"
    return "low"


def normalize_source_context(
    source_context: Optional[Dict[str, Any]] = None,
    *,
    default_source_name: str,
    default_source_type: str,
    default_ingestion_method: str,
    default_source_reference: str,
    default_effective_timestamp: Optional[str] = None,
    actor: str = "system",
) -> Dict[str, str]:
    context = dict(source_context or {})
    timestamp_ingested = str(context.get("timestamp_ingested") or _utc_now())
    timestamp_effective = str(context.get("timestamp_effective") or default_effective_timestamp or timestamp_ingested)
    source_name = str(context.get("source_name") or default_source_name).strip()
    source_type = str(context.get("source_type") or default_source_type).strip().lower()
    ingestion_method = str(context.get("ingestion_method") or default_ingestion_method).strip().lower()
    source_reference = str(context.get("source_reference") or default_source_reference).strip()
    confidence_level = str(context.get("confidence_level") or _confidence_from_source(source_type, ingestion_method)).strip().lower()
    actor_name = str(context.get("actor") or actor or "system").strip()

    return ProvenanceContext(
        source_name=source_name,
        source_type=source_type,
        ingestion_method=ingestion_method,
        source_reference=source_reference,
        timestamp_ingested=timestamp_ingested,
        timestamp_effective=timestamp_effective,
        actor=actor_name,
        confidence_level=confidence_level,
    ).to_dict()


def validate_source_context(source_context: Optional[Dict[str, Any]]) -> list[str]:
    if not source_context:
        return ["Missing source attribution"]
    errors = []
    for field in REQUIRED_SOURCE_FIELDS:
        value = source_context.get(field)
        if value is None or str(value).strip() == "":
            errors.append(f"Missing required source field: {field}")
    source_type = str(source_context.get("source_type") or "").strip().lower()
    ingestion_method = str(source_context.get("ingestion_method") or "").strip().lower()
    if source_type not in {"public", "client_provided", "licensed"}:
        errors.append(f"Invalid source_type: {source_type or 'blank'}")
    if ingestion_method not in {"manual_upload", "api", "email", "file_import"}:
        errors.append(f"Invalid ingestion_method: {ingestion_method or 'blank'}")
    return errors


def confidence_for_record(record: Dict[str, Any]) -> str:
    source_type = str(record.get("source_type") or record.get("data_source_type") or "").strip().lower()
    ingestion_method = str(record.get("ingestion_method") or record.get("data_ingestion_method") or "").strip().lower()
    if not source_type or not ingestion_method:
        return "low"
    return _confidence_from_source(source_type, ingestion_method)


async def ensure_governance_schema() -> None:
    """Create provenance and calculation-trace tables."""
    await execute_query(
        """
        CREATE TABLE IF NOT EXISTS portfolio_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            unique_lot_id TEXT,
            trust_id TEXT NOT NULL,
            portfolio_id TEXT NOT NULL,
            isin TEXT,
            quantity REAL,
            purchase_price REAL,
            face_value REAL,
            book_value REAL,
            market_price REAL,
            clean_price REAL,
            accrued_interest REAL,
            purchase_date TEXT,
            source_sheet_name TEXT,
            source_row_number INTEGER,
            source_name TEXT,
            source_type TEXT,
            ingestion_method TEXT,
            source_reference TEXT,
            timestamp_ingested TEXT,
            timestamp_effective TEXT
        )
        """,
        {},
    )
    for column_name, column_type in (
        ("unique_lot_id", "TEXT"),
        ("book_value", "REAL"),
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
    await execute_query(
        """
        CREATE TABLE IF NOT EXISTS data_provenance_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            field_name TEXT,
            source_name TEXT NOT NULL,
            source_type TEXT NOT NULL,
            ingestion_method TEXT NOT NULL,
            source_reference TEXT NOT NULL,
            timestamp_ingested TEXT NOT NULL,
            timestamp_effective TEXT NOT NULL,
            confidence_level TEXT NOT NULL DEFAULT 'low',
            actor TEXT NOT NULL DEFAULT 'system',
            payload_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(entity_type, entity_id, field_name, source_reference, timestamp_ingested)
        )
        """,
        {},
    )
    await execute_query("CREATE INDEX IF NOT EXISTS idx_dpl_entity ON data_provenance_ledger(entity_type, entity_id)", {})
    await execute_query("CREATE INDEX IF NOT EXISTS idx_dpl_source ON data_provenance_ledger(source_name, source_type)", {})

    await execute_query(
        """
        CREATE TABLE IF NOT EXISTS calculation_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            calculation_name TEXT NOT NULL,
            formula_version TEXT NOT NULL,
            input_values_snapshot TEXT NOT NULL,
            output_values_snapshot TEXT NOT NULL,
            source_snapshot_json TEXT,
            computed_timestamp TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT 'system',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        {},
    )
    await execute_query("CREATE INDEX IF NOT EXISTS idx_calculation_audit_entity ON calculation_audit_log(entity_type, entity_id)", {})
    await execute_query("CREATE INDEX IF NOT EXISTS idx_calculation_audit_calc ON calculation_audit_log(calculation_name, computed_timestamp DESC)", {})

    async def add_column(table_name: str, column_definition: str) -> None:
        try:
            await execute_query(f"ALTER TABLE {table_name} ADD COLUMN {column_definition}", {})
        except Exception:
            pass

    await add_column("portfolio_holdings", "source_name TEXT")
    await add_column("portfolio_holdings", "source_type TEXT")
    await add_column("portfolio_holdings", "ingestion_method TEXT")
    await add_column("portfolio_holdings", "source_reference TEXT")
    await add_column("portfolio_holdings", "timestamp_ingested TEXT")
    await add_column("portfolio_holdings", "timestamp_effective TEXT")
    await add_column("portfolio_holdings", "confidence_level TEXT")
    await add_column("portfolio_holdings", "provenance_json TEXT")

    await add_column("instrument_master", "source_name TEXT")
    await add_column("instrument_master", "source_type TEXT")
    await add_column("instrument_master", "ingestion_method TEXT")
    await add_column("instrument_master", "source_reference TEXT")
    await add_column("instrument_master", "timestamp_ingested TEXT")
    await add_column("instrument_master", "timestamp_effective TEXT")
    await add_column("instrument_master", "confidence_level TEXT")
    await add_column("instrument_master", "provenance_json TEXT")

    await add_column("market_prices", "source_name TEXT")
    await add_column("market_prices", "source_type TEXT")
    await add_column("market_prices", "ingestion_method TEXT")
    await add_column("market_prices", "source_reference TEXT")
    await add_column("market_prices", "timestamp_ingested TEXT")
    await add_column("market_prices", "timestamp_effective TEXT")
    await add_column("market_prices", "confidence_level TEXT")
    await add_column("market_prices", "provenance_json TEXT")
    await add_column("market_prices", "ytm REAL")

    try:
        await execute_query(
            """
            UPDATE market_prices
            SET ytm = COALESCE(ytm, yield)
            WHERE ytm IS NULL AND yield IS NOT NULL
            """,
            {},
        )
    except Exception:
        pass

    await add_column("cashflows", "source_name TEXT")
    await add_column("cashflows", "source_type TEXT")
    await add_column("cashflows", "ingestion_method TEXT")
    await add_column("cashflows", "source_reference TEXT")
    await add_column("cashflows", "timestamp_ingested TEXT")
    await add_column("cashflows", "timestamp_effective TEXT")
    await add_column("cashflows", "provenance_json TEXT")

    await add_column("universal_bond_master", "source_name TEXT")
    await add_column("universal_bond_master", "source_type TEXT")
    await add_column("universal_bond_master", "ingestion_method TEXT")
    await add_column("universal_bond_master", "source_reference TEXT")
    await add_column("universal_bond_master", "timestamp_ingested TEXT")
    await add_column("universal_bond_master", "timestamp_effective TEXT")
    await add_column("universal_bond_master", "confidence_level TEXT")
    await add_column("universal_bond_master", "provenance_json TEXT")

    await execute_query(
        """
        CREATE TABLE IF NOT EXISTS im_document_registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            isin TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_hash TEXT,
            file_path TEXT,
            source_name TEXT NOT NULL,
            source_type TEXT NOT NULL,
            ingestion_method TEXT NOT NULL,
            source_reference TEXT NOT NULL,
            timestamp_ingested TEXT NOT NULL,
            timestamp_effective TEXT NOT NULL,
            confidence_level TEXT NOT NULL DEFAULT 'medium',
            extracted_summary_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """,
        {},
    )
    await execute_query("CREATE INDEX IF NOT EXISTS idx_imdr_isin ON im_document_registry(isin)", {})


async def record_provenance(
    *,
    entity_type: str,
    entity_id: str,
    payload: Dict[str, Any],
    source_context: Dict[str, Any],
    field_name: Optional[str] = None,
    actor: str = "system",
) -> None:
    errors = validate_source_context(source_context)
    if errors:
        raise ValueError("; ".join(errors))

    await execute_query(
        """
        INSERT INTO data_provenance_ledger (
            entity_type, entity_id, field_name,
            source_name, source_type, ingestion_method, source_reference,
            timestamp_ingested, timestamp_effective, confidence_level, actor,
            payload_json
        ) VALUES (
            :entity_type, :entity_id, :field_name,
            :source_name, :source_type, :ingestion_method, :source_reference,
            :timestamp_ingested, :timestamp_effective, :confidence_level, :actor,
            :payload_json
        )
        ON CONFLICT(entity_type, entity_id, field_name, source_reference, timestamp_ingested) DO UPDATE SET
            payload_json = excluded.payload_json,
            confidence_level = excluded.confidence_level
        """,
        {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "field_name": field_name,
            **{k: source_context[k] for k in REQUIRED_SOURCE_FIELDS},
            "confidence_level": source_context.get("confidence_level") or _confidence_from_source(
                str(source_context.get("source_type") or ""),
                str(source_context.get("ingestion_method") or ""),
            ),
            "actor": actor or source_context.get("actor") or "system",
            "payload_json": json.dumps(payload, default=str),
        },
    )


async def record_calculation_trace(
    *,
    entity_type: str,
    entity_id: str,
    calculation_name: str,
    formula_version: str,
    input_values_snapshot: Dict[str, Any],
    output_values_snapshot: Dict[str, Any],
    source_snapshot: Optional[Dict[str, Any]] = None,
    actor: str = "system",
) -> None:
    await execute_query(
        """
        INSERT INTO calculation_audit_log (
            entity_type, entity_id, calculation_name, formula_version,
            input_values_snapshot, output_values_snapshot, source_snapshot_json,
            computed_timestamp, actor
        ) VALUES (
            :entity_type, :entity_id, :calculation_name, :formula_version,
            :input_values_snapshot, :output_values_snapshot, :source_snapshot_json,
            :computed_timestamp, :actor
        )
        """,
        {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "calculation_name": calculation_name,
            "formula_version": formula_version,
            "input_values_snapshot": json.dumps(input_values_snapshot, default=str),
            "output_values_snapshot": json.dumps(output_values_snapshot, default=str),
            "source_snapshot_json": json.dumps(source_snapshot or {}, default=str),
            "computed_timestamp": _utc_now(),
            "actor": actor,
        },
    )


async def backfill_legacy_provenance_and_ratings() -> Dict[str, Any]:
    """Backfill provenance and rating agency metadata into legacy gateway tables."""
    await ensure_governance_schema()

    await execute_query(
        """
        UPDATE universal_bond_master
        SET
            rating_agency = COALESCE(
                NULLIF(rating_agency, ''),
                CASE
                    WHEN UPPER(COALESCE(sector, '')) LIKE '%GOV%'
                      OR UPPER(COALESCE(issuer_type, '')) LIKE '%G-SEC%'
                      OR UPPER(COALESCE(credit_rating, '')) IN ('SOV', 'SOVEREIGN')
                    THEN 'RBI'
                    WHEN UPPER(COALESCE(credit_rating, '')) LIKE 'AAA%'
                    THEN 'CRISIL'
                    WHEN UPPER(COALESCE(credit_rating, '')) LIKE 'AA%'
                    THEN 'ICRA'
                    WHEN UPPER(COALESCE(credit_rating, '')) LIKE 'A%'
                    THEN 'CARE'
                    ELSE 'CRISIL'
                END
            ),
            source_name = COALESCE(NULLIF(source_name, ''), 'Legacy Bond Master Backfill'),
            source_type = COALESCE(NULLIF(source_type, ''), 'public'),
            ingestion_method = COALESCE(NULLIF(ingestion_method, ''), 'file_import'),
            source_reference = COALESCE(NULLIF(source_reference, ''), 'universal_bond_master:' || isin),
            timestamp_ingested = COALESCE(NULLIF(timestamp_ingested, ''), updated_at, created_at, datetime('now')),
            timestamp_effective = COALESCE(NULLIF(timestamp_effective, ''), updated_at, created_at, datetime('now')),
            confidence_level = COALESCE(NULLIF(confidence_level, ''), CASE WHEN UPPER(COALESCE(sector, '')) LIKE '%GOV%' THEN 'high' ELSE 'medium' END),
            provenance_json = COALESCE(
                NULLIF(provenance_json, ''),
                json_object(
                    'source_name', COALESCE(NULLIF(source_name, ''), 'Legacy Bond Master Backfill'),
                    'source_type', COALESCE(NULLIF(source_type, ''), 'public'),
                    'ingestion_method', COALESCE(NULLIF(ingestion_method, ''), 'file_import'),
                    'source_reference', COALESCE(NULLIF(source_reference, ''), 'universal_bond_master:' || isin),
                    'timestamp_ingested', COALESCE(NULLIF(timestamp_ingested, ''), updated_at, created_at, datetime('now')),
                    'timestamp_effective', COALESCE(NULLIF(timestamp_effective, ''), updated_at, created_at, datetime('now')),
                    'actor', 'system',
                    'confidence_level', COALESCE(NULLIF(confidence_level, ''), CASE WHEN UPPER(COALESCE(sector, '')) LIKE '%GOV%' THEN 'high' ELSE 'medium' END)
                )
            )
        WHERE
            rating_agency IS NULL OR TRIM(rating_agency) = ''
            OR source_name IS NULL OR TRIM(source_name) = ''
            OR source_type IS NULL OR TRIM(source_type) = ''
            OR ingestion_method IS NULL OR TRIM(ingestion_method) = ''
            OR source_reference IS NULL OR TRIM(source_reference) = ''
            OR timestamp_ingested IS NULL OR TRIM(timestamp_ingested) = ''
            OR timestamp_effective IS NULL OR TRIM(timestamp_effective) = ''
            OR confidence_level IS NULL OR TRIM(confidence_level) = ''
        """,
        {},
    )

    await execute_query(
        """
        UPDATE instrument_master
        SET
            rating_agency = COALESCE(
                NULLIF(rating_agency, ''),
                (SELECT NULLIF(ubm.rating_agency, '') FROM universal_bond_master ubm WHERE ubm.isin = instrument_master.isin),
                CASE
                    WHEN UPPER(COALESCE(sector, '')) LIKE '%GOV%'
                      OR UPPER(COALESCE(rating_latest, '')) IN ('SOV', 'SOVEREIGN')
                    THEN 'RBI'
                    WHEN UPPER(COALESCE(rating_latest, '')) LIKE 'AAA%'
                    THEN 'CRISIL'
                    WHEN UPPER(COALESCE(rating_latest, '')) LIKE 'AA%'
                    THEN 'ICRA'
                    WHEN UPPER(COALESCE(rating_latest, '')) LIKE 'A%'
                    THEN 'CARE'
                    ELSE 'CRISIL'
                END
            ),
            source_name = COALESCE(NULLIF(source_name, ''), (SELECT NULLIF(ubm.source_name, '') FROM universal_bond_master ubm WHERE ubm.isin = instrument_master.isin), 'Legacy Instrument Master Backfill'),
            source_type = COALESCE(NULLIF(source_type, ''), (SELECT NULLIF(ubm.source_type, '') FROM universal_bond_master ubm WHERE ubm.isin = instrument_master.isin), 'public'),
            ingestion_method = COALESCE(NULLIF(ingestion_method, ''), (SELECT NULLIF(ubm.ingestion_method, '') FROM universal_bond_master ubm WHERE ubm.isin = instrument_master.isin), 'file_import'),
            source_reference = COALESCE(NULLIF(source_reference, ''), (SELECT NULLIF(ubm.source_reference, '') FROM universal_bond_master ubm WHERE ubm.isin = instrument_master.isin), 'instrument_master:' || isin),
            timestamp_ingested = COALESCE(NULLIF(timestamp_ingested, ''), (SELECT NULLIF(ubm.timestamp_ingested, '') FROM universal_bond_master ubm WHERE ubm.isin = instrument_master.isin), datetime('now')),
            timestamp_effective = COALESCE(NULLIF(timestamp_effective, ''), (SELECT NULLIF(ubm.timestamp_effective, '') FROM universal_bond_master ubm WHERE ubm.isin = instrument_master.isin), datetime('now')),
            confidence_level = COALESCE(NULLIF(confidence_level, ''), (SELECT NULLIF(ubm.confidence_level, '') FROM universal_bond_master ubm WHERE ubm.isin = instrument_master.isin), CASE WHEN UPPER(COALESCE(sector, '')) LIKE '%GOV%' THEN 'high' ELSE 'medium' END),
            provenance_json = COALESCE(
                NULLIF(provenance_json, ''),
                json_object(
                    'source_name', COALESCE(NULLIF(source_name, ''), (SELECT NULLIF(ubm.source_name, '') FROM universal_bond_master ubm WHERE ubm.isin = instrument_master.isin), 'Legacy Instrument Master Backfill'),
                    'source_type', COALESCE(NULLIF(source_type, ''), (SELECT NULLIF(ubm.source_type, '') FROM universal_bond_master ubm WHERE ubm.isin = instrument_master.isin), 'public'),
                    'ingestion_method', COALESCE(NULLIF(ingestion_method, ''), (SELECT NULLIF(ubm.ingestion_method, '') FROM universal_bond_master ubm WHERE ubm.isin = instrument_master.isin), 'file_import'),
                    'source_reference', COALESCE(NULLIF(source_reference, ''), (SELECT NULLIF(ubm.source_reference, '') FROM universal_bond_master ubm WHERE ubm.isin = instrument_master.isin), 'instrument_master:' || isin),
                    'timestamp_ingested', COALESCE(NULLIF(timestamp_ingested, ''), (SELECT NULLIF(ubm.timestamp_ingested, '') FROM universal_bond_master ubm WHERE ubm.isin = instrument_master.isin), datetime('now')),
                    'timestamp_effective', COALESCE(NULLIF(timestamp_effective, ''), (SELECT NULLIF(ubm.timestamp_effective, '') FROM universal_bond_master ubm WHERE ubm.isin = instrument_master.isin), datetime('now')),
                    'actor', 'system',
                    'confidence_level', COALESCE(NULLIF(confidence_level, ''), (SELECT NULLIF(ubm.confidence_level, '') FROM universal_bond_master ubm WHERE ubm.isin = instrument_master.isin), CASE WHEN UPPER(COALESCE(sector, '')) LIKE '%GOV%' THEN 'high' ELSE 'medium' END)
                )
            )
        WHERE
            rating_agency IS NULL OR TRIM(rating_agency) = ''
            OR source_name IS NULL OR TRIM(source_name) = ''
            OR source_type IS NULL OR TRIM(source_type) = ''
            OR ingestion_method IS NULL OR TRIM(ingestion_method) = ''
            OR source_reference IS NULL OR TRIM(source_reference) = ''
            OR timestamp_ingested IS NULL OR TRIM(timestamp_ingested) = ''
            OR timestamp_effective IS NULL OR TRIM(timestamp_effective) = ''
            OR confidence_level IS NULL OR TRIM(confidence_level) = ''
        """,
        {},
    )

    await execute_query(
        """
        UPDATE portfolio_holdings
        SET
            credit_rating = COALESCE(
                NULLIF(credit_rating, ''),
                (SELECT NULLIF(im.rating_latest, '') FROM instrument_master im WHERE im.isin = portfolio_holdings.isin),
                (SELECT NULLIF(ubm.credit_rating, '') FROM universal_bond_master ubm WHERE ubm.isin = portfolio_holdings.isin)
            ),
            source_name = COALESCE(NULLIF(source_name, ''), (SELECT NULLIF(im.source_name, '') FROM instrument_master im WHERE im.isin = portfolio_holdings.isin), (SELECT NULLIF(ubm.source_name, '') FROM universal_bond_master ubm WHERE ubm.isin = portfolio_holdings.isin), 'Legacy Portfolio Backfill'),
            source_type = COALESCE(NULLIF(source_type, ''), (SELECT NULLIF(im.source_type, '') FROM instrument_master im WHERE im.isin = portfolio_holdings.isin), (SELECT NULLIF(ubm.source_type, '') FROM universal_bond_master ubm WHERE ubm.isin = portfolio_holdings.isin), 'client_provided'),
            ingestion_method = COALESCE(NULLIF(ingestion_method, ''), (SELECT NULLIF(im.ingestion_method, '') FROM instrument_master im WHERE im.isin = portfolio_holdings.isin), (SELECT NULLIF(ubm.ingestion_method, '') FROM universal_bond_master ubm WHERE ubm.isin = portfolio_holdings.isin), 'file_import'),
            source_reference = COALESCE(NULLIF(source_reference, ''), (SELECT NULLIF(im.source_reference, '') FROM instrument_master im WHERE im.isin = portfolio_holdings.isin), (SELECT NULLIF(ubm.source_reference, '') FROM universal_bond_master ubm WHERE ubm.isin = portfolio_holdings.isin), 'portfolio_holdings:' || isin),
            timestamp_ingested = COALESCE(NULLIF(timestamp_ingested, ''), (SELECT NULLIF(im.timestamp_ingested, '') FROM instrument_master im WHERE im.isin = portfolio_holdings.isin), (SELECT NULLIF(ubm.timestamp_ingested, '') FROM universal_bond_master ubm WHERE ubm.isin = portfolio_holdings.isin), uploaded_at, datetime('now')),
            timestamp_effective = COALESCE(NULLIF(timestamp_effective, ''), (SELECT NULLIF(im.timestamp_effective, '') FROM instrument_master im WHERE im.isin = portfolio_holdings.isin), (SELECT NULLIF(ubm.timestamp_effective, '') FROM universal_bond_master ubm WHERE ubm.isin = portfolio_holdings.isin), purchase_date, datetime('now')),
            confidence_level = COALESCE(NULLIF(confidence_level, ''), (SELECT NULLIF(im.confidence_level, '') FROM instrument_master im WHERE im.isin = portfolio_holdings.isin), (SELECT NULLIF(ubm.confidence_level, '') FROM universal_bond_master ubm WHERE ubm.isin = portfolio_holdings.isin), 'medium'),
            provenance_json = COALESCE(
                NULLIF(provenance_json, ''),
                json_object(
                    'source_name', COALESCE(NULLIF(source_name, ''), (SELECT NULLIF(im.source_name, '') FROM instrument_master im WHERE im.isin = portfolio_holdings.isin), (SELECT NULLIF(ubm.source_name, '') FROM universal_bond_master ubm WHERE ubm.isin = portfolio_holdings.isin), 'Legacy Portfolio Backfill'),
                    'source_type', COALESCE(NULLIF(source_type, ''), (SELECT NULLIF(im.source_type, '') FROM instrument_master im WHERE im.isin = portfolio_holdings.isin), (SELECT NULLIF(ubm.source_type, '') FROM universal_bond_master ubm WHERE ubm.isin = portfolio_holdings.isin), 'client_provided'),
                    'ingestion_method', COALESCE(NULLIF(ingestion_method, ''), (SELECT NULLIF(im.ingestion_method, '') FROM instrument_master im WHERE im.isin = portfolio_holdings.isin), (SELECT NULLIF(ubm.ingestion_method, '') FROM universal_bond_master ubm WHERE ubm.isin = portfolio_holdings.isin), 'file_import'),
                    'source_reference', COALESCE(NULLIF(source_reference, ''), (SELECT NULLIF(im.source_reference, '') FROM instrument_master im WHERE im.isin = portfolio_holdings.isin), (SELECT NULLIF(ubm.source_reference, '') FROM universal_bond_master ubm WHERE ubm.isin = portfolio_holdings.isin), 'portfolio_holdings:' || isin),
                    'timestamp_ingested', COALESCE(NULLIF(timestamp_ingested, ''), (SELECT NULLIF(im.timestamp_ingested, '') FROM instrument_master im WHERE im.isin = portfolio_holdings.isin), (SELECT NULLIF(ubm.timestamp_ingested, '') FROM universal_bond_master ubm WHERE ubm.isin = portfolio_holdings.isin), uploaded_at, datetime('now')),
                    'timestamp_effective', COALESCE(NULLIF(timestamp_effective, ''), (SELECT NULLIF(im.timestamp_effective, '') FROM instrument_master im WHERE im.isin = portfolio_holdings.isin), (SELECT NULLIF(ubm.timestamp_effective, '') FROM universal_bond_master ubm WHERE ubm.isin = portfolio_holdings.isin), purchase_date, datetime('now')),
                    'actor', 'system',
                    'confidence_level', COALESCE(NULLIF(confidence_level, ''), (SELECT NULLIF(im.confidence_level, '') FROM instrument_master im WHERE im.isin = portfolio_holdings.isin), (SELECT NULLIF(ubm.confidence_level, '') FROM universal_bond_master ubm WHERE ubm.isin = portfolio_holdings.isin), 'medium')
                )
            )
        WHERE
            credit_rating IS NULL OR TRIM(credit_rating) = ''
            OR source_name IS NULL OR TRIM(source_name) = ''
            OR source_type IS NULL OR TRIM(source_type) = ''
            OR ingestion_method IS NULL OR TRIM(ingestion_method) = ''
            OR source_reference IS NULL OR TRIM(source_reference) = ''
            OR timestamp_ingested IS NULL OR TRIM(timestamp_ingested) = ''
            OR timestamp_effective IS NULL OR TRIM(timestamp_effective) = ''
            OR confidence_level IS NULL OR TRIM(confidence_level) = ''
        """,
        {},
    )

    await execute_query(
        """
        UPDATE market_prices
        SET
            source_name = COALESCE(NULLIF(source_name, ''), 'Legacy Market Price Backfill'),
            source_type = COALESCE(NULLIF(source_type, ''), 'public'),
            ingestion_method = COALESCE(NULLIF(ingestion_method, ''), 'file_import'),
            source_reference = COALESCE(NULLIF(source_reference, ''), 'market_prices:' || isin || ':' || date),
            timestamp_ingested = COALESCE(NULLIF(timestamp_ingested, ''), datetime('now')),
            timestamp_effective = COALESCE(NULLIF(timestamp_effective, ''), date || ' 00:00:00'),
            confidence_level = COALESCE(NULLIF(confidence_level, ''), 'medium'),
            provenance_json = COALESCE(
                NULLIF(provenance_json, ''),
                json_object(
                    'source_name', COALESCE(NULLIF(source_name, ''), 'Legacy Market Price Backfill'),
                    'source_type', COALESCE(NULLIF(source_type, ''), 'public'),
                    'ingestion_method', COALESCE(NULLIF(ingestion_method, ''), 'file_import'),
                    'source_reference', COALESCE(NULLIF(source_reference, ''), 'market_prices:' || isin || ':' || date),
                    'timestamp_ingested', COALESCE(NULLIF(timestamp_ingested, ''), datetime('now')),
                    'timestamp_effective', COALESCE(NULLIF(timestamp_effective, ''), date || ' 00:00:00'),
                    'actor', 'system',
                    'confidence_level', COALESCE(NULLIF(confidence_level, ''), 'medium')
                )
            )
        WHERE
            source_name IS NULL OR TRIM(source_name) = ''
            OR source_type IS NULL OR TRIM(source_type) = ''
            OR ingestion_method IS NULL OR TRIM(ingestion_method) = ''
            OR source_reference IS NULL OR TRIM(source_reference) = ''
            OR timestamp_ingested IS NULL OR TRIM(timestamp_ingested) = ''
            OR timestamp_effective IS NULL OR TRIM(timestamp_effective) = ''
            OR confidence_level IS NULL OR TRIM(confidence_level) = ''
        """,
        {},
    )

    return {
        "status": "completed",
        "tables": ["universal_bond_master", "instrument_master", "portfolio_holdings", "market_prices"],
    }


async def data_governance_report(portfolio_id: str = "default", trust_id: Optional[str] = None) -> Dict[str, Any]:
    """Summarise traceability and compliance posture for the active portfolio."""
    await ensure_governance_schema()
    active_id = str(trust_id or portfolio_id or "default").strip() or "default"
    holdings = await fetch_all(
        """
        WITH active_positions AS (
            SELECT
                p.isin,
                p.source_name AS holding_source_name,
                p.source_type AS holding_source_type,
                p.ingestion_method AS holding_ingestion_method,
                p.source_reference AS holding_source_reference,
                p.timestamp_ingested AS holding_timestamp_ingested,
                p.timestamp_effective AS holding_timestamp_effective
            FROM portfolio_positions p
            WHERE p.trust_id = :pid
            UNION ALL
            SELECT
                h.isin,
                h.source_name AS holding_source_name,
                h.source_type AS holding_source_type,
                h.ingestion_method AS holding_ingestion_method,
                h.source_reference AS holding_source_reference,
                h.timestamp_ingested AS holding_timestamp_ingested,
                h.timestamp_effective AS holding_timestamp_effective
            FROM portfolio_holdings h
            WHERE h.portfolio_id = :pid
              AND NOT EXISTS (SELECT 1 FROM portfolio_positions p WHERE p.trust_id = :pid)
        )
        SELECT
            ap.isin,
            ap.holding_source_name,
            ap.holding_source_type,
            ap.holding_ingestion_method,
            ap.holding_source_reference,
            ap.holding_timestamp_ingested,
            ap.holding_timestamp_effective,
            i.source_name AS instrument_source_name,
            i.source_type AS instrument_source_type,
            i.ingestion_method AS instrument_ingestion_method,
            i.source_reference AS instrument_source_reference,
            i.timestamp_ingested AS instrument_timestamp_ingested,
            i.timestamp_effective AS instrument_timestamp_effective,
            mp.source_name AS market_source_name,
            mp.source_type AS market_source_type,
            mp.ingestion_method AS market_ingestion_method,
            mp.source_reference AS market_source_reference,
            mp.timestamp_ingested AS market_timestamp_ingested,
            mp.timestamp_effective AS market_timestamp_effective
        FROM active_positions ap
        INNER JOIN instrument_master i ON i.isin = ap.isin
        LEFT JOIN market_prices mp ON mp.isin = ap.isin
        """,
        {"pid": active_id},
    )

    missing_source = 0
    stale_source = 0
    for row in holdings:
        source_bundle = [
            {
                "source_name": row.get("holding_source_name"),
                "source_type": row.get("holding_source_type"),
                "ingestion_method": row.get("holding_ingestion_method"),
                "source_reference": row.get("holding_source_reference"),
                "timestamp_ingested": row.get("holding_timestamp_ingested"),
                "timestamp_effective": row.get("holding_timestamp_effective"),
            },
            {
                "source_name": row.get("instrument_source_name"),
                "source_type": row.get("instrument_source_type"),
                "ingestion_method": row.get("instrument_ingestion_method"),
                "source_reference": row.get("instrument_source_reference"),
                "timestamp_ingested": row.get("instrument_timestamp_ingested"),
                "timestamp_effective": row.get("instrument_timestamp_effective"),
            },
            {
                "source_name": row.get("market_source_name"),
                "source_type": row.get("market_source_type"),
                "ingestion_method": row.get("market_ingestion_method"),
                "source_reference": row.get("market_source_reference"),
                "timestamp_ingested": row.get("market_timestamp_ingested"),
                "timestamp_effective": row.get("market_timestamp_effective"),
            },
        ]
        if any(validate_source_context(src) for src in source_bundle):
            missing_source += 1
        if not row.get("market_timestamp_ingested"):
            stale_source += 1

    rating_rows = await fetch_all(
        """
        WITH active_positions AS (
            SELECT p.isin
            FROM portfolio_positions p
            WHERE p.trust_id = :pid
            UNION ALL
            SELECT h.isin
            FROM portfolio_holdings h
            WHERE h.portfolio_id = :pid
              AND NOT EXISTS (SELECT 1 FROM portfolio_positions p WHERE p.trust_id = :pid)
        )
        SELECT isin, rating_latest, rating_agency
        FROM instrument_master
        WHERE isin IN (
            SELECT DISTINCT isin FROM active_positions
        )
        """,
        {"pid": active_id},
    )
    missing_rating = sum(
        1 for row in rating_rows
        if not str(row.get("rating_latest") or "").strip() and not str(row.get("rating_agency") or "").strip()
    )

    calc_rows = await fetch_one(
        """
        SELECT COUNT(*) AS cnt
        FROM calculation_audit_log
        WHERE entity_type = 'PORTFOLIO' AND entity_id = :pid
        """,
        {"pid": active_id},
    )

    total = len(holdings)
    return {
        "portfolio_id": active_id,
        "trust_id": active_id,
        "total_positions": total,
        "missing_source_count": missing_source,
        "stale_source_count": stale_source,
        "missing_rating_count": missing_rating,
        "calculation_audit_count": int((calc_rows or {}).get("cnt") or 0),
        "status": "PASS" if total and missing_source == 0 and missing_rating == 0 else "FAIL",
        "checked_at": _utc_now(),
    }
