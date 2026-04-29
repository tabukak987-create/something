"""
IM Checker Router — Unified Gateway
====================================
CRUD for IM data + financial data + heuristic analysis for any bond.

Endpoints:
  GET  /api/v1/im-checker/search?q=...       search bonds (ISIN / issuer)
  GET  /api/v1/im-checker/{isin}             get stored IM data
  POST /api/v1/im-checker/{isin}             save / update IM data
  GET  /api/v1/im-checker/{isin}/heuristics  run 24 heuristics
  GET  /api/v1/im-checker/{isin}/financials  get financial data
  POST /api/v1/im-checker/{isin}/financials  save financial data
  GET  /api/v1/im-checker/{isin}/summary     full summary (IM + financials + heuristics)
"""

from __future__ import annotations

import asyncio
import html
import json
import io
import time
from datetime import date, datetime
from typing import Optional, Any, List
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, UploadFile, File, Body, Depends
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from ..shared.database import fetch_all, fetch_one, execute_query
from ..shared.auth import require_role, Roles
from ..shared.logger import get_logger
from ..shared.time_utils import utcnow_iso
from ..domain.risk.im_heuristics import IMHeuristicsEngine
from ..domain.market_data import im_fetcher_service as _fetcher
from ..domain.market_data.im_pipeline import IMPipeline
from ..domain.portfolio.im_connectors import list_connectors, IM_STORAGE_DIR
from ..domain.portfolio.im_institutional_engine import (
    get_institutional_analysis,
    extract_pages_from_pdf,
    detect_sections,
    extract_bond_metadata,
    run_clause_agents,
    enrich_clauses_with_llm,
    extract_covenants,
    compute_institutional_risk_score,
    _run_ocr_pipeline,
)

router = APIRouter(
    prefix="/api/v1/im-checker",
    tags=["IM Checker"],
    dependencies=[Depends(require_role(Roles.VIEWER))],
)

# ─── Constants ─────────────────────────────────────────────────────────────

RATING_ORDER = {
    "AAA": 20, "AA+": 19, "AA": 18, "AA-": 17, "A+": 16, "A": 15, "A-": 14,
    "BBB+": 13, "BBB": 12, "BBB-": 11, "BB+": 10, "BB": 9, "BB-": 8,
    "B+": 7, "B": 6, "B-": 5, "C": 4, "D": 0
}

log = get_logger("im_checker")


def _log_im_event(event: str, **fields: Any) -> None:
    """Structured IM checker logs with basic sensitive-data redaction."""
    redacted_keys = {
        "token", "authorization", "password", "full_text", "im_full_text",
        "override_text", "file_bytes", "raw_text", "clean_text",
    }
    safe: dict[str, Any] = {}
    for key, value in fields.items():
        if key.lower() in redacted_keys:
            safe[key] = "[redacted]"
            continue
        if isinstance(value, str) and len(value) > 240:
            safe[key] = value[:240] + "..."
            continue
        safe[key] = value
    log.info(event, module="im_checker", **safe)

# ─── Pydantic Models ─────────────────────────────────────────────────────────

class IMDataInput(BaseModel):
    object_of_issue:         Optional[str]   = None
    security_structure:      Optional[str]   = None
    charge_type:             Optional[str]   = "FIRST"   # FIRST|SECOND|PARI_PASSU|UNSECURED
    redemption_structure:    Optional[str]   = "BULLET"  # BULLET|AMORTIZING|SINKING|STRUCTURED
    covenants_financial:     Optional[Any]   = None      # list[str] or str
    covenants_non_financial: Optional[Any]   = None
    dscr_requirement:        Optional[float] = None
    interest_coverage_req:   Optional[float] = None
    debt_ebitda_limit:       Optional[float] = None
    call_option_details:     Optional[str]   = None
    put_option_details:      Optional[str]   = None
    step_up_clauses:         Optional[str]   = None
    default_clauses:         Optional[str]   = None
    cross_default_triggers:  Optional[str]   = None
    trustee_name:            Optional[str]   = None
    trustee_contact:         Optional[str]   = None
    escrow_mechanism:        Optional[str]   = None
    waterfall_structure:     Optional[str]   = None
    payment_mechanism:       Optional[str]   = None
    guarantee_details:       Optional[str]   = None
    im_full_text:            Optional[str]   = None


class FinancialDataInput(BaseModel):
    issuer_name:             Optional[str]   = None   # inferred from ISIN if not given
    financial_year:          str             = "FY2025"
    revenue_cr:              Optional[float] = None
    ebitda_cr:               Optional[float] = None
    net_profit_cr:           Optional[float] = None
    interest_expense_cr:     Optional[float] = None
    total_debt_cr:           Optional[float] = None
    cash_cr:                 Optional[float] = None
    total_assets_cr:         Optional[float] = None
    net_worth_cr:            Optional[float] = None
    current_assets_cr:       Optional[float] = None
    current_liabilities_cr:  Optional[float] = None
    cfo_cr:                  Optional[float] = None
    capex_cr:                Optional[float] = None
    data_source:             Optional[str]   = "MANUAL"


class ClauseFeedbackInput(BaseModel):
    status: str
    comment: Optional[str] = None
    override_text: Optional[str] = None


# ─── Helpers ────────────────────────────────────────────────────────────────

def _compute_ratios(d: dict) -> dict:
    """Auto-compute financial ratios from raw inputs."""
    def _to_num(v: Any) -> Optional[float]:
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    ebitda = _to_num(d.get("ebitda_cr"))
    interest = _to_num(d.get("interest_expense_cr"))
    debt = _to_num(d.get("total_debt_cr"))
    cfo = _to_num(d.get("cfo_cr"))
    net_worth = _to_num(d.get("net_worth_cr"))
    net_profit = _to_num(d.get("net_profit_cr"))
    cur_assets = _to_num(d.get("current_assets_cr"))
    cur_liab = _to_num(d.get("current_liabilities_cr"))

    d["debt_ebitda"] = round(debt / ebitda, 2) if (debt is not None and ebitda not in (None, 0)) else None
    d["interest_coverage"] = round(ebitda / interest, 2) if (ebitda is not None and interest not in (None, 0)) else None
    d["dscr"] = round(cfo / interest, 2) if (cfo is not None and interest not in (None, 0)) else None
    d["current_ratio"] = round(cur_assets / cur_liab, 2) if (cur_assets is not None and cur_liab not in (None, 0)) else None
    d["roe"] = round(net_profit / net_worth * 100, 1) if (net_profit is not None and net_worth not in (None, 0)) else None
    return d


def _financial_health_score(fin: dict) -> dict:
    """Simple 0-100 financial health score."""
    score = 0
    details = []
    # DSCR (≥1.5 = 25pts, ≥1.2 = 15pts)
    dscr = fin.get("dscr") or fin.get("interest_coverage")
    if dscr:
        pts = 25 if dscr >= 1.5 else (15 if dscr >= 1.2 else (5 if dscr >= 1.0 else 0))
        score += pts; details.append(f"DSCR/ICR {dscr:.2f} → {pts}pts")
    # D/EBITDA (≤2 = 25pts, ≤4 = 15pts, ≤6 = 5pts)
    de = fin.get("debt_ebitda")
    if de is not None:
        pts = 25 if de <= 2 else (15 if de <= 4 else (5 if de <= 6 else 0))
        score += pts; details.append(f"D/EBITDA {de:.2f} → {pts}pts")
    # Current ratio (≥2 = 20pts, ≥1.5 = 12pts, ≥1 = 6pts)
    cr = fin.get("current_ratio")
    if cr is not None:
        pts = 20 if cr >= 2 else (12 if cr >= 1.5 else (6 if cr >= 1.0 else 0))
        score += pts; details.append(f"Current ratio {cr:.2f} → {pts}pts")
    # Interest coverage (≥3 = 20pts, ≥2 = 12pts)
    icr = fin.get("interest_coverage")
    if icr:
        pts = 20 if icr >= 3 else (12 if icr >= 2 else (5 if icr >= 1 else 0))
        score += pts; details.append(f"ICR {icr:.2f} → {pts}pts")
    # ROE (≥15% = 10pts, ≥10% = 5pts)
    roe = fin.get("roe")
    if roe is not None:
        pts = 10 if roe >= 15 else (5 if roe >= 10 else 0)
        score += pts; details.append(f"ROE {roe:.1f}% → {pts}pts")
    level = "STRONG" if score >= 70 else ("ADEQUATE" if score >= 45 else ("WEAK" if score >= 20 else "DISTRESSED"))
    return {"score": min(score, 100), "level": level, "details": details}


