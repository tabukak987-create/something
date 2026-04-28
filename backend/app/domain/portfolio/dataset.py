"""
Portfolio dataset ingestion and audit service.

This service centralizes:
1) Upload parsing (CSV/JSON/XLSX/XLS)
2) Record normalization and validation
3) Sync into portfolio/master/market tables
4) Regulatory audit summaries

The implementation is intentionally additive and conservative so existing
routers/modules remain unchanged.
"""

from __future__ import annotations

import csv
import io
import hashlib
import json
import re
import uuid
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

try:
	import openpyxl
except Exception:  # pragma: no cover
	openpyxl = None

try:
	import xlrd
except Exception:  # pragma: no cover
	xlrd = None

from app.shared.database import IS_SQLITE, execute_query, fetch_all, fetch_one, transactional_session
from app.shared.deployment import strict_portfolio_versioning_enabled
from app.shared.logger import get_logger
from app.domain.core_trust.data_governance import normalize_source_context, record_provenance, validate_source_context
from app.domain.market_data.instrument_classification import classify_instrument
from app.domain.portfolio.institutional_financial_core import classify_issuer, parse_flexible_date, solve_ytm_percent, validate_bond_record
from app.domain.portfolio.analytics import invalidate_analytics_cache, normalize_rating
from app.domain.portfolio.engine import ensure_portfolio_core_schema
from app.shared.time_utils import utcnow

log = get_logger("service.portfolio_dataset")


def _utcnow() -> datetime:
	return utcnow()


async def _table_has_column(table_name: str, column_name: str) -> bool:
	if IS_SQLITE:
		rows = await fetch_all(f"PRAGMA table_info({table_name})")
		return any(str(row.get("name") or "").lower() == column_name.lower() for row in rows)

	rows = await fetch_all(
		"""
		SELECT 1
		FROM information_schema.columns
		WHERE table_name = :table_name
		  AND column_name = :column_name
		LIMIT 1
		""",
		{"table_name": table_name, "column_name": column_name},
	)
	return bool(rows)


