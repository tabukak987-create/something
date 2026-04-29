"""
Datasources Router
==================
Exposes endpoints for:
  - Portfolio CSV/JSON upload (drop-and-replace)
  - SEBI API on-demand sync
  - Four-Eyes approval workflow for IPS rule changes
  - SEBI rating updates & issuance calendar queries
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, List, Optional

try:
    import openpyxl
    _HAS_OPENPYXL = True
except ImportError:  # pragma: no cover
    _HAS_OPENPYXL = False

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel

from ..shared.auth import require_role, Roles
from ..shared.deployment import assert_external_data_egress
from ..shared.database import execute_query, fetch_all, fetch_one
from ..shared.logger import get_logger
from ..shared.time_utils import utcnow_iso
from ..domain.core_trust.audit import AuditAction, AuditService, AuditSeverity, EntityType
from ..domain.compliance.sebi_api_sync import SEBIApiSync
from ..domain.core_trust.approvals import FourEyesApprovalService
from ..domain.portfolio.dataset import PortfolioDatasetService
from ..domain.core_trust.data_governance import data_governance_report

log = get_logger("router.datasources")
router = APIRouter(prefix="/datasources", tags=["Data Sources"])


# ════════════════════════════════════════════════════════════════════════════
# PORTFOLIO UPLOAD
# ════════════════════════════════════════════════════════════════════════════

class PortfolioRow(BaseModel):
    isin:          str
    bond_name:     Optional[str] = None
    face_value:    Optional[float] = None
    quantity:      Optional[float] = None
    purchase_price: Optional[float] = None
    purchase_date: Optional[str] = None
    current_value: Optional[float] = None
    credit_rating: Optional[str] = None
    maturity_date: Optional[str] = None
    issuer_name:   Optional[str] = None
    coupon_rate:   Optional[float] = None
    sector:        Optional[str] = None


@router.post(
    "/portfolio/upload",
    summary="Upload Portfolio CSV, JSON, XLSX or XLS",
    description=(
        "Drop a CSV, JSON, XLSX or XLS file containing portfolio holdings. "
        "Accepted columns: isin, bond_name, face_value, quantity, purchase_price, "
        "purchase_date, current_value, credit_rating, maturity_date, issuer_name, "
        "coupon_rate, sector. "
        "mode=replace clears the portfolio first; mode=merge upserts records."
    ),
)
async def upload_portfolio(
    request: Request,
    file: UploadFile = File(..., description="CSV, JSON, XLSX or XLS file"),
    mode: str = Form("merge", description="'replace' or 'merge'"),
    trust_id: str = Form("default", description="Trust identifier"),
    user: dict = Depends(require_role(Roles.PORTFOLIO)),
):
    _XLSX_MIME = {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
    }
    _ALLOWED_MIME = {
        "text/csv", "application/json", "text/plain", "application/octet-stream",
    } | _XLSX_MIME

    raw = await file.read()
    filename = (file.filename or "").lower()
    content_type = file.content_type or ""
    uploaded_by = user.get("sub") or user.get("username", "unknown")

    is_xlsx = filename.endswith(".xlsx") or filename.endswith(".xls") or content_type in _XLSX_MIME
    is_json = filename.endswith(".json") or content_type == "application/json"

    if content_type not in _ALLOWED_MIME and not is_xlsx and not is_json:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Upload a CSV, JSON, XLSX or XLS file",
        )

    batch_meta = await PortfolioDatasetService.register_upload_batch(
        content=raw,
        filename=file.filename or "",
        content_type=content_type,
        trust_id=trust_id,
        uploaded_by=uploaded_by,
    )
    batch_id = batch_meta["batch_id"]

    try:
        parsed = await PortfolioDatasetService.parse_upload(
            raw,
            filename=file.filename or "",
            content_type=content_type,
            batch_id=batch_id,
            strict=True,
            portfolio_id=trust_id,
            trust_id=trust_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Parse error: {exc}")

    if parsed.get("summary", {}).get("total_rows", 0) <= 0:
        raise HTTPException(status_code=422, detail="File contains no valid rows")

    rejected = parsed.get("rejected") or []
    reject_errors = []
    for item in rejected[:50]:
        row_no = item.get("_row_number")
        isin = item.get("isin") or ""
        errors = ", ".join(item.get("_errors") or []) or "validation_failed"
        reject_errors.append({"row": row_no, "isin": isin, "errors": errors})

    if rejected:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Validation failed",
                "batch_id": batch_id,
                "rejected_rows": len(rejected),
                "errors": reject_errors,
            },
        )

    try:
        sync_result = await PortfolioDatasetService.sync_upload(
            parsed,
            mode=mode,
            uploaded_by=uploaded_by,
            source_context={
                "source_name": "Client Upload",
                "source_type": "client_provided",
                "ingestion_method": "manual_upload",
                "source_reference": file.filename or "uploaded_file",
                "timestamp_ingested": utcnow_iso(),
                "timestamp_effective": utcnow_iso(),
                "actor": uploaded_by,
                "trust_id": trust_id,
                "portfolio_id": trust_id,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    await AuditService.record(
        user={
            "id": str(user.get("id", user.get("sub", "unknown"))),
            "username": user.get("username") or user.get("sub") or "unknown",
            "role": user.get("role", Roles.PORTFOLIO),
        },
        action=AuditAction.PORTFOLIO_UPLOAD,
        entity_type=EntityType.PORTFOLIO,
        entity_id=trust_id,
        old_value=None,
        new_value={
            "batch_id": batch_id,
            "mode": mode,
            "version_number": sync_result.get("version_number"),
            "accepted_rows": parsed.get("summary", {}).get("accepted_rows", 0),
            "rejected_rows": parsed.get("summary", {}).get("rejected_rows", 0),
            "file_name": file.filename,
        },
        affected_module="datasources.portfolio_upload",
        ip_address=request.client.host if request.client else None,
        severity=AuditSeverity.INFO,
        compliance_flag=True,
        description=f"Portfolio upload committed for trust {trust_id} (batch {batch_id})",
    )

    await AuditService.record(
        user={
            "id": str(user.get("id", user.get("sub", "unknown"))),
            "username": user.get("username") or user.get("sub") or "unknown",
            "role": user.get("role", Roles.PORTFOLIO),
        },
        action=AuditAction.PORTFOLIO_VERSION_CREATED,
        entity_type=EntityType.PORTFOLIO,
        entity_id=trust_id,
        old_value=None,
        new_value={
            "batch_id": batch_id,
            "version_number": sync_result.get("version_number"),
        },
        affected_module="datasources.portfolio_upload",
        ip_address=request.client.host if request.client else None,
        severity=AuditSeverity.INFO,
        compliance_flag=True,
        description=f"Portfolio version {sync_result.get('version_number')} created for trust {trust_id}",
    )

    return {
        "success": True,
        "mode": mode,
        "total_rows": parsed.get("summary", {}).get("total_rows", 0),
        "accepted_rows": parsed.get("summary", {}).get("accepted_rows", 0),
        "rejected_rows": parsed.get("summary", {}).get("rejected_rows", 0),
        "inserted": sync_result.get("synced_rows", 0),
        "errors": sync_result.get("errors") or [],
        "uploaded_by": uploaded_by,
        "uploaded_at": utcnow_iso(),
        "source_name": "Client Upload",
        "source_type": "client_provided",
        "ingestion_method": "manual_upload",
        "batch_id": batch_id,
        "version_number": sync_result.get("version_number"),
    }


@router.get("/portfolio/regulatory-audit", summary="Regulatory allocation and data-quality audit")
async def portfolio_regulatory_audit(user: dict = Depends(require_role(Roles.AUDITOR))):
    try:
        return await PortfolioDatasetService.build_regulatory_audit()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Audit generation failed: {exc}")


@router.get("/portfolio/uploads/{batch_id}", summary="Get portfolio upload batch report")
async def get_portfolio_upload_batch(batch_id: str, user: dict = Depends(require_role(Roles.VIEWER))):
    try:
        report = await PortfolioDatasetService.get_upload_batch_report(batch_id)
        if not report.get("batch"):
            raise HTTPException(status_code=404, detail="Upload batch not found")
        return report
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Batch lookup failed: {exc}")


@router.get("/portfolio/holdings", summary="List current portfolio holdings")
async def list_portfolio_holdings(
    limit: int = Query(200, ge=1, le=1000),
    trust_id: str = Query("default"),
    user: dict = Depends(require_role(Roles.VIEWER)),
):
    """Return all portfolio holdings with computed market value."""
    await _ensure_portfolio_table()
    rows = await fetch_all(
        """
         WITH active_positions AS (
             SELECT p.id, p.portfolio_id, p.isin, p.purchase_date, p.quantity, p.purchase_price, p.face_value,
                    p.source_name, p.source_type, p.ingestion_method, p.source_reference,
                    p.timestamp_ingested, p.timestamp_effective
             FROM portfolio_positions p
             WHERE p.trust_id = :pid
             UNION ALL
             SELECT h.id, h.portfolio_id, h.isin, h.purchase_date, h.quantity, h.purchase_price, h.face_value,
                    h.source_name, h.source_type, h.ingestion_method, h.source_reference,
                    h.timestamp_ingested, h.timestamp_effective
             FROM portfolio_holdings h
             WHERE h.portfolio_id = :pid
               AND NOT EXISTS (SELECT 1 FROM portfolio_positions p WHERE p.trust_id = :pid)
         )
         SELECT ap.id, ap.portfolio_id, ap.isin, ap.purchase_date, ap.quantity, ap.purchase_price, ap.face_value,
             ap.source_name AS holding_source_name, ap.source_type AS holding_source_type,
             ap.ingestion_method AS holding_ingestion_method, ap.source_reference AS holding_source_reference,
             ap.timestamp_ingested AS holding_timestamp_ingested,
             ap.timestamp_effective AS holding_timestamp_effective,
             i.instrument_name, i.issuer, i.rating_latest, i.coupon_rate, i.maturity_date,
             i.source_name AS instrument_source_name, i.source_type AS instrument_source_type,
             i.ingestion_method AS instrument_ingestion_method, i.source_reference AS instrument_source_reference,
             i.timestamp_ingested AS instrument_timestamp_ingested,
             i.timestamp_effective AS instrument_timestamp_effective
        FROM active_positions ap
        LEFT JOIN instrument_master i ON i.isin = ap.isin
        ORDER BY ap.id DESC
        LIMIT :lim
        """,
        {"lim": limit, "pid": trust_id},
    )
    holdings = [dict(r) for r in rows]

    # Enrich each holding with current market price if available
    for h in holdings:
        md = await fetch_one(
            """
            SELECT price, date, source, source_name, source_type, ingestion_method,
                   source_reference, timestamp_ingested, timestamp_effective
            FROM market_prices
            WHERE isin = :isin
            ORDER BY date DESC
            LIMIT 1
            """,
            {"isin": h["isin"]},
        )
        if md and md["price"] is not None:
            h["market_price"] = md["price"]
            qty = h.get("quantity") or 0
            h["market_value"] = round(float(md["price"]) * float(qty), 2)
            h["market_price_date"] = md.get("date")
            h["market_price_source"] = md.get("source")
            h["source_name"] = md.get("source_name") or h.get("instrument_source_name") or h.get("holding_source_name") or md.get("source")
            h["source_type"] = md.get("source_type") or h.get("instrument_source_type") or h.get("holding_source_type") or "unknown"
            h["ingestion_method"] = md.get("ingestion_method") or h.get("instrument_ingestion_method") or h.get("holding_ingestion_method") or "unknown"
            h["source_reference"] = md.get("source_reference") or h.get("instrument_source_reference") or h.get("holding_source_reference") or ""
            h["timestamp_ingested"] = md.get("timestamp_ingested") or h.get("instrument_timestamp_ingested") or h.get("holding_timestamp_ingested") or ""
            h["timestamp_effective"] = md.get("timestamp_effective") or h.get("instrument_timestamp_effective") or h.get("holding_timestamp_effective") or ""
            h["confidence_level"] = "high" if h.get("source_type") in {"client_provided", "licensed"} else "medium"
        else:
            h["source_name"] = h.get("instrument_source_name") or h.get("holding_source_name") or "UNKNOWN"
            h["source_type"] = h.get("instrument_source_type") or h.get("holding_source_type") or "unknown"
            h["ingestion_method"] = h.get("instrument_ingestion_method") or h.get("holding_ingestion_method") or "unknown"
            h["source_reference"] = h.get("instrument_source_reference") or h.get("holding_source_reference") or ""
            h["timestamp_ingested"] = h.get("instrument_timestamp_ingested") or h.get("holding_timestamp_ingested") or ""
            h["timestamp_effective"] = h.get("instrument_timestamp_effective") or h.get("holding_timestamp_effective") or ""
            h["confidence_level"] = "low"

    return {"holdings": holdings, "count": len(holdings)}


@router.get("/portfolio/summary", summary="Portfolio summary metrics")
async def portfolio_summary(
    trust_id: str = Query("default"),
    user: dict = Depends(require_role(Roles.VIEWER)),
):
    await _ensure_portfolio_table()
    row = await fetch_one(
        """SELECT
                         COUNT(*) AS total_bonds,
                         SUM(quantity) AS total_quantity,
                         SUM(quantity * purchase_price) AS total_cost
                     FROM (
                         SELECT p.isin, p.quantity, p.purchase_price
                         FROM portfolio_positions p
                         WHERE p.trust_id = :pid
                         UNION ALL
                         SELECT h.isin, h.quantity, h.purchase_price
                         FROM portfolio_holdings h
                         WHERE h.portfolio_id = :pid
                             AND NOT EXISTS (SELECT 1 FROM portfolio_positions p WHERE p.trust_id = :pid)
                     )""",
                {"pid": trust_id},
    )
    return dict(row) if row else {}


@router.get("/governance/audit", summary="Data provenance and calculation audit")
async def governance_audit(
    trust_id: str = Query("default"),
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    return await data_governance_report(portfolio_id=trust_id, trust_id=trust_id)


# ════════════════════════════════════════════════════════════════════════════
# SEBI API SYNC
# ════════════════════════════════════════════════════════════════════════════

@router.post("/sebi/sync", summary="Trigger full SEBI data sync")
async def trigger_sebi_sync(user: dict = Depends(require_role(Roles.COMPLIANCE))):
    try:
        assert_external_data_egress("SEBI full sync")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    await SEBIApiSync.initialize()
    result = await SEBIApiSync.run_full_sync()
    return result


@router.get("/sebi/rating-updates", summary="Recent SEBI rating updates")
async def get_rating_updates(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(50, ge=1, le=500),
    user: dict = Depends(require_role(Roles.VIEWER)),
):
    await SEBIApiSync.initialize()
    return {"updates": await SEBIApiSync.get_recent_rating_updates(days=days, limit=limit)}


@router.get("/sebi/issuance-calendar", summary="Upcoming bond issuances")
async def get_issuance_calendar(
    limit: int = Query(30, ge=1, le=500),
    user: dict = Depends(require_role(Roles.VIEWER)),
):
    await SEBIApiSync.initialize()
    return {"issuances": await SEBIApiSync.get_issuance_calendar(limit=limit)}


@router.get("/sebi/sync-log", summary="SEBI sync history")
async def get_sebi_sync_log(user: dict = Depends(require_role(Roles.AUDITOR))):
    await SEBIApiSync.initialize()
    return {"log": await SEBIApiSync.get_sync_log()}


# ════════════════════════════════════════════════════════════════════════════
# FOUR-EYES APPROVAL WORKFLOW
# ════════════════════════════════════════════════════════════════════════════

class ChangeRequestBody(BaseModel):
    request_type: str = "IPS_RULE"    # IPS_RULE | THRESHOLD | LIMIT
    rule_key:     str
    old_value:    Optional[Any] = None
    new_value:    Any
    justification: str


class ResolveBody(BaseModel):
    decision:  str      # APPROVE | REJECT
    comment:   str = ""


@router.post("/four-eyes/submit", summary="Submit an IPS rule change for dual approval")
async def submit_change_request(
    body: ChangeRequestBody,
    user: dict = Depends(require_role(Roles.PORTFOLIO)),
):
    await FourEyesApprovalService.initialize()
    initiator = user.get("sub") or user.get("username", "unknown")
    record = await FourEyesApprovalService.submit_request(
        initiator=initiator,
        request_type=body.request_type,
        rule_key=body.rule_key,
        old_value=body.old_value,
        new_value=body.new_value,
        justification=body.justification,
    )
    return {"success": True, "request": record}


@router.post("/four-eyes/{request_id}/resolve", summary="Approve or reject a change request")
async def resolve_change_request(
    request_id: int,
    body: ResolveBody,
    user: dict = Depends(require_role(Roles.COMPLIANCE)),
):
    await FourEyesApprovalService.initialize()
    approver = user.get("sub") or user.get("username", "unknown")
    try:
        updated = await FourEyesApprovalService.resolve_request(
            request_id=request_id,
            approver=approver,
            decision=body.decision,
            comment=body.comment,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"success": True, "request": updated}


@router.get("/four-eyes/pending", summary="List pending IPS change requests")
async def list_pending_requests(user: dict = Depends(require_role(Roles.COMPLIANCE))):
    await FourEyesApprovalService.initialize()
    return {"pending": await FourEyesApprovalService.list_pending()}


@router.get("/four-eyes/all", summary="Full four-eyes request history")
async def list_all_requests(
    limit: int = Query(100, ge=1, le=1000),
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    await FourEyesApprovalService.initialize()
    return {"requests": await FourEyesApprovalService.list_all(limit=limit)}


@router.get("/four-eyes/audit", summary="Tamper-proof four-eyes audit trail")
async def four_eyes_audit(user: dict = Depends(require_role(Roles.AUDITOR))):
    await FourEyesApprovalService.initialize()
    return {"audit": await FourEyesApprovalService.get_audit_trail()}


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

def _safe_float(val) -> Optional[float]:
    try:
        return float(str(val).replace(",", "").strip()) if val is not None and str(val).strip() else None
    except (ValueError, TypeError):
        return None


async def _ensure_portfolio_table():
    await execute_query(
        """CREATE TABLE IF NOT EXISTS portfolio_positions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            unique_lot_id   TEXT,
            trust_id        TEXT    NOT NULL,
            portfolio_id    TEXT    NOT NULL,
            isin            TEXT    NOT NULL,
            purchase_date   TEXT,
            quantity        REAL    NOT NULL,
            purchase_price  REAL,
            face_value      REAL,
            book_value      REAL,
            market_price    REAL,
            clean_price     REAL,
            accrued_interest REAL,
            source_sheet_name TEXT,
            source_row_number INTEGER,
            source_name     TEXT,
            source_type     TEXT,
            ingestion_method TEXT,
            source_reference TEXT,
            timestamp_ingested TEXT,
            timestamp_effective TEXT
        )"""
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
        """CREATE TABLE IF NOT EXISTS portfolio_holdings (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            portfolio_id    TEXT    NOT NULL,
            isin            TEXT    NOT NULL,
            purchase_date   TEXT,
            quantity        REAL    NOT NULL,
            purchase_price  REAL    NOT NULL,
            face_value      REAL    NOT NULL DEFAULT 1000,
            source_name     TEXT,
            source_type     TEXT,
            ingestion_method TEXT,
            source_reference TEXT,
            timestamp_ingested TEXT,
            timestamp_effective TEXT,
            provenance_json TEXT
        )"""
    )
    await execute_query(
        """CREATE TABLE IF NOT EXISTS instrument_master (
            isin                 TEXT PRIMARY KEY,
            instrument_name      TEXT NOT NULL,
            issuer               TEXT NOT NULL,
            coupon_rate          REAL NOT NULL,
            maturity_date        TEXT NOT NULL,
            put_date             TEXT,
            call_date            TEXT,
            sector               TEXT,
            category             TEXT,
            rating_latest        TEXT,
            rating_agency        TEXT,
            payment_frequency    INTEGER NOT NULL DEFAULT 2,
            day_count_convention TEXT NOT NULL DEFAULT 'ACT/365',
            source_name          TEXT,
            source_type          TEXT,
            ingestion_method     TEXT,
            source_reference     TEXT,
            timestamp_ingested   TEXT,
            timestamp_effective  TEXT,
            provenance_json      TEXT
        )"""
    )
    await execute_query(
        """CREATE TABLE IF NOT EXISTS market_prices (
            isin    TEXT NOT NULL,
            date    TEXT NOT NULL,
            price   REAL NOT NULL,
            yield   REAL,
            source  TEXT,
            source_name TEXT,
            source_type TEXT,
            ingestion_method TEXT,
            source_reference TEXT,
            timestamp_ingested TEXT,
            timestamp_effective TEXT,
            provenance_json TEXT,
            UNIQUE(isin, date, source)
        )"""
    )
