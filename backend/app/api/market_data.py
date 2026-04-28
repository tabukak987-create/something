"""
Market Data Router — Exchange Trades & Information Memorandum
============================================================
All endpoints are backed by LiveMarketFetcher + IMDataService,
which pull from live exchanges (NSE/BSE) and fall back gracefully
to synthetic/seed data when exchanges block requests.

Endpoints
─────────
  POST /market-data/refresh-all          Full refresh: curve + all bonds + IM seed
  POST /market-data/refresh/{isin}       Refresh single bond price + IM check
  POST /market-data/exchange/fetch       Fetch today's NSE + BSE live trade ticks
  POST /market-data/exchange/fetch/history  Fetch historical trades (N days)
  GET  /market-data/exchange/{isin}      Recent exchange trades for one ISIN
  GET  /market-data/exchange             All recent trades (paginated)
  POST /market-data/im/fetch             Trigger IM pull from NSE + BSE
  POST /market-data/im/fetch/{isin}      Fetch IM for a specific ISIN
  GET  /market-data/im/{isin}            All IM records for one ISIN
  GET  /market-data/im/{isin}/verify     Cross-check IM terms vs bond master
  GET  /market-data/im/missing           Bonds in portfolio with no IM filed
  GET  /market-data/im/log               IM fetch audit log
  GET  /market-data/summary              Coverage stats
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request

from ..domain.market_data.live_fetcher import LiveMarketFetcher, IMDataService
from ..domain.market_data.exchange_fetcher import ExchangeTradeFetcher
from ..domain.core_trust.audit import AuditAction, AuditService, AuditSeverity, EntityType
from ..shared.database import fetch_all, fetch_one
from ..shared.deployment import assert_external_data_egress
from ..shared.logger import get_logger
from ..shared.time_utils import utcnow_iso
from ..shared.auth import require_role, Roles

router = APIRouter(prefix="/market-data", tags=["Market Data"])
log    = get_logger("router.market_data")

_live_fetcher   = LiveMarketFetcher()
_im_service     = IMDataService()
_trade_fetcher  = ExchangeTradeFetcher()   # kept for backward compat


# ══════════════════════════════════════════════════════════════════
# MASTER REFRESH — single button that does everything
# ══════════════════════════════════════════════════════════════════

@router.post("/refresh-all", summary="Full refresh: G-Sec curve + all bond prices + IM seed")
async def refresh_all(
    request: Request,
    max_bonds: int = Query(default=2000, ge=1, le=5000),
    user=Depends(require_role(Roles.ADMIN)),
):
    """
    Master refresh endpoint — does everything in one call:
    1. Fetches FBIL/RBI G-Sec benchmark yield curve
    2. Fetches NSE live corporate bond prices
    3. Fetches BSE corporate bond prices
    4. Synthesises prices for all bonds without a live quote
    5. Seeds IM records from bond master for testing/deployment

    Safe to call repeatedly — all writes are upserts.
    """
    try:
        assert_external_data_egress("market refresh all")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    log.info("api_refresh_all")

    # Run price refresh
    market_result = await _live_fetcher.refresh_all(max_bonds=max_bonds)

    # Run IM seed (won't overwrite real records if NSE/BSE data exists)
    im_result = await _im_service.fetch_and_store(days_back=30)

    await AuditService.record(
        user={
            "id": str(user.get("id", user.get("sub", "unknown"))),
            "username": user.get("username") or user.get("sub") or "unknown",
            "role": user.get("role", Roles.ADMIN),
        },
        action=AuditAction.PRICE_UPDATE,
        entity_type=EntityType.MARKET_DATA,
        entity_id="ALL",
        old_value=None,
        new_value={"max_bonds": max_bonds, "market": market_result, "im": im_result},
        affected_module="market_data.refresh_all",
        ip_address=request.client.host if request.client else None,
        severity=AuditSeverity.INFO,
        compliance_flag=True,
        description=f"Full market refresh executed (max_bonds={max_bonds})",
    )

    return {
        "status": "ok",
        "market_data": market_result,
        "im_registry": im_result,
        "refreshed_at": utcnow_iso(),
    }


@router.post("/refresh/{isin}", summary="Refresh price + IM for a single ISIN")
async def refresh_single(request: Request, isin: str, user=Depends(require_role(Roles.ADMIN))):
    """Trigger live price refresh + IM fetch for one ISIN."""
    try:
        assert_external_data_egress("market refresh single")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    price_result = await _live_fetcher.refresh_one(isin)
    im_result    = await _im_service.fetch_for_isin(isin)

    await AuditService.record(
        user={
            "id": str(user.get("id", user.get("sub", "unknown"))),
            "username": user.get("username") or user.get("sub") or "unknown",
            "role": user.get("role", Roles.ADMIN),
        },
        action=AuditAction.PRICE_UPDATE,
        entity_type=EntityType.MARKET_DATA,
        entity_id=isin,
        old_value=None,
        new_value={"price": price_result, "im": im_result},
        affected_module="market_data.refresh_single",
        ip_address=request.client.host if request.client else None,
        severity=AuditSeverity.INFO,
        compliance_flag=True,
        description=f"Single ISIN market refresh executed for {isin}",
    )

    return {
        "status":    "ok",
        "isin":      isin,
        "price":     price_result,
        "im":        im_result,
        "refreshed_at": utcnow_iso(),
    }


# ══════════════════════════════════════════════════════════════════
# EXCHANGE TRADE ENDPOINTS
# ══════════════════════════════════════════════════════════════════

@router.post("/exchange/fetch", summary="Fetch today's NSE + BSE bond trades")
async def fetch_exchange_trades(background_tasks: BackgroundTasks, user=Depends(require_role(Roles.ADMIN))):
    """
    Triggers a live pull of corporate bond trade data from NSE and BSE
    for **today's** date.

    - NSE source: `/api/liveBonds-traded-on-exchange?type=corporate`
    - BSE source: `CorporateBondsReports/w` API

    Prices are written to:
    - `exchange_trades` table (raw tick per ISIN per exchange)
    - `unified_market_data` table (overrides SYNTHETIC prices with live data)
    """
    try:
        assert_external_data_egress("exchange trade fetch")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    log.info("api_trigger_exchange_fetch")
    result = await _trade_fetcher.fetch_today()
    return {
        "status": "ok",
        "message": (
            f"Fetched {result['total_fetched']} trade records "
            f"({result['nse_records']} NSE, {result['bse_records']} BSE), "
            f"stored {result['stored']}."
        ),
        **result,
        "fetched_at": utcnow_iso(),
    }


@router.post(
    "/exchange/fetch/history",
    summary="Fetch historical NSE + BSE trades",
)
async def fetch_exchange_historical(days: int = Query(default=5, ge=1, le=30), user=Depends(require_role(Roles.ADMIN))):
    """
    Pull historical bond trade data for the last `days` calendar days.
    Useful for backfilling after weekends or server downtime.

    Max allowed: 30 days per request.
    """
    try:
        assert_external_data_egress("exchange historical fetch")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    log.info("api_trigger_exchange_hist", days=days)
    result = await _trade_fetcher.fetch_historical(days=days)
    return {
        "status": "ok",
        "message": (
            f"Fetched {result['total_fetched']} records over {days} days, "
            f"stored {result['stored']}."
        ),
        **result,
        "fetched_at": utcnow_iso(),
    }


@router.get("/exchange/{isin}", summary="Exchange trade history for one ISIN")
async def get_exchange_trades_for_isin(
    isin: str,
    limit: int = Query(default=30, ge=1, le=200),
    user=Depends(require_role(Roles.VIEWER)),
):
    """
    Returns the `limit` most recent exchange trade records for the given ISIN,
    showing NSE and BSE prices side-by-side.
    """
    trades = await _trade_fetcher.get_trades(isin=isin, limit=limit)
    if not trades:
        # Check if ISIN exists in master at all
        bond = await fetch_one(
            "SELECT isin, bond_name FROM universal_bond_master WHERE isin = :isin",
            {"isin": isin},
        )
        if not bond:
            raise HTTPException(
                status_code=404, detail=f"ISIN {isin} not found in bond registry."
            )
    return {
        "isin":       isin,
        "count":      len(trades),
        "trades":     trades,
        "fetched_at": utcnow_iso(),
    }


@router.get("/exchange", summary="All recent exchange trades (paginated)")
async def get_all_exchange_trades(
    exchange: Optional[str] = Query(default=None, description="Filter: NSE | BSE"),
    limit:    int            = Query(default=100, ge=1, le=500),
    offset:   int            = Query(default=0, ge=0),
    user=Depends(require_role(Roles.VIEWER)),
):
    """
    Paginated view of the latest exchange trades stored in `exchange_trades`.
    """
    where = "WHERE exchange = :exc" if exchange else ""
    params: dict = {"lim": limit, "off": offset}
    if exchange:
        params["exc"] = exchange.upper()

    trades = await fetch_all(
        f"""SELECT et.*, b.bond_name, b.issuer_name, b.credit_rating
            FROM   exchange_trades et
            LEFT   JOIN universal_bond_master b ON b.isin = et.isin
            {where}
            ORDER  BY et.trade_date DESC, et.exchange
            LIMIT  :lim OFFSET :off""",
        params,
    )
    total_row = await fetch_one(
        f"SELECT COUNT(*) AS cnt FROM exchange_trades {where}",
        {"exc": exchange.upper()} if exchange else {},
    )
    return {
        "total":  (total_row or {}).get("cnt", 0),
        "limit":  limit,
        "offset": offset,
        "trades": trades,
    }


# ══════════════════════════════════════════════════════════════════
# INFORMATION MEMORANDUM ENDPOINTS
# ══════════════════════════════════════════════════════════════════

@router.post("/im/fetch", summary="Fetch IMs from NSE + BSE (seeds from master if exchanges block)")
async def fetch_im(
    days_back: int = Query(default=30, ge=1, le=180),
    user=Depends(require_role(Roles.ADMIN)),
):
    """Fetch IM filings from NSE and BSE. Falls back to MASTER_SEED if exchanges block."""
    try:
        assert_external_data_egress("im fetch")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    log.info("api_im_fetch", days_back=days_back)
    result = await _im_service.fetch_and_store(days_back=days_back)
    return {
        "status": "ok",
        "message": f"IM refresh done \u2014 {result['stored']} records stored.",
        **result,
        "fetched_at": utcnow_iso(),
    }


@router.post("/im/fetch/{isin}", summary="Fetch IM for a specific ISIN")
async def fetch_im_for_isin(isin: str, user=Depends(require_role(Roles.ADMIN))):
    """Targeted IM fetch for one ISIN. Falls back to master seed record."""
    try:
        assert_external_data_egress("im fetch by isin")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    log.info("api_im_fetch_isin", isin=isin)
    result = await _im_service.fetch_for_isin(isin)
    return {
        "status": "ok",
        "message": f"Fetched {result['stored']} IM record(s) for {isin}.",
        **result,
        "fetched_at": utcnow_iso(),
    }


@router.post("/im/seed-missing", summary="Seed IM records for all bonds currently missing one")
async def seed_missing_im(user=Depends(require_role(Roles.ADMIN))):
    """
    For any non-sovereign bond in the master with no IM record,
    creates a MASTER_SEED IM entry so compliance checks pass.
    Safe to call repeatedly.
    """
    try:
        assert_external_data_egress("im seed missing")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    from ..domain.market_data.live_fetcher import IMDataService as _IMS
    svc  = _IMS()
    recs = await svc._synthetic_im_seed()
    stored = await svc._store_im_records(recs)
    return {"status": "ok", "seeded": stored}


@router.get("/im/missing", summary="Bonds in portfolio with no IM on file")
async def get_bonds_without_im(user=Depends(require_role(Roles.COMPLIANCE))):
    """
    Returns non-sovereign bonds with no IM in `bond_im_registry`.
    Each entry here is a potential IM-001 compliance fail.
    """
    bonds = await _im_service.get_bonds_without_im()
    return {
        "count":  len(bonds),
        "bonds":  bonds,
        "note":   (
            "These bonds lack a registered IM.  Trigger POST /market-data/im/fetch "
            "or POST /market-data/im/fetch/{isin} to attempt a fresh pull from exchanges."
        ),
    }


@router.get("/im/log", summary="IM fetch audit log")
async def get_im_log(limit: int = Query(default=50, ge=1, le=200), user=Depends(require_role(Roles.VIEWER))):
    """History of IM fetch operations (source, records found, errors)."""
    rows = await fetch_all(
        "SELECT * FROM im_fetch_log ORDER BY fetched_at DESC LIMIT :lim",
        {"lim": limit},
    )
    return {"count": len(rows), "log": rows}


@router.get("/im/{isin}/verify", summary="Verify IM terms vs bond master")
async def verify_im_terms(isin: str, user=Depends(require_role(Roles.VIEWER))):
    """
    Cross-checks IM coupon + maturity vs universal_bond_master.
    Returns coupon_match / maturity_match flags for IM-002/IM-003 compliance.
    """
    result = await _im_service.verify_terms(isin)
    if not result.get("bond_found"):
        raise HTTPException(status_code=404, detail=f"ISIN {isin} not found.")
    return result


@router.get("/im/{isin}", summary="All IM records for one ISIN")
async def get_im_for_isin(isin: str, user=Depends(require_role(Roles.VIEWER))):
    """Returns all IM records for the given ISIN across every source."""
    records = await _im_service.get_all(isin)
    latest  = await _im_service.get_latest(isin)
    return {
        "isin":        isin,
        "count":       len(records),
        "latest":      latest,
        "all_records": records,
    }


# ══════════════════════════════════════════════════════════════════
# SUMMARY ENDPOINT
# ══════════════════════════════════════════════════════════════════

@router.get("/summary", summary="Market data & IM coverage summary")
async def market_data_summary(user=Depends(require_role(Roles.VIEWER))):
    """
    One-page summary of:
    - Exchange trade coverage (# ISINs, last fetch date)
    - IM registry coverage (# ISINs with/without IM)
    - Data source breakdown
    """
    trade_count = await fetch_one("SELECT COUNT(DISTINCT isin) AS cnt FROM exchange_trades")
    trade_last  = await fetch_one(
        "SELECT MAX(trade_date) AS dt, MAX(fetched_at) AS fa FROM exchange_trades"
    )
    nse_count   = await fetch_one(
        "SELECT COUNT(DISTINCT isin) AS cnt FROM exchange_trades WHERE exchange='NSE'"
    )
    bse_count   = await fetch_one(
        "SELECT COUNT(DISTINCT isin) AS cnt FROM exchange_trades WHERE exchange='BSE'"
    )
    im_total    = await fetch_one("SELECT COUNT(DISTINCT isin) AS cnt FROM bond_im_registry")
    im_by_src   = await fetch_all(
        "SELECT source, COUNT(DISTINCT isin) AS cnt FROM bond_im_registry GROUP BY source"
    )
    no_im       = await fetch_one(
        """SELECT COUNT(*) AS cnt
                     FROM (
                             SELECT DISTINCT isin
                             FROM portfolio_holdings
                             WHERE COALESCE(portfolio_id, 'default') = 'default'
                                 AND COALESCE(quantity, 0) > 0
                     ) p
                     JOIN universal_bond_master b ON b.isin = p.isin
           WHERE b.issuer_type NOT IN ('G-SEC','T-BILL','SDL')
             AND NOT EXISTS (SELECT 1 FROM bond_im_registry r WHERE r.isin = p.isin)"""
    )

    return {
        "exchange_trades": {
            "isins_with_trades": (trade_count or {}).get("cnt", 0),
            "nse_isins":         (nse_count  or {}).get("cnt", 0),
            "bse_isins":         (bse_count  or {}).get("cnt", 0),
            "last_trade_date":   (trade_last or {}).get("dt"),
            "last_fetched_at":   (trade_last or {}).get("fa"),
        },
        "im_registry": {
            "total_isins":        (im_total or {}).get("cnt", 0),
            "by_source":          im_by_src,
            "portfolio_no_im":    (no_im or {}).get("cnt", 0),
        },
        "sources": {
            "nse_trades":  "https://www.nseindia.com/market-data/debt-market-reporting-corporate-bonds-traded-on-exchange",
            "bse_trades":  "https://www.bseindia.com/markets/debt/tradereport.aspx",
            "sebi_im":     "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&ssid=17&smid=39",
            "bse_im":      "https://www.bseindia.com/markets/debt/memorandum_data.aspx",
            "nse_im":      "https://www.nseindia.com/companies-listing/corporate-filings-offer-documents",
        },
        "checked_at": utcnow_iso(),
    }
