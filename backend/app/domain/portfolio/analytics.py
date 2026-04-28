"""
Institutional Portfolio Analytics Engine
=========================================
All server-side calculations for PF Trust Grade bond portfolio:
 - Accrued Interest & Dirty Price
 - Weighted YTM, Modified Duration, Macaulay Duration, Convexity, WAM
 - XIRR, Time-Weighted Return, Accrual Return
 - Credit Score, Liquidity Score
 - Health Score (100-point model)
 - Maturity Ladder & Cash-flow Schedule
 - Allocation Breakdowns with threshold alerts

Tolerance: Â±0.01% yields, Â±â‚¹1 valuation rounding.
"""

from __future__ import annotations

import math
import json
import re
from datetime import date, datetime, timedelta
from typing import Optional

from app.shared.database import fetch_all, fetch_one, execute_query
from app.shared.logger import get_logger
from app.shared.market_config import (
    GSEC_YIELD_CURVE,
    RATING_SPREAD_BPS,
    interpolate_gsec_yield,
)

log = get_logger("service.portfolio_analytics")

# â”€â”€ Result Cache (5-minute TTL for expensive full portfolio computation) â”€â”€â”€â”€â”€â”€
import asyncio as _asyncio
import time as _time
_ANALYTICS_CACHE: dict = {"result": {}, "ts": {}, "lock": None}
_ANALYTICS_CACHE_TTL = 0  # disabled for enforcement mode


async def _get_or_compute_analytics(portfolio_id: str = "default", force: bool = False) -> dict:
    """Return cached analytics result or recompute if stale."""
    if _ANALYTICS_CACHE["lock"] is None:
        _ANALYTICS_CACHE["lock"] = _asyncio.Lock()
    async with _ANALYTICS_CACHE["lock"]:
        now = _time.monotonic()
        pid = (portfolio_id or "default").strip() or "default"
        cached_result = (_ANALYTICS_CACHE["result"] or {}).get(pid)
        cached_ts = (_ANALYTICS_CACHE["ts"] or {}).get(pid, 0.0)
        if not force and cached_result and (now - cached_ts) < _ANALYTICS_CACHE_TTL:
            return cached_result
        result = await _compute_full_portfolio_analytics_impl(portfolio_id=pid)
        result = await _overlay_canonical_portfolio_summary(result, portfolio_id=pid)
        _ANALYTICS_CACHE["result"][pid] = result
        _ANALYTICS_CACHE["ts"][pid] = now
        return result


async def _overlay_canonical_portfolio_summary(result: dict, portfolio_id: str) -> dict:
    """Align analytics summary fields with the canonical portfolio view."""
    try:
        from app.domain.portfolio.engine import build_portfolio_view
    except Exception:
        return result

    try:
        portfolio_view = await build_portfolio_view(portfolio_id=portfolio_id)
    except Exception as exc:
        log.warning("portfolio_summary_overlay_failed", portfolio_id=portfolio_id, error=str(exc))
        return result

    view_summary = portfolio_view.get("summary") or {}
    summary = dict(result.get("summary") or {})

    summary_updates = {
        "invested_value": view_summary.get("invested_value", summary.get("total_invested", 0.0)),
        "current_value": view_summary.get("current_value", summary.get("total_dirty_value", 0.0)),
        "total_dirty_value": view_summary.get("current_value", summary.get("total_dirty_value", 0.0)),
        "pnl": view_summary.get("pnl", summary.get("unrealized_gain", 0.0)),
        "pnl_percent": view_summary.get("pnl_percent", summary.get("unrealized_gain_pct", 0.0)),
        "unrealized_gain": view_summary.get("unrealized_gain", summary.get("unrealized_gain", 0.0)),
        "unrealized_gain_pct": view_summary.get("unrealized_gain_pct", summary.get("unrealized_gain_pct", 0.0)),
        "holdings_count": view_summary.get("holdings_count", summary.get("holdings_count", 0)),
        "aum_crores": view_summary.get("aum_crores", summary.get("aum_crores", 0.0)),
        "total_market_value": view_summary.get("total_market_value", summary.get("total_market_value", 0.0)),
        "total_invested": view_summary.get("total_invested", summary.get("total_invested", 0.0)),
        "invested_crores": view_summary.get("invested_crores", summary.get("invested_crores", 0.0)),
        "face_value_crores": view_summary.get("face_value_crores", summary.get("face_value_crores", 0.0)),
        "total_clean_value": view_summary.get("total_clean_value", summary.get("total_clean_value", 0.0)),
        "total_accrued_interest": view_summary.get("total_accrued_interest", summary.get("total_accrued_interest", 0.0)),
        "weighted_ytm": view_summary.get("weighted_ytm", summary.get("weighted_ytm", 0.0)),
        "weighted_modified_duration": view_summary.get("weighted_modified_duration", summary.get("weighted_modified_duration", 0.0)),
        "weighted_duration": view_summary.get("weighted_duration", summary.get("weighted_duration", 0.0)),
        "weighted_avg_maturity": view_summary.get("weighted_avg_maturity", summary.get("weighted_avg_maturity", 0.0)),
        "portfolio_rating": view_summary.get("portfolio_rating", summary.get("portfolio_rating", "")),
        "credit_quality_rating": view_summary.get("credit_quality_rating", summary.get("credit_quality_rating", "")),
        "data_traceability_status": view_summary.get("data_traceability_status", summary.get("data_traceability_status", "")),
    }
    summary.update(summary_updates)

    result = dict(result)
    result["summary"] = summary
    return result

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# RATING SYSTEM
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

RATING_SCORE = {
    "SOV": 1.0, "SOVEREIGN": 1.0,
    "AAA+": 1.5,
    "AAA": 2.0,
    "AA+": 3.0, "AA": 4.0, "AA-": 5.0,
    "A+": 6.0, "A": 7.0, "A-": 8.0,
    "BBB+": 9.0, "BBB": 10.0, "BBB-": 11.0,
    "BB+": 12.0, "BB": 13.0,
    "NR": 7.0, "UNKNOWN": 7.0, "": 7.0,
}

# â”€â”€ Agency-prefix stripping + canonical form â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
_AGENCY_PREFIXES = re.compile(
    r"^(?:CRISIL|ICRA|CARE|BRICKWORKS|IND|INDIA\s*RATINGS|FITCH|MOODY|S&P)[\s/\-]*",
    re.IGNORECASE,
)
_RATING_TOKEN_RE = re.compile(
    r"(?<![A-Z0-9])(SOVEREIGN|SOV|AAA\+|AAA|AA\+|AA\-|AA|A\+|A\-|A|BBB\+|BBB\-|BBB|BB\+|BB)(?![A-Z0-9])",
    re.IGNORECASE,
)
_KNOWN_AGENCIES = {
    "CRISIL": "CRISIL",
    "ICRA": "ICRA",
    "CARE": "CARE",
    "BRICKWORKS": "BRICKWORKS",
    "INDIA RATINGS": "INDIA RATINGS",
    "IND": "INDIA RATINGS",
    "FITCH": "FITCH",
    "MOODY": "MOODY",
    "S&P": "S&P",
}
_GOV_ISSUERTYPES = {"G-SEC", "SDL", "T-BILL", "CENTRAL_GOVT", "STATE_GOVT", "SOVEREIGN"}
_SOV_RAW = {"SOV", "G-SEC", "GSEC", "SOVEREIGN"}

# â”€â”€ SDL ISIN pattern: INxx... where xx = state code digit (NOT INE) â”€â”€
_SDL_ISIN_RE = re.compile(r'^IN\d')

# â”€â”€ Sovereign name patterns in issuer_name â”€â”€
_SOV_NAME_PATTERNS = [
    'STATE DEVELOPMENT LOAN', 'STATE DEV LOAN', 'SDL ',
    ' GOI ', 'GOVT OF INDIA', 'GOVERNMENT OF INDIA',
    'UDAY BOND', 'UDAY ',
    'GUJARAT STATE INVESTMENTS',
]

