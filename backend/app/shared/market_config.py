"""
Shared Market Data Constants — Single Source of Truth
======================================================
Canonical internal location (gateway-first architecture):
    unified_gateway/shared/market_config.py

Import from here instead of duplicating:
    from app.shared.market_config import GSEC_YIELD_CURVE, RATING_SPREAD_BPS, NSE_HEADERS
"""

from __future__ import annotations

# ── HTTP Headers (NSE / BSE scraping) ────────────────────────────────────────
NSE_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}

BSE_HEADERS: dict[str, str] = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
    "Referer": "https://www.bseindia.com/",
}

# ── Benchmark G-Sec Yield Curve (tenor in years → yield in %) ────────────────
# Updated: Feb 2026 | Source: FIMMDA / RBI
GSEC_YIELD_CURVE: dict[float, float] = {
    0.25: 6.82,
    0.50: 6.87,
    1.00: 6.95,
    2.00: 7.00,
    3.00: 7.02,
    5.00: 7.05,
    7.00: 7.08,
    10.0: 7.10,
    14.0: 7.15,
    20.0: 7.20,
    30.0: 7.25,
    40.0: 7.28,
}

# ── Credit Spread Matrix (bps over G-Sec, same tenor) ────────────────────────
RATING_SPREAD_BPS: dict[str, int] = {
    "SOVEREIGN": 0,
    "AAA":       45,
    "AA+":       75,
    "AA":       110,
    "AA-":      140,
    "A+":       180,
    "A":        220,
    "A-":       270,
    "BBB+":     340,
    "BBB":      420,
    "UNKNOWN":  200,
}

# ── Issuer Type → Default Tenor Assumptions ───────────────────────────────────
ISSUER_TYPE_TENORS: dict[str, float] = {
    "G-SEC":   10.0,
    "SDL":     10.0,
    "T-BILL":   0.25,
    "PSU":      7.0,
    "CORP":     5.0,
    "BANK":     5.0,
    "NBFC":     3.0,
    "HFC":      5.0,
    "MFI":      2.0,
}

# ── Live Data Endpoints ────────────────────────────────────────────────────────
NSE_GSEC_URL  = "https://www.nseindia.com/api/allGSecWithNDS-OM"
NSE_BOND_URL  = "https://www.nseindia.com/api/corporateBondsListByCriteria"
NSE_WARMUP_URL = "https://www.nseindia.com/"
BSE_DEBT_URL  = "https://api.bseindia.com/BseIndiaAPI/api/DebtSec/w"
FIMMDA_URL    = "https://www.fimmda.org/apps/data/GetValuationData"

# ── Synthetic Data Noise Budget ───────────────────────────────────────────────
# Gaussian noise applied to synthetic prices (mean=0, std in bps)
SYNTHETIC_NOISE_STD_BPS: float = 2.0


# ── Recovery Rate Assumptions (LGD Modelling) ────────────────────────────────
# Recovery rates as % of face value at default, by seniority / issuer type.
# Source: SEBI / CRISIL / Moody's historical Indian bond default studies.
# Used in: stress testing, expected loss calculations, CVA/DVA adjustments.
RECOVERY_RATES: dict[str, float] = {
    # Government / Quasi-Sovereign — assumed zero-loss (sovereign guarantee)
    "G-SEC":       1.00,
    "SDL":         1.00,
    "T-BILL":      1.00,
    # PSU / Government-backed — high recovery due to implicit sovereign support
    "PSU":         0.80,
    # Banks (Basel III / RBI-regulated) — moderate-high recovery
    "BANK":        0.60,
    # Senior secured corporate bonds — typical Indian experience
    "CORP_SENIOR_SECURED":  0.45,
    # Senior unsecured corporate bonds
    "CORP_SENIOR_UNSECURED": 0.30,
    # Subordinated / Tier-2 instruments
    "CORP_SUBORDINATED":     0.15,
    # NBFC / HFC — lower recovery, unsecured
    "NBFC":        0.30,
    "HFC":         0.35,
    # MFI — lowest recovery due to unsecured nature
    "MFI":         0.20,
    # Default fallback for unknown issuer types
    "UNKNOWN":     0.25,
}

# Loss Given Default = 1 - Recovery Rate
def get_lgd(issuer_type: str) -> float:
    """Return the Loss Given Default fraction for a given issuer type."""
    return round(1.0 - RECOVERY_RATES.get(issuer_type, RECOVERY_RATES["UNKNOWN"]), 4)