# ─── Endpoints ──────────────────────────────────────────────────────────────

@router.get("/search", dependencies=[Depends(require_role(Roles.VIEWER))])
async def search_bonds(q: Optional[str] = Query(None)):
    """Search bonds by ISIN or issuer name."""
    if not q or not q.strip():
        return {"results": [], "count": 0}

    param = f"%{q.upper()}%"
    rows = await fetch_all(
        """SELECT isin, issuer_name, bond_name, credit_rating, maturity_date,
                  coupon_rate, issuer_type, is_pf_eligible
           FROM universal_bond_master
           WHERE isin LIKE :p OR UPPER(issuer_name) LIKE :p OR UPPER(bond_name) LIKE :p
           LIMIT 20""",
        {"p": param}
    )
    return {"results": rows, "count": len(rows)}


# ─── Document Library Endpoints ──────────────────────────────────────────────

@router.get("/documents", dependencies=[Depends(require_role(Roles.VIEWER))])
async def list_documents(
    source: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    page: int = Query(1, ge=1, le=100000),
    page_size: int = Query(25, ge=1, le=200),
):
    """
    List scraped IM documents stored in im_documents.
    Filter by source (SEBI|BSE|NSE), extraction_status, or keyword search.
    """
    conditions = []
    params: dict = {}
    if source:
        conditions.append("source = :source")
        params["source"] = source.upper()
    if status:
        conditions.append("extraction_status = :status")
        params["status"] = status.upper()
    if q:
        conditions.append("(UPPER(doc_title) LIKE :q OR UPPER(issuer_name) LIKE :q OR isin LIKE :q)")
        params["q"] = f"%{q.upper()}%"
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    offset = (page - 1) * page_size
    params["limit"] = page_size
    params["offset"] = offset

    rows = await fetch_all(
        f"""SELECT id, isin, issuer_name, doc_title, source, doc_type,
                   filing_date, extraction_status, text_length,
                   heuristic_score, created_at
            FROM im_documents {where}
            ORDER BY filing_date DESC, created_at DESC
            LIMIT :limit OFFSET :offset""",
        params
    )
    # Count total
    count_params = {k: v for k, v in params.items() if k not in ("limit", "offset")}
    total_row = await fetch_one(
        f"SELECT COUNT(*) as cnt FROM im_documents {where}", count_params
    )
    total = total_row["cnt"] if total_row else 0
    return {
        "documents": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
    }


@router.get("/documents/{doc_id}", dependencies=[Depends(require_role(Roles.VIEWER))])
async def get_document(doc_id: int):
    """Get full details of a stored IM document including extracted text."""
    row = await fetch_one("SELECT * FROM im_documents WHERE id = :id", {"id": doc_id})
    if not row:
        raise HTTPException(404, detail=f"Document {doc_id} not found")
    # Truncate full_text in listing to 5000 chars — caller can ask for more separately
    if row.get("full_text") and len(row["full_text"]) > 5000:
        row["full_text_preview"] = row["full_text"][:5000] + "…"
        row["full_text"] = None  # omit in default response — use /text endpoint
    return row


@router.get("/documents/{doc_id}/text", dependencies=[Depends(require_role(Roles.VIEWER))])
async def get_document_text(doc_id: int):
    """Return the full extracted text of a document."""
    row = await fetch_one(
        "SELECT id, doc_title, full_text, text_length FROM im_documents WHERE id = :id",
        {"id": doc_id}
    )
    if not row:
        raise HTTPException(404, detail=f"Document {doc_id} not found")
    return {"id": doc_id, "doc_title": row.get("doc_title"), "full_text": row.get("full_text"),
            "text_length": row.get("text_length")}


@router.post("/documents/{doc_id}/analyse", dependencies=[Depends(require_role(Roles.PORTFOLIO))])
async def analyse_document(doc_id: int):
    """
    Run IM heuristics on a stored document's extracted text.
    If the document is linked to an ISIN in the bond master, uses the full
    24-heuristic engine. Otherwise, runs text-only heuristics on the raw PDF text.
    """
    row = await fetch_one(
        "SELECT id, isin, doc_title, full_text, extraction_status FROM im_documents WHERE id = :id",
        {"id": doc_id}
    )
    if not row:
        raise HTTPException(404, detail=f"Document {doc_id} not found")
    if row.get("extraction_status") != "DONE":
        raise HTTPException(400, detail=f"Document text not yet extracted (status={row.get('extraction_status')}). "
                                        "Run a fetch job or re-process this document first.")

    _log_im_event("analyse_document_start", doc_id=doc_id, isin=row.get("isin"), extraction_status=row.get("extraction_status"))

    isin = row.get("isin")
    full_text = row.get("full_text", "")
    result = {}

    if isin:
        # Full heuristic engine (uses DB data + text)
        engine = IMHeuristicsEngine()
        result = await engine.run_all_heuristics(isin)
        if "error" not in result:
            score = result.get("hidden_risk_score", 0)
            await execute_query(
                "UPDATE im_documents SET heuristic_score = :s, heuristic_run_at = :at WHERE id = :id",
                {"s": score, "at": utcnow_iso(), "id": doc_id}
            )
    else:
        # Text-only basic analysis
        result = _text_only_risk_scan(full_text, row.get("doc_title", ""))
        await execute_query(
            "UPDATE im_documents SET heuristic_score = :s, heuristic_run_at = :at WHERE id = :id",
            {"s": result.get("risk_score", 0), "at": utcnow_iso(), "id": doc_id}
        )

    _log_im_event("analyse_document_done", doc_id=doc_id, isin=isin, risk_score=result.get("hidden_risk_score") or result.get("risk_score"))

    return {"doc_id": doc_id, "isin": isin, "analysis": result}


def _text_only_risk_scan(text: str, title: str = "") -> dict:
    """
    Quick keyword-based risk scan for documents without a matched ISIN.
    Returns a simple risk summary.
    """
    text_lower = text.lower()
    flags = []
    score = 0

    HIGH_RISK_KEYWORDS = [
        ("litigation", "H_LIT"),
        ("winding up", "H_WINDING"),
        ("default", "H_DEFAULT"),
        ("arrested", "H_CRIMINAL"),
        ("criminal", "H_CRIMINAL"),
        ("insolvency", "H_INSOLVENCY"),
        ("npa", "H_NPA"),
        ("overdue", "H_OVERDUE"),
        ("wilful defaulter", "H_WILFUL"),
        ("restructured", "H_RESTRUCTURED"),
    ]
    MEDIUM_RISK_KEYWORDS = [
        ("unsecured", "M_UNSECURED"),
        ("pledge", "M_PLEDGE"),
        ("guarantor", "M_GUARANTOR"),
        ("waiver", "M_WAIVER"),
        ("amendment", "M_AMENDMENT"),
        ("extension", "M_EXTENSION"),
        ("deferment", "M_DEFERMENT"),
    ]
    POSITIVE_KEYWORDS = [
        ("aaa", "P_AAA"),
        ("secured", "P_SECURED"),
        ("escrow", "P_ESCROW"),
        ("oversubscribed", "P_OVERSUB"),
        ("rated", "P_RATED"),
    ]

    for kw, code in HIGH_RISK_KEYWORDS:
        cnt = text_lower.count(kw)
        if cnt > 0:
            flags.append({"code": code, "keyword": kw, "occurrences": cnt, "severity": "HIGH"})
            score += min(cnt * 5, 20)

    for kw, code in MEDIUM_RISK_KEYWORDS:
        cnt = text_lower.count(kw)
        if cnt > 0:
            flags.append({"code": code, "keyword": kw, "occurrences": cnt, "severity": "MEDIUM"})
            score += min(cnt * 2, 8)

    for kw, code in POSITIVE_KEYWORDS:
        cnt = text_lower.count(kw)
        if cnt > 0:
            score = max(0, score - 3)

    risk_level = "CRITICAL" if score >= 50 else ("HIGH" if score >= 30 else ("MEDIUM" if score >= 15 else "LOW"))
    return {
        "risk_score": min(score, 100),
        "risk_level": risk_level,
        "flags": flags,
        "note": "Text-only scan (no ISIN match) — limited to keyword analysis",
    }


# ─── Fetch Trigger Endpoints ──────────────────────────────────────────────────

