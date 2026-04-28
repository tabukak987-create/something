"""
News Router — Bond Market News Aggregator
==========================================
Endpoints for bond-market news from multiple free, public RSS and API sources.

Endpoints
─────────
  GET  /news                   Latest bond news (filtered, paginated)
  GET  /news/stats             News service statistics
  GET  /news/isin/{isin}       News relevant to a specific bond
  POST /news/refresh           Trigger a manual news refresh
  POST /news/{hash_id}/read    Mark a news article as read
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..domain.market_data.news_fetcher import NewsFetcher
from ..shared.database import execute_query, fetch_one
from ..shared.deployment import assert_external_data_egress
from ..shared.logger import get_logger
from ..shared.auth import require_role, Roles
from ..shared.time_utils import utcnow_iso

router = APIRouter(prefix="/news", tags=["News"])
log    = get_logger("router.news")

_news_service = NewsFetcher()


# ══════════════════════════════════════════════════════════════════
# GET — Latest News
# ══════════════════════════════════════════════════════════════════

@router.get("", summary="Latest bond market news")
async def get_news(
    limit:    int            = Query(default=50, ge=1, le=200),
    category: Optional[str] = Query(default=None, description="REGULATORY | MARKET | MACRO"),
    source:   Optional[str] = Query(default=None, description="Source code: RBI, SEBI, ET_BONDS, MC_BONDS, etc."),
    search:   Optional[str] = Query(default=None, description="Search in title/summary/issuers"),
    days:     int            = Query(default=30, ge=1, le=365),
    user=Depends(require_role(Roles.VIEWER)),
):
    """
    Returns bond-market news articles sorted by relevance and date.

    All news is pre-filtered for bond/debt-market relevance.

    Categories:
    - **REGULATORY**: RBI policy, SEBI circulars — highest impact
    - **MARKET**: Bond market news, trade data, yield changes
    - **MACRO**: Economic indicators affecting bond prices (inflation, GDP, fiscal)

    Relevance score (0-100) considers:
    - Source authority (RBI/SEBI > ET > MoneyControl)
    - Keyword density (bond, yield, NCD, rating, etc.)
    - Portfolio issuer mentions (IRFC, Tata, Axis, etc.)
    """
    articles = await _news_service.get_news(
        limit=limit, category=category, source=source,
        search=search, days=days,
    )
    return {
        "count":    len(articles),
        "articles": articles,
        "filters": {
            "category": category,
            "source":   source,
            "search":   search,
            "days":     days,
        },
        "fetched_at": utcnow_iso(),
    }


# ══════════════════════════════════════════════════════════════════
# GET — News for Specific Bond
# ══════════════════════════════════════════════════════════════════

@router.get("/isin/{isin}", summary="News relevant to a specific bond")
async def get_news_for_bond(
    isin: str,
    limit: int = Query(default=20, ge=1, le=100),
    user=Depends(require_role(Roles.VIEWER)),
):
    """
    Returns news articles mentioning the issuer of the specified bond.

    Useful for per-bond due diligence — shows recent events that may
    affect the bond's credit quality or price.
    """
    articles = await _news_service.get_news_for_isin(isin, limit=limit)
    return {
        "isin":     isin,
        "count":    len(articles),
        "articles": articles,
    }


# ══════════════════════════════════════════════════════════════════
# GET — News Statistics
# ══════════════════════════════════════════════════════════════════

@router.get("/stats", summary="News service statistics")
async def news_stats(user=Depends(require_role(Roles.VIEWER))):
    """
    Coverage stats: total articles, breakdown by source and category,
    last fetch timestamp.
    """
    stats = await _news_service.get_stats()
    return {
        "status": "ok",
        **stats,
        "sources_available": [
            {"code": "RBI",       "name": "RBI Press Releases",       "type": "RSS"},
            {"code": "RBI_NOTIF", "name": "RBI Notifications",        "type": "RSS"},
            {"code": "SEBI",      "name": "SEBI Circulars",           "type": "HTML"},
            {"code": "ET_BONDS",  "name": "Economic Times - Bonds",   "type": "RSS"},
            {"code": "ET_ECON",   "name": "Economic Times - Economy", "type": "RSS"},
            {"code": "MC_BONDS",  "name": "MoneyControl - Bonds",     "type": "RSS"},
            {"code": "MC_ECON",   "name": "MoneyControl - Economy",   "type": "RSS"},
            {"code": "LIVEMINT",  "name": "Livemint - Markets",       "type": "RSS"},
            {"code": "BS_MARKETS","name": "Business Standard Markets", "type": "RSS"},
        ],
    }


# ══════════════════════════════════════════════════════════════════
# POST — Refresh News
# ══════════════════════════════════════════════════════════════════

@router.post("/refresh", summary="Trigger manual news refresh from all sources")
async def refresh_news(
    max_age_days: int = Query(default=7, ge=1, le=30),
    user=Depends(require_role(Roles.ADMIN)),
):
    """
    Fetches news from all configured RSS feeds and regulatory sources.

    Sources:
    - RBI press releases and notifications (monetary policy, rate decisions)
    - SEBI circulars (regulatory changes affecting bond markets)
    - Economic Times bonds section
    - MoneyControl bonds & economy
    - Livemint & Business Standard markets

    Articles are filtered for bond-market relevance using keyword matching
    and scored by relevance (0-100). Duplicates are automatically removed.
    """
    try:
        assert_external_data_egress("news refresh")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    log.info("api_news_refresh", max_age_days=max_age_days)
    result = await _news_service.fetch_all_sources(max_age_days=max_age_days)
    return result


# ══════════════════════════════════════════════════════════════════
# POST — Mark as Read
# ══════════════════════════════════════════════════════════════════

@router.post("/{hash_id}/read", summary="Mark a news article as read")
async def mark_read(hash_id: str, user=Depends(require_role(Roles.VIEWER))):
    """Mark a single news article as read."""
    article = await fetch_one(
        "SELECT id FROM bond_news WHERE hash_id = :h",
        {"h": hash_id},
    )
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    await execute_query(
        "UPDATE bond_news SET is_read = 1 WHERE hash_id = :h",
        {"h": hash_id},
    )
    return {"status": "ok", "hash_id": hash_id, "is_read": True}