# â”€â”€ Known PSU/govt-backed entity ratings (implicit sovereign guarantee) â”€â”€
_KNOWN_RATINGS = {
    'INDIAN RAILWAY FINANCE': 'AAA',
    'IRFC': 'AAA',
    'HDB FINANCIAL SERVICES': 'AAA',
    'L&T FINANCE': 'AAA',
    'PNB HOUSING FINANCE': 'AA+',
    'TATA CAPITAL FINANCIAL': 'AAA',
    'TATA CAPITAL HOUSING': 'AAA',
    'TATA MOTORS FINANCE': 'AA',
    'AXIS BANK': 'AAA',
    'WEST BENGAL STATE ELECTRICITY': 'AA-',
    'U.P. POWER CORPORATION': 'A',
    'UP POWER CORPORATION': 'A',
}


def normalize_rating(
    raw: str | None,
    issuer_type: str | None = None,
    isin: str | None = None,
    issuer_name: str | None = None,
) -> str:
    """
    Return a canonical rating string.
    - Strip rating-agency prefixes (CRISIL, ICRA, CARE, IND, Brickworks â€¦)
    - Map SOV / G-SEC / GSEC â†’ SOVEREIGN
    - Government issuer_types â†’ SOVEREIGN regardless of stored rating
    - Detect SDL bonds by ISIN prefix (IN+digit, NOT INE)
    - Detect sovereign bonds by issuer name patterns
    - Look up known corporate ratings for major issuers
    - Empty / None / SELECT â†’ NR
    """
    itype = (issuer_type or "").upper().strip()
    raw_up = (raw or "").upper().strip()
    isin_up = (isin or "").upper().strip()
    name_up = (issuer_name or "").upper().strip()

    # â”€â”€ Government classification overrides everything â”€â”€
    if itype in _GOV_ISSUERTYPES or raw_up in _SOV_RAW:
        return "SOVEREIGN"

    # â”€â”€ SDL ISIN: starts with IN followed by digit (not INE) â”€â”€
    if isin_up and _SDL_ISIN_RE.match(isin_up) and not isin_up.startswith('INE'):
        return "SOVEREIGN"

    # â”€â”€ Sovereign name detection â”€â”€
    if name_up:
        for pat in _SOV_NAME_PATTERNS:
            if pat in name_up:
                return "SOVEREIGN"

    # Strip agency prefix ("CRISIL AAA" â†’ "AAA", "IND/AA" â†’ "AA")
    cleaned = _AGENCY_PREFIXES.sub("", raw_up).strip().rstrip("/").strip()

    # Handle doubled/compound patterns like "SELECT/SELECT", "AAA/STABLE", "AA+/WATCH"
    if "/" in cleaned:
        parts = [p.strip() for p in cleaned.split("/")]
        for part in parts:
            if part in RATING_SCORE:
                return part
        cleaned = parts[0] if parts else cleaned

    # Handle placeholders that aren't real ratings
    if cleaned in ("", "SELECT", "WATCHED", "SUSPENDED", "NA", "N/A", "NOT RATED", "NOT AVAILABLE"):
        # Try known corporate ratings before falling back to NR
        if name_up:
            for pattern, known_rat in _KNOWN_RATINGS.items():
                if pattern in name_up:
                    return known_rat
        return "NR"

    token_match = _RATING_TOKEN_RE.search(cleaned)
    if token_match:
        token = token_match.group(1).upper()
        return "SOVEREIGN" if token in {"SOV", "SOVEREIGN"} else token

    return cleaned or "NR"


def _clean_rating_agency(raw_agency: str | None) -> str | None:
    txt = str(raw_agency or "").strip()
    if not txt:
        return None
    up = txt.upper()
    for key, canonical in _KNOWN_AGENCIES.items():
        if key in up:
            return canonical
    return None

RATING_LABEL = {
    1.0: "SOV", 2.0: "AAA", 3.0: "AA+", 4.0: "AA", 5.0: "AA-",
    6.0: "A+", 7.0: "A", 8.0: "A-", 9.0: "BBB+", 10.0: "BBB",
}


def _is_low_rating(rating: str | None) -> bool:
    r = (rating or "NR").upper().strip()
    return RATING_SCORE.get(r, RATING_SCORE["NR"]) > RATING_SCORE["AA-"]


def _rating_reason(rating: str, raw_rating: str | None, issuer_type: str | None) -> str:
    r = (rating or "NR").upper().strip()
    raw = (raw_rating or "").upper().strip()
    itype = (issuer_type or "").upper().strip()

    if not _is_low_rating(r):
        return ""

    if r in ("SOV", "SOVEREIGN") or itype in _GOV_ISSUERTYPES:
        return ""

    if r == "NR":
        if raw and raw not in ("NR", "UNKNOWN", ""):
            return f"Rating value '{raw}' is non-standard; treated as NR and flagged for review."
        return "No recognized external credit rating available in current dataset."

    if r in ("A+", "A", "A-"):
        return "A-category credit; below AA- preference threshold, requires active monitoring."

    if r in ("BBB+", "BBB", "BBB-"):
        return "Lower investment grade credit profile; elevated downgrade and spread-widening risk."

    return "Sub-investment-grade / weak credit profile relative to portfolio quality targets."

PF_TRUST_LIMITS = {
    "min_rating": "A",           # Minimum investment grade
    "max_single_issuer_pct": 15, # Private sector
    "max_sector_pct": 40,
    "max_below_aa_pct": 20,
    "min_sovereign_pct": 20,
    "max_avg_duration": 8.0,     # years
    "concentration_threshold": 10,
}


def _normalized_allocation_category(
    category: str | None,
    sector: str | None,
    issuer_type: str | None,
    issuer_name: str | None,
    isin: str | None,
) -> str:
    """Map raw labels to the canonical four-category model."""
    c = (category or "").upper().strip()
    s = (sector or "").upper().strip()
    it = (issuer_type or "").upper().strip()
    nm = (issuer_name or "").upper().strip()
    _ = (isin or "").upper().strip()

    text = f"{c} {s} {it} {nm}"
    if any(tok in text for tok in ("EQUITY", "ETF", "MUTUAL FUND", "INDEX")):
        return "EQUITY"
    if any(tok in text for tok in ("G-SEC", "GSEC", "SDL", "T-BILL", "TBILL", "SOV", "SOVEREIGN", "GOV", "GOVT", "GOVERNMENT", "CENTRAL", "STATE GOVERNMENT", "STATE DEVELOPMENT LOAN", "TREASURY BILL")):
        return "STATE_CENTRAL"
    if any(tok in text for tok in ("PSU", "PUBLIC SECTOR")):
        return "PSU"
    if any(tok in text for tok in ("PRIVATE", "CORPORATE", "NBFC", "BANK", "FINANC")):
        return "PRIVATE"

    return "PRIVATE"


def _normalized_allocation_sector(
    sector: str | None,
    category: str | None,
    issuer_type: str | None,
    issuer_name: str | None,
    isin: str | None,
) -> str:
    c = (category or "").upper().strip()
    s = (sector or "").upper().strip()
    it = (issuer_type or "").upper().strip()
    nm = (issuer_name or "").upper().strip()

    def _match_any(text: str, tokens: tuple[str, ...]) -> bool:
        return any(tok in text for tok in tokens)

    canonical_direct = {
        "CORPORATE_BONDS": "CORPORATE_BONDS",
        "CORPORATE BONDS": "CORPORATE_BONDS",
        "GOVERNMENT": "GOVERNMENT",
        "PSU": "PSU",
        "SOVEREIGN": "SOVEREIGN",
    }

    # Strict sector-first canonicalization so sheet and API paths use the same
    # dictionary regardless of issuer-type noise for a subset of holdings.
    if s in canonical_direct:
        return canonical_direct[s]
    if _match_any(s, ("PUBLIC SECTOR", "PSU")):
        return "PSU"
    if _match_any(s, ("CORPORATE", "NBFC", "BANK", "FINANC")):
        return "CORPORATE_BONDS"
    if _match_any(s, ("SOVEREIGN", "STATE DEVELOPMENT LOAN", "UDAY")):
        return "SOVEREIGN"
    if _match_any(s, ("GOV", "G-SEC", "GSEC", "TREASURY", "TBILL", "CENTRAL", "STATE GOVERNMENT")):
        return "GOVERNMENT"

    ci_text = f"{c} {it}"
    if _match_any(ci_text, ("PSU", "PUBLIC SECTOR")):
        return "PSU"
    if _match_any(ci_text, ("SOVEREIGN", "SDL", "UDAY")):
        return "SOVEREIGN"
    if _match_any(ci_text, ("GOV", "G-SEC", "GSEC", "TREASURY", "TBILL", "CENTRAL", "STATE GOVERNMENT")):
        return "GOVERNMENT"
    if _match_any(ci_text, ("CORPORATE", "NBFC", "BANK", "FINANC")):
        return "CORPORATE_BONDS"

    if _match_any(nm, ("STATE DEVELOPMENT LOAN", "UDAY", "GOVT OF INDIA", "GOVERNMENT OF INDIA", "TREASURY BILL")):
        return "SOVEREIGN"

    return "CORPORATE_BONDS"