@router.post("/fetch/sebi", dependencies=[Depends(require_role(Roles.COMPLIANCE))])
async def trigger_sebi_fetch(
    background_tasks: BackgroundTasks,
    max_pages: int = Query(3, ge=1, le=50, description="Number of SEBI listing pages to scrape (25 docs/page)"),
    max_docs: int = Query(50, ge=1, le=2000, description="Max documents to process"),
    extract_pdf: bool = Query(True, description="Whether to download and extract PDF text"),
):
    """
    Trigger background scrape of SEBI debt offer document listing.
    Returns a job_id to poll for status.
    """
    job_id = f"SEBI_{int(time.time())}"
    _log_im_event("fetch_start", source="SEBI", job_id=job_id, max_pages=max_pages, max_docs=max_docs, extract_pdf=extract_pdf)
    background_tasks.add_task(
        _fetcher.run_fetch_job,
        source="SEBI",
        max_pages=max_pages,
        max_docs=max_docs,
        extract_pdf=extract_pdf,
        job_id=job_id,
    )
    return {
        "status": "started",
        "job_id": job_id,
        "message": f"Scraping up to {max_pages} SEBI pages ({max_docs} docs max). Poll /fetch/status/{job_id} for progress.",
    }


@router.post("/fetch/bse", dependencies=[Depends(require_role(Roles.COMPLIANCE))])
async def trigger_bse_fetch(
    background_tasks: BackgroundTasks,
    max_pages: int = Query(10, ge=1, le=50, description="Number of BSE listing pages to scrape"),
    max_docs: int = Query(250, ge=1, le=2000),
    extract_pdf: bool = Query(False, description="BSE docs are ZIP files — set False for metadata-only"),
):
    """Trigger background scrape of BSE debt memorandum listing."""
    job_id = f"BSE_{int(time.time())}"
    _log_im_event("fetch_start", source="BSE", job_id=job_id, max_pages=max_pages, max_docs=max_docs, extract_pdf=extract_pdf)
    background_tasks.add_task(
        _fetcher.run_fetch_job,
        source="BSE",
        max_pages=max_pages,
        max_docs=max_docs,
        extract_pdf=extract_pdf,
        job_id=job_id,
    )
    return {"status": "started", "job_id": job_id,
            "message": f"BSE fetch started (metadata-only={not extract_pdf}). Poll /fetch/status/{job_id}."}


@router.post("/fetch/nse", dependencies=[Depends(require_role(Roles.COMPLIANCE))])
async def trigger_nse_fetch(
    background_tasks: BackgroundTasks,
    max_docs: int = Query(100, ge=1, le=1000),
):
    """Trigger background scrape of NSE corporate offer documents."""
    job_id = f"NSE_{int(time.time())}"
    _log_im_event("fetch_start", source="NSE", job_id=job_id, max_docs=max_docs)
    background_tasks.add_task(
        _fetcher.run_fetch_job,
        source="NSE",
        max_pages=1,
        max_docs=max_docs,
        extract_pdf=False,
        job_id=job_id,
    )
    return {"status": "started", "job_id": job_id,
            "message": f"NSE fetch started. Poll /fetch/status/{job_id}."}


@router.get("/fetch/status", dependencies=[Depends(require_role(Roles.VIEWER))])
async def get_all_fetch_status():
    """List all fetch job statuses (in-memory, cleared on server restart)."""
    return {"jobs": _fetcher.list_all_jobs()}


@router.get("/fetch/jobs", dependencies=[Depends(require_role(Roles.VIEWER))])
async def get_fetch_jobs(limit: int = Query(100, ge=1, le=1000)):
    """Alias for active fetch jobs with optional cap used by UI/clients."""
    jobs = _fetcher.list_all_jobs() or []
    return {"jobs": jobs[:limit]}


@router.get("/fetch/status/{job_id}", dependencies=[Depends(require_role(Roles.VIEWER))])
async def get_fetch_status(job_id: str):
    """Get status of a specific fetch job."""
    job = _fetcher.get_job_status(job_id)
    if not job:
        raise HTTPException(404, detail=f"Job {job_id} not found (may have been cleared by server restart)")
    return job


@router.get("/fetch/history", dependencies=[Depends(require_role(Roles.VIEWER))])
async def get_fetch_history(limit: int = Query(20, ge=1, le=500)):
    """Get the last N fetch log entries from the database."""
    rows = await fetch_all(
        "SELECT * FROM im_fetch_log ORDER BY fetched_at DESC LIMIT :limit",
        {"limit": limit}
    )
    return {"history": rows}


@router.post("/documents/{doc_id}/reprocess", dependencies=[Depends(require_role(Roles.PORTFOLIO))])
async def reprocess_document(doc_id: int, background_tasks: BackgroundTasks):
    """Re-trigger PDF download + text extraction for a document (e.g. after a FAILED status)."""
    row = await fetch_one(
        "SELECT id, doc_url, pdf_url, source FROM im_documents WHERE id = :id",
        {"id": doc_id}
    )
    if not row:
        raise HTTPException(404, detail=f"Document {doc_id} not found")
    meta = dict(row)

    async def _reprocess():
        async with __import__("httpx").AsyncClient(
            headers=_fetcher.BROWSER_HEADERS, timeout=60, follow_redirects=True
        ) as client:
            await _fetcher.process_document(meta, client, extract_pdf=True)

    background_tasks.add_task(_reprocess)
    return {"status": "reprocessing", "doc_id": doc_id}


# ═══════════════════════════════════════════════════════════════════════════════
# IM Upload System — Full Pipeline (Manual Upload + Connector Layer)
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/upload", dependencies=[Depends(require_role(Roles.PORTFOLIO))])
async def upload_im_document(
    isin: str = Query(..., description="ISIN of the bond"),
    file: UploadFile = File(...),
    issuer_name: Optional[str] = Query(None),
):
    """
    Upload an IM PDF through the full processing pipeline.
    Steps: Validate → Checksum → Store → Extract → NLP → Risk → DB → Audit
    Admin-only endpoint.
    """
    isin = isin.upper()
    contents = await file.read()
    _log_im_event("upload_start", route="/upload", isin=isin, file_name=file.filename or "document.pdf", file_size=len(contents), issuer_name=issuer_name)

    pipeline = IMPipeline()
    result = await pipeline.process_upload(
        isin=isin,
        file_bytes=contents,
        filename=file.filename or "document.pdf",
        uploaded_by="system",
        issuer_name=issuer_name,
    )

    if result["status"] == "error":
        _log_im_event("upload_failed", route="/upload", isin=isin, errors=result.get("errors", []))
        raise HTTPException(400, detail={"status": "error", "errors": result.get("errors", [])})

    _log_im_event("upload_done", route="/upload", isin=isin, upload_id=result.get("upload_id"), status=result.get("status"))

    return result


@router.get("/uploads", dependencies=[Depends(require_role(Roles.VIEWER))])
async def list_uploads(
    isin: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    active_only: bool = Query(False),
):
    """List all IM uploads, optionally filtered by ISIN."""
    conditions = []
    params = {}

    if isin:
        conditions.append("isin = :isin")
        params["isin"] = isin.upper()
    if active_only:
        conditions.append("is_active = 1")

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    offset = (page - 1) * page_size
    params["limit"] = page_size
    params["offset"] = offset

    total_row = await fetch_one(f"SELECT COUNT(*) AS cnt FROM bond_im_uploads {where}", params)
    total = total_row["cnt"] if total_row else 0

    rows = await fetch_all(
        f"""SELECT id, isin, issuer_name, file_name, file_size, checksum,
                   version, is_active, upload_source, uploaded_by,
                   coupon, maturity_date, issue_size_cr, face_value,
                   credit_rating, rating_agency, call_option, put_option,
                   security_type, seniority, extraction_status,
                   risk_score, risk_level, risk_flags, text_length,
                   created_at, updated_at
            FROM bond_im_uploads {where}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset""",
        params,
    )

    for r in rows:
        if r.get("risk_flags") and isinstance(r["risk_flags"], str):
            try:
                r["risk_flags"] = json.loads(r["risk_flags"])
            except Exception:
                pass

    return {
        "uploads": rows,
        "total": total,
        "page": page,
        "pages": (total + page_size - 1) // page_size if total else 0,
    }