# ── Day-Count Conventions (Indian Fixed Income) ──────────────────────────────
# Day-count conventions used across Indian bond market for accrued interest
# and yield calculations. Per RBI / FIMMDA guidelines.
#
# Convention             | Description                                       | Used For
# -----------------------|---------------------------------------------------|---------------------
# 30/360                 | Each month = 30 days, year = 360 days             | Corporate bonds (India)
# ACT/365               | Actual days / 365                                 | G-Sec, SDL, T-Bills
# ACT/360               | Actual days / 360                                 | Money market instruments
# ACT/ACT (ISDA)        | Actual days / actual days in year                 | Some international bonds
# ACT/ACT (ICMA)        | Actual days / actual days in coupon period × freq  | Some FIMMDA valuations
#
# DEFAULT for Indian G-Sec: ACT/365 (per RBI Master Direction on Government Securities)
# DEFAULT for Indian Corporate Bonds: 30/360 (per SEBI LODR / FIMMDA guidelines)
# DEFAULT for T-Bills: ACT/365 (discount yield basis)

class DayCountConvention:
    """Enumeration of day-count conventions used in the platform."""
    THIRTY_360      = "30/360"
    ACT_365         = "ACT/365"
    ACT_360         = "ACT/360"
    ACT_ACT_ISDA    = "ACT/ACT_ISDA"
    ACT_ACT_ICMA    = "ACT/ACT_ICMA"

# Mapping: issuer type → default day-count convention
ISSUER_DAY_COUNT: dict[str, str] = {
    "G-SEC":    DayCountConvention.ACT_365,
    "SDL":      DayCountConvention.ACT_365,
    "T-BILL":   DayCountConvention.ACT_365,
    "PSU":      DayCountConvention.THIRTY_360,
    "CORP":     DayCountConvention.THIRTY_360,
    "BANK":     DayCountConvention.THIRTY_360,
    "NBFC":     DayCountConvention.THIRTY_360,
    "HFC":      DayCountConvention.THIRTY_360,
    "MFI":      DayCountConvention.THIRTY_360,
}

def get_day_count_fraction(start_date, end_date, convention: str = DayCountConvention.ACT_365) -> float:
    """
    Calculate the day-count fraction between two dates for a given convention.
    Used for accrued interest and yield calculations.
    """
    from datetime import date
    if isinstance(start_date, str):
        start_date = date.fromisoformat(start_date)
    if isinstance(end_date, str):
        end_date = date.fromisoformat(end_date)

    if convention == DayCountConvention.ACT_365:
        return (end_date - start_date).days / 365.0
    elif convention == DayCountConvention.ACT_360:
        return (end_date - start_date).days / 360.0
    elif convention == DayCountConvention.THIRTY_360:
        d1, m1, y1 = min(start_date.day, 30), start_date.month, start_date.year
        d2, m2, y2 = min(end_date.day, 30), end_date.month, end_date.year
        return (360 * (y2 - y1) + 30 * (m2 - m1) + (d2 - d1)) / 360.0
    elif convention in (DayCountConvention.ACT_ACT_ISDA, DayCountConvention.ACT_ACT_ICMA):
        # Simplified ACT/ACT: actual days / actual days in year
        import calendar
        year_days = 366 if calendar.isleap(start_date.year) else 365
        return (end_date - start_date).days / year_days
    else:
        raise ValueError(f"Unknown day-count convention: {convention}")


def interpolate_gsec_yield(tenor_years: float) -> float:
    """
    Linearly interpolate a G-Sec benchmark yield from the yield curve.
    Extrapolates at both ends using nearest known value.
    """
    tenors = sorted(GSEC_YIELD_CURVE.keys())
    if tenor_years <= tenors[0]:
        return GSEC_YIELD_CURVE[tenors[0]]
    if tenor_years >= tenors[-1]:
        return GSEC_YIELD_CURVE[tenors[-1]]
    for i in range(len(tenors) - 1):
        t1, t2 = tenors[i], tenors[i + 1]
        if t1 <= tenor_years <= t2:
            y1, y2 = GSEC_YIELD_CURVE[t1], GSEC_YIELD_CURVE[t2]
            frac = (tenor_years - t1) / (t2 - t1)
            return round(y1 + frac * (y2 - y1), 4)
    return GSEC_YIELD_CURVE[tenors[-1]]


def get_synthetic_yield(issuer_type: str, credit_rating: str, tenor_years: float) -> float:
    """
    Compute a realistic synthetic yield for any bond using:
        yield = G-Sec benchmark (tenor) + rating spread
    """
    base_yield = interpolate_gsec_yield(tenor_years)
    spread_bps = RATING_SPREAD_BPS.get(
        credit_rating,
        RATING_SPREAD_BPS["UNKNOWN"]
    )
    # S overeign / G-SEC always return clean benchmark yield
    if issuer_type in ("G-SEC", "SDL", "T-BILL") and credit_rating in ("SOVEREIGN", ""):
        spread_bps = 0
    return round(base_yield + spread_bps / 100.0, 4)