def _resolve_position_face_value(face_value: float, quantity: float, book_value: float) -> float:
    """
    Normalize holding nominal amount:
    - Some datasets store face_value as per-unit nominal and quantity as units.
    - Some datasets store face_value as total nominal for the full position.
    Use book_value proximity at-par to pick a stable interpretation.
    """
    face = float(face_value or 0)
    qty = float(quantity or 0)
    book = float(book_value or 0)

    if face <= 0:
        return 0.0
    if qty <= 1:
        return face

    total_candidate = face * qty
    if book > 0:
        dist_face = abs(face - book)
        dist_total = abs(total_candidate - book)
        return total_candidate if dist_total < dist_face else face

    # Heuristic fallback when book value is unavailable.
    if face <= 10000:
        return total_candidate
    return face


async def _load_allocation_share_overrides(portfolio_id: str = "default") -> dict[str, dict]:
    """
    Optional overrides sourced from holdings sheet, persisted in DB table:
      portfolio_sheet_allocations(isin, category_bucket, sector_bucket, basis_value)
    Returns map:
      overrides[isin] = {"category": {bucket: value}, "sector": {bucket: value}, "total": value}
    """
    overrides: dict[str, dict] = {}
    try:
        rows = await fetch_all(
            """
            WITH trust_isins AS (
                SELECT DISTINCT isin
                FROM portfolio_positions
                WHERE trust_id = :pid
                UNION
                SELECT DISTINCT isin
                FROM portfolio_holdings
                WHERE COALESCE(portfolio_id, 'default') = :pid
                  AND NOT EXISTS (SELECT 1 FROM portfolio_positions p WHERE p.trust_id = :pid)
            )
            SELECT a.isin, a.category_bucket, a.sector_bucket, a.basis_value
            FROM portfolio_sheet_allocations a
            INNER JOIN trust_isins t ON t.isin = a.isin
            """,
            {"pid": portfolio_id},
        )
    except Exception:
        return overrides

    for r in rows:
        isin = str(r.get("isin") or "").upper().strip()
        if not isin:
            continue
        cat = str(r.get("category_bucket") or "").strip()
        sec = _normalized_allocation_sector(
            str(r.get("sector_bucket") or ""),
            cat,
            None,
            None,
            isin,
        )
        val = float(r.get("basis_value") or 0.0)
        if val <= 0:
            continue
        entry = overrides.setdefault(isin, {"category": {}, "sector": {}, "total": 0.0})
        if cat:
            entry["category"][cat] = entry["category"].get(cat, 0.0) + val
        if sec:
            entry["sector"][sec] = entry["sector"].get(sec, 0.0) + val
        entry["total"] += val
    return overrides


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# ACCRUED INTEREST CALCULATION
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def compute_accrued_interest(
    coupon_rate: float,
    face_value: float,
    maturity_date: date,
    issue_date: Optional[date] = None,
    settlement_date: Optional[date] = None,
    coupon_frequency: int = 2,
) -> dict:
    """
    Compute accrued interest using 30/360 or actual/actual day count convention.
    Indian bonds typically use 30/360 for corporates, actual/actual for G-Secs.

    Returns:
        {accrued_interest, days_since_last_coupon, days_in_period,
         last_coupon_date, next_coupon_date, accrued_pct}
    """
    today = settlement_date or date.today()
    if maturity_date <= today:
        return {
            "accrued_interest": 0.0,
            "days_since_last_coupon": 0,
            "days_in_period": 180,
            "last_coupon_date": str(maturity_date),
            "next_coupon_date": str(maturity_date),
            "accrued_pct": 0.0,
        }

    annual_coupon = coupon_rate / 100.0 * face_value
    period_coupon = annual_coupon / coupon_frequency
    period_months = 12 // coupon_frequency

    # Build coupon schedule backwards from maturity
    coupon_dates = []
    d = maturity_date
    while d > (issue_date or date(2000, 1, 1)):
        coupon_dates.append(d)
        month = d.month - period_months
        year = d.year
        while month <= 0:
            month += 12
            year -= 1
        day = min(d.day, 28)  # safe day
        try:
            d = date(year, month, day)
        except ValueError:
            d = date(year, month, 28)

    coupon_dates.sort()

    # Find last coupon before today and next coupon after today
    last_coupon = coupon_dates[0]
    next_coupon = coupon_dates[-1]
    for i, cd in enumerate(coupon_dates):
        if cd > today:
            next_coupon = cd
            last_coupon = coupon_dates[max(0, i - 1)]
            break
    else:
        last_coupon = coupon_dates[-1]
        next_coupon = maturity_date

    days_since = (today - last_coupon).days
    days_in_period = (next_coupon - last_coupon).days or 1
    if days_since < 0:
        # Settlement before first coupon in schedule (e.g., future issue/purchase date)
        # should accrue zero, never negative accrued interest.
        days_since = 0
    if days_since > days_in_period:
        days_since = days_in_period
    accrued = period_coupon * days_since / days_in_period

    return {
        "accrued_interest": round(accrued, 2),
        "days_since_last_coupon": days_since,
        "days_in_period": days_in_period,
        "last_coupon_date": str(last_coupon),
        "next_coupon_date": str(next_coupon),
        "accrued_pct": round(accrued / face_value * 100, 4),
    }


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# DIRTY PRICE & FULL VALUATION
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def compute_dirty_price(clean_price: float, accrued_per_100: float) -> float:
    """Dirty Price = Clean Price + Accrued Interest (per 100 face)."""
    return round(clean_price + accrued_per_100, 4)