@router.get("/uploads/isin/{isin_code}", dependencies=[Depends(require_role(Roles.VIEWER))])
async def get_uploads_by_isin(isin_code: str):
    """Get all uploads for a specific ISIN with extracted data."""
    isin = isin_code.upper()
    uploads = await fetch_all(
        """SELECT u.*, e.covenants, e.risk_factors,
                  e.cross_default_clause, e.change_of_control,
                  e.acceleration_clause, e.negative_pledge,
                  e.step_up_triggers, e.event_of_default,
                  e.promoter_guarantee, e.liquidity_risk,
                  e.security_charge_ranking
           FROM bond_im_uploads u
           LEFT JOIN im_extracted_data e ON e.upload_id = u.id
           WHERE u.isin = :isin ORDER BY u.version DESC""",
        {"isin": isin},
    )

    for r in uploads:
        for f in ("risk_flags", "covenants", "risk_factors", "step_up_triggers", "debt_covenants"):
            if r.get(f) and isinstance(r[f], str):
                try:
                    r[f] = json.loads(r[f])
                except Exception:
                    pass

    return {"isin": isin, "uploads": uploads, "total": len(uploads)}


@router.get("/uploads/{upload_id}/versions", dependencies=[Depends(require_role(Roles.VIEWER))])
async def get_upload_versions(upload_id: int):
    """Get version history for the ISIN of this upload."""
    upload = await fetch_one("SELECT isin FROM bond_im_uploads WHERE id = :id", {"id": upload_id})
    if not upload:
        raise HTTPException(404, detail="Upload not found")

    versions = await fetch_all(
        """SELECT id, version, is_active, file_name, file_size, checksum,
                  upload_source, uploaded_by, risk_score, risk_level,
                  extraction_status, created_at
           FROM bond_im_uploads
           WHERE isin = :isin ORDER BY version DESC""",
        {"isin": upload["isin"]},
    )
    return {"isin": upload["isin"], "versions": versions, "total": len(versions)}


@router.post("/uploads/{upload_id}/reprocess", dependencies=[Depends(require_role(Roles.PORTFOLIO))])
async def reprocess_upload(upload_id: int):
    """Re-run text extraction and risk analysis on an existing upload."""
    pipeline = IMPipeline()
    _log_im_event("upload_reprocess_start", upload_id=upload_id)
    result = await pipeline.reprocess_upload(upload_id, actor="admin")
    if result["status"] == "error":
        _log_im_event("upload_reprocess_failed", upload_id=upload_id, errors=result.get("errors", []))
        raise HTTPException(400, detail=result.get("errors", ["Reprocess failed"]))
    _log_im_event("upload_reprocess_done", upload_id=upload_id, status=result.get("status"))
    return result


@router.get("/uploads/{upload_id}/integrity", dependencies=[Depends(require_role(Roles.VIEWER))])
async def check_upload_integrity(upload_id: int):
    """Cross-check uploaded IM data against bond master data."""
    pipeline = IMPipeline()
    _log_im_event("integrity_check_start", upload_id=upload_id)
    result = await pipeline.check_integrity(upload_id)
    if result["status"] == "error":
        _log_im_event("integrity_check_failed", upload_id=upload_id, errors=result.get("errors", []))
        raise HTTPException(400, detail=result.get("errors", ["Integrity check failed"]))
    _log_im_event("integrity_check_done", upload_id=upload_id, integrity_score=result.get("integrity_score"))
    return result


@router.get("/uploads/{upload_id}", dependencies=[Depends(require_role(Roles.VIEWER))])
async def get_upload_detail(upload_id: int):
    """Get full details of an IM upload including extracted data."""
    upload = await fetch_one("SELECT * FROM bond_im_uploads WHERE id = :id", {"id": upload_id})
    if not upload:
        raise HTTPException(404, detail="Upload not found")

    for f in ("risk_flags",):
        if upload.get(f) and isinstance(upload[f], str):
            try:
                upload[f] = json.loads(upload[f])
            except Exception:
                pass

    extracted = await fetch_one(
        "SELECT * FROM im_extracted_data WHERE upload_id = :uid", {"uid": upload_id}
    )
    if extracted:
        for f in ("covenants", "risk_factors", "step_up_triggers", "debt_covenants"):
            if extracted.get(f) and isinstance(extracted[f], str):
                try:
                    extracted[f] = json.loads(extracted[f])
                except Exception:
                    pass

    audit = await fetch_all(
        "SELECT * FROM im_audit_log WHERE upload_id = :uid ORDER BY created_at DESC",
        {"uid": upload_id},
    )

    return {
        "upload": upload,
        "extracted_data": extracted,
        "audit_trail": audit,
    }


@router.get("/uploads/{upload_id}/institutional", dependencies=[Depends(require_role(Roles.VIEWER))])
async def get_upload_institutional(upload_id: int):
    """Get institutional-grade analysis for an IM upload.

    Returns bond metadata, clause detection results with evidence,
    covenants, section mapping, and risk scoring breakdown.
    """
    upload = await fetch_one("SELECT id, isin FROM bond_im_uploads WHERE id = :id", {"id": upload_id})
    if not upload:
        raise HTTPException(404, detail="Upload not found")

    analysis = await get_institutional_analysis(upload_id)
    return analysis


@router.get("/uploads/{upload_id}/file", dependencies=[Depends(require_role(Roles.VIEWER))])
async def get_upload_file(upload_id: int):
    """Stream the original uploaded IM PDF for preview/download."""
    upload = await fetch_one(
        "SELECT id, file_path, file_name FROM bond_im_uploads WHERE id = :id",
        {"id": upload_id},
    )
    if not upload:
        raise HTTPException(404, detail="Upload not found")

    full_path = (IM_STORAGE_DIR / upload["file_path"]).resolve()
    if not str(full_path).startswith(str(IM_STORAGE_DIR.resolve())):
        raise HTTPException(403, detail="Access denied: path traversal detected")
    if not full_path.exists():
        raise HTTPException(404, detail="PDF file not found on disk")

    return FileResponse(
        str(full_path),
        media_type="application/pdf",
        filename=upload.get("file_name") or f"upload_{upload_id}.pdf",
    )


