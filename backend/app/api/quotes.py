"""
Gateway Router — Bond Quote System (v2)
========================================
Comprehensive broker quote management:

  POST /ingest             -- Single manual quote entry
  POST /ingest/gmail       -- Trigger Gmail poll
  POST /ingest/bulk        -- Bulk JSON quotes
  POST /upload             -- Upload PDF/Excel broker quote file (auto-parse)
  POST /ingest-workspace   -- Re-parse all known files in the workspace

  GET  /                   -- List all raw quotes (legacy)
  GET  /best               -- Best quote per ISIN (same-day comparison)
  GET  /today              -- Today's quotes from broker_daily_quotes
  GET  /broker-profiles    -- All broker analytics profiles
  GET  /broker/{name}      -- Specific broker trend & history
  GET  /compare/{isin}     -- All broker quotes for one ISIN (ranked)
  GET  /history/{isin}     -- Historical quote trail for an ISIN
  GET  /stats/brokers      -- Broker stats
  GET  /ingest-log         -- Recent file ingest history
  GET  /unknown-brokers    -- Files ingested as Unknown broker (grouped)
  PATCH /rename-broker     -- Rename Unknown (or any) broker in all quote tables
  GET  /{isin}             -- Quote history for one ISIN
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, UploadFile, File
from pydantic import BaseModel

from ..shared.database import execute_query, fetch_all, fetch_one
from ..shared.logger import get_logger
from ..shared.auth import require_role, Roles
from ..utils.gmail_ingest import run_gmail_ingest_cycle
from ..domain.market_data.parsers import BrokerQuoteParser, parse_files, detect_broker
from ..domain.market_data.broker_analytics import (
    refresh_broker_profiles,
    get_all_broker_profiles,
    get_broker_comparison_for_isin,
)
from ..domain.market_data.instrument_classification import (
    category_metadata,
    category_to_allocation_bucket,
    classify_instrument,
)

log = get_logger("router.quotes")
router = APIRouter(prefix="/quotes", tags=["Bond Quote System"])

_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent


def _quote_category_bucket(category: Optional[str], issuer_type: Optional[str] = None) -> str:
    text = f"{category or ''} {issuer_type or ''}".upper()
    if any(tok in text for tok in ("G-SEC", "GSEC", "SDL", "T-BILL", "TBILL", "GOV", "SOV", "STATE", "TREASURY", "CENTRAL")):
        return "SDL / G SEC"
    return "PRIVATE + PSU"


def _attach_quote_category_profile(row: dict[str, Any]) -> dict[str, Any]:
    profiled = dict(row)
    derived = classify_instrument(profiled)
    category = str(profiled.get("instrument_category") or derived.get("instrument_category") or "PRIVATE").upper()
    subtype = str(profiled.get("instrument_subtype") or derived.get("instrument_subtype") or "CORPORATE").upper()
    meta = category_metadata(category)
    profile_bucket = category_to_allocation_bucket(category)
    if profile_bucket == "GOVT":
        legacy_bucket = "SDL / G SEC"
    elif profile_bucket == "EQUITY":
        legacy_bucket = "EQUITY"
    else:
        legacy_bucket = "PRIVATE + PSU"

    profiled["instrument_category"] = category
    profiled["instrument_subtype"] = subtype
    profiled["category_display"] = meta.get("category_display")
    profiled["category_tag"] = meta.get("category_tag")
    profiled["risk_label"] = meta.get("risk_label")
    # Keep legacy field for existing UI filters while exposing canonical contract.
    profiled["category_bucket"] = legacy_bucket
    return profiled


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class QuoteIn(BaseModel):
    isin: str
    broker_name: str
    quote_date: str
    bid_price: Optional[float] = None
    ask_price: Optional[float] = None
    bid_yield: Optional[float] = None
    ask_yield: Optional[float] = None
    lot_size: Optional[int] = None
    settlement_days: int = 1


# ---------------------------------------------------------------------------
# Internal: store parsed quotes -> broker_daily_quotes
# ---------------------------------------------------------------------------

async def _store_parsed_quotes(quotes: list, log_entry: dict) -> dict:
    inserted = 0
    errors = []

    # Enrich bond names from universal_bond_master for quotes that lack them
    isins_needing = [q.isin for q in quotes if not q.bond_name and q.isin]
    if isins_needing:
        try:
            master_rows = await fetch_all(
                "SELECT isin, COALESCE(bond_name, issuer_name) AS name "
                "FROM universal_bond_master WHERE isin IN ("
                + ",".join(f"'{i}'" for i in isins_needing[:200])
                + ")",
                {},
            )
            name_lut = {r["isin"]: r["name"] for r in master_rows}
            for q in quotes:
                if not q.bond_name and q.isin in name_lut:
                    q.bond_name = name_lut[q.isin]
        except Exception as _enr_exc:
            log.warning("bond_name_enrich_fail", error=str(_enr_exc))

    for q in quotes:
        if not q.isin:
            continue
        profiled = _attach_quote_category_profile(
            {
                "isin": q.isin,
                "bond_name": q.bond_name,
                "category": q.category,
                "rating": q.rating,
            }
        )
        try:
            await execute_query(
                """
                INSERT INTO broker_daily_quotes
                    (isin, broker_name, quote_date, bond_name, category, instrument_category,
                     coupon_rate, maturity_date, rating, rating_agency,
                     clean_price, ytm, ytm_semi, ytm_screen,
                     bid_price, ask_price, bid_yield, ask_yield,
                     lot_size, quantity_lacs, put_call_date, ip_dates,
                     settlement_days, source_file, source_format)
                VALUES
                    (:isin, :broker, :date, :bond_name, :category, :instrument_category,
                     :coupon, :maturity, :rating, :rating_agency,
                     :price, :ytm, :ytm_semi, :ytm_screen,
                     :bid_price, :ask_price, :bid_yield, :ask_yield,
                     :lot, :qty_lacs, :put_call, :ip_dates,
                     :settle, :source_file, :source_format)
                ON CONFLICT(isin, broker_name, quote_date) DO UPDATE SET
                    bond_name       = excluded.bond_name,
                    category        = excluded.category,
                    instrument_category = excluded.instrument_category,
                    coupon_rate     = excluded.coupon_rate,
                    maturity_date   = excluded.maturity_date,
                    rating          = excluded.rating,
                    rating_agency   = excluded.rating_agency,
                    clean_price     = COALESCE(excluded.clean_price, broker_daily_quotes.clean_price),
                    ytm             = COALESCE(excluded.ytm, broker_daily_quotes.ytm),
                    ytm_semi        = COALESCE(excluded.ytm_semi, broker_daily_quotes.ytm_semi),
                    ytm_screen      = COALESCE(excluded.ytm_screen, broker_daily_quotes.ytm_screen),
                    bid_price       = COALESCE(excluded.bid_price, broker_daily_quotes.bid_price),
                    ask_price       = COALESCE(excluded.ask_price, broker_daily_quotes.ask_price),
                    bid_yield       = COALESCE(excluded.bid_yield, broker_daily_quotes.bid_yield),
                    ask_yield       = COALESCE(excluded.ask_yield, broker_daily_quotes.ask_yield),
                    lot_size        = COALESCE(excluded.lot_size, broker_daily_quotes.lot_size),
                    quantity_lacs   = COALESCE(excluded.quantity_lacs, broker_daily_quotes.quantity_lacs),
                    source_file     = excluded.source_file,
                    extracted_at    = datetime('now')
                """,
                {
                    "isin": q.isin, "broker": q.broker_name, "date": q.quote_date,
                    "bond_name": q.bond_name, "category": q.category,
                    "instrument_category": profiled.get("instrument_category"),
                    "coupon": q.coupon_rate, "maturity": q.maturity_date,
                    "rating": q.rating, "rating_agency": q.rating_agency,
                    "price": q.clean_price, "ytm": q.ytm,
                    "ytm_semi": q.ytm_semi, "ytm_screen": q.ytm_screen,
                    "bid_price": q.bid_price, "ask_price": q.ask_price,
                    "bid_yield": q.bid_yield, "ask_yield": q.ask_yield,
                    "lot": q.lot_size, "qty_lacs": q.quantity_lacs,
                    "put_call": q.put_call_date, "ip_dates": q.ip_dates,
                    "settle": q.settlement_days,
                    "source_file": q.source_file, "source_format": q.source_format,
                },
            )
            inserted += 1
        except Exception as exc:
            errors.append(f"{q.isin}: {exc}")
            log.warning("store_quote_fail", isin=q.isin, error=str(exc))

    try:
        await execute_query(
            """
            INSERT INTO broker_ingest_log
                (filename, broker_name, quote_date, source_format,
                 quotes_parsed, quotes_inserted, quotes_updated, errors_json)
            VALUES
                (:filename, :broker, :date, :fmt,
                 :parsed, :inserted, :updated, :errors)
            """,
            {
                "filename": log_entry.get("filename", ""),
                "broker": log_entry.get("broker_name", ""),
                "date": log_entry.get("quote_date", ""),
                "fmt": log_entry.get("source_format", ""),
                "parsed": len(quotes),
                "inserted": inserted,
                "updated": 0,
                "errors": json.dumps(errors[:20]) if errors else None,
            },
        )
    except Exception as exc:
        log.warning("ingest_log_fail", error=str(exc))

    return {"inserted": inserted, "errors": errors[:10]}


# ---------------------------------------------------------------------------
# POST /upload
# ---------------------------------------------------------------------------

@router.post("/upload")
async def upload_broker_quote(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    broker_override: Optional[str] = None,
    user=Depends(require_role(Roles.PORTFOLIO)),
):
    """Upload a broker quote PDF or Excel file and auto-parse it."""
    filename = file.filename or "upload.bin"
    suffix = Path(filename).suffix.lower()
    if suffix not in (".pdf", ".xlsx", ".xls"):
        raise HTTPException(400, "Only PDF, XLSX and XLS files are supported")

    content = await file.read()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        parser = BrokerQuoteParser()
        # Detect broker from the ORIGINAL filename (not the temp file path)
        effective_broker = broker_override or detect_broker(filename)
        result = parser.parse(tmp_path, broker_override=effective_broker)
        for q in result.quotes:
            q.source_file = filename

        store_result = await _store_parsed_quotes(
            result.quotes,
            {"filename": filename, "broker_name": result.broker_name,
             "quote_date": result.quote_date, "source_format": result.source_format},
        )
        background_tasks.add_task(refresh_broker_profiles)

        return {
            "status": "ok",
            "filename": filename,
            "broker": result.broker_name,
            "quote_date": result.quote_date,
            "format": result.source_format,
            "quotes_parsed": result.count,
            "quotes_stored": store_result["inserted"],
            "errors": result.errors[:5] if result.errors else [],
            "sample": [{"isin": q.isin, "ytm": q.ytm, "price": q.clean_price} for q in result.quotes[:5]],
        }
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# POST /ingest-workspace
# ---------------------------------------------------------------------------

@router.post("/ingest-workspace")
async def ingest_workspace_files(background_tasks: BackgroundTasks, user=Depends(require_role(Roles.ADMIN))):
    """Scan workspace for broker quote files and (re-)ingest all of them."""
    search_dirs = [_BASE_DIR, _BASE_DIR / "Private3"]
    broker_patterns = ["Quote", "Quotes", "RFS", "Nuvama", "Darashaw",
                       "Aditya Birla", "Birla", "Bond Vector"]
    found: List[Path] = []
    for d in search_dirs:
        if not d.exists():
            continue
        for ext in ("*.xlsx", "*.xls", "*.pdf"):
            for fp in d.glob(ext):
                if any(p.lower() in fp.name.lower() for p in broker_patterns):
                    found.append(fp)

    results = parse_files(found)
    total_quotes = 0
    total_stored = 0
    file_summaries = []

    for r in results:
        store = await _store_parsed_quotes(
            r.quotes,
            {"filename": r.filename, "broker_name": r.broker_name,
             "quote_date": r.quote_date, "source_format": r.source_format},
        )
        total_quotes += r.count
        total_stored += store["inserted"]
        file_summaries.append({
            "file": r.filename,
            "broker": r.broker_name,
            "date": r.quote_date,
            "parsed": r.count,
            "stored": store["inserted"],
        })

    background_tasks.add_task(refresh_broker_profiles)

    return {
        "status": "ok",
        "files_scanned": len(found),
        "total_quotes_parsed": total_quotes,
        "total_quotes_stored": total_stored,
        "files": file_summaries,
    }


# ---------------------------------------------------------------------------
# GET /best
# ---------------------------------------------------------------------------

def _build_reason_inline(q: dict, prof: dict, is_best: bool, ytm_diff_bps: Optional[float]) -> str:
    parts = []
    if is_best:
        parts.append("Best yield today")
    if ytm_diff_bps is not None:
        if ytm_diff_bps > 3:
            parts.append(f"+{ytm_diff_bps:.0f}bps above peer avg")
        elif ytm_diff_bps < -3:
            parts.append(f"{ytm_diff_bps:.0f}bps below peer avg")
    avg_prem = prof.get("avg_ytm_premium_bps")
    if avg_prem is not None:
        if avg_prem < -5:
            parts.append(f"historically competitive (avg {avg_prem:.0f}bps vs market)")
        elif avg_prem > 10:
            parts.append(f"historically expensive (+{avg_prem:.0f}bps vs market)")
    trend = prof.get("trend_direction")
    if trend == "TIGHTENING":
        parts.append("improving trend")
    elif trend == "WIDENING":
        parts.append("widening spreads")
    tags_raw = prof.get("behaviour_tags")
    tags = json.loads(tags_raw) if tags_raw else []
    if "highly_consistent" in tags:
        parts.append("reliable pricing")
    if "stale_data" in tags:
        parts.append("stale data warning")
    return "; ".join(parts) if parts else "No additional profile data"


@router.get("/best")
async def best_quotes(
    limit: int = Query(100, le=500),
    keyword: Optional[str] = None,
    as_of_date: Optional[str] = None,
    user=Depends(require_role(Roles.VIEWER)),
):
    """Best broker quote per ISIN on a single date (same-day comparison only)."""
    if as_of_date:
        target_date = as_of_date
    else:
        row = await fetch_one(
            "SELECT MAX(quote_date) as d FROM broker_daily_quotes WHERE ytm IS NOT NULL"
        )
        target_date = (row or {}).get("d") or date.today().isoformat()

    rows = await fetch_all(
        """
        SELECT
            bdq.isin, bdq.broker_name,
            COALESCE(NULLIF(TRIM(bdq.bond_name), ''), b.bond_name, b.issuer_name) AS bond_name,
            COALESCE(bdq.category, b.issuer_type)                                 AS category,
            bdq.coupon_rate,
            CASE
                WHEN bdq.maturity_date IS NULL OR bdq.maturity_date < '1950-01-01'
                THEN b.maturity_date
                ELSE bdq.maturity_date
            END AS maturity_date,
            COALESCE(NULLIF(TRIM(bdq.rating), ''), b.credit_rating)               AS rating,
            bdq.clean_price, bdq.ytm, bdq.ytm_semi, bdq.ytm_screen,
            bdq.bid_price, bdq.ask_price, bdq.bid_yield, bdq.ask_yield,
            bdq.lot_size, bdq.quantity_lacs, bdq.quote_date,
            b.is_pf_eligible, b.issuer_type, b.credit_rating AS master_rating
        FROM broker_daily_quotes bdq
        LEFT JOIN universal_bond_master b ON bdq.isin = b.isin
        WHERE bdq.quote_date = :date AND bdq.ytm IS NOT NULL
        ORDER BY bdq.isin, bdq.ytm DESC
        LIMIT :limit
        """,
        {"date": target_date, "limit": limit * 10},
    )

    profiles_rows = await fetch_all("SELECT * FROM broker_profiles")
    profiles: Dict[str, dict] = {p["broker_name"]: dict(p) for p in profiles_rows}

    isin_groups: Dict[str, list] = defaultdict(list)
    for row in rows:
        isin_groups[row["isin"]].append(dict(row))

    result = []
    for isin, quotes in isin_groups.items():
        if keyword:
            kw = keyword.lower()
            bn = (quotes[0].get("bond_name") or "").lower()
            if kw not in isin.lower() and kw not in bn:
                continue

        best = max(quotes, key=lambda x: x.get("ytm") or 0)
        peer_avg = sum(q.get("ytm") or 0 for q in quotes) / len(quotes) if quotes else 0

        enriched = []
        for q in sorted(quotes, key=lambda x: x.get("ytm") or 0, reverse=True):
            prof = profiles.get(q["broker_name"], {})
            is_best = q["broker_name"] == best["broker_name"]
            diff = round(((q.get("ytm") or 0) - peer_avg) * 10_000, 1) if peer_avg else None
            tags_raw = prof.get("behaviour_tags")
            enriched.append({
                "broker_name": q["broker_name"],
                "clean_price": q.get("clean_price"),
                "ytm": q.get("ytm"),
                "ytm_semi": q.get("ytm_semi"),
                "bid_price": q.get("bid_price"),
                "ask_price": q.get("ask_price"),
                "bid_yield": q.get("bid_yield"),
                "ask_yield": q.get("ask_yield"),
                "quantity_lacs": q.get("quantity_lacs"),
                "lot_size": q.get("lot_size"),
                "is_best": is_best,
                "ytm_diff_bps": diff,
                "reliability_score": prof.get("reliability_score"),
                "trend_direction": prof.get("trend_direction"),
                "behaviour_tags": json.loads(tags_raw) if tags_raw else [],
                "reason": _build_reason_inline(q, prof, is_best, diff),
            })

        first = _attach_quote_category_profile(quotes[0])
        result.append({
            "isin": isin,
            "bond_name": first.get("bond_name") or isin,
            "category": first.get("category"),
            "category_bucket": first.get("category_bucket") or _quote_category_bucket(first.get("category"), first.get("issuer_type")),
            "instrument_category": first.get("instrument_category"),
            "instrument_subtype": first.get("instrument_subtype"),
            "category_display": first.get("category_display"),
            "category_tag": first.get("category_tag"),
            "risk_label": first.get("risk_label"),
            "coupon_rate": first.get("coupon_rate"),
            "maturity_date": first.get("maturity_date"),
            "rating": first.get("rating") or first.get("master_rating"),
            "is_pf_eligible": first.get("is_pf_eligible"),
            "best_broker": best.get("broker_name"),
            "best_ytm": best.get("ytm"),
            "best_price": best.get("clean_price"),
            "peer_avg_ytm": round(peer_avg, 6) if peer_avg else None,
            "broker_count": len(quotes),
            "quote_date": target_date,
            "all_broker_quotes": enriched,
        })

    result.sort(key=lambda x: x.get("best_ytm") or 0, reverse=True)
    return {"count": len(result), "as_of": target_date, "quotes": result[:limit]}


# ---------------------------------------------------------------------------
# GET /today
# ---------------------------------------------------------------------------

@router.get("/today")
async def todays_quotes(
    broker: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = Query(200, le=1000),
    user=Depends(require_role(Roles.VIEWER)),
):
    """All quotes on the most recent available date."""
    row = await fetch_one(
        "SELECT MAX(quote_date) as d FROM broker_daily_quotes WHERE ytm IS NOT NULL"
    )
    target_date = (row or {}).get("d") or date.today().isoformat()

    conditions = ["bdq.quote_date = :date"]
    params: dict = {"date": target_date, "limit": limit}
    if broker:
        conditions.append("bdq.broker_name = :broker")
        params["broker"] = broker
    if category:
        conditions.append("bdq.category LIKE :category")
        params["category"] = f"%{category}%"

    where = " AND ".join(conditions)
    rows = await fetch_all(
        f"""
        SELECT bdq.*, b.issuer_name, b.is_pf_eligible
        FROM broker_daily_quotes bdq
        LEFT JOIN universal_bond_master b ON bdq.isin = b.isin
        WHERE {where}
        ORDER BY bdq.ytm DESC
        LIMIT :limit
        """,
        params,
    )
    enriched_rows = [_attach_quote_category_profile(dict(r)) for r in rows]
    brokers = list({r["broker_name"] for r in enriched_rows})
    isins = list({r["isin"] for r in enriched_rows})
    best_ytm = max((r["ytm"] or 0 for r in enriched_rows), default=0)

    return {
        "quote_date": target_date,
        "total": len(enriched_rows),
        "brokers": brokers,
        "unique_isins": len(isins),
        "best_ytm_available": round(best_ytm, 4),
        "quotes": enriched_rows,
    }


# ---------------------------------------------------------------------------
# GET /broker-profiles
# ---------------------------------------------------------------------------

@router.get("/broker-profiles")
async def broker_profiles_endpoint(user=Depends(require_role(Roles.VIEWER))):
    """All broker analytics profiles with reliability, trend, and behaviour tags."""
    profiles = await get_all_broker_profiles()
    return {"count": len(profiles), "profiles": [dict(p) for p in profiles]}


@router.post("/broker-profiles/refresh")
async def refresh_profiles_endpoint(background_tasks: BackgroundTasks, user=Depends(require_role(Roles.ADMIN))):
    """Manually queue a broker profile recomputation."""
    background_tasks.add_task(refresh_broker_profiles)
    return {"status": "recomputation queued"}


# ---------------------------------------------------------------------------
# GET /broker/{name}
# ---------------------------------------------------------------------------

@router.get("/broker/{broker_name}")
async def broker_detail(broker_name: str, user=Depends(require_role(Roles.VIEWER))):
    """Detailed analytics for one broker: profile, daily premium history, trend, top ISINs."""
    profile = await fetch_one(
        "SELECT * FROM broker_profiles WHERE broker_name = :b", {"b": broker_name}
    )
    if not profile:
        raise HTTPException(404, f"No profile for broker '{broker_name}'")

    # Daily quote volume + yields
    daily = await fetch_all(
        """
        SELECT quote_date, COUNT(*) AS quotes, AVG(ytm) AS avg_ytm,
               MIN(ytm) AS min_ytm, MAX(ytm) AS max_ytm
        FROM broker_daily_quotes
        WHERE broker_name = :b AND ytm IS NOT NULL
        GROUP BY quote_date ORDER BY quote_date ASC
        """,
        {"b": broker_name},
    )

    # Daily premium history (vs market peer average)
    premium_history = await fetch_all(
        """
        SELECT quote_date, avg_premium_bps, num_isins, avg_ytm, avg_spread_bps, peer_avg_ytm
        FROM broker_daily_premiums
        WHERE broker_name = :b
        ORDER BY quote_date ASC
        """,
        {"b": broker_name},
    )

    top_isins = await fetch_all(
        """
        SELECT bdq.isin, COALESCE(ubm.issuer_name, bdq.bond_name) AS bond_name_display, 
               COUNT(*) AS days_quoted,
               AVG(bdq.ytm) AS avg_ytm, MAX(bdq.quote_date) AS last_date,
               ubm.credit_rating, ubm.issuer_type
        FROM broker_daily_quotes bdq
        LEFT JOIN universal_bond_master ubm ON bdq.isin = ubm.isin
        WHERE bdq.broker_name = :b AND bdq.ytm IS NOT NULL
        GROUP BY bdq.isin, bond_name_display, ubm.credit_rating, ubm.issuer_type
        ORDER BY days_quoted DESC LIMIT 30
        """,
        {"b": broker_name},
    )

    prof = dict(profile)
    if prof.get("behaviour_tags"):
        try:
            prof["behaviour_tags"] = json.loads(prof["behaviour_tags"])
        except Exception:
            pass

    # Compute linear trend over premium history for charting
    trend_analysis = {}
    if len(premium_history) >= 2:
        ph = [dict(r) for r in premium_history]
        prems = [r["avg_premium_bps"] for r in ph if r.get("avg_premium_bps") is not None]
        if prems:
            import statistics as _stats
            trend_analysis = {
                "mean_premium_bps": round(_stats.mean(prems), 2),
                "min_premium_bps": round(min(prems), 2),
                "max_premium_bps": round(max(prems), 2),
                "stdev_premium_bps": round(_stats.stdev(prems), 2) if len(prems) > 1 else 0.0,
                "days_with_data": len(ph),
                "first_date": ph[0]["quote_date"],
                "last_date": ph[-1]["quote_date"],
                "interpretation": (
                    "Aggressively competitive (quotes well below market)" if _stats.mean(prems) < -10
                    else "Competitive pricer" if _stats.mean(prems) < -3
                    else "Fair market pricer" if abs(_stats.mean(prems)) <= 3
                    else "Slightly above market" if _stats.mean(prems) <= 10
                    else "Expensive vs peers — negotiate or use other brokers"
                ),
            }

    return {
        "profile": prof,
        "daily_history": [dict(r) for r in daily],
        "premium_history": [dict(r) for r in premium_history],
        "trend_analysis": trend_analysis,
        "top_isins": [dict(r) for r in top_isins],
    }


# ---------------------------------------------------------------------------
# GET /compare/{isin}
# ---------------------------------------------------------------------------

@router.get("/compare/{isin}")
async def compare_isin(isin: str, as_of_date: Optional[str] = None, user=Depends(require_role(Roles.VIEWER))):
    """Rank all brokers quoting an ISIN on the same day with explanations."""
    return await get_broker_comparison_for_isin(isin.upper(), as_of_date)


# ---------------------------------------------------------------------------
# GET /history/{isin}
# ---------------------------------------------------------------------------

@router.get("/history/{isin}")
async def isin_history(
    isin: str,
    days: int = Query(30, le=365),
    broker: Optional[str] = None,
    user=Depends(require_role(Roles.VIEWER)),
):
    """Time-series of quotes for a single ISIN across all brokers."""
    conditions = ["isin = :isin", "quote_date >= date('now', :days)"]
    params: dict = {"isin": isin.upper(), "days": f"-{days} days"}
    if broker:
        conditions.append("broker_name = :broker")
        params["broker"] = broker
    rows = await fetch_all(
        f"""
        SELECT broker_name, quote_date, clean_price, ytm, ytm_semi,
               rating, quantity_lacs, source_file
        FROM broker_daily_quotes
        WHERE {" AND ".join(conditions)}
        ORDER BY quote_date DESC, ytm DESC
        """,
        params,
    )
    return {"isin": isin.upper(), "days": days, "records": len(rows), "history": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Legacy / backward-compatible endpoints
# ---------------------------------------------------------------------------

@router.post("/ingest", status_code=201)
async def ingest_quote(quote: QuoteIn, user=Depends(require_role(Roles.PORTFOLIO))):
    """Manually ingest a single broker quote into legacy table."""
    await execute_query(
        """
        INSERT INTO unified_broker_quotes
            (isin, broker_name, quote_date, bid_price, ask_price,
             bid_yield, ask_yield, lot_size, settlement_days)
        VALUES
            (:isin, :broker, :date, :bid_p, :ask_p, :bid_y, :ask_y, :lot, :settle)
        """,
        {
            "isin": quote.isin.strip().upper(), "broker": quote.broker_name,
            "date": quote.quote_date, "bid_p": quote.bid_price, "ask_p": quote.ask_price,
            "bid_y": quote.bid_yield, "ask_y": quote.ask_yield,
            "lot": quote.lot_size, "settle": quote.settlement_days,
        },
    )
    return {"status": "accepted", "isin": quote.isin}


@router.post("/ingest/gmail")
async def trigger_gmail_ingest(background_tasks: BackgroundTasks, user=Depends(require_role(Roles.ADMIN))):
    """
    Trigger Gmail poll → download attachments → parse PDFs/Excel →
    upsert into broker_daily_quotes → refresh broker profiles.
    Returns full pipeline summary including quote_preview (top 25 bonds stored).
    """
    result = await run_gmail_ingest_cycle()
    if "error" in result:
        raise HTTPException(500, result["error"])

    # Fetch preview of newly stored quotes for UI display
    try:
        preview_rows = await fetch_all(
            """
            SELECT bdq.isin, bdq.broker_name, bdq.bond_name, bdq.rating,
                   bdq.coupon_rate, bdq.maturity_date, bdq.category,
                   bdq.ytm, bdq.ytm_semi, bdq.clean_price,
                   bdq.bid_yield, bdq.ask_yield, bdq.quantity_lacs,
                   bdq.quote_date, bdq.extracted_at
            FROM broker_daily_quotes bdq
            WHERE bdq.quote_date = (
                SELECT MAX(quote_date) FROM broker_daily_quotes WHERE ytm IS NOT NULL
            )
            ORDER BY bdq.extracted_at DESC, bdq.ytm DESC
            LIMIT 50
            """,
            {},
        )
        # Group by ISIN, pick best YTM row per ISIN, keep top 25
        seen_isins: dict = {}
        for r in preview_rows:
            isin = r["isin"]
            if isin not in seen_isins or (r["ytm"] or 0) > (seen_isins[isin]["ytm"] or 0):
                seen_isins[isin] = dict(r)
        quote_preview = sorted(seen_isins.values(), key=lambda x: x.get("ytm") or 0, reverse=True)[:25]
        result["quote_preview"] = quote_preview
        result["preview_date"] = quote_preview[0]["quote_date"] if quote_preview else ""
    except Exception as exc:
        log.warning("quote_preview_fail", error=str(exc))
        result["quote_preview"] = []
        result["preview_date"] = ""

    # Refresh broker profiles in background
    background_tasks.add_task(refresh_broker_profiles)
    return result


@router.post("/ingest/bulk", status_code=201)
async def bulk_ingest(quotes: List[QuoteIn], user=Depends(require_role(Roles.PORTFOLIO))):
    """Bulk ingest broker quotes into the legacy table."""
    count = 0
    for q in quotes:
        try:
            await ingest_quote(q)
            count += 1
        except Exception as exc:
            log.warning("bulk_ingest_fail", isin=q.isin, error=str(exc))
    return {"status": "ok", "ingested": count, "total": len(quotes)}


@router.get("/stats/brokers")
async def broker_stats(user=Depends(require_role(Roles.VIEWER))):
    """Aggregated stats per broker from broker_daily_quotes."""
    rows = await fetch_all(
        """
        SELECT broker_name,
               COUNT(*) AS total_quotes,
               COUNT(DISTINCT isin) AS unique_isins,
               MAX(quote_date) AS latest_quote,
               COUNT(DISTINCT quote_date) AS active_days
        FROM broker_daily_quotes
        GROUP BY broker_name
        ORDER BY total_quotes DESC
        """
    )
    return {"brokers": [dict(r) for r in rows]}


@router.get("/ingest-log")
async def ingest_log(limit: int = Query(50, le=200), user=Depends(require_role(Roles.VIEWER))):
    """Recent file ingest history."""
    rows = await fetch_all(
        "SELECT * FROM broker_ingest_log ORDER BY ingested_at DESC LIMIT :limit",
        {"limit": limit},
    )
    return {"log": [dict(r) for r in rows]}


@router.get("/")
async def list_quotes(
    broker: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    limit: int = Query(200, le=1000),
    user=Depends(require_role(Roles.VIEWER)),
):
    """List recent broker daily quotes."""
    conditions = []
    params: dict = {"limit": limit}
    if broker:
        conditions.append("broker_name = :broker")
        params["broker"] = broker
    if date_from:
        conditions.append("quote_date >= :date_from")
        params["date_from"] = date_from
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    rows = await fetch_all(
        f"SELECT * FROM broker_daily_quotes {where} ORDER BY quote_date DESC, ytm DESC LIMIT :limit",
        params,
    )
    enriched_rows = [_attach_quote_category_profile(dict(r)) for r in rows]
    return {"quotes": enriched_rows, "count": len(enriched_rows)}


# ── Broker identification ──────────────────────────────────────────────────────

class BrokerRenameRequest(BaseModel):
    old_name: str = "Unknown"
    new_name: str
    source_file: Optional[str] = None  # if given, only rename quotes from that file


@router.get("/unknown-brokers")
async def get_unknown_brokers(user=Depends(require_role(Roles.VIEWER))):
    """List all source files ingested as 'Unknown' broker, with quote counts."""
    rows = await fetch_all(
        """SELECT source_file, quote_date, COUNT(*) AS quote_count,
                  MAX(extracted_at) AS last_seen
           FROM broker_daily_quotes
           WHERE broker_name = 'Unknown'
           GROUP BY source_file, quote_date
           ORDER BY last_seen DESC
           LIMIT 50"""
    )
    return {"unknown_files": [dict(r) for r in rows], "total": len(rows)}


@router.patch("/rename-broker")
async def rename_broker(
    req: BrokerRenameRequest,
    background_tasks: BackgroundTasks,
    user=Depends(require_role(Roles.PORTFOLIO)),
):
    """Rename a broker across broker_daily_quotes and broker_ingest_log."""
    params: dict = {"old": req.old_name, "new": req.new_name}
    where_q = "WHERE broker_name = :old"
    where_log = "WHERE broker_name = :old"
    if req.source_file:
        where_q += " AND source_file = :file"
        where_log += " AND filename = :file"
        params["file"] = req.source_file
    await execute_query(
        f"UPDATE broker_daily_quotes SET broker_name = :new {where_q}", params
    )
    log_params: dict = {"old": req.old_name, "new": req.new_name}
    if req.source_file:
        log_params["file"] = req.source_file
    await execute_query(
        f"UPDATE broker_ingest_log SET broker_name = :new {where_log}", log_params
    )
    background_tasks.add_task(refresh_broker_profiles)
    return {"ok": True, "renamed_from": req.old_name, "renamed_to": req.new_name}


@router.get("/{isin}")
async def get_quotes(isin: str, days: int = Query(30, le=365), user=Depends(require_role(Roles.VIEWER))):
    """Latest quotes for a given ISIN across all brokers."""
    from datetime import timedelta
    date_from = str(date.today() - timedelta(days=days))
    rows = await fetch_all(
        """
        SELECT broker_name, quote_date, bond_name, clean_price, ytm, ytm_semi,
               rating, rating_agency, coupon_rate, maturity_date, category,
               quantity_lacs, source_file, source_format
        FROM broker_daily_quotes
        WHERE isin = :isin AND quote_date >= :date_from
        ORDER BY quote_date DESC, ytm DESC
        """,
        {"isin": isin.strip().upper(), "date_from": date_from},
    )
    enriched_rows = [_attach_quote_category_profile(dict(r)) for r in rows]
    return {"isin": isin.upper(), "quotes": enriched_rows, "count": len(enriched_rows)}