def compute_market_value(price: float, face_value: float, quantity: float = 1.0) -> float:
    """
    Market value from the stored quote price.

    The VM SPF workbook stores price at the lot/unit level, so market value scales
    by quantity rather than by nominal face value.
    """
    return round(price * max(quantity, 1), 2)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# BOND-LEVEL ANALYTICS
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def compute_bond_analytics(
    coupon_rate: float,
    face_value: float,
    clean_price: float,
    ytm: float,
    maturity_date: date,
    coupon_frequency: int = 2,
    issue_date: Optional[date] = None,
    settlement_date: Optional[date] = None,
) -> dict:
    """
    Full bond analytics:
      - Accrued interest
      - Dirty price
      - Macaulay Duration
      - Modified Duration
      - Convexity
      - DV01
      - Spread over G-Sec
    """
    today = settlement_date or date.today()
    years_to_mat = max((maturity_date - today).days / 365.25, 0.01)

    # Accrued interest
    ai = compute_accrued_interest(
        coupon_rate, face_value, maturity_date, issue_date, today, coupon_frequency
    )
    accrued = ai["accrued_interest"]
    accrued_per_100 = accrued / face_value * 100

    # Dirty price
    dirty = compute_dirty_price(clean_price, accrued_per_100)

    # Cash flow based duration & convexity
    freq = coupon_frequency or 2
    n = max(int(math.ceil(years_to_mat * freq)), 1)
    r = ytm / 100.0 / freq
    c = coupon_rate / 100.0 * face_value / freq

    if r <= 0:
        r = 0.0001

    # Full cash flow PV
    pv_total = 0.0
    mac_dur_num = 0.0
    convexity_num = 0.0

    for t in range(1, n + 1):
        cf = c if t < n else c + face_value
        time_years = t / freq
        discount = (1 + r) ** t
        pv_cf = cf / discount

        pv_total += pv_cf
        mac_dur_num += time_years * pv_cf
        convexity_num += (time_years * (time_years + 1 / freq)) * pv_cf

    # If PV is zero (shouldn't happen), fallback
    pv_total = max(pv_total, 0.01)

    macaulay_duration = round(mac_dur_num / pv_total, 4)
    modified_duration = round(macaulay_duration / (1 + r), 4)
    convexity = round(convexity_num / (pv_total * (1 + r) ** 2), 4)
    dv01 = round(dirty / 100.0 * face_value * modified_duration / 10000, 4)

    # Spread over G-Sec
    gsec_yield = interpolate_gsec_yield(years_to_mat)
    spread_bps = round((ytm - gsec_yield) * 100, 2)

    return {
        "accrued_interest": accrued,
        "accrued_per_100": round(accrued_per_100, 4),
        "dirty_price": dirty,
        "clean_price": clean_price,
        "macaulay_duration": macaulay_duration,
        "modified_duration": modified_duration,
        "convexity": convexity,
        "dv01": dv01,
        "spread_over_gsec_bps": spread_bps,
        "gsec_benchmark_yield": gsec_yield,
        "years_to_maturity": round(years_to_mat, 4),
        "coupon_dates": {
            "last": ai["last_coupon_date"],
            "next": ai["next_coupon_date"],
            "days_since_last": ai["days_since_last_coupon"],
            "days_in_period": ai["days_in_period"],
        },
    }


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# COUPON SCHEDULE GENERATOR
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def generate_coupon_schedule(
    isin: str,
    coupon_rate: float,
    face_value: float,
    maturity_date: date,
    issue_date: Optional[date] = None,
    coupon_frequency: int = 2,
    quantity: int = 1,
) -> list[dict]:
    """Generate full coupon schedule for a bond from today to maturity."""
    today = date.today()
    period_months = 12 // coupon_frequency
    annual_coupon = coupon_rate / 100.0 * face_value
    period_coupon = annual_coupon / coupon_frequency

    # Build schedule backwards from maturity
    dates = []
    d = maturity_date
    start = issue_date or date(2000, 1, 1)
    while d > start:
        dates.append(d)
        month = d.month - period_months
        year = d.year
        while month <= 0:
            month += 12
            year -= 1
        day = min(d.day, 28)
        try:
            d = date(year, month, day)
        except ValueError:
            d = date(year, month, 28)

    dates.sort()

    schedule = []
    for cd in dates:
        if cd >= today:
            is_maturity = cd == maturity_date
            cf = period_coupon * quantity
            if is_maturity:
                cf += face_value * quantity  # Principal redemption
            schedule.append({
                "isin": isin,
                "coupon_date": str(cd),
                "coupon_amount": round(period_coupon * quantity, 2),
                "principal_amount": round(face_value * quantity if is_maturity else 0, 2),
                "total_cash_flow": round(cf, 2),
                "is_maturity": is_maturity,
            })

    return schedule


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# PORTFOLIO-LEVEL AGGREGATION
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

async def compute_full_portfolio_analytics(portfolio_id: str = "default", force: bool = False) -> dict:
    """
    Master function â€” computes everything for the portfolio tab.
    Enforcement mode recomputes on every call.
    """
    return await _get_or_compute_analytics(portfolio_id=portfolio_id, force=True)


def invalidate_analytics_cache() -> None:
    """Invalidate the portfolio analytics cache (call after trades/market data updates)."""
    _ANALYTICS_CACHE["result"] = {}
    _ANALYTICS_CACHE["ts"] = {}