@router.get("/uploads/{upload_id}/institutional/ic-note", dependencies=[Depends(require_role(Roles.VIEWER))])
async def get_upload_ic_note(upload_id: int):
    """Generate a UI-ready IC note HTML view for the institutional dossier."""
    upload = await fetch_one(
        "SELECT id, isin, issuer_name, file_name FROM bond_im_uploads WHERE id = :id",
        {"id": upload_id},
    )
    if not upload:
        raise HTTPException(404, detail="Upload not found")

    analysis = await get_institutional_analysis(upload_id)
    meta = analysis.get("metadata") or {}
    risk = analysis.get("risk") or {}
    clauses = analysis.get("clauses") or []
    covenants = analysis.get("covenants") or []
    diagnostics = analysis.get("processing_log") or {}
    benchmark = analysis.get("sector_benchmark") or {}

    clause_rows = "".join(
        f"<tr><td>{html.escape((c.get('clause_type') or '').replace('_', ' '))}</td>"
        f"<td>{html.escape(c.get('status') or '')}</td>"
        f"<td>{c.get('page_number') or '–'}</td>"
        f"<td>{round(float(c.get('confidence') or 0) * 100)}%</td></tr>"
        for c in clauses
    ) or "<tr><td colspan='4'>No clauses available</td></tr>"

    covenant_rows = "".join(
        f"<tr><td>{html.escape(c.get('covenant_type') or '')}</td>"
        f"<td>{html.escape(c.get('covenant_name') or '')}</td>"
        f"<td>{html.escape(c.get('threshold') or '–')}</td>"
        f"<td>{c.get('page_number') or '–'}</td></tr>"
        for c in covenants
    ) or "<tr><td colspan='4'>No covenants available</td></tr>"

    html_doc = f"""
    <html>
    <head>
      <title>IC Note — {html.escape(upload.get('isin') or '')}</title>
      <style>
        body {{ font-family: Arial, sans-serif; margin: 28px; color: #0f172a; }}
        h1, h2 {{ margin-bottom: 8px; }}
        .muted {{ color: #64748b; }}
        .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 16px 0; }}
        .card {{ border: 1px solid #e2e8f0; border-radius: 12px; padding: 14px 16px; margin-bottom: 16px; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
        th, td {{ border: 1px solid #e2e8f0; padding: 8px 10px; text-align: left; font-size: 12px; }}
        th {{ background: #f8fafc; }}
        .pill {{ display: inline-block; padding: 4px 10px; border-radius: 999px; background: #eff6ff; color: #1d4ed8; font-weight: 700; }}
      </style>
    </head>
    <body>
      <h1>Investment Committee Note</h1>
      <div class="muted">Upload #{upload_id} • {html.escape(upload.get('file_name') or '')}</div>
      <div class="card">
        <h2>Executive Summary</h2>
        <div class="grid">
          <div><strong>Issuer:</strong> {html.escape(meta.get('issuer_name') or upload.get('issuer_name') or '–')}</div>
          <div><strong>ISIN:</strong> {html.escape(meta.get('isin') or upload.get('isin') or '–')}</div>
          <div><strong>Coupon:</strong> {html.escape(str(meta.get('coupon_rate') or '–'))}</div>
          <div><strong>Maturity:</strong> {html.escape(meta.get('maturity_date') or '–')}</div>
          <div><strong>Security:</strong> {html.escape(meta.get('security_type') or '–')}</div>
          <div><strong>Seniority:</strong> {html.escape(meta.get('seniority') or '–')}</div>
          <div><strong>Credit Rating:</strong> {html.escape(meta.get('credit_rating') or '–')}</div>
          <div><strong>Rating Agency:</strong> {html.escape(meta.get('rating_agency') or '–')}</div>
        </div>
        <div><span class="pill">Risk Score: {risk.get('risk_score') or 0} / {html.escape(risk.get('risk_level') or 'UNKNOWN')}</span></div>
      </div>
      <div class="card">
        <h2>Risk Comparison</h2>
        <div><strong>Peer Group:</strong> {html.escape(benchmark.get('group') or '–')}</div>
        <div><strong>Average Risk:</strong> {benchmark.get('avg_score') or '–'} | <strong>Delta:</strong> {benchmark.get('delta') or 0}</div>
      </div>
      <div class="card">
        <h2>Clause Matrix</h2>
        <table>
          <thead><tr><th>Clause</th><th>Status</th><th>Page</th><th>Confidence</th></tr></thead>
          <tbody>{clause_rows}</tbody>
        </table>
      </div>
      <div class="card">
        <h2>Covenants</h2>
        <table>
          <thead><tr><th>Type</th><th>Name</th><th>Threshold</th><th>Page</th></tr></thead>
          <tbody>{covenant_rows}</tbody>
        </table>
      </div>
      <div class="card">
        <h2>Diagnostics</h2>
        <div><strong>Parser Method:</strong> {html.escape(str(diagnostics.get('method_used') or '–'))}</div>
        <div><strong>Pages Processed:</strong> {diagnostics.get('pages_processed') or analysis.get('pages_count') or 0}</div>
        <div><strong>Text Length:</strong> {diagnostics.get('char_count') or (analysis.get('full_text_preview') or {}).get('text_length') or 0}</div>
        <div><strong>Processing Time:</strong> {diagnostics.get('processing_time') or '–'} sec</div>
      </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_doc)


@router.post(
    "/uploads/{upload_id}/institutional/clauses/{clause_type}/feedback",
    dependencies=[Depends(require_role(Roles.COMPLIANCE))],
)
async def submit_clause_feedback(upload_id: int, clause_type: str, payload: ClauseFeedbackInput):
    """Store analyst validation feedback for an institutional clause result."""
    clause_type = clause_type.upper()
    status = payload.status.upper().strip()
    if status not in {"PRESENT", "ABSENT", "INCONCLUSIVE"}:
        raise HTTPException(400, detail="status must be PRESENT, ABSENT, or INCONCLUSIVE")

    _log_im_event("clause_feedback_start", upload_id=upload_id, clause_type=clause_type, status=status)

    upload = await fetch_one(
        "SELECT id, isin FROM bond_im_uploads WHERE id = :id",
        {"id": upload_id},
    )
    if not upload:
        raise HTTPException(404, detail="Upload not found")

    clause = await fetch_one(
        "SELECT * FROM bond_clauses WHERE upload_id = :uid AND clause_type = :ctype",
        {"uid": upload_id, "ctype": clause_type},
    )
    if not clause:
        raise HTTPException(404, detail="Clause not found")

    await execute_query(
        """UPDATE bond_clauses
           SET status = :status,
               confidence = 1.0,
               clause_text = COALESCE(:override_text, clause_text)
           WHERE upload_id = :uid AND clause_type = :ctype""",
        {
            "uid": upload_id,
            "ctype": clause_type,
            "status": status,
            "override_text": payload.override_text,
        },
    )
    await execute_query(
        """INSERT INTO im_audit_log (upload_id, isin, action, actor, details, created_at)
           VALUES (:uid, :isin, 'CLAUSE_FEEDBACK', 'analyst', :details, :now)""",
        {
            "uid": upload_id,
            "isin": upload["isin"],
            "details": json.dumps({
                "clause_type": clause_type,
                "status": status,
                "comment": payload.comment,
                "override_text": payload.override_text,
            }),
            "now": utcnow_iso(),
        },
    )
    _log_im_event("clause_feedback_done", upload_id=upload_id, clause_type=clause_type, status=status)
    return {"status": "success", "upload_id": upload_id, "clause_type": clause_type, "updated_status": status}


@router.post(
    "/uploads/{upload_id}/institutional/rerun-clauses",
    dependencies=[Depends(require_role(Roles.COMPLIANCE))],
)
async def rerun_institutional_clauses(upload_id: int):
    """Re-run clause detection from stored text/pages without re-uploading the file."""
    upload = await fetch_one("SELECT id, isin FROM bond_im_uploads WHERE id = :id", {"id": upload_id})
    if not upload:
        raise HTTPException(404, detail="Upload not found")
    _log_im_event("rerun_clauses_start", upload_id=upload_id, isin=upload.get("isin"))

    pages = await fetch_all(
        "SELECT page_number, raw_text, clean_text, token_count FROM im_pages WHERE upload_id = :uid ORDER BY page_number",
        {"uid": upload_id},
    )
    sections = await fetch_all(
        "SELECT section_name, start_page, end_page, section_text, confidence FROM im_sections WHERE upload_id = :uid ORDER BY start_page",
        {"uid": upload_id},
    )
    text_row = await fetch_one(
        "SELECT full_text FROM im_full_text WHERE upload_id = :uid",
        {"uid": upload_id},
    )
    if not text_row or not text_row.get("full_text"):
        raise HTTPException(400, detail="No stored text available for clause rerun")

    clauses = run_clause_agents(text_row["full_text"], pages, sections)
    clauses = await enrich_clauses_with_llm(clauses, text_row["full_text"])

    await execute_query("DELETE FROM bond_clauses WHERE upload_id = :uid", {"uid": upload_id})
    await execute_query("DELETE FROM clause_evidence WHERE upload_id = :uid", {"uid": upload_id})
    for cl in clauses:
        await execute_query(
            """INSERT INTO bond_clauses (upload_id, clause_type, status, page_number, section, clause_text, confidence)
               VALUES (:uid, :ctype, :status, :page, :section, :text, :conf)""",
            {
                "uid": upload_id,
                "ctype": cl["clause_type"],
                "status": cl["status"],
                "page": cl.get("page_number"),
                "section": cl.get("section"),
                "text": cl.get("clause_text"),
                "conf": cl.get("confidence", 0.0),
            },
        )
        for ev in cl.get("evidence", []):
            await execute_query(
                """INSERT INTO clause_evidence (upload_id, clause_type, page_number, section, text_snippet, confidence)
                   VALUES (:uid, :ctype, :page, :section, :text, :conf)""",
                {
                    "uid": upload_id,
                    "ctype": cl["clause_type"],
                    "page": ev.get("page_number"),
                    "section": ev.get("section"),
                    "text": ev.get("text_snippet", ""),
                    "conf": ev.get("confidence", 0.0),
                },
            )
    await execute_query(
        """INSERT INTO im_audit_log (upload_id, isin, action, actor, details, created_at)
           VALUES (:uid, :isin, 'RERUN_CLAUSES', 'system', :details, :now)""",
        {
            "uid": upload_id,
            "isin": upload["isin"],
            "details": json.dumps({"clause_count": len(clauses)}),
            "now": utcnow_iso(),
        },
    )
    _log_im_event("rerun_clauses_done", upload_id=upload_id, clause_count=len(clauses))
    return {"status": "success", "upload_id": upload_id, "clauses": len(clauses)}


@router.post(
    "/uploads/{upload_id}/institutional/rerun-covenants",
    dependencies=[Depends(require_role(Roles.COMPLIANCE))],
)
async def rerun_institutional_covenants(upload_id: int):
    """Re-run covenant detection from stored text/pages without re-uploading the file."""
    upload = await fetch_one("SELECT id, isin FROM bond_im_uploads WHERE id = :id", {"id": upload_id})
    if not upload:
        raise HTTPException(404, detail="Upload not found")
    _log_im_event("rerun_covenants_start", upload_id=upload_id, isin=upload.get("isin"))

    pages = await fetch_all(
        "SELECT page_number, raw_text, clean_text, token_count FROM im_pages WHERE upload_id = :uid ORDER BY page_number",
        {"uid": upload_id},
    )
    sections = await fetch_all(
        "SELECT section_name, start_page, end_page, section_text, confidence FROM im_sections WHERE upload_id = :uid ORDER BY start_page",
        {"uid": upload_id},
    )
    text_row = await fetch_one(
        "SELECT full_text FROM im_full_text WHERE upload_id = :uid",
        {"uid": upload_id},
    )
    if not text_row or not text_row.get("full_text"):
        raise HTTPException(400, detail="No stored text available for covenant rerun")

    covenants = extract_covenants(text_row["full_text"], pages, sections)
    await execute_query("DELETE FROM bond_covenants WHERE upload_id = :uid", {"uid": upload_id})
    for cov in covenants:
        await execute_query(
            """INSERT INTO bond_covenants (upload_id, covenant_type, covenant_name, threshold, page_number, section, text, confidence)
               VALUES (:uid, :ctype, :cname, :thresh, :page, :section, :text, :conf)""",
            {
                "uid": upload_id,
                "ctype": cov["covenant_type"],
                "cname": cov["covenant_name"],
                "thresh": cov.get("threshold"),
                "page": cov.get("page_number"),
                "section": cov.get("section"),
                "text": cov.get("text"),
                "conf": cov.get("confidence", 0.0),
            },
        )
    await execute_query(
        """INSERT INTO im_audit_log (upload_id, isin, action, actor, details, created_at)
           VALUES (:uid, :isin, 'RERUN_COVENANTS', 'system', :details, :now)""",
        {
            "uid": upload_id,
            "isin": upload["isin"],
            "details": json.dumps({"covenant_count": len(covenants)}),
            "now": utcnow_iso(),
        },
    )
    _log_im_event("rerun_covenants_done", upload_id=upload_id, covenant_count=len(covenants))
    return {"status": "success", "upload_id": upload_id, "covenants": len(covenants)}


@router.post(
    "/uploads/{upload_id}/institutional/reprocess-ocr",
    dependencies=[Depends(require_role(Roles.COMPLIANCE))],
)
async def rerun_institutional_with_ocr(upload_id: int):
    """Force OCR extraction and rebuild the institutional analysis tables."""
    upload = await fetch_one("SELECT * FROM bond_im_uploads WHERE id = :id", {"id": upload_id})
    if not upload:
        raise HTTPException(404, detail="Upload not found")
    _log_im_event("rerun_ocr_start", upload_id=upload_id, isin=upload.get("isin"))

    full_path = (IM_STORAGE_DIR / upload["file_path"]).resolve()
    if not str(full_path).startswith(str(IM_STORAGE_DIR.resolve())):
        raise HTTPException(403, detail="Access denied: path traversal detected")
    if not full_path.exists():
        raise HTTPException(404, detail="PDF file not found on disk")

    file_bytes = full_path.read_bytes()
    pages = _run_ocr_pipeline(file_bytes)
    if not pages:
        raise HTTPException(400, detail="OCR extraction did not return any page text")

    full_text = "\n\n".join((p.get("clean_text") or p.get("raw_text") or "") for p in pages)
    sections = detect_sections(pages)
    metadata = extract_bond_metadata(full_text, pages)
    metadata["upload_id"] = upload_id
    metadata["isin"] = metadata.get("isin") or upload["isin"]
    clauses = await enrich_clauses_with_llm(run_clause_agents(full_text, pages, sections), full_text)
    covenants = extract_covenants(full_text, pages, sections)
    risk = compute_institutional_risk_score(metadata, clauses, covenants, metadata.get("credit_rating"))

    await execute_query("DELETE FROM im_pages WHERE upload_id = :uid", {"uid": upload_id})
    for pg in pages:
        await execute_query(
            """INSERT INTO im_pages (upload_id, page_number, raw_text, clean_text, token_count)
               VALUES (:uid, :pn, :raw, :clean, :tc)""",
            {
                "uid": upload_id,
                "pn": pg["page_number"],
                "raw": pg.get("raw_text"),
                "clean": pg.get("clean_text"),
                "tc": pg.get("token_count", 0),
            },
        )
    await execute_query("DELETE FROM im_full_text WHERE upload_id = :uid", {"uid": upload_id})
    await execute_query(
        "INSERT INTO im_full_text (upload_id, full_text, text_length) VALUES (:uid, :text, :length)",
        {"uid": upload_id, "text": full_text[:500000], "length": len(full_text)},
    )
    await execute_query("DELETE FROM im_sections WHERE upload_id = :uid", {"uid": upload_id})
    for sec in sections:
        await execute_query(
            """INSERT INTO im_sections (upload_id, section_name, start_page, end_page, section_text, confidence)
               VALUES (:uid, :name, :sp, :ep, :text, :conf)""",
            {
                "uid": upload_id,
                "name": sec["section_name"],
                "sp": sec.get("start_page"),
                "ep": sec.get("end_page"),
                "text": sec.get("section_text"),
                "conf": sec.get("confidence", 0),
            },
        )
    await execute_query("DELETE FROM bond_metadata WHERE upload_id = :uid", {"uid": upload_id})
    _ALLOWED_META_COLS = {"isin", "issuer_name", "bond_name", "coupon_rate", "maturity_date",
                          "face_value", "issue_size", "listing_date", "credit_rating",
                          "rating_agency", "rating_date", "sector", "instrument_type",
                          "put_call_features", "security_type", "issuer_category",
                          "covenants_financial", "covenants_non_financial", "upload_id"}
    meta_cols = [k for k, v in metadata.items() if v is not None and k in _ALLOWED_META_COLS]
    if meta_cols:
        await execute_query(
            f"INSERT INTO bond_metadata ({', '.join(meta_cols)}) VALUES ({', '.join(f':{k}' for k in meta_cols)})",
            {k: metadata[k] for k in meta_cols},
        )
    await execute_query("DELETE FROM bond_clauses WHERE upload_id = :uid", {"uid": upload_id})
    await execute_query("DELETE FROM clause_evidence WHERE upload_id = :uid", {"uid": upload_id})
    for cl in clauses:
        await execute_query(
            """INSERT INTO bond_clauses (upload_id, clause_type, status, page_number, section, clause_text, confidence)
               VALUES (:uid, :ctype, :status, :page, :section, :text, :conf)""",
            {
                "uid": upload_id,
                "ctype": cl["clause_type"],
                "status": cl["status"],
                "page": cl.get("page_number"),
                "section": cl.get("section"),
                "text": cl.get("clause_text"),
                "conf": cl.get("confidence", 0.0),
            },
        )
        for ev in cl.get("evidence", []):
            await execute_query(
                """INSERT INTO clause_evidence (upload_id, clause_type, page_number, section, text_snippet, confidence)
                   VALUES (:uid, :ctype, :page, :section, :text, :conf)""",
                {
                    "uid": upload_id,
                    "ctype": cl["clause_type"],
                    "page": ev.get("page_number"),
                    "section": ev.get("section"),
                    "text": ev.get("text_snippet"),
                    "conf": ev.get("confidence", 0.0),
                },
            )
    await execute_query("DELETE FROM bond_covenants WHERE upload_id = :uid", {"uid": upload_id})
    for cov in covenants:
        await execute_query(
            """INSERT INTO bond_covenants (upload_id, covenant_type, covenant_name, threshold, page_number, section, text, confidence)
               VALUES (:uid, :ctype, :cname, :thresh, :page, :section, :text, :conf)""",
            {
                "uid": upload_id,
                "ctype": cov["covenant_type"],
                "cname": cov["covenant_name"],
                "thresh": cov.get("threshold"),
                "page": cov.get("page_number"),
                "section": cov.get("section"),
                "text": cov.get("text"),
                "conf": cov.get("confidence", 0.0),
            },
        )
    await execute_query("DELETE FROM bond_risk_analysis WHERE upload_id = :uid", {"uid": upload_id})
    await execute_query(
        """INSERT INTO bond_risk_analysis
           (upload_id, isin, risk_score, risk_level,
            cross_default, negative_pledge, acceleration, change_of_control,
            security, guarantee, covenants,
            unsecured_pts, no_cross_default_pts, no_negative_pledge_pts,
            no_guarantee_pts, no_covenants_pts, low_rating_pts,
            high_coupon_pts, bullet_repayment_pts,
            red_flags)
           VALUES
           (:uid, :isin, :score, :level,
            :cd, :np, :accel, :coc,
            :sec, :guar, :covs,
            :p_unsec, :p_cd, :p_np,
            :p_guar, :p_cov, :p_rating,
            :p_coupon, :p_bullet,
            :flags)""",
        {
            "uid": upload_id,
            "isin": upload["isin"],
            "score": risk["risk_score"],
            "level": risk["risk_level"],
            "cd": risk["clause_presence"].get("cross_default", 0),
            "np": risk["clause_presence"].get("negative_pledge", 0),
            "accel": risk["clause_presence"].get("acceleration", 0),
            "coc": risk["clause_presence"].get("change_of_control", 0),
            "sec": risk["clause_presence"].get("security", 0),
            "guar": risk["clause_presence"].get("guarantee", 0),
            "covs": risk.get("covenant_count", 0),
            "p_unsec": risk["breakdown"].get("unsecured_pts", 0),
            "p_cd": risk["breakdown"].get("no_cross_default_pts", 0),
            "p_np": risk["breakdown"].get("no_negative_pledge_pts", 0),
            "p_guar": risk["breakdown"].get("no_guarantee_pts", 0),
            "p_cov": risk["breakdown"].get("no_covenants_pts", 0),
            "p_rating": risk["breakdown"].get("low_rating_pts", 0),
            "p_coupon": risk["breakdown"].get("high_coupon_pts", 0),
            "p_bullet": risk["breakdown"].get("bullet_repayment_pts", 0),
            "flags": json.dumps(risk.get("red_flags", [])),
        },
    )
    await execute_query(
        "DELETE FROM im_processing_log WHERE upload_id = :uid",
        {"uid": upload_id},
    )
    await execute_query(
        """INSERT INTO im_processing_log (upload_id, method_used, char_count, pages_processed, processing_time, status, error_message)
           VALUES (:uid, 'ocr', :chars, :pages, 0, 'SUCCESS', NULL)""",
        {"uid": upload_id, "chars": len(full_text), "pages": len(pages)},
    )
    await execute_query(
        """INSERT INTO im_audit_log (upload_id, isin, action, actor, details, created_at)
           VALUES (:uid, :isin, 'RERUN_OCR', 'system', :details, :now)""",
        {
            "uid": upload_id,
            "isin": upload["isin"],
            "details": json.dumps({"pages": len(pages), "text_length": len(full_text)}),
            "now": utcnow_iso(),
        },
    )
    _log_im_event("rerun_ocr_done", upload_id=upload_id, pages=len(pages), text_length=len(full_text), risk_score=risk.get("risk_score"))
    return {"status": "success", "upload_id": upload_id, "pages": len(pages), "text_length": len(full_text)}


@router.get("/connectors")
async def list_im_connectors():
    """List all available IM source connectors and their status."""
    return {"connectors": list_connectors()}


@router.get("/audit-log", dependencies=[Depends(require_role(Roles.AUDITOR))])
async def get_audit_log(
    isin: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    """Get IM audit log entries."""
    if isin:
        rows = await fetch_all(
            "SELECT * FROM im_audit_log WHERE isin = :isin ORDER BY created_at DESC LIMIT :limit",
            {"isin": isin.upper(), "limit": limit},
        )
    else:
        rows = await fetch_all(
            "SELECT * FROM im_audit_log ORDER BY created_at DESC LIMIT :limit",
            {"limit": limit},
        )
    for r in rows:
        if r.get("details") and isinstance(r["details"], str):
            try:
                r["details"] = json.loads(r["details"])
            except Exception:
                pass
    return {"audit_log": rows, "total": len(rows)}


@router.get("/{isin}", dependencies=[Depends(require_role(Roles.VIEWER))])
async def get_im_data(isin: str):
    """Get stored IM data for a bond."""
    isin = isin.upper()
    bond = await fetch_one(
        "SELECT isin, issuer_name, bond_name, credit_rating, maturity_date, coupon_rate, issuer_type "
        "FROM universal_bond_master WHERE isin = :isin", {"isin": isin}
    )
    if not bond:
        raise HTTPException(404, detail=f"Bond {isin} not found in master registry")
    im = await fetch_one("SELECT * FROM bond_im_data WHERE isin = :isin", {"isin": isin})
    if im:
        for field in ["covenants_financial", "covenants_non_financial", "risk_flags", "heuristic_results"]:
            val = im.get(field)
            if val and isinstance(val, str):
                try:
                    im[field] = json.loads(val)
                except Exception:
                    pass
    return {"bond": bond, "im_data": im}


@router.post("/{isin}", dependencies=[Depends(require_role(Roles.PORTFOLIO))])
async def save_im_data(isin: str, payload: IMDataInput):
    """Save / update IM data for a bond."""
    isin = isin.upper()
    bond = await fetch_one("SELECT issuer_name FROM universal_bond_master WHERE isin = :isin", {"isin": isin})
    if not bond:
        raise HTTPException(404, detail=f"Bond {isin} not found")

    d = payload.model_dump(exclude_none=True)
    _log_im_event("save_im_data_start", isin=isin, field_count=len(d))
    # Serialise JSON fields
    for field in ("covenants_financial", "covenants_non_financial"):
        if field in d and not isinstance(d[field], str):
            d[field] = json.dumps(d[field])

    _ALLOWED_IM_COLS = {
        "isin",
        "object_of_issue",
        "security_structure",
        "charge_type",
        "redemption_structure",
        "covenants_financial",
        "covenants_non_financial",
        "dscr_requirement",
        "interest_coverage_req",
        "debt_ebitda_limit",
        "call_option_details",
        "put_option_details",
        "step_up_clauses",
        "default_clauses",
        "cross_default_triggers",
        "trustee_name",
        "trustee_contact",
        "escrow_mechanism",
        "waterfall_structure",
        "payment_mechanism",
        "guarantee_details",
        "im_full_text",
        "risk_flags",
        "heuristic_results",
        "hidden_risk_score",
        "im_parsed_at",
        "created_at",
        "updated_at",
    }
    safe_d = {k: v for k, v in d.items() if k in _ALLOWED_IM_COLS}
    safe_d["isin"] = isin
    safe_d["updated_at"] = date.today().isoformat()

    existing = await fetch_one("SELECT id FROM bond_im_data WHERE isin = :isin", {"isin": isin})
    if existing:
        sets = ", ".join(f"{k} = :{k}" for k in safe_d if k != "isin")
        await execute_query(f"UPDATE bond_im_data SET {sets} WHERE isin = :isin", safe_d)
    else:
        safe_d["created_at"] = date.today().isoformat()
        safe_d["im_parsed_at"] = date.today().isoformat()
        cols = ", ".join(safe_d.keys())
        vals = ", ".join(f":{k}" for k in safe_d.keys())
        await execute_query(f"INSERT INTO bond_im_data ({cols}) VALUES ({vals})", safe_d)

    _log_im_event("save_im_data_done", isin=isin, saved_field_count=len(safe_d))

    return {"status": "ok", "isin": isin, "saved_fields": list(safe_d.keys())}


@router.post("/{isin}/upload", dependencies=[Depends(require_role(Roles.PORTFOLIO))])
async def upload_im_pdf(isin: str, file: UploadFile = File(...)):
    """Upload an IM PDF through the full institutional analysis pipeline."""
    isin = isin.upper()
    contents = await file.read()
    _log_im_event("upload_start", route="/{isin}/upload", isin=isin, file_name=file.filename or "document.pdf", file_size=len(contents))

    pipeline = IMPipeline()
    result = await pipeline.process_upload(
        isin=isin,
        file_bytes=contents,
        filename=file.filename or "document.pdf",
        uploaded_by="system",
    )

    if result["status"] == "error":
        _log_im_event("upload_failed", route="/{isin}/upload", isin=isin, errors=result.get("errors", []))
        raise HTTPException(400, detail={"status": "error", "errors": result.get("errors", [])})

    _log_im_event("upload_done", route="/{isin}/upload", isin=isin, upload_id=result.get("upload_id"), status=result.get("status"))

    return result


@router.get("/{isin}/heuristics", dependencies=[Depends(require_role(Roles.VIEWER))])
async def run_heuristics(isin: str):
    """Run all 24 IM Hidden Risk heuristics for a bond."""
    isin = isin.upper()
    _log_im_event("heuristics_start", isin=isin)
    engine = IMHeuristicsEngine()
    result = await engine.run_all_heuristics(isin)
    if "error" in result:
        _log_im_event("heuristics_failed", isin=isin, error=result.get("error"))
        raise HTTPException(404, detail=result["error"])
    _log_im_event("heuristics_done", isin=isin, hidden_risk_score=result.get("hidden_risk_score"), triggered_count=result.get("triggered_count"))
    return result


@router.get("/{isin}/financials", dependencies=[Depends(require_role(Roles.VIEWER))])
async def get_financials(isin: str):
    """Get financial data for a bond's issuer."""
    isin = isin.upper()
    bond = await fetch_one("SELECT issuer_name FROM universal_bond_master WHERE isin = :isin", {"isin": isin})
    if not bond:
        raise HTTPException(404, detail=f"Bond {isin} not found")
    issuer = bond.get("issuer_name", "")
    rows = await fetch_all(
        "SELECT * FROM issuer_financials WHERE issuer_name = :name ORDER BY financial_year DESC LIMIT 5",
        {"name": issuer}
    )
    latest = rows[0] if rows else {}
    health = _financial_health_score(latest) if latest else {}
    return {"issuer_name": issuer, "financials": rows, "health_score": health}