class PortfolioDatasetService:
	"""Service used for robust portfolio upload handling and audit output."""

	PORTFOLIO_SHEET_HINTS: Dict[str, set[str]] = {
		"bond_name": {"bondname", "particulars", "securityname", "name"},
		"issuer_name": {"issuer", "issuername", "borrower", "counterparty"},
		"face_value": {"facevalue", "fv", "parvalue", "nominalvalue", "closingfacevalue", "originalfacevalue"},
		"quantity": {"qty", "quantity", "units", "noofunits"},
		"purchase_date": {"purchasedate", "acquisitiondate", "buydate", "issue_date"},
		"maturity_date": {"maturitydate", "maturity", "redemptiondate", "maturity/call"},
		"isin": {"isin", "isinno", "isinnumber", "loan code", "loancode", "isinnoloancode"},
	}

	# Canonical aliases used during upload parsing.
	COLUMN_ALIASES: Dict[str, set[str]] = {
		"isin": {"isin", "isinno", "isinnumber", "loan code", "loancode", "isinnoloancode", "isinnoloancode"},
		"bond_name": {"bondname", "particulars", "securityname", "name"},
		"issuer_name": {"issuer", "issuername", "borrower", "counterparty"},
		"face_value": {"facevalue", "fv", "parvalue", "nominalvalue", "closingfacevalue", "originalfacevalue"},
		"quantity": {"qty", "quantity", "units", "noofunits"},
		"purchase_price": {"purchaseprice", "buyprice", "costprice", "price"},
		"current_value": {"currentvalue", "marketvalue", "bookvalue", "value", "closinginvestment", "originalbookvalue"},
		"purchase_date": {"purchasedate", "acquisitiondate", "buydate", "issue_date"},
		"maturity_date": {"maturitydate", "maturity", "redemptiondate", "maturity/call"},
		"credit_rating": {"creditrating", "rating"},
		"credit_rating_1": {"rating1"},
		"credit_rating_2": {"rating2"},
		"coupon_rate": {"coupon", "couponrate", "interestrate", "rateofint"},
		"ytm_at_purchase": {"ytm", "yield", "yieldtomaturity", "ytmatpurchase"},
		"sector": {"sector", "industry"},
		"category": {"category", "assetclass", "instrumenttype"},
		"ip_dates": {"ipdates"},
		"put_date": {"putdt"},
		"call_date": {"calldt"},
		"rating_agency": {"ratingagency"},
		"rating_agency_1": {"ratingagency1"},
		"rating_agency_2": {"ratingagency2"},
		# Hidden sheet columns seen in reviewed files.
		"op_amortisation": {"opamortisation", "openingamortisation"},
		"amortisation": {"amortisation"},
		"closing_amortisation": {"closingamortisation"},
	}

	@staticmethod
	def _sha256_bytes(content: bytes) -> str:
		return hashlib.sha256(content).hexdigest()

	@staticmethod
	def _canonicalize_upload_reference(source_reference: Optional[str], fallback_filename: Optional[str] = None) -> str:
		candidate = str(source_reference or fallback_filename or "").strip()
		if not candidate:
			return "uploaded_file"
		name = candidate.replace("\\", "/").rsplit("/", 1)[-1].strip()
		if not name:
			return "uploaded_file"
		path = Path(name)
		stem = path.stem or name
		suffix = path.suffix
		stem_lower = stem.lower()
		for prefix in ("qa_", "temp_", "tmp_", "copy_of_"):
			while stem_lower.startswith(prefix):
				stem = stem[len(prefix):]
				stem_lower = stem.lower()
		for suffix_token in ("_final", "-final", "_copy", "-copy", "_latest", "-latest"):
			while stem_lower.endswith(suffix_token):
				stem = stem[: -len(suffix_token)]
				stem_lower = stem.lower()
		stem = re.sub(r"([_-])v\d+$", "", stem, flags=re.IGNORECASE).strip(" _-")
		canonical = f"{stem}{suffix}" if stem else name
		return canonical or name

	@classmethod
	async def _ensure_upload_tables(cls) -> None:
		await execute_query(
			"""CREATE TABLE IF NOT EXISTS portfolio_sheet_allocations (
			   isin TEXT NOT NULL,
			   category_bucket TEXT NOT NULL,
			   sector_bucket TEXT NOT NULL,
			   basis_value REAL NOT NULL DEFAULT 0,
			   source_name TEXT,
			   source_type TEXT,
			   ingestion_method TEXT,
			   timestamp_ingested TEXT,
			   timestamp_effective TEXT,
			   source_reference TEXT,
			   source_sheet_name TEXT,
			   source_row_number INTEGER,
			   created_at TEXT DEFAULT CURRENT_TIMESTAMP
			)""",
			{},
		)
		for column_name, column_type in (
			("source_reference", "TEXT"),
			("source_sheet_name", "TEXT"),
			("source_row_number", "INTEGER"),
		):
			if not await _table_has_column("portfolio_sheet_allocations", column_name):
				await execute_query(f"ALTER TABLE portfolio_sheet_allocations ADD COLUMN {column_name} {column_type}", {})
		await execute_query(
			"""CREATE TABLE IF NOT EXISTS portfolio_upload_batches (
			   id INTEGER PRIMARY KEY AUTOINCREMENT,
			   batch_id TEXT NOT NULL UNIQUE,
			   trust_id TEXT NOT NULL,
			   portfolio_id TEXT NOT NULL,
			   file_name TEXT NOT NULL,
			   mime_type TEXT,
			   file_hash TEXT NOT NULL,
			   raw_content BYTEA,
			   raw_storage_path TEXT,
			   uploaded_by TEXT NOT NULL,
			   uploaded_at TEXT NOT NULL DEFAULT (datetime('now')),
			   batch_status TEXT NOT NULL DEFAULT 'RECEIVED',
			   row_count INTEGER NOT NULL DEFAULT 0,
			   accepted_count INTEGER NOT NULL DEFAULT 0,
			   rejected_count INTEGER NOT NULL DEFAULT 0,
			   version_number INTEGER,
			   validation_report_json TEXT,
			   source_reference TEXT
			)""",
			{},
		)
		await execute_query(
			"""CREATE TABLE IF NOT EXISTS portfolio_upload_rows (
			   id INTEGER PRIMARY KEY AUTOINCREMENT,
			   batch_id TEXT NOT NULL,
			   portfolio_id TEXT NOT NULL,
			   row_number INTEGER NOT NULL,
			   source_sheet_name TEXT,
			   stage TEXT NOT NULL,
			   status TEXT NOT NULL,
			   raw_json TEXT,
			   normalized_json TEXT,
			   validated_json TEXT,
			   canonical_json TEXT,
			   error_json TEXT,
			   warning_json TEXT,
			   created_at TEXT NOT NULL DEFAULT (datetime('now')),
			   UNIQUE(batch_id, row_number, stage)
			)""",
			{},
		)
		for column_name, column_type in (
			("canonical_json", "TEXT"),
			("error_json", "TEXT"),
			("warning_json", "TEXT"),
		):
			if not await _table_has_column("portfolio_upload_rows", column_name):
				await execute_query(f"ALTER TABLE portfolio_upload_rows ADD COLUMN {column_name} {column_type}", {})
		await execute_query(
			"""CREATE TABLE IF NOT EXISTS portfolio_upload_versions (
			   id INTEGER PRIMARY KEY AUTOINCREMENT,
			   batch_id TEXT NOT NULL,
			   trust_id TEXT NOT NULL,
			   portfolio_id TEXT NOT NULL,
			   version_number INTEGER NOT NULL,
			   approved_by TEXT,
			   approved_at TEXT NOT NULL DEFAULT (datetime('now')),
			   version_status TEXT NOT NULL DEFAULT 'APPROVED',
			   summary_json TEXT,
			   UNIQUE(trust_id, portfolio_id, version_number),
			   UNIQUE(batch_id)
			)""",
			{},
		)
		await execute_query(
			"""CREATE TABLE IF NOT EXISTS portfolio_validation_reports (
			   id INTEGER PRIMARY KEY AUTOINCREMENT,
			   batch_id TEXT NOT NULL UNIQUE,
			   portfolio_id TEXT NOT NULL,
			   trust_id TEXT NOT NULL,
			   report_json TEXT NOT NULL,
			   report_status TEXT NOT NULL,
			   created_at TEXT NOT NULL DEFAULT (datetime('now')),
			   updated_at TEXT NOT NULL DEFAULT (datetime('now'))
			)""",
			{},
		)

	@classmethod
	async def register_upload_batch(
		cls,
		*,
		content: bytes,
		filename: str,
		content_type: str,
		uploaded_by: str,
		trust_id: str,
		portfolio_id: Optional[str] = None,
		source_reference: Optional[str] = None,
	) -> Dict[str, Any]:
		await cls._ensure_upload_tables()
		batch_id = uuid.uuid4().hex
		file_hash = cls._sha256_bytes(content)
		resolved_portfolio_id = str(portfolio_id or trust_id or "default").strip() or "default"
		canonical_source_reference = cls._canonicalize_upload_reference(source_reference or filename or batch_id)
		await execute_query(
			"""INSERT INTO portfolio_upload_batches
			   (batch_id, trust_id, portfolio_id, file_name, mime_type, file_hash, raw_content,
			    uploaded_by, batch_status, source_reference)
			   VALUES (:batch_id, :trust_id, :portfolio_id, :file_name, :mime_type, :file_hash, :raw_content,
			    :uploaded_by, 'RECEIVED', :source_reference)""",
			{
				"batch_id": batch_id,
				"trust_id": trust_id,
				"portfolio_id": resolved_portfolio_id,
				"file_name": filename or "uploaded_file",
				"mime_type": content_type or None,
				"file_hash": file_hash,
				"raw_content": content,
				"uploaded_by": uploaded_by,
				"source_reference": canonical_source_reference,
			},
		)
		return {
			"batch_id": batch_id,
			"file_hash": file_hash,
			"portfolio_id": resolved_portfolio_id,
			"trust_id": trust_id,
			"source_reference": canonical_source_reference,
		}

	@classmethod
	async def get_upload_batch_report(cls, batch_id: str) -> dict:
		batch = await fetch_one(
			"""SELECT * FROM portfolio_upload_batches WHERE batch_id = :batch_id""",
			{"batch_id": batch_id},
		)
		version = await fetch_one(
			"""SELECT * FROM portfolio_upload_versions WHERE batch_id = :batch_id""",
			{"batch_id": batch_id},
		)
		rows = await fetch_all(
			"""SELECT row_number, source_sheet_name, stage, status, raw_json, normalized_json, validated_json,
			          canonical_json, error_json, warning_json
			   FROM portfolio_upload_rows
			   WHERE batch_id = :batch_id
			   ORDER BY row_number, stage""",
			{"batch_id": batch_id},
		)
		validation = await fetch_one(
			"""SELECT * FROM portfolio_validation_reports WHERE batch_id = :batch_id""",
			{"batch_id": batch_id},
		)
		batch_payload = dict(batch) if batch else None
		if batch_payload is not None:
			raw_content = batch_payload.pop("raw_content", None)
			if isinstance(raw_content, (bytes, bytearray, memoryview)):
				batch_payload["raw_content_bytes"] = len(raw_content)
			elif raw_content not in (None, ""):
				batch_payload["raw_content_bytes"] = len(str(raw_content).encode("utf-8", errors="ignore"))
		return {
			"batch": batch_payload,
			"version": dict(version) if version else None,
			"validation_report": dict(validation) if validation else None,
			"rows": [dict(row) for row in rows],
		}

	@classmethod
	async def parse_upload(
		cls,
		content: bytes,
		*,
		filename: str = "",
		content_type: str = "",
		batch_id: Optional[str] = None,
		strict: bool = False,
		portfolio_id: str = "default",
		trust_id: Optional[str] = None,
	) -> dict:
		"""Parse uploaded content and validate normalized holdings rows."""
		report_trust_id = str(trust_id or portfolio_id or "default").strip() or "default"
		rows = cls._load_rows(content, filename=filename, content_type=content_type) or []
		known_isins: Optional[set[str]] = None
		if strict:
			master_rows = await fetch_all("SELECT isin FROM universal_bond_master", {})
			known_isins = {str(row.get("isin") or "").strip().upper() for row in master_rows if row.get("isin")}
			uploaded_isins = {
				str((raw or {}).get("isin") or (raw or {}).get("ISIN") or "").strip().upper()
				for raw in rows
				if str((raw or {}).get("isin") or (raw or {}).get("ISIN") or "").strip()
			}
			known_isins.update(uploaded_isins)

		accepted: List[dict] = []
		rejected: List[dict] = []
		for index, raw in enumerate(rows, start=1):
			normalized = cls._canonicalize_row(raw)
			if not isinstance(normalized, dict):
				rejected.append(
					{
						"isin": "",
						"_errors": ["row_canonicalization_failed"],
						"_warnings": [],
						"_row_number": int((raw or {}).get("_source_row_number") or (raw or {}).get("source_row_number") or index),
						"_source_sheet_name": str((raw or {}).get("_source_sheet_name") or (raw or {}).get("source_sheet_name") or "").strip(),
					}
				)
				continue
			validation = validate_bond_record(normalized, strict=strict, known_isins=known_isins)

			# Keep normalized values from validation to enforce consistent storage.
			materialized = dict(normalized)
			materialized.update(validation.normalized)
			materialized["_errors"] = list(validation.errors)
			materialized["_warnings"] = list(validation.warnings)
			materialized["_row_number"] = int(raw.get("_source_row_number") or raw.get("source_row_number") or index)
			materialized["_source_sheet_name"] = str(raw.get("_source_sheet_name") or raw.get("source_sheet_name") or "").strip()
			if batch_id:
				await execute_query(
					"""INSERT INTO portfolio_upload_rows
					   (batch_id, portfolio_id, row_number, source_sheet_name, stage, status, raw_json)
					   VALUES (:batch_id, :portfolio_id, :row_number, :source_sheet_name, 'raw', :status, :raw_json)
					   ON CONFLICT(batch_id, row_number, stage) DO UPDATE SET
					    status=excluded.status,
					    raw_json=excluded.raw_json""",
					{
						"batch_id": batch_id,
						"portfolio_id": portfolio_id,
						"row_number": materialized["_row_number"],
						"source_sheet_name": materialized["_source_sheet_name"],
						"status": "RECEIVED",
						"raw_json": json.dumps(raw, default=str),
					},
				)
				await execute_query(
					"""INSERT INTO portfolio_upload_rows
					   (batch_id, portfolio_id, row_number, source_sheet_name, stage, status, raw_json,
					    normalized_json, validated_json, error_json, warning_json)
					   VALUES (:batch_id, :portfolio_id, :row_number, :source_sheet_name, 'normalized', :status, :raw_json,
					    :normalized_json, :validated_json, :error_json, :warning_json)
					   ON CONFLICT(batch_id, row_number, stage) DO UPDATE SET
					    status=excluded.status,
					    raw_json=excluded.raw_json,
					    normalized_json=excluded.normalized_json,
					    validated_json=excluded.validated_json,
					    error_json=excluded.error_json,
					    warning_json=excluded.warning_json""",
					{
						"batch_id": batch_id,
						"portfolio_id": portfolio_id,
						"row_number": materialized["_row_number"],
						"source_sheet_name": materialized["_source_sheet_name"],
						"status": "ACCEPTED" if validation.is_valid and materialized.get("isin") else "REJECTED",
						"raw_json": json.dumps(raw, default=str),
						"normalized_json": json.dumps(normalized, default=str),
						"validated_json": json.dumps(materialized, default=str),
						"error_json": json.dumps(materialized.get("_errors") or [], default=str),
						"warning_json": json.dumps(materialized.get("_warnings") or [], default=str),
					},
				)
				await execute_query(
					"""INSERT INTO portfolio_upload_rows
					   (batch_id, portfolio_id, row_number, source_sheet_name, stage, status, raw_json,
					    normalized_json, validated_json, error_json, warning_json)
					   VALUES (:batch_id, :portfolio_id, :row_number, :source_sheet_name, 'validated', :status, :raw_json,
					    :normalized_json, :validated_json, :error_json, :warning_json)
					   ON CONFLICT(batch_id, row_number, stage) DO UPDATE SET
					    status=excluded.status,
					    raw_json=excluded.raw_json,
					    normalized_json=excluded.normalized_json,
					    validated_json=excluded.validated_json,
					    error_json=excluded.error_json,
					    warning_json=excluded.warning_json""",
					{
						"batch_id": batch_id,
						"portfolio_id": portfolio_id,
						"row_number": materialized["_row_number"],
						"source_sheet_name": materialized["_source_sheet_name"],
						"status": "ACCEPTED" if validation.is_valid and materialized.get("isin") else "REJECTED",
						"raw_json": json.dumps(raw, default=str),
						"normalized_json": json.dumps(normalized, default=str),
						"validated_json": json.dumps(materialized, default=str),
						"error_json": json.dumps(materialized.get("_errors") or [], default=str),
						"warning_json": json.dumps(materialized.get("_warnings") or [], default=str),
					},
				)

			if validation.is_valid and materialized.get("isin"):
				primary_rating = str(materialized.get("credit_rating_1") or materialized.get("credit_rating") or "").strip().upper()
				secondary_rating = str(materialized.get("credit_rating_2") or "").strip().upper()
				primary_agency = str(materialized.get("rating_agency_1") or materialized.get("rating_agency") or "").strip().upper()
				secondary_agency = str(materialized.get("rating_agency_2") or "").strip().upper()
				if primary_rating:
					materialized["credit_rating"] = primary_rating
					materialized["credit_rating_1"] = primary_rating
				if secondary_rating:
					materialized["credit_rating_2"] = secondary_rating
				if primary_agency:
					materialized["rating_agency"] = primary_agency
					materialized["rating_agency_1"] = primary_agency
				if secondary_agency:
					materialized["rating_agency_2"] = secondary_agency
				accepted.append(materialized)
			else:
				rejected.append(materialized)

		summary = {
			"total_rows": len(rows),
			"accepted_rows": len(accepted),
			"rejected_rows": len(rejected),
			"distinct_isin": len({r.get("isin") for r in accepted if r.get("isin")}),
		}
		if batch_id:
			report_trust_id = str(portfolio_id or "default").strip() or "default"
			await execute_query(
				"""UPDATE portfolio_upload_batches
				   SET row_count = :row_count,
				       accepted_count = :accepted_count,
				       rejected_count = :rejected_count,
				       batch_status = :batch_status,
				       validation_report_json = :validation_report_json
				   WHERE batch_id = :batch_id""",
				{
					"row_count": len(rows),
					"accepted_count": len(accepted),
					"rejected_count": len(rejected),
					"batch_status": "VALIDATED" if not rejected else "REJECTED",
					"validation_report_json": json.dumps(
						{
							"summary": summary,
							"accepted": accepted,
							"rejected": rejected,
						},
						default=str,
					),
					"batch_id": batch_id,
				},
			)
			await execute_query(
				"""INSERT INTO portfolio_validation_reports
				   (batch_id, portfolio_id, trust_id, report_json, report_status)
				   VALUES (:batch_id, :portfolio_id, :trust_id, :report_json, :report_status)
				   ON CONFLICT(batch_id) DO UPDATE SET
				    portfolio_id=excluded.portfolio_id,
				    trust_id=excluded.trust_id,
				    report_json=excluded.report_json,
				    report_status=excluded.report_status,
				    updated_at=datetime('now')""",
				{
					"batch_id": batch_id,
					"portfolio_id": portfolio_id,
					"trust_id": report_trust_id,
					"report_json": json.dumps(
						{
							"summary": summary,
							"accepted": accepted,
							"rejected": rejected,
						},
						default=str,
					),
					"report_status": "VALIDATED" if not rejected else "REJECTED",
				},
			)
		return {
			"rows": rows,
			"accepted": accepted,
			"rejected": rejected,
			"summary": summary,
			"batch_id": batch_id,
		}

	@classmethod
	@transactional_session
	async def sync_upload(
		cls,
		parsed: dict,
		*,
		mode: str = "merge",
		uploaded_by: str = "system",
		source_context: Optional[Dict[str, Any]] = None,
	) -> dict:
		"""Persist accepted rows into existing portfolio/master tables."""
		accepted = list(parsed.get("accepted") or [])
		batch_id = str(parsed.get("batch_id") or (source_context or {}).get("batch_id") or uuid.uuid4().hex)
		await ensure_portfolio_core_schema()
		await cls._ensure_upload_tables()
		raw_source_reference = str((source_context or {}).get("source_reference") or parsed.get("source_reference") or parsed.get("filename") or "uploaded_file").strip() or "uploaded_file"
		canonical_source_reference = cls._canonicalize_upload_reference(raw_source_reference)
		provenance = normalize_source_context(
			source_context,
			default_source_name="Client Upload",
			default_source_type="client_provided",
			default_ingestion_method="manual_upload",
			default_source_reference=canonical_source_reference,
			actor=uploaded_by,
		)
		provenance["source_reference"] = canonical_source_reference
		provenance_errors = validate_source_context(provenance)
		if provenance_errors:
			raise ValueError("; ".join(provenance_errors))
		trust_id = str((source_context or {}).get("trust_id") or (source_context or {}).get("portfolio_id") or "default").strip() or "default"
		portfolio_id = trust_id
		version_row = await fetch_one(
			"SELECT COALESCE(MAX(version_number), 0) AS version_number FROM portfolio_upload_versions WHERE trust_id = :trust_id AND portfolio_id = :portfolio_id",
			{"trust_id": trust_id, "portfolio_id": portfolio_id},
		)
		version_number = int((version_row or {}).get("version_number") or 0) + 1
		errors: List[str] = []
		now = _utcnow().isoformat()
		inserted = 0
		unified_market_data_sync_enabled = True
		await execute_query("CREATE INDEX IF NOT EXISTS idx_pp_trust_ref_row ON portfolio_positions(trust_id, source_reference, source_sheet_name, source_row_number)", {})
		await execute_query("CREATE INDEX IF NOT EXISTS idx_pp_trust_isin ON portfolio_positions(trust_id, isin)", {})
		replace_mode = str(mode or "").strip().lower() == "replace"
		if replace_mode and strict_portfolio_versioning_enabled():
			raise ValueError(
				"mode=replace is disabled by STRICT_PORTFOLIO_VERSIONING. "
				"Each upload must create a new version without deleting prior live holdings."
			)
		if replace_mode:
			await execute_query(
				"""
				DELETE FROM portfolio_sheet_allocations
				WHERE isin IN (
					SELECT DISTINCT isin FROM portfolio_positions WHERE trust_id = :trust_id
					UNION
					SELECT DISTINCT isin FROM portfolio_holdings WHERE portfolio_id = :portfolio_id
				)
				""",
				{"trust_id": trust_id, "portfolio_id": portfolio_id},
			)
			await execute_query(
				"DELETE FROM portfolio_positions WHERE trust_id = :trust_id",
				{"trust_id": trust_id},
			)
			await execute_query(
				"DELETE FROM portfolio_holdings WHERE portfolio_id = :portfolio_id",
				{"portfolio_id": portfolio_id},
			)
		else:
			await execute_query(
				"DELETE FROM portfolio_positions WHERE trust_id = :trust_id AND source_reference = :source_reference",
				{"trust_id": trust_id, "source_reference": canonical_source_reference},
			)
			await execute_query(
				"DELETE FROM portfolio_holdings WHERE source_reference = :source_reference",
				{"source_reference": canonical_source_reference},
			)
			await execute_query(
				"DELETE FROM portfolio_sheet_allocations WHERE source_reference = :source_reference",
				{"source_reference": canonical_source_reference},
			)

		for row in accepted:
			isin = (row.get("isin") or "").strip().upper()
			if not isin:
				continue
			try:
				issuer_name = str(row.get("issuer_name") or row.get("bond_name") or isin)
				instrument_name = str(row.get("bond_name") or issuer_name or isin)
				primary_rating_raw = row.get("credit_rating_1") or row.get("credit_rating") or row.get("rating")
				secondary_rating_raw = row.get("credit_rating_2")
				primary_agency = str(row.get("rating_agency_1") or row.get("rating_agency") or "").strip() or None
				secondary_agency = str(row.get("rating_agency_2") or "").strip() or None
				rating = normalize_rating(primary_rating_raw, row.get("category") or row.get("sector"), isin=isin, issuer_name=issuer_name)
				rating_2 = normalize_rating(secondary_rating_raw, row.get("category") or row.get("sector"), isin=isin, issuer_name=issuer_name) if secondary_rating_raw else None
				rating_agency = primary_agency
				issuer_cls = classify_issuer(issuer_name, rating=rating)
				issuer_type = str(
					row.get("issuer_type")
					or issuer_cls.get("issuer_type")
					or cls._infer_issuer_type(
						isin=isin,
						issuer_name=issuer_name,
						sector=row.get("sector"),
						category=row.get("category"),
						bond_name=row.get("bond_name") or issuer_name,
					)
					or "CORPORATE"
				)
				sector = cls._normalize_sector(
					row.get("sector") or issuer_cls.get("sector"),
					issuer_type=issuer_type,
					category=row.get("category"),
					issuer_name=issuer_name,
				)
				instrument_category = str(
					classify_instrument(
						{
							"issuer_type": issuer_type,
							"issuer_name": issuer_name,
							"sector": sector,
							"category": row.get("category"),
							"bond_name": row.get("bond_name") or instrument_name,
						}
					).get("instrument_category")
					or "PRIVATE"
				).upper()

				qty = cls._safe_float(row.get("quantity")) or 0.0
				fv = cls._safe_float(row.get("face_value")) or 1000.0
				purchase_price = cls._safe_float(row.get("purchase_price"))
				current_value = cls._safe_float(row.get("current_value"))
				if (purchase_price is None or purchase_price <= 0) and qty > 0 and current_value and current_value > 0:
					purchase_price = current_value / qty
				coupon_rate = cls._safe_float(row.get("coupon_rate")) or 0.0
				purchase_date = cls._parse_date(row.get("purchase_date"))
				maturity_date = cls._parse_date(row.get("maturity_date"))
				ytm = cls._safe_float(row.get("ytm_at_purchase") or row.get("ytm"))
				if (ytm is None or ytm <= 0) and current_value is not None and current_value > 0 and fv > 0 and coupon_rate > 0:
					maturity_dt = parse_flexible_date(maturity_date) if maturity_date else None
					if maturity_dt is not None:
						years_to_maturity = max(0.0, (maturity_dt - date.today()).days / 365.25)
						if years_to_maturity > 0:
							try:
								ytm = solve_ytm_percent(
									clean_price=float(current_value),
									face_value=float(fv),
									coupon_rate_pct=float(coupon_rate),
									years_to_maturity=years_to_maturity,
									frequency=2,
								)
							except Exception:
								pass
				effective_maturity = maturity_date or cls._parse_date(row.get("call_date")) or cls._parse_date(row.get("put_date")) or "2099-12-31"
				put_date = cls._parse_date(row.get("put_date"))
				call_date = cls._parse_date(row.get("call_date"))
				payment_frequency = cls._infer_payment_frequency(row.get("ip_dates"))
				day_count = "ACT/365"
				row_number = int(row.get("_row_number") or 0)
				source_sheet_name = str(row.get("_source_sheet_name") or "").strip() or None
				row_provenance = dict(provenance)
				row_provenance["source_reference"] = canonical_source_reference
				if raw_source_reference != canonical_source_reference:
					row_provenance["source_reference_alias"] = raw_source_reference
				if row_number:
					row_provenance["source_row_number"] = row_number
				if source_sheet_name:
					row_provenance["source_sheet_name"] = source_sheet_name
				if row_number:
					row_provenance["source_row_key"] = f"{canonical_source_reference}:{source_sheet_name or 'sheet'}:{row_number}"
				cap_category, cap_sector = cls._cap_bucket_from_row(
					issuer_type=issuer_type,
					issuer_name=issuer_name,
					sector=sector,
					category=row.get("category") or issuer_cls.get("sector"),
					bond_name=str(row.get("bond_name") or issuer_name),
				)

				await execute_query(
					"""INSERT INTO instrument_master
					   (isin, instrument_name, issuer, coupon_rate, maturity_date,
					put_date, call_date, sector, category, instrument_category, rating_latest, rating_agency,
						workbook_rating_1, workbook_rating_2, workbook_rating_agency_1, workbook_rating_agency_2,
						payment_frequency, day_count_convention,
						source_name, source_type, ingestion_method, source_reference,
						timestamp_ingested, timestamp_effective, provenance_json)
					   VALUES (:isin, :instrument_name, :issuer, :coupon_rate, :maturity_date,
					:put_date, :call_date, :sector, :category, :instrument_category, :rating_latest, :rating_agency,
						:workbook_rating_1, :workbook_rating_2, :workbook_rating_agency_1, :workbook_rating_agency_2,
						:payment_frequency, :day_count_convention,
						:source_name, :source_type, :ingestion_method, :source_reference,
						:timestamp_ingested, :timestamp_effective, :provenance_json)
					   ON CONFLICT(isin) DO UPDATE SET
						 instrument_name=excluded.instrument_name,
						 issuer=excluded.issuer,
						 coupon_rate=excluded.coupon_rate,
						 maturity_date=COALESCE(excluded.maturity_date, instrument_master.maturity_date),
						 put_date=excluded.put_date,
						 call_date=excluded.call_date,
						 sector=excluded.sector,
						 category=excluded.category,
						 instrument_category=excluded.instrument_category,
						 rating_latest=COALESCE(NULLIF(excluded.rating_latest,''), instrument_master.rating_latest),
						 rating_agency=COALESCE(NULLIF(excluded.rating_agency,''), instrument_master.rating_agency),
						 workbook_rating_1=COALESCE(NULLIF(excluded.workbook_rating_1,''), instrument_master.workbook_rating_1),
						 workbook_rating_2=COALESCE(NULLIF(excluded.workbook_rating_2,''), instrument_master.workbook_rating_2),
						 workbook_rating_agency_1=COALESCE(NULLIF(excluded.workbook_rating_agency_1,''), instrument_master.workbook_rating_agency_1),
						 workbook_rating_agency_2=COALESCE(NULLIF(excluded.workbook_rating_agency_2,''), instrument_master.workbook_rating_agency_2),
						 payment_frequency=excluded.payment_frequency,
						 day_count_convention=excluded.day_count_convention,
						 source_name=excluded.source_name,
						 source_type=excluded.source_type,
						 ingestion_method=excluded.ingestion_method,
						 source_reference=excluded.source_reference,
						 timestamp_ingested=excluded.timestamp_ingested,
						 timestamp_effective=excluded.timestamp_effective,
						 provenance_json=excluded.provenance_json""",
					{
						"isin": isin,
						"instrument_name": instrument_name,
						"issuer": issuer_name,
						"coupon_rate": coupon_rate,
						"maturity_date": effective_maturity,
						"put_date": put_date,
						"call_date": call_date,
						"sector": sector,
						"instrument_category": instrument_category,
						"category": str(row.get("category") or ""),
						"rating_latest": rating,
						"rating_agency": rating_agency,
						"workbook_rating_1": rating,
						"workbook_rating_2": rating_2,
						"workbook_rating_agency_1": primary_agency,
						"workbook_rating_agency_2": secondary_agency,
						"payment_frequency": payment_frequency,
						"day_count_convention": day_count,
						"source_name": provenance["source_name"],
						"source_type": provenance["source_type"],
						"ingestion_method": provenance["ingestion_method"],
						"source_reference": provenance["source_reference"],
						"timestamp_ingested": provenance["timestamp_ingested"],
						"timestamp_effective": provenance["timestamp_effective"],
						"provenance_json": json.dumps(provenance, default=str),
					},
				)

				await execute_query(
					"""INSERT INTO universal_bond_master
					   (isin, bond_name, issuer_name, issuer_type, coupon_rate,
						maturity_date, issue_date, face_value, credit_rating,
					sector, instrument_category, is_pf_eligible, is_active,
						source_name, source_type, ingestion_method, source_reference,
						timestamp_ingested, timestamp_effective, provenance_json, updated_at)
					   VALUES (:isin, :bn, :issuer, :itype, :coupon,
						   :maturity, :issue_date, :fv, :rating,
					   :sector, :instrument_category, :pf, 1,
						   :source_name, :source_type, :ingestion_method, :source_reference,
						   :timestamp_ingested, :timestamp_effective, :provenance_json, :updated)
					   ON CONFLICT(isin) DO UPDATE SET
						 bond_name=excluded.bond_name,
						 issuer_name=excluded.issuer_name,
						 issuer_type=excluded.issuer_type,
						 coupon_rate=excluded.coupon_rate,
						 maturity_date=COALESCE(excluded.maturity_date, universal_bond_master.maturity_date),
						 issue_date=COALESCE(excluded.issue_date, universal_bond_master.issue_date),
						 face_value=COALESCE(excluded.face_value, universal_bond_master.face_value),
						 credit_rating=COALESCE(NULLIF(excluded.credit_rating, ''), universal_bond_master.credit_rating),
						 sector=COALESCE(NULLIF(excluded.sector, ''), universal_bond_master.sector),
						 instrument_category=excluded.instrument_category,
						 is_pf_eligible=excluded.is_pf_eligible,
						 is_active=1,
						 source_name=excluded.source_name,
						 source_type=excluded.source_type,
						 ingestion_method=excluded.ingestion_method,
						 source_reference=excluded.source_reference,
						 timestamp_ingested=excluded.timestamp_ingested,
						 timestamp_effective=excluded.timestamp_effective,
						 provenance_json=excluded.provenance_json,
						 updated_at=excluded.updated_at""",
					{
						"isin": isin,
						"bn": str(row.get("bond_name") or ""),
						"issuer": issuer_name,
						"itype": issuer_type,
						"coupon": coupon_rate,
						"maturity": effective_maturity,
						"issue_date": purchase_date,
						"fv": fv,
						"rating": rating,
						"sector": sector,
						"instrument_category": instrument_category,
						"pf": cls._infer_pf_eligible(issuer_type, rating),
						"source_name": provenance["source_name"],
						"source_type": provenance["source_type"],
						"ingestion_method": provenance["ingestion_method"],
						"source_reference": provenance["source_reference"],
						"timestamp_ingested": provenance["timestamp_ingested"],
						"timestamp_effective": provenance["timestamp_effective"],
						"provenance_json": json.dumps(provenance, default=str),
						"updated": now,
					},
				)

				book_value = current_value if current_value is not None and current_value > 0 else cls._derive_basis_value(
					face_value=fv,
					quantity=qty,
					purchase_price=purchase_price,
					current_value=current_value,
				)
				market_price = (book_value / qty) if qty > 0 and book_value > 0 else (purchase_price or 0.0)
				clean_price = market_price
				accrued_interest = cls._safe_float(row.get("accrued_interest")) or 0.0
				unique_lot_id = str(
					row_provenance.get("source_row_key")
					or f"{canonical_source_reference}:{source_sheet_name or 'sheet'}:{row_number or uuid.uuid4().hex}"
				)
				await execute_query(
					"""INSERT INTO portfolio_positions
					   (unique_lot_id, trust_id, portfolio_id, isin, category, instrument_category, instrument_name, coupon, maturity,
					    quantity, face_value, book_value, current_value, market_price, clean_price, ytm, accrued_interest, purchase_price, purchase_date,
					    issuer_name, credit_rating, credit_rating_2, rating_agency, rating_agency_2, sector,
					    source_name, source_type, ingestion_method, source_reference,
					    source_sheet_name, source_row_number,
					    timestamp_ingested, timestamp_effective, provenance_json)
					   VALUES (:unique_lot_id, :trust_id, :portfolio_id, :isin, :category, :instrument_category, :instrument_name, :coupon, :maturity,
					    :quantity, :face_value, :book_value, :current_value, :market_price, :clean_price, :ytm, :accrued_interest, :purchase_price, :purchase_date,
					    :issuer_name, :credit_rating, :credit_rating_2, :rating_agency, :rating_agency_2, :sector,
					    :source_name, :source_type, :ingestion_method, :source_reference,
					    :source_sheet_name, :source_row_number,
					    :timestamp_ingested, :timestamp_effective, :provenance_json)""",
					{
						"unique_lot_id": unique_lot_id,
						"trust_id": trust_id,
						"portfolio_id": portfolio_id,
						"isin": isin,
						"category": str(row.get("category") or ""),
						"instrument_category": instrument_category,
						"instrument_name": instrument_name,
						"coupon": coupon_rate,
						"maturity": maturity_date,
						"quantity": qty,
						"face_value": fv,
						"book_value": book_value,
						"current_value": current_value or 0.0,
						"market_price": market_price,
						"clean_price": clean_price,
						"ytm": ytm,
						"accrued_interest": accrued_interest,
						"purchase_price": purchase_price or 0.0,
						"purchase_date": purchase_date,
						"issuer_name": issuer_name,
						"credit_rating": rating,
						"credit_rating_2": rating_2,
						"rating_agency": primary_agency,
						"rating_agency_2": secondary_agency,
						"sector": sector,
						"source_name": provenance["source_name"],
						"source_type": provenance["source_type"],
						"ingestion_method": provenance["ingestion_method"],
						"source_reference": provenance["source_reference"],
						"source_sheet_name": source_sheet_name,
						"source_row_number": row_number or None,
						"timestamp_ingested": provenance["timestamp_ingested"],
						"timestamp_effective": provenance["timestamp_effective"],
						"provenance_json": json.dumps(row_provenance, default=str),
					},
				)

				portfolio_holding_existing = await fetch_one(
					"SELECT id, quantity, purchase_price, purchase_date, face_value FROM portfolio_holdings WHERE portfolio_id = :portfolio_id AND isin = :isin",
					{"portfolio_id": portfolio_id, "isin": isin},
				)
				if portfolio_holding_existing:
					existing_qty = cls._safe_float(portfolio_holding_existing.get("quantity")) or 0.0
					existing_purchase_price = cls._safe_float(portfolio_holding_existing.get("purchase_price")) or 0.0
					existing_face_value = cls._safe_float(portfolio_holding_existing.get("face_value")) or 0.0
					existing_purchase_date = cls._parse_date(portfolio_holding_existing.get("purchase_date"))
					aggregated_quantity = existing_qty + qty
					weighted_purchase_price = existing_purchase_price
					if aggregated_quantity > 0:
						weighted_purchase_price = ((existing_purchase_price * existing_qty) + ((purchase_price or 0.0) * qty)) / aggregated_quantity
					aggregated_face_value = existing_face_value + fv
					aggregated_purchase_date = existing_purchase_date or purchase_date
					if existing_purchase_date and purchase_date:
						aggregated_purchase_date = min(existing_purchase_date, purchase_date)
					await execute_query(
						"""UPDATE portfolio_holdings
						   SET purchase_date = :purchase_date,
						       quantity = :quantity,
						       purchase_price = :purchase_price,
						       face_value = :face_value,
						       instrument_category = :instrument_category,
						       credit_rating = :credit_rating,
						       credit_rating_2 = :credit_rating_2,
						       rating_agency = :rating_agency,
						       rating_agency_2 = :rating_agency_2,
						       source_name = :source_name,
						       source_type = :source_type,
						       ingestion_method = :ingestion_method,
						       source_reference = :source_reference,
						       source_sheet_name = :source_sheet_name,
						       source_row_number = :source_row_number,
						       timestamp_ingested = :timestamp_ingested,
						       timestamp_effective = :timestamp_effective,
						       provenance_json = :provenance_json
						   WHERE portfolio_id = :portfolio_id AND isin = :isin""",
						{
							"portfolio_id": portfolio_id,
							"isin": isin,
							"purchase_date": aggregated_purchase_date,
							"quantity": aggregated_quantity,
							"purchase_price": weighted_purchase_price,
							"face_value": aggregated_face_value,
							"instrument_category": instrument_category,
							"credit_rating": rating,
							"credit_rating_2": rating_2,
							"rating_agency": primary_agency,
							"rating_agency_2": secondary_agency,
							"source_name": provenance["source_name"],
							"source_type": provenance["source_type"],
							"ingestion_method": provenance["ingestion_method"],
							"source_reference": provenance["source_reference"],
							"source_sheet_name": source_sheet_name,
							"source_row_number": row_number or None,
							"timestamp_ingested": provenance["timestamp_ingested"],
							"timestamp_effective": provenance["timestamp_effective"],
							"provenance_json": json.dumps(row_provenance, default=str),
						},
					)
				else:
					await execute_query(
						"""INSERT INTO portfolio_holdings
						   (portfolio_id, isin, purchase_date, quantity, purchase_price, face_value,
						    instrument_category, credit_rating, credit_rating_2, rating_agency, rating_agency_2,
						    source_name, source_type, ingestion_method, source_reference,
						    source_sheet_name, source_row_number,
						    timestamp_ingested, timestamp_effective, provenance_json)
						   VALUES (:portfolio_id, :isin, :purchase_date, :quantity, :purchase_price, :face_value,
						    :instrument_category, :credit_rating, :credit_rating_2, :rating_agency, :rating_agency_2,
						    :source_name, :source_type, :ingestion_method, :source_reference,
						    :source_sheet_name, :source_row_number,
						    :timestamp_ingested, :timestamp_effective, :provenance_json)""",
						{
							"portfolio_id": portfolio_id,
							"isin": isin,
							"purchase_date": purchase_date,
							"quantity": qty,
							"purchase_price": purchase_price or 0.0,
							"face_value": fv,
							"instrument_category": instrument_category,
							"credit_rating": rating,
							"credit_rating_2": rating_2,
							"rating_agency": primary_agency,
							"rating_agency_2": secondary_agency,
							"source_name": provenance["source_name"],
							"source_type": provenance["source_type"],
							"ingestion_method": provenance["ingestion_method"],
							"source_reference": provenance["source_reference"],
							"source_sheet_name": source_sheet_name,
							"source_row_number": row_number or None,
							"timestamp_ingested": provenance["timestamp_ingested"],
							"timestamp_effective": provenance["timestamp_effective"],
							"provenance_json": json.dumps(row_provenance, default=str),
						},
					)

				basis_value = cls._derive_basis_value(
					face_value=fv,
					quantity=qty,
					purchase_price=purchase_price,
					current_value=current_value,
				)
				allocation_existing = await fetch_one(
					"""SELECT basis_value
					   FROM portfolio_sheet_allocations
					   WHERE isin = :isin AND category_bucket = :category_bucket AND sector_bucket = :sector_bucket""",
					{
						"isin": isin,
						"category_bucket": cap_category,
						"sector_bucket": cap_sector,
					},
				)
				if allocation_existing:
					await execute_query(
						"""UPDATE portfolio_sheet_allocations
						   SET basis_value = COALESCE(basis_value, 0) + :basis_value,
						       source_reference = :source_reference,
						       source_sheet_name = COALESCE(:source_sheet_name, source_sheet_name),
						       source_row_number = COALESCE(:source_row_number, source_row_number)
						   WHERE isin = :isin AND category_bucket = :category_bucket AND sector_bucket = :sector_bucket""",
						{
							"isin": isin,
							"category_bucket": cap_category,
							"sector_bucket": cap_sector,
							"basis_value": basis_value,
							"source_reference": provenance["source_reference"],
							"source_sheet_name": source_sheet_name,
							"source_row_number": row_number or None,
						},
					)
				else:
					await execute_query(
						"""INSERT INTO portfolio_sheet_allocations
						   (isin, category_bucket, sector_bucket, basis_value, source_reference, source_sheet_name, source_row_number)
						   VALUES (:isin, :category_bucket, :sector_bucket, :basis_value, :source_reference, :source_sheet_name, :source_row_number)""",
						{
							"isin": isin,
							"category_bucket": cap_category,
							"sector_bucket": cap_sector,
							"basis_value": basis_value,
							"source_reference": provenance["source_reference"],
							"source_sheet_name": source_sheet_name,
							"source_row_number": row_number or None,
						}
					)

				if market_price is not None and market_price > 0:
					await execute_query(
						"""INSERT INTO market_prices
						   (isin, date, price, yield, source,
						    source_name, source_type, ingestion_method, source_reference,
						    timestamp_ingested, timestamp_effective, provenance_json)
						   VALUES (:isin, :date, :price, :yield, :source,
						    :source_name, :source_type, :ingestion_method, :source_reference,
						    :timestamp_ingested, :timestamp_effective, :provenance_json)
						   ON CONFLICT(isin, date, source) DO UPDATE SET
						     price=excluded.price,
						     yield=excluded.yield,
						     source_name=excluded.source_name,
						     source_type=excluded.source_type,
						     ingestion_method=excluded.ingestion_method,
						     source_reference=excluded.source_reference,
						     timestamp_ingested=excluded.timestamp_ingested,
						     timestamp_effective=excluded.timestamp_effective,
						     provenance_json=excluded.provenance_json""",
						{
							"isin": isin,
							"date": date.today().isoformat(),
							"price": market_price,
							"yield": ytm,
							"source": "PORTFOLIO_UPLOAD",
							"source_name": provenance["source_name"],
							"source_type": provenance["source_type"],
							"ingestion_method": provenance["ingestion_method"],
							"source_reference": provenance["source_reference"],
							"timestamp_ingested": provenance["timestamp_ingested"],
							"timestamp_effective": provenance["timestamp_effective"],
							"provenance_json": json.dumps(provenance, default=str),
						},
					)
					if unified_market_data_sync_enabled:
						try:
							await execute_query(
								"""INSERT INTO unified_market_data
								   (isin, price_date, clean_price, ytm, data_source,
								    source_name, source_type, ingestion_method, source_reference,
								    source_sheet_name, source_row_number,
								    timestamp_ingested, timestamp_effective, provenance_json, last_updated)
								   VALUES (:isin, :price_date, :clean_price, :ytm, :data_source,
								    :source_name, :source_type, :ingestion_method, :source_reference,
								    :source_sheet_name, :source_row_number,
								    :timestamp_ingested, :timestamp_effective, :provenance_json, :last_updated)
								   ON CONFLICT(isin, price_date) DO UPDATE SET
								     clean_price = COALESCE(excluded.clean_price, unified_market_data.clean_price),
								     ytm = COALESCE(excluded.ytm, unified_market_data.ytm),
								     source_name = excluded.source_name,
								     source_type = excluded.source_type,
								     ingestion_method = excluded.ingestion_method,
								     source_reference = excluded.source_reference,
								     source_sheet_name = COALESCE(excluded.source_sheet_name, unified_market_data.source_sheet_name),
								     source_row_number = COALESCE(excluded.source_row_number, unified_market_data.source_row_number),
								     timestamp_ingested = excluded.timestamp_ingested,
								     timestamp_effective = excluded.timestamp_effective,
								     provenance_json = excluded.provenance_json,
								     last_updated = excluded.last_updated""",
								{
									"isin": isin,
									"price_date": date.today().isoformat(),
									"clean_price": market_price,
									"ytm": ytm,
									"data_source": "PORTFOLIO_UPLOAD",
									"source_name": provenance["source_name"],
									"source_type": provenance["source_type"],
									"ingestion_method": provenance["ingestion_method"],
									"source_reference": provenance["source_reference"],
									"source_sheet_name": source_sheet_name,
									"source_row_number": row_number or None,
									"timestamp_ingested": provenance["timestamp_ingested"],
									"timestamp_effective": provenance["timestamp_effective"],
									"provenance_json": json.dumps(row_provenance, default=str),
									"last_updated": now,
								},
							)
						except Exception as market_sync_exc:
							unified_market_data_sync_enabled = False
							log.warning(
								"portfolio_upload_unified_market_sync_disabled",
								error=str(market_sync_exc),
								batch_id=batch_id,
								source_reference=canonical_source_reference,
							)

				await cls._upsert_cashflow_schedule(
					isin=isin,
					coupon_rate=coupon_rate,
					maturity_date=maturity_date,
					put_date=put_date,
					call_date=call_date,
					payment_frequency=payment_frequency,
					source_context=provenance,
				)

				try:
					await record_provenance(
						entity_type="PORTFOLIO_POSITION",
						entity_id=isin,
						payload={k: row.get(k) for k in row.keys()},
						source_context=provenance,
						field_name=None,
						actor=uploaded_by,
					)
				except Exception:
					pass
				try:
					await execute_query(
						"""UPDATE portfolio_upload_rows
						   SET canonical_json = :canonical_json,
						       stage = 'canonical',
						       status = 'ACCEPTED'
						   WHERE batch_id = :batch_id AND row_number = :row_number AND stage = 'validated'""",
						{
							"canonical_json": json.dumps(
								{
									**row,
									"isin": isin,
									"portfolio_id": portfolio_id,
									"trust_id": trust_id,
									"batch_id": batch_id,
									"credit_rating": rating,
									"credit_rating_2": rating_2,
									"rating_agency": primary_agency,
									"rating_agency_2": secondary_agency,
								},
								default=str,
							),
							"batch_id": batch_id,
							"row_number": row_number or None,
						},
					)
				except Exception:
					pass

				inserted += 1
			except Exception as exc:
				errors.append(f"{isin}: {exc}")

		try:
			validation_row = await fetch_one(
				"""
				WITH latest_market AS (
					SELECT umd.isin, umd.ytm, umd.modified_duration, umd.spread_over_gsec
					FROM unified_market_data umd
					INNER JOIN (
						SELECT isin, MAX(price_date) AS max_price_date
						FROM unified_market_data
						GROUP BY isin
					) latest ON latest.isin = umd.isin AND latest.max_price_date = umd.price_date
				)
				SELECT
					COUNT(*) AS holdings_count,
					COALESCE(SUM(CASE WHEN COALESCE(p.market_value, 0) <= 0 THEN 1 ELSE 0 END), 0) AS missing_market_value,
					COALESCE(SUM(CASE WHEN lm.ytm IS NULL THEN 1 ELSE 0 END), 0) AS missing_ytm,
					COALESCE(SUM(CASE WHEN lm.modified_duration IS NULL THEN 1 ELSE 0 END), 0) AS missing_modified_duration,
					COALESCE(SUM(CASE WHEN lm.spread_over_gsec IS NULL THEN 1 ELSE 0 END), 0) AS missing_spread_bps
				FROM portfolio_current_positions p
				LEFT JOIN latest_market lm ON lm.isin = p.isin
				WHERE COALESCE(p.quantity, 0) > 0
				""",
				{},
			)
			log.info(
				"portfolio_upload_validation_summary",
				batch_id=batch_id,
				trust_id=trust_id,
				source_reference=canonical_source_reference,
				accepted_rows=len(accepted),
				synced_rows=inserted,
				error_count=len(errors),
				holdings_count=int((validation_row or {}).get("holdings_count") or 0),
				missing_market_value=int((validation_row or {}).get("missing_market_value") or 0),
				missing_ytm=int((validation_row or {}).get("missing_ytm") or 0),
				missing_modified_duration=int((validation_row or {}).get("missing_modified_duration") or 0),
				missing_spread_bps=int((validation_row or {}).get("missing_spread_bps") or 0),
			)
		except Exception as validation_exc:
			log.warning(
				"portfolio_upload_validation_summary_failed",
				error=str(validation_exc),
				batch_id=batch_id,
				trust_id=trust_id,
				source_reference=canonical_source_reference,
			)

		invalidate_analytics_cache()
		try:
			await execute_query(
				"""INSERT INTO portfolio_upload_versions
				   (batch_id, trust_id, portfolio_id, version_number, approved_by, version_status, summary_json)
				   VALUES (:batch_id, :trust_id, :portfolio_id, :version_number, :approved_by, 'APPROVED', :summary_json)
				   ON CONFLICT(batch_id) DO UPDATE SET
				    trust_id=excluded.trust_id,
				    portfolio_id=excluded.portfolio_id,
				    version_number=excluded.version_number,
				    approved_by=excluded.approved_by,
				    version_status=excluded.version_status,
				    summary_json=excluded.summary_json""",
				{
					"batch_id": batch_id,
					"trust_id": trust_id,
					"portfolio_id": portfolio_id,
					"version_number": version_number,
					"approved_by": uploaded_by,
					"summary_json": json.dumps(
						{
							"summary": parsed.get("summary") or {},
							"synced_rows": inserted,
							"errors": errors,
						},
						default=str,
					),
				},
			)
			version_status = "COMMITTED" if inserted and not errors else ("PARTIAL" if inserted else "REJECTED")
			await execute_query(
				"""UPDATE portfolio_upload_batches
				   SET batch_status = :batch_status,
				       row_count = :row_count,
				       accepted_count = :accepted_count,
				       rejected_count = :rejected_count,
				       version_number = :version_number,
				       validation_report_json = :validation_report_json
				   WHERE batch_id = :batch_id""",
				{
					"batch_status": "COMMITTED" if inserted and not errors else ("PARTIAL" if inserted else "REJECTED"),
					"row_count": int(parsed.get("summary", {}).get("total_rows", len(accepted)) or len(accepted)),
					"accepted_count": len(accepted),
					"rejected_count": int(parsed.get("summary", {}).get("rejected_rows", 0) or 0),
					"version_number": version_number,
					"validation_report_json": json.dumps(parsed, default=str),
					"batch_id": batch_id,
				},
			)
			await execute_query(
				"""INSERT INTO portfolio_validation_reports
				   (batch_id, portfolio_id, trust_id, report_json, report_status)
				   VALUES (:batch_id, :portfolio_id, :trust_id, :report_json, :report_status)
				   ON CONFLICT(batch_id) DO UPDATE SET
				    portfolio_id=excluded.portfolio_id,
				    trust_id=excluded.trust_id,
				    report_json=excluded.report_json,
				    report_status=excluded.report_status,
				    updated_at=datetime('now')""",
				{
					"batch_id": batch_id,
					"portfolio_id": portfolio_id,
					"trust_id": trust_id,
					"report_json": json.dumps(
						{
							"summary": parsed.get("summary") or {},
							"synced_rows": inserted,
							"errors": errors,
							"accepted_rows": len(accepted),
						},
						default=str,
					),
					"report_status": version_status,
				},
			)
			await execute_query(
				"""UPDATE portfolio_upload_versions
				   SET version_status = :version_status
				   WHERE batch_id = :batch_id""",
				{"version_status": version_status, "batch_id": batch_id},
			)
		except Exception:
			pass

		return {
			"mode": mode,
			"accepted_rows": len(accepted),
			"synced_rows": inserted,
			"errors": errors[:100],
			"updated_at": now,
			"batch_id": batch_id,
			"version_number": version_number,
		}

	@classmethod
	async def _upsert_cashflow_schedule(
		cls,
		*,
		isin: str,
		coupon_rate: float,
		maturity_date: Optional[str],
		put_date: Optional[str],
		call_date: Optional[str],
		payment_frequency: int,
		source_context: Optional[Dict[str, Any]] = None,
	) -> None:
		provenance = normalize_source_context(
			source_context,
			default_source_name="Client Upload",
			default_source_type="client_provided",
			default_ingestion_method="manual_upload",
			default_source_reference=f"{isin}:cashflows",
			actor="system",
		)
		mat = cls._parse_date(maturity_date) if maturity_date else None
		if not mat:
			return
		mat_d = datetime.strptime(mat, "%Y-%m-%d").date()
		end_d = mat_d
		if put_date:
			pd = cls._parse_date(put_date)
			if pd:
				end_d = min(end_d, datetime.strptime(pd, "%Y-%m-%d").date())
		if call_date:
			cd = cls._parse_date(call_date)
			if cd:
				end_d = min(end_d, datetime.strptime(cd, "%Y-%m-%d").date())

		freq = max(1, int(payment_frequency or 2))
		months = max(1, int(12 / freq))
		coupon_amt = round((coupon_rate / 100.0) * (100.0 / freq), 6)
		provenance_json = json.dumps(provenance, default=str)

		await execute_query("DELETE FROM cashflows WHERE isin = :isin", {"isin": isin})
		cursor = end_d
		schedule: list[date] = []
		while cursor > date.today() - timedelta(days=365 * 30):
			schedule.append(cursor)
			year = cursor.year
			month = cursor.month - months
			while month <= 0:
				month += 12
				year -= 1
			day = min(cursor.day, 28)
			cursor = date(year, month, day)
			if len(schedule) > 400:
				break

		schedule_rows = [
			{
				"isin": isin,
				"payment_date": d.isoformat(),
				"coupon_amount": coupon_amt,
				"principal_amount": 100.0 if d == end_d else 0.0,
				"source_name": provenance["source_name"],
				"source_type": provenance["source_type"],
				"ingestion_method": provenance["ingestion_method"],
				"source_reference": provenance["source_reference"],
				"timestamp_ingested": provenance["timestamp_ingested"],
				"timestamp_effective": provenance["timestamp_effective"],
				"provenance_json": provenance_json,
			}
			for d in sorted(schedule)
		]
		if schedule_rows:
			await execute_query(
				"""INSERT INTO cashflows (isin, payment_date, coupon_amount, principal_amount,
				   source_name, source_type, ingestion_method, source_reference,
				   timestamp_ingested, timestamp_effective, provenance_json)
				   VALUES (:isin, :payment_date, :coupon_amount, :principal_amount,
				           :source_name, :source_type, :ingestion_method, :source_reference,
				           :timestamp_ingested, :timestamp_effective, :provenance_json)
				   ON CONFLICT(isin, payment_date) DO UPDATE SET
				     coupon_amount=excluded.coupon_amount,
				     principal_amount=excluded.principal_amount,
				     source_name=excluded.source_name,
				     source_type=excluded.source_type,
				     ingestion_method=excluded.ingestion_method,
				     source_reference=excluded.source_reference,
				     timestamp_ingested=excluded.timestamp_ingested,
				     timestamp_effective=excluded.timestamp_effective,
				     provenance_json=excluded.provenance_json""",
				schedule_rows,
			)

	@classmethod
	def _infer_payment_frequency(cls, ip_dates: Any) -> int:
		if ip_dates in (None, ""):
			return 2
		txt = str(ip_dates)
		# Heuristic: two date tokens usually mean semi-annual stream in this dataset.
		if txt.count("/") >= 2 or txt.count("-") >= 2:
			return 2
		return 1

	@classmethod
	async def build_regulatory_audit(cls) -> dict:
		"""Build a consolidated data-quality and allocation audit."""
		holdings = await fetch_all(
			"""SELECT p.*, b.issuer_type, b.bond_name, b.credit_rating AS master_rating,
					  b.sector AS master_sector,
					  (SELECT MAX(price_date) FROM unified_market_data m WHERE m.isin = p.isin) AS latest_market_update,
					  (SELECT m2.ytm FROM unified_market_data m2
						WHERE m2.isin = p.isin
						ORDER BY m2.price_date DESC, m2.id DESC
						LIMIT 1) AS latest_market_ytm
			   FROM portfolio_current_positions p
			   LEFT JOIN universal_bond_master b ON b.isin = p.isin
			   WHERE COALESCE(p.quantity, 0) > 0"""
		)

		total_value = 0.0
		bucket_values = {
			"GOVERNMENT_SECURITIES": 0.0,
			"DEBT_SECURITIES": 0.0,
			"EQUITY_RELATED": 0.0,
		}
		government_breakout = {
			"GSEC": 0.0,
			"SDL": 0.0,
			"TBILL": 0.0,
			"OTHER_GOV": 0.0,
		}
		issuer_values: Dict[str, float] = {}
		data_quality_issues: List[dict] = []
		quote_gaps: List[dict] = []
		yield_coverage_issues: List[dict] = []
		maturity_issues: List[dict] = []
		quarantine_recommendations: List[dict] = []
		classification_consistency_issues: List[dict] = []
		today = date.today()
		gov_issuer_types = {"G-SEC", "SDL", "T-BILL"}
		gov_sectors = {"CENTRAL_GOVERNMENT", "STATE_GOVERNMENT", "GOVERNMENT_SECURITIES", "G-SEC", "SDL", "SOVEREIGN", "GOVERNMENT"}

		for row in holdings:
			item = dict(row)
			isin = item.get("isin")
			issuer_name = item.get("issuer_name") or item.get("bond_name") or isin
			issuer_type = (item.get("issuer_type") or "").upper() or cls._infer_issuer_type(
				isin=isin,
				issuer_name=issuer_name,
				sector=item.get("master_sector") or item.get("sector"),
				category=item.get("category"),
				bond_name=item.get("bond_name") or issuer_name,
			)
			rating = normalize_rating(
				item.get("master_rating") or item.get("rating"),
				issuer_type=issuer_type,
				isin=isin,
				issuer_name=issuer_name,
			)
			bucket = cls._infer_allocation_bucket(
				issuer_type=issuer_type,
				issuer_name=issuer_name,
				category=item.get("category"),
				sector=item.get("master_sector") or item.get("sector"),
				bond_name=item.get("bond_name") or issuer_name,
			)
			value = float(item.get("market_value") or item.get("book_value") or item.get("face_value") or 0.0)
			total_value += value
			bucket_values[bucket] += value
			issuer_values[issuer_name] = issuer_values.get(issuer_name, 0.0) + value
			if bucket == "GOVERNMENT_SECURITIES":
				it = (issuer_type or "").upper()
				if it == "G-SEC":
					government_breakout["GSEC"] += value
				elif it == "SDL":
					government_breakout["SDL"] += value
				elif it == "T-BILL":
					government_breakout["TBILL"] += value
				else:
					government_breakout["OTHER_GOV"] += value

			maturity_date = cls._parse_date(item.get("maturity_date"))
			if not maturity_date and bucket != "EQUITY_RELATED":
				data_quality_issues.append({"isin": isin, "issuer_name": issuer_name, "issue": "missing_maturity_date"})
				maturity_issues.append(
					{
						"isin": isin,
						"issuer_name": issuer_name,
						"maturity_date": item.get("maturity_date"),
						"issue": "missing_or_invalid_maturity_date",
						"action_tier": "CRITICAL",
						"fix_hint": "Set valid maturity date in YYYY-MM-DD (or supported DD/MM/YYYY) and re-run upload sync.",
					}
				)
				quarantine_recommendations.append(
					{
						"isin": isin,
						"reason": "missing_or_invalid_maturity_date",
						"action_tier": "CRITICAL",
						"recommended_action": "AUTO_QUARANTINE_RECOMMENDED",
					}
				)
			elif maturity_date and bucket != "EQUITY_RELATED":
				try:
					mdate = date.fromisoformat(maturity_date)
					if mdate < today:
						maturity_issues.append(
							{
								"isin": isin,
								"issuer_name": issuer_name,
								"maturity_date": maturity_date,
								"issue": "active_holding_past_maturity",
								"action_tier": "HIGH",
								"fix_hint": "Verify redemption/rollover booking; if redeemed, close position, else correct maturity metadata.",
							}
						)
						quarantine_recommendations.append(
							{
								"isin": isin,
								"reason": "active_holding_past_maturity",
								"action_tier": "HIGH",
								"recommended_action": "REVIEW_AND_QUARANTINE_IF_UNCONFIRMED",
							}
						)
				except Exception:
					maturity_issues.append(
						{
							"isin": isin,
							"issuer_name": issuer_name,
							"maturity_date": maturity_date,
							"issue": "maturity_date_parse_failure",
							"action_tier": "CRITICAL",
							"fix_hint": "Correct maturity date format and re-upload this ISIN.",
						}
					)
					quarantine_recommendations.append(
						{
							"isin": isin,
							"reason": "maturity_date_parse_failure",
							"action_tier": "CRITICAL",
							"recommended_action": "AUTO_QUARANTINE_RECOMMENDED",
						}
					)
			if not rating and bucket != "EQUITY_RELATED":
				data_quality_issues.append({"isin": isin, "issuer_name": issuer_name, "issue": "missing_rating"})
			if value <= 0:
				data_quality_issues.append({"isin": isin, "issuer_name": issuer_name, "issue": "non_positive_position_value"})

			latest_market = cls._parse_date(item.get("latest_market_update"))
			latest_ytm = cls._safe_float(item.get("latest_market_ytm"))
			if not latest_market:
				quote_gaps.append({"isin": isin, "issuer_name": issuer_name, "latest_market_update": None})
			else:
				try:
					if (today - date.fromisoformat(latest_market)).days > 7:
						quote_gaps.append({"isin": isin, "issuer_name": issuer_name, "latest_market_update": latest_market})
				except Exception:
					quote_gaps.append({"isin": isin, "issuer_name": issuer_name, "latest_market_update": latest_market})

			# Hard yield coverage checks with action tiers (for debt/government holdings).
			if bucket != "EQUITY_RELATED":
				yield_issue = None
				if latest_ytm is None:
					yield_issue = {
						"isin": isin,
						"issuer_name": issuer_name,
						"latest_ytm": None,
						"latest_market_update": latest_market,
						"issue": "missing_ytm",
						"action_tier": "CRITICAL",
						"required_action": "Backfill YTM from approved source (NSE/BSE/FIMMDA) before portfolio analytics sign-off.",
					}
				elif latest_ytm < 0.1 or latest_ytm > 25.0:
					yield_issue = {
						"isin": isin,
						"issuer_name": issuer_name,
						"latest_ytm": latest_ytm,
						"latest_market_update": latest_market,
						"issue": "invalid_ytm_range",
						"action_tier": "HIGH",
						"required_action": "Correct YTM to valid range [0.1, 25.0] and verify market data source mapping.",
					}
				elif latest_market:
					try:
						age_days = (today - date.fromisoformat(latest_market)).days
						if age_days > 30:
							yield_issue = {
								"isin": isin,
								"issuer_name": issuer_name,
								"latest_ytm": latest_ytm,
								"latest_market_update": latest_market,
								"issue": "stale_ytm_gt_30_days",
								"action_tier": "HIGH",
								"required_action": "Refresh market quote and YTM; hold recommendation engine usage for this ISIN until refreshed.",
							}
						elif age_days > 7:
							yield_issue = {
								"isin": isin,
								"issuer_name": issuer_name,
								"latest_ytm": latest_ytm,
								"latest_market_update": latest_market,
								"issue": "stale_ytm_gt_7_days",
								"action_tier": "MEDIUM",
								"required_action": "Refresh YTM within next cycle; mark as stale in monitoring.",
							}
					except Exception:
						yield_issue = {
							"isin": isin,
							"issuer_name": issuer_name,
							"latest_ytm": latest_ytm,
							"latest_market_update": latest_market,
							"issue": "invalid_market_update_date",
							"action_tier": "HIGH",
							"required_action": "Fix market update date format and re-publish quote snapshot.",
						}
				else:
					yield_issue = {
						"isin": isin,
						"issuer_name": issuer_name,
						"latest_ytm": latest_ytm,
						"latest_market_update": None,
						"issue": "missing_market_date",
						"action_tier": "HIGH",
						"required_action": "Populate market date along with YTM from trusted quote feed.",
					}

				if yield_issue:
					yield_coverage_issues.append(yield_issue)

			# Classification consistency checks with per-ISIN fix hints.
			raw_sector = (item.get("master_sector") or item.get("sector") or "").strip().upper().replace(" ", "_")
			norm_sector = cls._normalize_sector(
				item.get("master_sector") or item.get("sector"),
				issuer_type=issuer_type,
				category=item.get("category"),
				issuer_name=issuer_name,
			)
			category = (item.get("category") or "").strip().upper()
			inconsistencies: List[str] = []
			fix_hints: List[str] = []

			if issuer_type in gov_issuer_types and raw_sector and raw_sector not in gov_sectors:
				inconsistencies.append("government_issuer_with_non_government_sector")
				fix_hints.append("Map sector to CENTRAL_GOVERNMENT / STATE_GOVERNMENT / G-SEC / SDL.")
			if rating in {"SOV", "SOVEREIGN"} and issuer_type not in gov_issuer_types:
				inconsistencies.append("sovereign_rating_with_non_government_issuer_type")
				fix_hints.append("Align issuer_type to G-SEC/SDL/T-BILL or correct rating source entry.")
			if issuer_type == "CORPORATE" and norm_sector in gov_sectors:
				inconsistencies.append("corporate_issuer_with_government_sector")
				fix_hints.append("Either set issuer_type to government class or change sector to CORPORATE/PSU/BANK/NBFC.")
			if "EQUITY" in category and issuer_type in gov_issuer_types:
				inconsistencies.append("equity_category_with_government_issuer_type")
				fix_hints.append("Set category to debt/government bucket for sovereign instruments.")

			if inconsistencies:
				action_tier = "HIGH" if "sovereign_rating_with_non_government_issuer_type" in inconsistencies else "MEDIUM"
				classification_consistency_issues.append(
					{
						"isin": isin,
						"issuer_name": issuer_name,
						"issuer_type": issuer_type,
						"sector": raw_sector or norm_sector,
						"category": category,
						"rating": rating,
						"issues": inconsistencies,
						"action_tier": action_tier,
						"fix_hint": " ".join(dict.fromkeys(fix_hints)),
					}
				)

		def pct(value: float) -> float:
			return round((value / total_value * 100.0), 2) if total_value else 0.0

		pattern = {
			"government_securities": {
				"value": round(bucket_values["GOVERNMENT_SECURITIES"], 2),
				"pct": pct(bucket_values["GOVERNMENT_SECURITIES"]),
				"target_min": 45.0,
				"target_max": 65.0,
			},
			"debt_securities": {
				"value": round(bucket_values["DEBT_SECURITIES"], 2),
				"pct": pct(bucket_values["DEBT_SECURITIES"]),
				"target_min": 35.0,
				"target_max": 45.0,
			},
			"equity_related": {
				"value": round(bucket_values["EQUITY_RELATED"], 2),
				"pct": pct(bucket_values["EQUITY_RELATED"]),
				"target_min": 5.0,
				"target_max": 15.0,
			},
		}

		for entry in pattern.values():
			entry["status"] = "PASS" if entry["target_min"] <= entry["pct"] <= entry["target_max"] else "FAIL"

		concentration = []
		for issuer_name, value in sorted(issuer_values.items(), key=lambda kv: kv[1], reverse=True):
			exposure = pct(value)
			concentration.append(
				{
					"issuer_name": issuer_name,
					"value": round(value, 2),
					"pct": exposure,
					"limit": 10.0,
					"status": "BREACH" if exposure > 10.0 else "PASS",
				}
			)

		breaches = [row for row in concentration if row["status"] == "BREACH"]

		# Rating reconciliation across portfolio/master/dual/SEBI snapshots.
		rating_rows = await fetch_all(
			"""SELECT p.isin,
					  p.rating AS portfolio_rating,
					  b.credit_rating AS master_rating,
					  d.effective_rating AS dual_effective_rating,
					  d.rating1 AS dual_rating_1,
					  d.rating2 AS dual_rating_2,
					  (SELECT s.new_rating
						 FROM sebi_rating_updates s
						WHERE s.isin = p.isin
						ORDER BY COALESCE(s.action_date, s.fetched_at) DESC, s.id DESC
						LIMIT 1) AS latest_sebi_rating
			   FROM portfolio_current_positions p
			   LEFT JOIN universal_bond_master b ON b.isin = p.isin
			   LEFT JOIN dual_ratings d ON d.isin = p.isin
			   WHERE COALESCE(p.quantity, 0) > 0"""
		)
		rating_mismatches: List[dict] = []
		non_comparable = {"NR", "UNRATED", "NA", "N/A", "NOT_RATED"}
		for row in rating_rows:
			item = dict(row)
			isin = item.get("isin")
			vals = {
				"portfolio": normalize_rating(item.get("portfolio_rating"), isin=isin),
				"master": normalize_rating(item.get("master_rating"), isin=isin),
				"dual_effective": normalize_rating(item.get("dual_effective_rating"), isin=isin),
				"sebi_latest": normalize_rating(item.get("latest_sebi_rating"), isin=isin),
			}
			known = {k: v for k, v in vals.items() if v and str(v).upper() not in non_comparable}
			if len(known) >= 2 and len(set(known.values())) > 1:
				rating_mismatches.append({"isin": isin, "ratings": known})

		# Open governance/regulatory actions captured in compliance breach log.
		regulatory_action_rows = await fetch_all(
			"""SELECT id, breach_type, breach_key, severity, status, description,
					  affected_isins, detected_at
			   FROM compliance_breaches
			   WHERE UPPER(COALESCE(status, 'OPEN')) NOT IN ('RESOLVED', 'CLOSED')
				 AND (
				   UPPER(COALESCE(breach_type, '')) LIKE '%REGULATORY%'
				   OR UPPER(COALESCE(breach_type, '')) LIKE '%SEBI%'
				   OR UPPER(COALESCE(breach_type, '')) LIKE '%BLACKLIST%'
				   OR UPPER(COALESCE(breach_key, '')) LIKE '%RBI%'
				 )
			   ORDER BY detected_at DESC
			   LIMIT 200"""
		)

		# Accounting amortization cross-check: latest accounting book value vs portfolio book value.
		amort_rows = await fetch_all(
			"""SELECT p.isin,
					  p.book_value AS portfolio_book_value,
					  a.book_value AS accounting_book_value,
					  a.amortization,
					  a.record_date
			   FROM portfolio_current_positions p
			   LEFT JOIN accounting_records a
				 ON a.isin = p.isin
				AND a.record_date = (
					  SELECT MAX(a2.record_date) FROM accounting_records a2 WHERE a2.isin = p.isin
				 )
			   WHERE COALESCE(p.quantity, 0) > 0"""
		)
		amortization_mismatches: List[dict] = []
		for row in amort_rows:
			item = dict(row)
			p_bv = cls._safe_float(item.get("portfolio_book_value"))
			a_bv = cls._safe_float(item.get("accounting_book_value"))
			if p_bv is None or a_bv is None:
				continue
			delta = p_bv - a_bv
			if abs(delta) > 1.0:
				base = abs(a_bv) if abs(a_bv) > 1e-9 else 1.0
				amortization_mismatches.append(
					{
						"isin": item.get("isin"),
						"portfolio_book_value": round(p_bv, 4),
						"accounting_book_value": round(a_bv, 4),
						"difference": round(delta, 4),
						"difference_pct": round((delta / base) * 100.0, 4),
						"record_date": item.get("record_date"),
					}
				)

		actions: List[str] = []
		if any(v["status"] == "FAIL" for v in pattern.values()):
			actions.append("Rebalance allocation buckets to match 45-65 / 35-55 / 5-15 regulatory pattern.")
		if breaches:
			actions.append("Reduce issuer concentration above 10% of AUM.")
		if data_quality_issues:
			actions.append("Complete missing holding metadata such as maturity date and rating.")
		if quote_gaps:
			actions.append("Refresh stale/missing market quotes before analytics or recommendations.")
		if rating_mismatches:
			actions.append("Reconcile rating inconsistencies across portfolio, master, dual-rating, and SEBI feeds.")
		if regulatory_action_rows:
			actions.append("Resolve open regulatory/governance actions from compliance breaches.")
		if amortization_mismatches:
			actions.append("Investigate accounting amortization vs portfolio book-value variances.")
		if yield_coverage_issues:
			actions.append("Resolve missing/invalid/stale YTM coverage based on action tiers before downstream risk/reco usage.")
		if maturity_issues:
			actions.append("Address maturity validity issues and apply quarantine recommendations for unresolved critical ISINs.")
		if classification_consistency_issues:
			actions.append("Fix issuer_type/sector/category/rating inconsistencies at ISIN level using provided fix hints.")

		government_total = sum(government_breakout.values())
		def gov_pct(v: float) -> float:
			return round((v / government_total) * 100.0, 2) if government_total else 0.0

		def tier_counts(items: List[dict]) -> Dict[str, int]:
			out = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
			for item in items:
				tier = str(item.get("action_tier") or "LOW").upper()
				out[tier] = out.get(tier, 0) + 1
			return out

		return {
			"generated_at": _utcnow().isoformat(),
			"total_holdings": len(holdings),
			"portfolio_value": round(total_value, 2),
			"investment_pattern": pattern,
			"government_breakout": {
				"total_government_value": round(government_total, 2),
				"gsec": {"value": round(government_breakout["GSEC"], 2), "pct_of_government": gov_pct(government_breakout["GSEC"])},
				"sdl": {"value": round(government_breakout["SDL"], 2), "pct_of_government": gov_pct(government_breakout["SDL"])},
				"tbill": {"value": round(government_breakout["TBILL"], 2), "pct_of_government": gov_pct(government_breakout["TBILL"])},
				"other_government": {"value": round(government_breakout["OTHER_GOV"], 2), "pct_of_government": gov_pct(government_breakout["OTHER_GOV"])},
			},
			"concentration": {
				"limit_pct": 10.0,
				"top_issuers": concentration[:10],
				"breaches": breaches,
			},
			"quote_coverage": {
				"missing_or_stale_count": len(quote_gaps),
				"holdings": quote_gaps[:100],
			},
			"market_yield_coverage": {
				"issues_count": len(yield_coverage_issues),
				"tier_counts": tier_counts(yield_coverage_issues),
				"holdings": yield_coverage_issues[:300],
			},
			"data_quality": {
				"issues_count": len(data_quality_issues),
				"holdings": data_quality_issues[:200],
			},
			"maturity_remediation": {
				"issues_count": len(maturity_issues),
				"tier_counts": tier_counts(maturity_issues),
				"holdings": maturity_issues[:300],
				"quarantine_recommendations": quarantine_recommendations[:300],
			},
			"classification_consistency": {
				"issues_count": len(classification_consistency_issues),
				"tier_counts": tier_counts(classification_consistency_issues),
				"holdings": classification_consistency_issues[:300],
			},
			"rating_reconciliation": {
				"mismatch_count": len(rating_mismatches),
				"holdings": rating_mismatches[:200],
			},
			"regulatory_actions": {
				"open_count": len(regulatory_action_rows),
				"items": [dict(r) for r in regulatory_action_rows],
			},
			"amortization_check": {
				"mismatch_count": len(amortization_mismatches),
				"holdings": amortization_mismatches[:200],
			},
			"actions": actions,
		}

	@classmethod
	def _load_rows(cls, content: bytes, *, filename: str, content_type: str) -> List[dict]:
		name = (filename or "").lower()
		ctype = (content_type or "").lower()

		is_xlsx = name.endswith(".xlsx") or ("spreadsheet" in ctype and not name.endswith(".xls")) or ("excel" in ctype and not name.endswith(".xls"))
		is_xls = name.endswith(".xls") and not name.endswith(".xlsx")
		is_json = name.endswith(".json") or "application/json" in ctype

		if is_json:
			parsed = json.loads(content.decode("utf-8"))
			if isinstance(parsed, list):
				return [dict(x) for x in parsed if isinstance(x, dict)]
			if isinstance(parsed, dict):
				rows = parsed.get("rows") or parsed.get("holdings") or parsed.get("portfolio") or []
				return [dict(x) for x in rows if isinstance(x, dict)]
			return []

		if is_xlsx:
			return cls._load_excel_rows(content)

		if is_xls:
			return cls._load_xls_rows(content)

		text = content.decode("utf-8-sig")
		reader = csv.DictReader(io.StringIO(text))
		return [dict(r) for r in reader]

	@classmethod
	def _load_xls_rows(cls, content: bytes) -> List[dict]:
		if xlrd is None:
			raise RuntimeError("xlrd not installed")

		wb = xlrd.open_workbook(file_contents=content)
		out: List[dict] = []
		try:
			for ws in wb.sheets():
				all_rows = [ws.row_values(r) for r in range(ws.nrows)]
				if not all_rows:
					continue
				header_idx = cls._find_header_row(all_rows)
				if header_idx is None:
					continue
				headers = [str(v or "").strip() for v in all_rows[header_idx]]
				if not cls._looks_like_portfolio_sheet(headers):
					continue
				for row_number, row in enumerate(all_rows[header_idx + 1 :], start=header_idx + 2):
					payload = {}
					non_blank = False
					for i, value in enumerate(row):
						if i >= len(headers):
							continue
						key = headers[i]
						payload[key] = value
						if value not in (None, ""):
							non_blank = True
					if non_blank:
						payload["_source_sheet_name"] = ws.name
						payload["_source_row_number"] = row_number
						out.append(payload)
		finally:
			pass
		return out

	@classmethod
	def _load_excel_rows(cls, content: bytes) -> List[dict]:
		if openpyxl is None:
			raise RuntimeError("openpyxl not installed")

		wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
		out: List[dict] = []
		try:
			for ws in wb.worksheets:
				all_rows = [list(r) for r in ws.iter_rows(values_only=True)]
				if not all_rows:
					continue
				header_idx = cls._find_header_row(all_rows)
				if header_idx is None:
					continue
				headers = [str(v or "").strip() for v in all_rows[header_idx]]
				if not cls._looks_like_portfolio_sheet(headers):
					continue
				for row_number, row in enumerate(all_rows[header_idx + 1 :], start=header_idx + 2):
					payload = {}
					non_blank = False
					for i, value in enumerate(row):
						if i >= len(headers):
							continue
						key = headers[i]
						payload[key] = value
						if value not in (None, ""):
							non_blank = True
					if non_blank:
						payload["_source_sheet_name"] = ws.title
						payload["_source_row_number"] = row_number
						out.append(payload)
		finally:
			wb.close()
		return out

	@classmethod
	def _find_header_row(cls, rows: Iterable[List[Any]]) -> Optional[int]:
		for idx, row in enumerate(rows):
			if idx > 15:
				break
			hits = 0
			for cell in row:
				n = cls._normalize_key(cell)
				if not n:
					continue
				if n in cls._alias_lookup():
					hits += 1
			if hits >= 2:
				return idx
		return None

	@classmethod
	def _looks_like_portfolio_sheet(cls, headers: Iterable[str]) -> bool:
		normalized_headers = [cls._normalize_key(header) for header in headers if cls._normalize_key(header)]
		if not normalized_headers:
			return False
		lookup = cls._alias_lookup()
		matched_groups = set()
		for header in normalized_headers:
			for canonical, aliases in cls.PORTFOLIO_SHEET_HINTS.items():
				if header in aliases or header == canonical:
					matched_groups.add(canonical)
					break
		# Require the structural columns that identify a bond register rather than an audit/tabular note sheet.
		required = {"bond_name", "quantity", "maturity_date"}
		if not required.issubset(matched_groups):
			return False
		return any(header in lookup for header in normalized_headers)

	@classmethod
	def _canonicalize_row(cls, row: dict) -> dict:
		out: Dict[str, Any] = {}
		lookup = cls._alias_lookup()

		for key, value in row.items():
			normalized_key = cls._normalize_key(key)
			canonical = lookup.get(normalized_key)
			if canonical:
				out[canonical] = value

		isin = str(out.get("isin") or row.get("isin") or "").strip().upper()
		if isin:
			out["isin"] = isin
		for canonical_key in ("bond_name", "issuer_name", "face_value", "quantity", "purchase_price", "current_value", "purchase_date", "maturity_date", "credit_rating", "credit_rating_1", "credit_rating_2", "coupon_rate", "ytm_at_purchase", "sector", "category", "ip_dates", "put_date", "call_date", "rating_agency", "rating_agency_1", "rating_agency_2"):
			if canonical_key not in out and canonical_key in row:
				out[canonical_key] = row.get(canonical_key)
		if not out.get("credit_rating_1") and out.get("credit_rating"):
			out["credit_rating_1"] = out.get("credit_rating")
		if not out.get("rating_agency_1") and out.get("rating_agency"):
			out["rating_agency_1"] = out.get("rating_agency")
		return out

	@classmethod
	async def build_excel_system_validation_report(
		cls,
		*,
		workbook_content: bytes,
		workbook_filename: str,
		workbook_content_type: str,
		trust_id: str = "default",
		tolerance_pct: float = 0.1,
		source_reference: Optional[str] = None,
	) -> dict:
		"""Compare uploaded workbook rows against persisted system positions."""
		parsed = await cls.parse_upload(
			workbook_content,
			filename=workbook_filename,
			content_type=workbook_content_type,
		)
		accepted = list(parsed.get("accepted") or [])
		effective_source_ref = cls._canonicalize_upload_reference(source_reference or workbook_filename or "") or None

		system_rows = (await fetch_all(
			"""
			SELECT
				isin,
				quantity,
				book_value,
				source_sheet_name,
				source_row_number,
				source_reference
			FROM portfolio_positions
			WHERE trust_id = :trust_id
			  AND (:source_reference IS NULL OR source_reference = :source_reference)
			ORDER BY id ASC
			""",
			{"trust_id": trust_id, "source_reference": effective_source_ref},
		)) or []

		wb_by_row: Dict[str, dict] = {}
		wb_by_isin: Dict[str, Dict[str, float]] = {}
		for idx, row in enumerate(accepted, start=1):
			isin = str(row.get("isin") or "").strip().upper()
			if not isin:
				continue
			qty = cls._safe_float(row.get("quantity")) or 0.0
			book_value = cls._safe_float(row.get("current_value"))
			if book_value is None:
				book_value = cls._derive_basis_value(
					face_value=cls._safe_float(row.get("face_value")) or 1000.0,
					quantity=qty,
					purchase_price=cls._safe_float(row.get("purchase_price")),
					current_value=cls._safe_float(row.get("current_value")),
				)
			sheet_name = str(row.get("_source_sheet_name") or "").strip() or "sheet"
			row_num = int(row.get("_row_number") or idx)
			row_key = f"{sheet_name}:{row_num}"
			wb_by_row[row_key] = {
				"row_key": row_key,
				"isin": isin,
				"sheet_name": sheet_name,
				"row_number": row_num,
				"workbook_quantity": qty,
				"workbook_book_value": float(book_value or 0.0),
			}
			agg = wb_by_isin.setdefault(isin, {"quantity": 0.0, "book_value": 0.0})
			agg["quantity"] += qty
			agg["book_value"] += float(book_value or 0.0)

		sy_by_row: Dict[str, dict] = {}
		sy_by_isin: Dict[str, Dict[str, float]] = {}
		for idx, row in enumerate(system_rows, start=1):
			isin = str(row.get("isin") or "").strip().upper()
			if not isin:
				continue
			sheet_name = str(row.get("source_sheet_name") or "").strip() or "sheet"
			row_num = int(row.get("source_row_number") or idx)
			row_key = f"{sheet_name}:{row_num}"
			qty = float(row.get("quantity") or 0.0)
			book_value = float(row.get("book_value") or 0.0)
			sy_by_row[row_key] = {
				"row_key": row_key,
				"isin": isin,
				"sheet_name": sheet_name,
				"row_number": row_num,
				"system_quantity": qty,
				"system_book_value": book_value,
			}
			agg = sy_by_isin.setdefault(isin, {"quantity": 0.0, "book_value": 0.0})
			agg["quantity"] += qty
			agg["book_value"] += book_value

		def pct_diff(a: float, b: float) -> float:
			den = max(abs(a), 1e-9)
			return abs(b - a) * 100.0 / den

		row_results: List[dict] = []
		missing_in_system: List[dict] = []
		extra_in_system: List[dict] = []
		for key in sorted(set(wb_by_row.keys()) | set(sy_by_row.keys())):
			wb = wb_by_row.get(key)
			sy = sy_by_row.get(key)
			if wb and not sy:
				missing_in_system.append(wb)
				row_results.append({
					"row_key": key,
					"isin": wb.get("isin"),
					"status": "MISSING_IN_SYSTEM",
					"quantity_diff_pct": 100.0,
					"book_value_diff_pct": 100.0,
				})
				continue
			if sy and not wb:
				extra_in_system.append(sy)
				row_results.append({
					"row_key": key,
					"isin": sy.get("isin"),
					"status": "EXTRA_IN_SYSTEM",
					"quantity_diff_pct": 100.0,
					"book_value_diff_pct": 100.0,
				})
				continue

			qty_diff_pct = pct_diff(float(wb["workbook_quantity"]), float(sy["system_quantity"]))
			bv_diff_pct = pct_diff(float(wb["workbook_book_value"]), float(sy["system_book_value"]))
			status = "PASS" if max(qty_diff_pct, bv_diff_pct) <= tolerance_pct else "MISMATCH"
			row_results.append({
				"row_key": key,
				"isin": wb.get("isin") or sy.get("isin"),
				"status": status,
				"workbook_quantity": float(wb["workbook_quantity"]),
				"system_quantity": float(sy["system_quantity"]),
				"workbook_book_value": float(wb["workbook_book_value"]),
				"system_book_value": float(sy["system_book_value"]),
				"quantity_diff_pct": round(qty_diff_pct, 6),
				"book_value_diff_pct": round(bv_diff_pct, 6),
			})

		bond_results: List[dict] = []
		all_isins = sorted(set(wb_by_isin.keys()) | set(sy_by_isin.keys()))
		for isin in all_isins:
			wb = wb_by_isin.get(isin, {"quantity": 0.0, "book_value": 0.0})
			sy = sy_by_isin.get(isin, {"quantity": 0.0, "book_value": 0.0})
			qty_diff_pct = pct_diff(float(wb["quantity"]), float(sy["quantity"]))
			bv_diff_pct = pct_diff(float(wb["book_value"]), float(sy["book_value"]))
			status = "PASS" if max(qty_diff_pct, bv_diff_pct) <= tolerance_pct else "MISMATCH"
			bond_results.append(
				{
					"isin": isin,
					"status": status,
					"workbook_quantity": round(float(wb["quantity"]), 6),
					"system_quantity": round(float(sy["quantity"]), 6),
					"workbook_book_value": round(float(wb["book_value"]), 6),
					"system_book_value": round(float(sy["book_value"]), 6),
					"quantity_diff_pct": round(qty_diff_pct, 6),
					"book_value_diff_pct": round(bv_diff_pct, 6),
				}
			)

		wb_total_qty = sum(float(v.get("quantity") or 0.0) for v in wb_by_isin.values())
		wb_total_bv = sum(float(v.get("book_value") or 0.0) for v in wb_by_isin.values())
		sy_total_qty = sum(float(v.get("quantity") or 0.0) for v in sy_by_isin.values())
		sy_total_bv = sum(float(v.get("book_value") or 0.0) for v in sy_by_isin.values())

		portfolio_quantity_diff_pct = pct_diff(wb_total_qty, sy_total_qty)
		portfolio_book_value_diff_pct = pct_diff(wb_total_bv, sy_total_bv)
		row_mismatch_count = sum(1 for r in row_results if r.get("status") != "PASS")
		bond_mismatch_count = sum(1 for r in bond_results if r.get("status") != "PASS")

		overall_pass = (
			portfolio_quantity_diff_pct <= tolerance_pct
			and portfolio_book_value_diff_pct <= tolerance_pct
			and row_mismatch_count == 0
			and bond_mismatch_count == 0
		)

		return {
			"overall_status": "PASS" if overall_pass else "FAIL",
			"trust_id": trust_id,
			"source_reference": effective_source_ref,
			"tolerance_pct": tolerance_pct,
			"portfolio_level": {
				"workbook_rows": len(wb_by_row),
				"system_rows": len(sy_by_row),
				"workbook_total_quantity": round(wb_total_qty, 6),
				"system_total_quantity": round(sy_total_qty, 6),
				"workbook_total_book_value": round(wb_total_bv, 6),
				"system_total_book_value": round(sy_total_bv, 6),
				"quantity_diff_pct": round(portfolio_quantity_diff_pct, 6),
				"book_value_diff_pct": round(portfolio_book_value_diff_pct, 6),
				"pass": portfolio_quantity_diff_pct <= tolerance_pct and portfolio_book_value_diff_pct <= tolerance_pct,
			},
			"bond_level": {
				"total_isins": len(all_isins),
				"mismatch_count": bond_mismatch_count,
				"rows": bond_results,
			},
			"row_level": {
				"mismatch_count": row_mismatch_count,
				"missing_in_system": missing_in_system,
				"extra_in_system": extra_in_system,
				"rows": row_results,
			},
			"parsed_summary": parsed.get("summary", {}),
			"checked_at": _utcnow().isoformat(),
		}

	@classmethod
	def _alias_lookup(cls) -> Dict[str, str]:
		mapping: Dict[str, str] = {}
		for canonical, aliases in cls.COLUMN_ALIASES.items():
			mapping[cls._normalize_key(canonical)] = canonical
			for alias in aliases:
				mapping[cls._normalize_key(alias)] = canonical
		return mapping

	@staticmethod
	def _normalize_key(value: Any) -> str:
		if value is None:
			return ""
		s = str(value).strip().lower()
		s = re.sub(r"[^a-z0-9]+", "", s)
		return s

	@staticmethod
	def _safe_float(value: Any) -> Optional[float]:
		if value is None:
			return None
		s = str(value).strip().replace(",", "")
		if s == "":
			return None
		try:
			return float(s)
		except Exception:
			return None

	@classmethod
	def _infer_pf_eligible(cls, issuer_type: str, rating: str) -> int:
		itype = (issuer_type or "").upper()
		if itype in {"G-SEC", "SDL", "T-BILL"}:
			return 1
		allowed = {"AAA", "AA+", "AA", "AA-", "SOV", "SOVEREIGN"}
		return 1 if (rating or "").upper() in allowed else 0

	@classmethod
	def _infer_issuer_type(
		cls,
		*,
		isin: Optional[str],
		issuer_name: Optional[str],
		sector: Optional[str],
		category: Optional[str],
		bond_name: Optional[str],
	) -> str:
		issuer = (issuer_name or "").upper()
		sec = (sector or "").upper()
		cat = (category or "").upper()
		name = (bond_name or "").upper()

		if "GOVERNMENT OF INDIA" in issuer or " G-SEC" in name:
			return "G-SEC"
		if "STATE" in issuer or "SDL" in sec or "STATE" in cat:
			return "SDL"
		if "T-BILL" in name or "TREASURY BILL" in name:
			return "T-BILL"
		if "PSU" in sec or "PUBLIC" in sec:
			return "PSU"
		if "BANK" in sec:
			return "BANK"
		if "NBFC" in sec:
			return "NBFC"
		return "CORPORATE"

	@classmethod
	def _infer_allocation_bucket(
		cls,
		*,
		issuer_type: str,
		issuer_name: Optional[str],
		category: Optional[str],
		sector: Optional[str],
		bond_name: Optional[str],
	) -> str:
		itype = (issuer_type or "").upper()
		cat = (category or "").upper()
		sec = (sector or "").upper()
		name = (bond_name or "").upper()

		if itype in {"G-SEC", "SDL", "T-BILL"}:
			return "GOVERNMENT_SECURITIES"
		if "EQUITY" in cat or "EQUITY" in sec or "EQUITY" in name:
			return "EQUITY_RELATED"
		return "DEBT_SECURITIES"

	@classmethod
	def _normalize_sector(
		cls,
		sector: Optional[str],
		*,
		issuer_type: Optional[str],
		category: Optional[str],
		issuer_name: Optional[str],
	) -> str:
		allowed = {
			"CENTRAL_GOVERNMENT",
			"STATE_GOVERNMENT",
			"GOVERNMENT_SECURITIES",
			"G-SEC",
			"SDL",
			"PSU",
			"CORPORATE",
			"BANK",
			"NBFC",
			"EQUITY",
			"MUTUAL_FUND",
			"SOVEREIGN",
			"GOVERNMENT",
		}

		raw = (sector or "").strip().upper().replace(" ", "_")
		cat = (category or "").strip().upper().replace(" ", "_")
		itype = (issuer_type or "").strip().upper()
		issuer = (issuer_name or "").strip().upper()

		if raw in allowed:
			return raw
		if raw == "STATE":
			return "STATE_GOVERNMENT"
		if raw in {"CENTRAL", "GOI", "GOVT"}:
			return "CENTRAL_GOVERNMENT"
		if itype in {"G-SEC", "T-BILL"}:
			return "CENTRAL_GOVERNMENT"
		if itype == "SDL":
			return "STATE_GOVERNMENT"
		if itype in {"PSU", "BANK", "NBFC", "CORPORATE"}:
			return itype
		if "STATE" in issuer:
			return "STATE_GOVERNMENT"
		if "GOVERNMENT OF INDIA" in issuer:
			return "CENTRAL_GOVERNMENT"
		if "EQUITY" in cat:
			return "EQUITY"
		return "CORPORATE"

	@classmethod
	def _cap_bucket_from_row(
		cls,
		*,
		issuer_type: str,
		issuer_name: Optional[str],
		sector: Optional[str],
		category: Optional[str],
		bond_name: Optional[str],
	) -> tuple[str, str]:
		"""Return canonical cap buckets for category and sector tracking."""
		itype = (issuer_type or "").upper()
		cat = (category or "").upper()
		sec = (sector or "").upper()
		name = (bond_name or "").upper()

		if "EQUITY" in cat or "EQUITY" in sec or any(token in name for token in ("EQUITY", "FUND", "ETF", "BLUE CHIP", "BALANCED ADVANTAGE", "FLEXICAP", "LARGE CAP", "MID CAP", "SMALL CAP", "INDEX")):
			return "EQUITY_RELATED", "EQUITY"
		if itype in {"G-SEC", "SDL", "T-BILL"} or "GOV" in sec or "STATE" in sec or "SOV" in sec:
			return "CENTRAL_STATE", "CENTRAL_STATE"
		return "PSU_PRIVATE", "PSU_PRIVATE"

	@classmethod
	def _derive_basis_value(
		cls,
		*,
		face_value: float,
		quantity: float,
		purchase_price: Optional[float],
		current_value: Optional[float],
	) -> float:
		"""Derive the single invested basis used for cap checks and summaries."""
		if current_value is not None and current_value > 0:
			return float(current_value)
		if purchase_price is not None and purchase_price > 0 and face_value > 0:
			return float(purchase_price) * float(face_value) * max(float(quantity or 0.0), 1.0) / 100.0
		if face_value > 0:
			return float(face_value) * max(float(quantity or 0.0), 1.0)
		return 0.0

	@classmethod
	def _parse_date(cls, value: Any) -> Optional[str]:
		if value in (None, ""):
			return None
		if isinstance(value, (int, float)) and not isinstance(value, bool):
			parsed_numeric = parse_flexible_date(value)
			if parsed_numeric is not None:
				return parsed_numeric.isoformat()
		if isinstance(value, datetime):
			return value.date().isoformat()
		if isinstance(value, date):
			return value.isoformat()

		raw = str(value).strip().replace("\n", " ")

		# Partial redemption schedule text: take latest date as final maturity.
		if "=" in raw or "%" in raw:
			parts = re.findall(r"(\d{2}[-./]\d{2}[-./]\d{4})", raw)
			parsed = []
			for part in parts:
				one = cls._parse_date(part)
				if one:
					parsed.append(one)
			if parsed:
				return max(parsed)

		if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
			return raw

		if re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
			parsed_numeric = parse_flexible_date(raw)
			if parsed_numeric is not None:
				return parsed_numeric.isoformat()

		for fmt in (
			"%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%y",
			"%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y", "%Y/%m/%d",
			"%m/%d/%Y", "%m-%d-%Y", "%d-%b-%Y", "%d-%B-%Y", "%Y%m%d",
		):
			try:
				return datetime.strptime(raw, fmt).date().isoformat()
			except Exception:
				continue

		parsed = parse_flexible_date(raw)
		if parsed is not None:
			return parsed.isoformat()
		return None