async def _compute_full_portfolio_analytics_impl(portfolio_id: str = "default") -> dict:
    today = date.today()
    today_str = str(today)

    # â”€â”€ Step 1: Fetch all holdings with latest market data â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    query = """
    WITH active_positions AS (
        SELECT
            p.isin,
            COALESCE(p.issuer_name, p.instrument_name, p.isin) AS issuer_name,
            COALESCE(p.face_value, 1000.0) AS face_value,
            COALESCE(p.book_value, 0.0) AS book_value,
            COALESCE(p.quantity, 0.0) AS quantity,
            p.purchase_date,
            COALESCE(p.maturity, i.maturity_date, ubm.maturity_date) AS maturity_date,
            COALESCE(p.coupon, i.coupon_rate, ubm.coupon_rate, 0.0) AS coupon_rate,
            NULL AS ytm_at_purchase,
            COALESCE(p.category, i.category, ubm.issuer_type, '') AS category,
            COALESCE(p.sector, i.sector, ubm.sector, '') AS sector,
            COALESCE(p.credit_rating, i.rating_latest, ubm.credit_rating, '') AS rating
        FROM portfolio_positions p
        LEFT JOIN instrument_master i ON i.isin = p.isin
        LEFT JOIN universal_bond_master ubm ON ubm.isin = p.isin
        WHERE p.trust_id = :pid
          AND COALESCE(p.quantity, 0) > 0

        UNION ALL

        SELECT
            h.isin,
            COALESCE(i.issuer, ubm.issuer_name, h.isin) AS issuer_name,
            COALESCE(h.face_value, ubm.face_value, 1000.0) AS face_value,
            (COALESCE(h.quantity, 0.0) * COALESCE(h.purchase_price, 0.0)) AS book_value,
            COALESCE(h.quantity, 0.0) AS quantity,
            h.purchase_date,
            COALESCE(i.maturity_date, ubm.maturity_date) AS maturity_date,
            COALESCE(i.coupon_rate, ubm.coupon_rate, 0.0) AS coupon_rate,
            NULL AS ytm_at_purchase,
            COALESCE(i.category, ubm.issuer_type, '') AS category,
            COALESCE(i.sector, ubm.sector, '') AS sector,
            COALESCE(i.rating_latest, ubm.credit_rating, '') AS rating
        FROM portfolio_holdings h
        LEFT JOIN instrument_master i ON i.isin = h.isin
        LEFT JOIN universal_bond_master ubm ON ubm.isin = h.isin
        WHERE COALESCE(h.portfolio_id, 'default') = :pid
          AND COALESCE(h.quantity, 0) > 0
          AND NOT EXISTS (SELECT 1 FROM portfolio_positions p WHERE p.trust_id = :pid)
    )
    SELECT
        p.isin, p.issuer_name, p.face_value, p.book_value, p.quantity,
        p.purchase_date, p.maturity_date, p.coupon_rate, p.ytm_at_purchase,
        p.category, p.sector,
        COALESCE(NULLIF(TRIM(p.rating), ''), NULLIF(TRIM(b.credit_rating), '')) AS raw_rating,
        NULLIF(TRIM(b.rating_agency), '') AS rating_agency,
        COALESCE(
            NULLIF(UPPER(TRIM(p.rating)), ''),
            NULLIF(UPPER(TRIM(b.credit_rating)), ''),
            CASE WHEN b.issuer_type IN ('G-SEC','SDL','T-BILL','CENTRAL_GOVT','STATE_GOVT','SOVEREIGN')
                      OR (SUBSTR(p.isin,1,2) = 'IN' AND SUBSTR(p.isin,3,1) BETWEEN '0' AND '9' AND p.isin NOT LIKE 'INE%')
                      OR UPPER(COALESCE(p.issuer_name,'')) LIKE '%STATE DEVELOPMENT LOAN%'
                      OR UPPER(COALESCE(p.issuer_name,'')) LIKE '%SDL%'
                      OR UPPER(COALESCE(b.issuer_name,'')) LIKE '%STATE DEVELOPMENT LOAN%'
                      OR UPPER(COALESCE(b.issuer_name,'')) LIKE '%SDL%'
                      OR UPPER(COALESCE(p.issuer_name,'')) LIKE '% GOI %'
                      OR UPPER(COALESCE(b.issuer_name,'')) LIKE '%GOVT OF %'
                      OR UPPER(COALESCE(p.issuer_name,'')) LIKE '%UDAY BOND%'
                      OR UPPER(COALESCE(p.issuer_name,'')) LIKE '%UDAY %'
                      OR UPPER(COALESCE(p.issuer_name,'')) LIKE '%GUJARAT STATE INVESTMENTS%'
                 THEN 'SOVEREIGN' ELSE NULL END
        ) AS rating,
        b.issuer_type  AS bond_issuer_type,
        b.credit_rating AS master_credit_rating,
        m.clean_price, m.ytm, m.modified_duration, m.dv01, m.convexity,
        m.spread_over_gsec, m.traded_volume, m.liquidity_score,
        m.data_source, m.price_date,
        b.coupon_frequency, b.issue_date
    FROM active_positions p
    LEFT JOIN (
        SELECT isin, clean_price, ytm, modified_duration, dv01, convexity,
               spread_over_gsec, traded_volume, liquidity_score, data_source, price_date
        FROM unified_market_data
        WHERE (isin, price_date) IN (
            SELECT isin, MAX(price_date) FROM unified_market_data GROUP BY isin
        )
    ) m ON p.isin = m.isin
    LEFT JOIN universal_bond_master b ON p.isin = b.isin
    WHERE p.quantity > 0
    ORDER BY p.book_value DESC
    """

    holdings_raw = await fetch_all(query, {"pid": portfolio_id})

    # â”€â”€ Step 2: Compute per-holding analytics â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    processed = []
    total_dirty_value = 0.0
    total_clean_value = 0.0
    total_accrued = 0.0
    total_invested = 0.0
    total_face = 0.0
    total_allocation_basis = 0.0

    # Weighted accumulators
    w_ytm = 0.0
    w_mod_dur = 0.0
    w_mac_dur = 0.0
    w_convexity = 0.0
    w_credit = 0.0
    w_liquidity = 0.0
    w_spread = 0.0

    # Allocation buckets
    alloc_category = {}
    alloc_issuer = {}
    alloc_rating = {}
    alloc_sector = {}
    alloc_maturity = {"<1Y": 0.0, "1-3Y": 0.0, "3-5Y": 0.0, "5-7Y": 0.0, "7-10Y": 0.0, ">10Y": 0.0}

    # Cash flow schedule
    all_cash_flows = []

    # Optional sheet-based overrides for allocation split.
    alloc_overrides = await _load_allocation_share_overrides(portfolio_id=portfolio_id)
    used_override_isins: set[str] = set()

    for h in holdings_raw:
        isin = h["isin"]
        face_raw = float(h["face_value"] or 1000)
        qty = float(h["quantity"] or 0)
        book = float(h["book_value"] or 0)
        face = _resolve_position_face_value(face_raw, qty, book) or face_raw
        coupon = float(h["coupon_rate"] or 0)
        clean_price = float(h["clean_price"] or 0.0)
        ytm = float(h["ytm"] or coupon or 7.0)
        freq = int(h["coupon_frequency"] or 2) or 2
        raw_r = str(h.get("raw_rating") or h.get("rating") or "").strip()
        bond_itype = str(h.get("bond_issuer_type") or h.get("category") or "").upper()
        rating = normalize_rating(raw_r, bond_itype, isin=isin, issuer_name=h.get("issuer_name"))
        display_rating = raw_r.upper() if raw_r else rating

        try:
            mat_date = date.fromisoformat(str(h["maturity_date"])[:10])
        except Exception:
            mat_date = today + timedelta(days=365 * 5)

        try:
            issue_dt = date.fromisoformat(str(h["issue_date"])[:10]) if h.get("issue_date") else None
        except Exception:
            issue_dt = None

        # Invested amount basis for allocations / P&L:
        # - Prefer stored book_value (sheet basis when imported correctly).
        # - If book_value is missing but we have a real quote, fall back to quote × quantity.
        # - If both are missing, keep the holding out of valuation totals.
        invested = book if book > 0 else 0.0

        # Full bond-level analytics
        analytics = compute_bond_analytics(
            coupon_rate=coupon,
            face_value=face,
            clean_price=clean_price,
            ytm=ytm,
            maturity_date=mat_date,
            coupon_frequency=freq,
            issue_date=issue_dt,
            settlement_date=today,
        )

        # face = normalized total nominal of the holding after unit/position reconciliation.
        accrued = analytics["accrued_interest"]
        dirty_price = analytics["dirty_price"]
        has_quote = clean_price > 0
        market_value = compute_market_value(clean_price, face, qty) if has_quote else 0.0
        clean_value = market_value
        accrued_total = accrued  # accrued_interest computed on total face already

        # Accumulate totals
        total_dirty_value += market_value
        total_clean_value += clean_value
        total_accrued += accrued_total
        if invested <= 0 and has_quote:
            invested = clean_price * max(qty, 1)
        total_invested += invested
        total_face += face  # face_value is already total position size

        # Weighted metrics (by dirty market value)
        w_ytm += ytm * market_value
        w_mod_dur += analytics["modified_duration"] * market_value
        w_mac_dur += analytics["macaulay_duration"] * market_value
        w_convexity += analytics["convexity"] * market_value
        credit_score = RATING_SCORE.get(rating, RATING_SCORE.get("NR", 7.0))
        w_credit += credit_score * market_value
        liq = float(h.get("liquidity_score") or 50)
        w_liquidity += liq * market_value
        w_spread += analytics["spread_over_gsec_bps"] * market_value

        # Allocation tracking
        cat = _normalized_allocation_category(
            h.get("category"), h.get("sector"), h.get("bond_issuer_type"), h.get("issuer_name"), isin
        )
        sec = _normalized_allocation_sector(
            h.get("sector"), h.get("category"), h.get("bond_issuer_type"), h.get("issuer_name"), isin
        )
        issuer = h.get("issuer_name") or isin
        allocation_value = invested if invested > 0 else market_value

        ov = alloc_overrides.get(isin)
        if alloc_overrides:
            # When sheet-derived overrides exist, allocation percentages should be
            # computed strictly on the same sheet basis universe and values.
            if ov and ov.get("total", 0.0) > 0:
                ov_total = float(ov["total"])
                used_override_isins.add(isin)
                total_allocation_basis += ov_total
                for b, v in ov.get("category", {}).items():
                    alloc_category[b] = alloc_category.get(b, 0.0) + float(v)
                for b, v in ov.get("sector", {}).items():
                    alloc_sector[b] = alloc_sector.get(b, 0.0) + float(v)
                allocation_value = ov_total
            else:
                # If an ISIN has no sheet basis row, exclude it from allocation pct math.
                allocation_value = 0.0
        else:
            total_allocation_basis += allocation_value
            alloc_category[cat] = alloc_category.get(cat, 0) + allocation_value
            alloc_sector[sec] = alloc_sector.get(sec, 0) + allocation_value

        alloc_issuer[issuer] = alloc_issuer.get(issuer, 0) + allocation_value
        alloc_rating[rating] = alloc_rating.get(rating, 0) + allocation_value

        yrs = analytics["years_to_maturity"]
        if yrs < 1:
            alloc_maturity["<1Y"] += allocation_value
        elif yrs < 3:
            alloc_maturity["1-3Y"] += allocation_value
        elif yrs < 5:
            alloc_maturity["3-5Y"] += allocation_value
        elif yrs < 7:
            alloc_maturity["5-7Y"] += allocation_value
        elif yrs < 10:
            alloc_maturity["7-10Y"] += allocation_value
        else:
            alloc_maturity[">10Y"] += allocation_value

    if alloc_overrides:
        for isin, ov in alloc_overrides.items():
            if isin in used_override_isins:
                continue
            ov_total = float(ov.get("total", 0.0) or 0.0)
            if ov_total <= 0:
                continue
            total_allocation_basis += ov_total
            for b, v in ov.get("category", {}).items():
                alloc_category[b] = alloc_category.get(b, 0.0) + float(v)
            for b, v in ov.get("sector", {}).items():
                alloc_sector[b] = alloc_sector.get(b, 0.0) + float(v)

        # Coupon schedule for this holding (face is total position face value)
        coupons = generate_coupon_schedule(
            isin=isin, coupon_rate=coupon, face_value=face,
            maturity_date=mat_date, issue_date=issue_dt,
            coupon_frequency=freq, quantity=1,  # face already includes qty
        )
        all_cash_flows.extend(coupons)

        # Per-holding output
        h_dict = dict(h)
        unrealized_gain = market_value - invested
        h_dict.update({
            "rating": display_rating,
            "credit_rating": display_rating,
            "rating_normalized": rating,
            "rating_agency": _clean_rating_agency(h.get("rating_agency")),
            "accrued_interest": round(accrued, 2),
            "accrued_total": round(accrued_total, 2),
            "accrued_per_100": analytics["accrued_per_100"],
            "dirty_price": dirty_price,
            "market_value": round(market_value, 2),
            "clean_value": round(clean_value, 2),
            "investment_value": round(invested, 2),
            "unrealized_gain": round(unrealized_gain, 2),
            "gain_pct": round((market_value - invested) / invested * 100, 4) if invested else 0,
            "macaulay_duration": analytics["macaulay_duration"],
            "modified_duration": analytics["modified_duration"],
            "convexity": analytics["convexity"],
            "dv01": analytics["dv01"],
            "spread_over_gsec_bps": analytics["spread_over_gsec_bps"],
            "gsec_benchmark_yield": analytics["gsec_benchmark_yield"],
            "years_to_maturity": analytics["years_to_maturity"],
            "rating_score": credit_score,
            "rating_reason": _rating_reason(rating, raw_r, bond_itype),
            "next_coupon_date": analytics["coupon_dates"]["next"],
            "last_coupon_date": analytics["coupon_dates"]["last"],
            "days_since_last_coupon": analytics["coupon_dates"]["days_since_last"],
            "status": _bond_status(analytics, rating, liq, market_value, total_dirty_value or 1),
        })
        processed.append(h_dict)

    # â”€â”€ Step 3: Portfolio-level weighted metrics â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    tv = total_dirty_value or 1.0

    weighted_ytm = round(w_ytm / tv, 4)
    weighted_mod_duration = round(w_mod_dur / tv, 4)
    weighted_mac_duration = round(w_mac_dur / tv, 4)
    weighted_convexity = round(w_convexity / tv, 4)
    avg_credit_score = round(w_credit / tv, 2)
    avg_liquidity = round(w_liquidity / tv, 2)
    avg_spread = round(w_spread / tv, 2)
    weighted_avg_maturity = _compute_wam(processed)

    # Credit score â†’ rating label
    portfolio_rating = "NR"
    for score, label in sorted(RATING_LABEL.items()):
        if avg_credit_score <= score + 0.5:
            portfolio_rating = label
            break

    # â”€â”€ Step 4: Allocation breakdowns with threshold alerts â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    alloc_tv = total_allocation_basis or total_dirty_value or 1.0
    allocations = {
        "by_category": _alloc_list(alloc_category, alloc_tv),
        "by_sector": _alloc_list(alloc_sector, alloc_tv),
        "by_issuer": _alloc_list(alloc_issuer, alloc_tv, top_n=15),
        "by_rating": _alloc_list(alloc_rating, alloc_tv),
        "by_maturity": [{"bucket": k, "value": round(v, 0), "pct": round(v / alloc_tv * 100, 2) if alloc_tv > 0 else 0.0}
                        for k, v in alloc_maturity.items()],
    }

    # Normalize allocation values so allocation cards tie to the same total as summary cards.
    alloc_target_total = total_invested if total_invested > 0 else alloc_tv
    if alloc_tv > 0 and alloc_target_total > 0:
        scale = alloc_target_total / alloc_tv

        def _fix_rows_total(rows: list[dict], expected_total: float) -> None:
            if not rows:
                return
            actual_total = round(sum(float(r.get("value") or 0.0) for r in rows), 2)
            delta = round(expected_total - actual_total, 2)
            if abs(delta) >= 0.01:
                idx = max(range(len(rows)), key=lambda i: float(rows[i].get("value") or 0.0))
                rows[idx]["value"] = round(float(rows[idx].get("value") or 0.0) + delta, 2)

        for key in ("by_category", "by_sector", "by_issuer", "by_rating"):
            rows = allocations.get(key) or []
            for row in rows:
                row["value"] = round(float(row.get("value") or 0.0) * scale, 2)
            _fix_rows_total(rows, round(alloc_target_total, 2))
            row_total = sum(float(r.get("value") or 0.0) for r in rows)
            for row in rows:
                row["pct"] = round((float(row.get("value") or 0.0) / row_total * 100.0), 2) if row_total > 0 else 0.0

        maturity_rows = allocations.get("by_maturity") or []
        for row in maturity_rows:
            row["value"] = round(float(row.get("value") or 0.0) * scale, 2)
        _fix_rows_total(maturity_rows, round(alloc_target_total, 2))
        maturity_total = sum(float(r.get("value") or 0.0) for r in maturity_rows)
        for row in maturity_rows:
            row["pct"] = round((float(row.get("value") or 0.0) / maturity_total * 100.0), 2) if maturity_total > 0 else 0.0

    # Threshold alerts
    threshold_alerts = _check_thresholds(alloc_category, alloc_issuer, alloc_rating, alloc_tv, weighted_mod_duration)

    # â”€â”€ Step 5: Maturity ladder (next 12 months) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    maturity_ladder = _build_maturity_ladder(processed)

    # â”€â”€ Step 6: Cash flow schedule â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    cf_sorted = sorted(all_cash_flows, key=lambda x: x["coupon_date"])
    # Group by month
    cf_monthly = {}
    for cf in cf_sorted:
        month = cf["coupon_date"][:7]
        if month not in cf_monthly:
            cf_monthly[month] = {"month": month, "coupon": 0.0, "principal": 0.0, "total": 0.0}
        cf_monthly[month]["coupon"] += cf["coupon_amount"]
        cf_monthly[month]["principal"] += cf["principal_amount"]
        cf_monthly[month]["total"] += cf["total_cash_flow"]

    # Round
    for m in cf_monthly.values():
        m["coupon"] = round(m["coupon"], 2)
        m["principal"] = round(m["principal"], 2)
        m["total"] = round(m["total"], 2)

    # â”€â”€ Step 7: Health Score (100-point model) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    health = _compute_health_score(
        avg_credit_score, alloc_issuer, alloc_rating, alloc_category, alloc_maturity,
        weighted_mod_duration, avg_liquidity, alloc_tv
    )

    # â”€â”€ Step 8: Portfolio XIRR approximation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Simplified XIRR: annual return = (current_value / invested - 1) annualized
    accrual_return = round(weighted_ytm, 4)  # Accrual return â‰ˆ weighted YTM for held-to-maturity
    mtm_return = round((total_dirty_value - total_invested) / total_invested * 100, 4) if total_invested else 0

    # Annualized MTM Return (approximate)
    # Compute average holding period from trade records where available
    avg_holding_years = max(
        sum(float(h.get("years_to_maturity", 0)) for h in processed) / len(processed)
        if processed else 1.0,
        0.25,  # floor at 3 months to avoid division issues
    )
    mtm_base = 1 + mtm_return / 100
    if avg_holding_years > 0 and mtm_base > 0:
        annualized_mtm = round(((mtm_base ** (1 / avg_holding_years)) - 1) * 100, 4)
    else:
        # Fall back to non-annualized MTM when data produces non-positive compounding base.
        annualized_mtm = mtm_return

    # â”€â”€ Build summary â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    summary = {
        "total_face_value": round(total_face, 2),
        "total_invested": round(total_invested, 2),
        "total_clean_value": round(total_clean_value, 2),
        "total_accrued_interest": round(total_accrued, 2),
        "total_dirty_value": round(total_dirty_value, 2),
        "total_market_value": round(total_dirty_value, 2),  # alias
        "unrealized_gain": round(total_dirty_value - total_invested, 2),
        "unrealized_gain_pct": round((total_dirty_value - total_invested) / total_invested * 100, 4) if total_invested else 0,

        "aum_crores": round(total_dirty_value / 1e7, 2),
        "invested_crores": round(total_invested / 1e7, 2),
        "face_value_crores": round(total_face / 1e7, 2),

        "weighted_ytm": weighted_ytm,
        "weighted_modified_duration": weighted_mod_duration,
        "weighted_duration": weighted_mod_duration,
        "weighted_macaulay_duration": weighted_mac_duration,
        "weighted_convexity": weighted_convexity,
        "weighted_avg_maturity": weighted_avg_maturity,
        "avg_credit_score": avg_credit_score,
        "portfolio_rating": health["grade"],
        "credit_quality_rating": portfolio_rating,
        "avg_liquidity_score": avg_liquidity,
        "avg_spread_bps": avg_spread,

        "accrual_return_pct": accrual_return,
        "mtm_return_pct": mtm_return,
        "annualized_return_pct": annualized_mtm,

        "holdings_count": len(processed),
        "issuers_count": len(alloc_issuer),

        "health_score": health["total_score"],
        "health_grade": health["grade"],

        "last_updated": datetime.now().isoformat(),
        "valuation_date": today_str,
    }

    # â”€â”€ Persist risk metrics snapshot â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    try:
        await _persist_risk_snapshot(today_str, summary, health)
    except Exception as e:
        log.warning("persist_risk_snapshot_failed", error=str(e))

    return {
        "summary": summary,
        "holdings": processed,
        "allocations": allocations,
        "maturity_ladder": maturity_ladder,
        "cash_flow_schedule": list(cf_monthly.values()),
        "cash_flow_details": cf_sorted[:500],  # Cap for API size
        "health": health,
        "threshold_alerts": threshold_alerts,
    }


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# HELPER FUNCTIONS
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def _bond_status(analytics: dict, rating: str, liquidity: float, value: float, total: float) -> str:
    """Determine per-bond health status: HEALTHY / MONITOR / RISK."""
    issues = 0
    if analytics.get("spread_over_gsec_bps", 0) > 300:
        issues += 2
    if rating in ("BBB+", "BBB", "BBB-", "BB+", "BB", "NR"):
        issues += 2
    elif rating in ("A-", "A"):
        issues += 1
    if liquidity < 30:
        issues += 1
    if analytics.get("years_to_maturity", 0) < 0.25:
        issues += 1  # Near maturity (may need reinvestment)
    if total > 0 and value / total > 0.10:
        issues += 1  # Concentration

    if issues >= 3:
        return "RISK"
    elif issues >= 1:
        return "MONITOR"
    return "HEALTHY"


def _compute_wam(holdings: list[dict]) -> float:
    """Weighted Average Maturity (years) by market value."""
    w_sum = 0.0
    total = 0.0
    for h in holdings:
        mv = float(h.get("market_value", 0))
        yrs = float(h.get("years_to_maturity", 0))
        w_sum += yrs * mv
        total += mv
    return round(w_sum / total, 4) if total else 0


def _alloc_list(bucket: dict, total: float, top_n: int = 0) -> list[dict]:
    """Convert allocation dict to sorted list with pct."""
    if total <= 0:
        return [{"name": k, "value": round(v, 0), "pct": 0.0} for k, v in bucket.items()]
    items = sorted(bucket.items(), key=lambda x: -x[1])
    if top_n:
        items = items[:top_n]
    return [{"name": k, "value": round(v, 0), "pct": round(v / total * 100, 2)} for k, v in items]


def _check_thresholds(
    cat: dict, issuers: dict, ratings: dict, total: float, duration: float
) -> list[dict]:
    """Check PF Trust limits and return alerts."""
    alerts = []
    limits = PF_TRUST_LIMITS

    if total <= 0:
        return alerts

    # Single issuer limit
    for issuer, val in issuers.items():
        pct = val / total * 100
        if pct > limits["max_single_issuer_pct"]:
            alerts.append({
                "type": "CONCENTRATION",
                "severity": "HIGH",
                "message": f"Issuer '{issuer}' at {pct:.1f}% exceeds {limits['max_single_issuer_pct']}% limit",
                "current": round(pct, 2),
                "limit": limits["max_single_issuer_pct"],
            })

    # Sovereign minimum
    sov_pct = (sum(v for k, v in ratings.items() if k in ("SOV", "SOVEREIGN")) / total * 100) if total > 0 else 0
    if sov_pct < limits["min_sovereign_pct"]:
        alerts.append({
            "type": "ALLOCATION",
            "severity": "MEDIUM",
            "message": f"Sovereign exposure at {sov_pct:.1f}% below {limits['min_sovereign_pct']}% minimum",
            "current": round(sov_pct, 2),
            "limit": limits["min_sovereign_pct"],
        })

    # Below AA exposure limit
    below_aa_val = sum(v for k, v in ratings.items()
                       if k not in ("SOV", "SOVEREIGN", "AAA", "AA+", "AA", "AA-"))
    below_aa_pct = (below_aa_val / total * 100) if total > 0 else 0
    if below_aa_pct > limits["max_below_aa_pct"]:
        alerts.append({
            "type": "CREDIT_RISK",
            "severity": "HIGH",
            "message": f"Below-AA exposure at {below_aa_pct:.1f}% exceeds {limits['max_below_aa_pct']}% limit",
            "current": round(below_aa_pct, 2),
            "limit": limits["max_below_aa_pct"],
        })

    # Duration limit
    if duration > limits["max_avg_duration"]:
        alerts.append({
            "type": "DURATION",
            "severity": "MEDIUM",
            "message": f"Portfolio duration {duration:.2f}Y exceeds {limits['max_avg_duration']}Y limit",
            "current": round(duration, 2),
            "limit": limits["max_avg_duration"],
        })

    return alerts


def _build_maturity_ladder(holdings: list[dict]) -> list[dict]:
    """Build month-by-month maturity ladder for the next 36 months."""
    today = date.today()
    ladder = {}

    for h in holdings:
        try:
            mat = date.fromisoformat(str(h.get("maturity_date") or "2030-01-01")[:10])
        except Exception:
            continue

        if mat < today:
            continue

        months_to_mat = (mat.year - today.year) * 12 + (mat.month - today.month)
        if months_to_mat > 36:
            continue

        month_key = f"{mat.year}-{mat.month:02d}"
        if month_key not in ladder:
            ladder[month_key] = {"month": month_key, "count": 0, "face_value": 0.0, "market_value": 0.0, "isins": []}

        ladder[month_key]["count"] += 1
        ladder[month_key]["face_value"] += float(h.get("face_value", 0)) * int(h.get("quantity", 0))
        ladder[month_key]["market_value"] += float(h.get("market_value", 0))
        ladder[month_key]["isins"].append(h["isin"])

    for m in ladder.values():
        m["face_value"] = round(m["face_value"], 2)
        m["market_value"] = round(m["market_value"], 2)

    return sorted(ladder.values(), key=lambda x: x["month"])


def _compute_health_score(
    avg_credit: float, issuers: dict, ratings: dict, categories: dict,
    maturity: dict, duration: float, liquidity: float, total: float
) -> dict:
    """
    100-point Health Score Model:
      - Credit Quality      : 30 points
      - Diversification     : 20 points
      - Duration Risk       : 15 points
      - Liquidity           : 15 points
      - Concentration Risk  : 20 points
    """
    strengths = []
    weaknesses = []
    suggestions = []
    exact_actions = []

    def _to_cr(v: float) -> float:
        return (v or 0.0) / 1e7

    # â”€â”€ Credit Quality (30 pts) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # SOV=1 â†’ 30pts, AAA=2 â†’ 28pts, AA+=3 â†’ 25pts, AA=4 â†’ 22pts
    if avg_credit <= 1.5:
        credit_score = 30
    elif avg_credit <= 2.5:
        credit_score = 28
    elif avg_credit <= 3.5:
        credit_score = 25
    elif avg_credit <= 4.5:
        credit_score = 22
    elif avg_credit <= 5.5:
        credit_score = 18
    elif avg_credit <= 7:
        credit_score = 14
    else:
        credit_score = 10

    if credit_score >= 25:
        strengths.append("Strong credit quality â€” predominantly investment grade")
    else:
        weaknesses.append(f"Average credit score {avg_credit:.1f} indicates elevated credit risk")
        suggestions.append("Consider reducing exposure to below-AA rated bonds")

    if avg_credit > 3.5 and total > 0:
        shift_pct = 8 if avg_credit <= 4.5 else 12 if avg_credit <= 5.5 else 18
        shift_val = total * (shift_pct / 100.0)
        exact_actions.append(
            f"Reallocate â‚¹{_to_cr(shift_val):.2f} Cr ({shift_pct:.1f}% of AUM) from AA and lower-rated bonds into AAA/SOV and AA+ holdings."
        )

    # â”€â”€ Diversification (20 pts) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    n_issuers = len(issuers)
    n_ratings = len([r for r, v in ratings.items() if total > 0 and v / total > 0.01])
    n_sectors = len([c for c, v in categories.items() if total > 0 and v / total > 0.01])

    if n_issuers >= 15:
        div_score = 15
    elif n_issuers >= 10:
        div_score = 12
    elif n_issuers >= 5:
        div_score = 8
    else:
        div_score = 4

    if n_sectors >= 3:
        div_score += 5
    elif n_sectors >= 2:
        div_score += 3
    else:
        div_score += 1

    div_score = min(div_score, 20)

    if n_issuers >= 10:
        strengths.append(f"Well diversified across {n_issuers} issuers")
    else:
        weaknesses.append(f"Only {n_issuers} issuers â€” limited diversification")
        suggestions.append("Add exposure to more issuers for better diversification")
        if total > 0:
            need = max(0, 10 - n_issuers)
            add_each_val = total * 0.02
            exact_actions.append(
                f"Add {need} new issuers with ~â‚¹{_to_cr(add_each_val):.2f} Cr each (about 2.0% AUM per issuer) to reach at least 10 issuers."
            )

    # â”€â”€ Duration Risk (15 pts) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if duration <= 3:
        dur_score = 15
    elif duration <= 5:
        dur_score = 12
    elif duration <= 7:
        dur_score = 9
    elif duration <= 10:
        dur_score = 6
    else:
        dur_score = 3

    if duration <= 5:
        strengths.append(f"Conservative duration of {duration:.2f}Y")
    elif duration > 7:
        weaknesses.append(f"High duration of {duration:.2f}Y increases rate sensitivity")
        suggestions.append("Consider shortening duration by adding shorter-maturity bonds")

    if duration > 5 and total > 0:
        reduce_years = duration - 5
        shift_pct = min(25.0, max(5.0, reduce_years * 4.0))
        shift_val = total * (shift_pct / 100.0)
        exact_actions.append(
            f"Shift â‚¹{_to_cr(shift_val):.2f} Cr ({shift_pct:.1f}% of AUM) from >7Y maturity bonds into 1â€“5Y bucket to bring modified duration near 5.0Y."
        )

    # â”€â”€ Liquidity (15 pts) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if liquidity >= 80:
        liq_score = 15
    elif liquidity >= 60:
        liq_score = 12
    elif liquidity >= 40:
        liq_score = 9
    elif liquidity >= 20:
        liq_score = 6
    else:
        liq_score = 3

    if liquidity >= 60:
        strengths.append(f"Good portfolio liquidity (score: {liquidity:.0f})")
    else:
        weaknesses.append(f"Low liquidity score {liquidity:.0f} â€” may face exit difficulty")
        suggestions.append("Increase allocation to listed, actively-traded bonds")
        if total > 0:
            shift_pct = min(20.0, max(5.0, 60.0 - liquidity))
            shift_val = total * (shift_pct / 100.0)
            exact_actions.append(
                f"Move â‚¹{_to_cr(shift_val):.2f} Cr ({shift_pct:.1f}% of AUM) from low-liquidity lines into actively traded listed bonds to target liquidity score â‰¥ 60."
            )

    # â”€â”€ Concentration Risk (20 pts) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    top_issuer_name = None
    top_issuer_val = 0.0
    if issuers and total > 0:
        top_issuer_name, top_issuer_val = max(issuers.items(), key=lambda kv: kv[1])
        top_issuer_pct = top_issuer_val / total * 100
    else:
        top_issuer_pct = 100

    if top_issuer_pct <= 5:
        conc_score = 20
    elif top_issuer_pct <= 10:
        conc_score = 16
    elif top_issuer_pct <= 15:
        conc_score = 12
    elif top_issuer_pct <= 20:
        conc_score = 8
    else:
        conc_score = 4

    if top_issuer_pct <= 10:
        strengths.append(f"No single issuer exceeds {top_issuer_pct:.1f}%")
    else:
        weaknesses.append(f"Top issuer at {top_issuer_pct:.1f}% â€” concentration risk")
        suggestions.append(f"Reduce top issuer exposure below 10%")
        if total > 0 and top_issuer_name:
            excess_pct = max(0.0, top_issuer_pct - 10.0)
            rebalance_val = total * (excess_pct / 100.0)
            per_target = rebalance_val / 3.0
            exact_actions.append(
                f"Reduce {top_issuer_name} by â‚¹{_to_cr(rebalance_val):.2f} Cr ({excess_pct:.2f}% of AUM), then redeploy ~â‚¹{_to_cr(per_target):.2f} Cr each into 3 different issuers rated AA+ or better."
            )

    total_score = credit_score + div_score + dur_score + liq_score + conc_score
    total_score = min(max(total_score, 0), 100)

    if total_score >= 85:
        grade = "A+"
    elif total_score >= 75:
        grade = "A"
    elif total_score >= 65:
        grade = "B+"
    elif total_score >= 55:
        grade = "B"
    elif total_score >= 45:
        grade = "C"
    else:
        grade = "D"

    return {
        "total_score": total_score,
        "grade": grade,
        "components": {
            "credit_quality": {"score": credit_score, "max": 30},
            "diversification": {"score": div_score, "max": 20},
            "duration_risk": {"score": dur_score, "max": 15},
            "liquidity": {"score": liq_score, "max": 15},
            "concentration_risk": {"score": conc_score, "max": 20},
        },
        "strengths": strengths,
        "weaknesses": weaknesses,
        "suggestions": suggestions,
        "exact_actions": exact_actions,
    }


async def _persist_risk_snapshot(calc_date: str, summary: dict, health: dict) -> None:
    """Persist portfolio-level metrics snapshot to risk_metrics table."""
    await execute_query("""
        INSERT INTO risk_metrics (
            calc_date, total_market_value, total_accrued_interest, total_dirty_value,
            weighted_ytm, weighted_mod_duration, weighted_mac_duration, weighted_avg_maturity,
            convexity, avg_credit_score, liquidity_score, portfolio_xirr,
            health_score, health_credit_score, health_diversification,
            health_duration_score, health_liquidity_score, health_concentration,
            strengths_json, weaknesses_json, suggestions_json
        ) VALUES (
            :calc_date, :tmv, :tai, :tdv,
            :wytm, :wmd, :wmac, :wam,
            :conv, :acs, :ls, :xirr,
            :hs, :hc, :hd, :hdr, :hl, :hcon,
            :str, :weak, :sug
        )
        ON CONFLICT(calc_date) DO UPDATE SET
            total_market_value = excluded.total_market_value,
            total_accrued_interest = excluded.total_accrued_interest,
            total_dirty_value = excluded.total_dirty_value,
            weighted_ytm = excluded.weighted_ytm,
            weighted_mod_duration = excluded.weighted_mod_duration,
            weighted_mac_duration = excluded.weighted_mac_duration,
            weighted_avg_maturity = excluded.weighted_avg_maturity,
            convexity = excluded.convexity,
            avg_credit_score = excluded.avg_credit_score,
            liquidity_score = excluded.liquidity_score,
            portfolio_xirr = excluded.portfolio_xirr,
            health_score = excluded.health_score,
            health_credit_score = excluded.health_credit_score,
            health_diversification = excluded.health_diversification,
            health_duration_score = excluded.health_duration_score,
            health_liquidity_score = excluded.health_liquidity_score,
            health_concentration = excluded.health_concentration,
            strengths_json = excluded.strengths_json,
            weaknesses_json = excluded.weaknesses_json,
            suggestions_json = excluded.suggestions_json
    """, {
        "calc_date": calc_date,
        "tmv": summary.get("total_market_value"),
        "tai": summary.get("total_accrued_interest"),
        "tdv": summary.get("total_dirty_value"),
        "wytm": summary.get("weighted_ytm"),
        "wmd": summary.get("weighted_modified_duration"),
        "wmac": summary.get("weighted_macaulay_duration"),
        "wam": summary.get("weighted_avg_maturity"),
        "conv": summary.get("weighted_convexity"),
        "acs": summary.get("avg_credit_score"),
        "ls": summary.get("avg_liquidity_score"),
        "xirr": summary.get("annualized_return_pct"),
        "hs": health.get("total_score"),
        "hc": health["components"]["credit_quality"]["score"],
        "hd": health["components"]["diversification"]["score"],
        "hdr": health["components"]["duration_risk"]["score"],
        "hl": health["components"]["liquidity"]["score"],
        "hcon": health["components"]["concentration_risk"]["score"],
        "str": json.dumps(health.get("strengths", [])),
        "weak": json.dumps(health.get("weaknesses", [])),
        "sug": json.dumps(health.get("suggestions", [])),
    })