@router.post("/{isin}/financials", dependencies=[Depends(require_role(Roles.PORTFOLIO))])
async def save_financials(isin: str, payload: FinancialDataInput):
    """Save financial data for a bond issuer."""
    isin = isin.upper()
    _log_im_event("save_financials_start", isin=isin, financial_year=payload.financial_year)
    bond = await fetch_one(
        "SELECT issuer_name FROM universal_bond_master WHERE isin = :isin", {"isin": isin}
    )
    if not bond:
        raise HTTPException(404, detail=f"Bond {isin} not found")

    d = payload.model_dump()
    d["isin"] = isin
    if not d.get("issuer_name"):
        d["issuer_name"] = bond.get("issuer_name", "")
    d = _compute_ratios(d)
    d["created_at"] = date.today().isoformat()

    existing = await fetch_one(
        "SELECT id FROM issuer_financials WHERE issuer_name = :name AND financial_year = :fy",
        {"name": d["issuer_name"], "fy": d["financial_year"]}
    )
    if existing:
        sets = ", ".join(f"{k} = :{k}" for k in d if k not in ("issuer_name", "financial_year", "created_at"))
        await execute_query(
            f"UPDATE issuer_financials SET {sets} WHERE issuer_name = :issuer_name AND financial_year = :financial_year",
            d
        )
    else:
        d_clean = {k: v for k, v in d.items() if v is not None}
        cols = ", ".join(d_clean.keys())
        vals = ", ".join(f":{k}" for k in d_clean.keys())
        await execute_query(f"INSERT INTO issuer_financials ({cols}) VALUES ({vals})", d_clean)

    return {"status": "ok", "issuer_name": d["issuer_name"], "financial_year": d["financial_year"],
            "ratios": {k: d.get(k) for k in ("debt_ebitda", "interest_coverage", "dscr", "current_ratio", "roe")}}


@router.get("/{isin}/migration", dependencies=[Depends(require_role(Roles.VIEWER))])
async def predict_migration(isin: str):
    """Predict likelihood of rating migration based on deteriorating/improving financials."""
    isin = isin.upper()
    bond = await fetch_one("SELECT isin, issuer_name, credit_rating FROM universal_bond_master WHERE isin = :isin", {"isin": isin})
    if not bond:
         raise HTTPException(404, detail=f"Bond {isin} not found")

    issuer = bond.get("issuer_name", "")
    rating = bond.get("credit_rating", "NR")

    financials = await fetch_all(
        "SELECT * FROM issuer_financials WHERE issuer_name = :name ORDER BY financial_year DESC LIMIT 3",
        {"name": issuer}
    )
    if not financials or len(financials) < 2:
        return {"isin": isin, "status": "Insufficient data", "signals": [], "prediction": "STABLE"}

    latest = financials[0]
    prev = financials[1]
    signals = []
    dn_prob = 0.0
    up_prob = 0.0

    # ICR deterioration
    icr_now = latest.get("interest_coverage") or 0
    icr_prev = prev.get("interest_coverage") or 0
    if icr_now < 1.0:
        signals.append({"id": "ICR_CRITICAL", "sev": "CRITICAL", "msg": f"ICR {icr_now:.2f} < 1.0 (Critical)", "impact": 0.25})
    elif icr_now < icr_prev - 0.5:
        signals.append({"id": "ICR_DECLINE", "sev": "HIGH", "msg": f"ICR dropped from {icr_prev:.2f} to {icr_now:.2f}", "impact": 0.15})

    # DSCR check
    dscr_now = latest.get("dscr") or 0
    if dscr_now < 1.0:
        signals.append({"id": "DSCR_CRITICAL", "sev": "CRITICAL", "msg": f"DSCR {dscr_now:.2f} < 1.0 — Breach of debt serviceability", "impact": 0.30})

    # Debt / EBITDA
    de_now = latest.get("debt_ebitda") or 0
    de_prev = prev.get("debt_ebitda") or 0
    if de_now > 5.0:
        signals.append({"id": "LEVERAGE_HIGH", "sev": "HIGH", "msg": f"D/EBITDA {de_now:.2f}x is high", "impact": 0.15})
    elif de_now > de_prev + 0.8:
        signals.append({"id": "LEVERAGE_RISING", "sev": "MEDIUM", "msg": f"Leverage rose from {de_prev:.2f}x to {de_now:.2f}x", "impact": 0.10})

    # Revenue
    rev_now = latest.get("revenue_cr") or 0
    rev_prev = prev.get("revenue_cr") or 1
    rev_chg = (rev_now - rev_prev) / rev_prev * 100
    if rev_chg < -15:
        signals.append({"id": "REV_SHOCK", "sev": "HIGH", "msg": f"Revenue shock: {rev_chg:.1f}% decline YoY", "impact": 0.12})

    for s in signals: dn_prob += s["impact"]
    dn_prob = min(dn_prob, 0.95); up_prob = min(up_prob, 0.25)

    # Simple migration prediction
    sorted_ratings = sorted(RATING_ORDER.items(), key=lambda x: x[1], reverse=True)
    curr_pos = next((i for i, (r, s) in enumerate(sorted_ratings) if r in rating), -1)

    predicted_rating = rating
    alert_level = "STABLE"

    if dn_prob >= 0.5:
        alert_level = "CRITICAL"
        if curr_pos >= 0 and curr_pos < len(sorted_ratings) - 1:
            predicted_rating = sorted_ratings[curr_pos + 1][0]
    elif dn_prob >= 0.25:
        alert_level = "WATCH"
        predicted_rating = f"{rating} (NEG)"

    return {
        "isin": isin, "alert_level": alert_level, "signals": signals,
        "downgrade_probability": round(dn_prob, 2), "upgrade_probability": round(up_prob, 2),
        "predicted_rating": predicted_rating, "current_rating": rating
    }


@router.get("/{isin}/summary", dependencies=[Depends(require_role(Roles.VIEWER))])
async def get_full_summary(isin: str):
    """
    Full IM Checker summary: bond info + IM data + financials + heuristics
    (cached heuristics if already run; re-run flag optional).
    """
    isin = isin.upper()
    bond = await fetch_one(
        "SELECT * FROM universal_bond_master WHERE isin = :isin", {"isin": isin}
    )
    if not bond:
        raise HTTPException(404, detail=f"Bond {isin} not found")

    im = await fetch_one("SELECT * FROM bond_im_data WHERE isin = :isin", {"isin": isin})
    if im:
        for field in ["covenants_financial", "covenants_non_financial", "risk_flags", "heuristic_results"]:
            val = im.get(field)
            if val and isinstance(val, str):
                try:
                    im[field] = json.loads(val)
                except Exception:
                    pass

    issuer = bond.get("issuer_name", "")
    fin_rows = await fetch_all(
        "SELECT * FROM issuer_financials WHERE issuer_name = :name ORDER BY financial_year DESC LIMIT 3",
        {"name": issuer}
    )
    latest_fin = fin_rows[0] if fin_rows else {}
    health = _financial_health_score(latest_fin) if latest_fin else {}

    # Heuristics: use cached if available, else run fresh
    heuristics = None
    if im and im.get("heuristic_results"):
        cached = im["heuristic_results"]
        if isinstance(cached, list):
            heuristics = {"cached": True, "heuristics": cached, "triggered_count": sum(1 for r in cached if r.get("triggered"))}
    if not heuristics:
        engine = IMHeuristicsEngine()
        heuristics = await engine.run_all_heuristics(isin)

    return {
        "bond": bond,
        "im_data": im,
        "financials": fin_rows,
        "health_score": health,
        "heuristics": heuristics,
    }
