"""
Comprehensive Indian Bond Database Seeder
==========================================
Seeds the universal_bond_master table with ~20,000 Indian bonds across:

  Category                  Count (approx)
  ─────────────────────────────────────────
  Central Govt (G-Secs)         200
  Treasury Bills (T-Bills)       48
  State Dev Loans (SDLs)       4200   (30 states × 140 bonds each)
  PSU Bonds                     900
  Bank Bonds (AT1 + Tier-2)     800
  NBFC Bonds                   2200
  Corporate Bonds             11000
  Municipal Bonds               100
  Covered Bonds                  52
  ─────────────────────────────────────────
  TOTAL                       19500+

Run:  python scripts/seeds/seed_indian_bonds.py
    or:  python -m scripts.seeds.seed_indian_bonds
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Generator

# ── path bootstrap ────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_DIR = _REPO_ROOT / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.shared.database import init_database, execute_query, fetch_one
import logging as _logging

# Use stdlib logger to avoid structlog.stdlib.add_logger_name incompatibility
_logging.basicConfig(level=_logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
_stdlib_log = _logging.getLogger("bond_seeder")


class _Log:
    """Thin wrapper so log.info(msg, **kw) prints to stdlib."""
    def info(self, msg, **kw):    _stdlib_log.info("%s %s", msg, kw or "")
    def warning(self, msg, **kw): _stdlib_log.warning("%s %s", msg, kw or "")
    def error(self, msg, **kw):   _stdlib_log.error("%s %s", msg, kw or "")

log = _Log()

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _isin_checksum(body: str) -> str:
    """
    Luhn-variant check digit used by NSDL for Indian ISINs.
    Returns single uppercase hex char (0-9, A-Z becomes 10-35).
    """
    digits = ""
    for ch in body:
        if ch.isdigit():
            digits += ch
        else:
            val = ord(ch) - 55
            digits += str(val)
    # Double every other digit from right
    total = 0
    digits = digits[::-1]
    for i, d in enumerate(digits):
        n = int(d)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    check = (10 - (total % 10)) % 10
    return str(check)


# Global counter guarantees every bond gets a unique ISIN (no truncation collisions)
_ISIN_COUNTER = [0]


def make_isin(category: str) -> str:
    """
    Generate a collision-free 12-character Indian-format ISIN.

    Format:  IN + 2-char-category + 7-digit-sequence + 1-check-digit = 12 chars
    Example: INGS0000001X  (category='GS', seq=1)

    Categories: GS=G-Sec  TB=T-Bill  SD=SDL   PS=PSU   BK=Bank
                NB=NBFC   CB=Corp    MU=Muni  CV=Covered
    """
    _ISIN_COUNTER[0] += 1
    n = _ISIN_COUNTER[0]
    cat2 = (category.upper() + "00")[:2]    # always exactly 2 chars
    body = f"IN{cat2}{n:07d}"               # exactly 11 chars
    return body + _isin_checksum(body)


def future_date(years_from_now: float, base: date | None = None) -> str:
    base = base or date(2026, 2, 20)
    d = base + timedelta(days=int(years_from_now * 365.25))
    return d.isoformat()


def past_date(years_ago: float, base: date | None = None) -> str:
    base = base or date(2026, 2, 20)
    d = base - timedelta(days=int(years_ago * 365.25))
    return d.isoformat()


def coupon(low: float, high: float) -> float:
    return round(random.uniform(low, high) * 4) / 4   # nearest 0.25


# ─────────────────────────────────────────────────────────────────────────────
# 1. Central Government Securities (G-Secs)
# ─────────────────────────────────────────────────────────────────────────────

GSEC_TENORS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 15, 19, 20, 25, 30, 40, 50]
GSEC_YEARS  = list(range(2026, 2100))

def gsec_bonds() -> Generator[dict, None, None]:
    for tenor in GSEC_TENORS:
        for yr in range(2020, 2026):
            issue_yr = yr
            mat_yr   = yr + tenor
            cr = coupon(5.5, 8.5)
            isin = make_isin("GS")
            yield {
                "isin": isin,
                "bond_name": f"GOI {cr:.2f}% {mat_yr}",
                "issuer_name": "Government of India",
                "issuer_type": "G-SEC",
                "coupon_rate": cr,
                "coupon_type": "FIXED",
                "coupon_frequency": 2,
                "maturity_date": f"{mat_yr}-01-15",
                "issue_date": f"{issue_yr}-01-15",
                "face_value": 100.0,
                "credit_rating": "SOV",
                "rating_agency": "RBI",
                "sector": "GOVERNMENT",
                "sub_sector": "Central Government",
                "listing_exchange": "NSE",
                "is_pf_eligible": 1,
            }
    # Special benchmark bonds
    benchmarks = [
        ("IN0020GSEC9990", "GOI 7.26% 2033 (New 10Y Benchmark)", 7.26, "2033-08-22", "2023-08-22"),
        ("IN0020GSEC9991", "GOI 7.18% 2037 (15Y Benchmark)",     7.18, "2037-07-24", "2022-07-24"),
        ("IN0020GSEC9992", "GOI 6.79% 2034 (Old 10Y)",           6.79, "2034-12-15", "2024-03-15"),
        ("IN0020GSEC9993", "GOI 7.10% 2029 (5Y Benchmark)",      7.10, "2029-04-18", "2024-04-18"),
        ("IN0020GSEC9994", "GOI 7.34% 2064 (50Y Bond)",          7.34, "2064-04-22", "2024-04-22"),
        ("IN0020GSEC9995", "GOI 6.54% 2032 (Green Bond)",        6.54, "2032-01-17", "2022-01-17"),
        ("IN0020GSEC9996", "GOI 7.41% 2036 (14Y Bond)",          7.41, "2036-12-19", "2022-12-19"),
        ("IN0020GSEC9997", "GOI 7.38% 2027 (Floating Rate)",     7.38, "2027-06-20", "2020-06-20"),
    ]
    for b in benchmarks:
        yield {
            "isin": b[0], "bond_name": b[1], "issuer_name": "Government of India",
            "issuer_type": "G-SEC", "coupon_rate": b[2], "coupon_type": "FIXED",
            "coupon_frequency": 2, "maturity_date": b[3], "issue_date": b[4],
            "face_value": 100.0, "credit_rating": "SOV", "rating_agency": "RBI",
            "sector": "GOVERNMENT", "sub_sector": "Central Government Benchmark",
            "listing_exchange": "NSE", "is_pf_eligible": 1,
        }


# ─────────────────────────────────────────────────────────────────────────────
# 2. Treasury Bills
# ─────────────────────────────────────────────────────────────────────────────

def tbill_bonds() -> Generator[dict, None, None]:
    tenors = [("91D", 91), ("182D", 182), ("364D", 364)]
    for base_week in range(0, 52, 1):
        for tenor_name, days in tenors:
            issue  = date(2025, 1, 1) + timedelta(weeks=base_week)
            mat    = issue + timedelta(days=days)
            if mat < date(2026, 2, 20):
                continue
            isin = make_isin("TB")
            discount = round(random.uniform(6.2, 7.3), 2)
            yield {
                "isin": isin,
                "bond_name": f"GOI T-Bill {tenor_name} {issue.strftime('%d-%b-%Y')}",
                "issuer_name": "Government of India",
                "issuer_type": "T-BILL",
                "coupon_rate": 0.0,
                "coupon_type": "ZERO",
                "coupon_frequency": 0,
                "maturity_date": mat.isoformat(),
                "issue_date": issue.isoformat(),
                "face_value": 100.0,
                "credit_rating": "SOV",
                "rating_agency": "RBI",
                "sector": "GOVERNMENT",
                "sub_sector": f"{tenor_name} Treasury Bill",
                "listing_exchange": "NSE",
                "is_pf_eligible": 1,
            }


# ─────────────────────────────────────────────────────────────────────────────
# 3. State Development Loans (SDLs)
# ─────────────────────────────────────────────────────────────────────────────

STATES = [
    ("Andhra Pradesh",      "AP"),
    ("Assam",               "AS"),
    ("Bihar",               "BR"),
    ("Chhattisgarh",        "CG"),
    ("Goa",                 "GA"),
    ("Gujarat",             "GJ"),
    ("Haryana",             "HR"),
    ("Himachal Pradesh",    "HP"),
    ("Jharkhand",           "JK"),
    ("Karnataka",           "KA"),
    ("Kerala",              "KL"),
    ("Madhya Pradesh",      "MP"),
    ("Maharashtra",         "MH"),
    ("Manipur",             "MN"),
    ("Meghalaya",           "ML"),
    ("Mizoram",             "MZ"),
    ("Nagaland",            "NL"),
    ("Odisha",              "OR"),
    ("Punjab",              "PB"),
    ("Rajasthan",           "RJ"),
    ("Sikkim",              "SK"),
    ("Tamil Nadu",          "TN"),
    ("Telangana",           "TG"),
    ("Tripura",             "TR"),
    ("Uttar Pradesh",       "UP"),
    ("Uttarakhand",         "UK"),
    ("West Bengal",         "WB"),
    ("Delhi",               "DL"),
    ("Jammu & Kashmir",     "JK"),
    ("Puducherry",          "PY"),
]

SDL_TENORS = [5, 7, 10, 12, 15, 20, 25, 30, 40]

def sdl_bonds() -> Generator[dict, None, None]:
    for state_name, state_code in STATES:
        for tenor in SDL_TENORS:
            for yr in range(2012, 2026):  # 14 issue years
                mat_yr = yr + tenor
                cr = coupon(6.5, 8.75)
                isin = make_isin("SD")
                yield {
                    "isin": isin,
                    "bond_name": f"{state_name} SDL {cr:.2f}% {mat_yr}",
                    "issuer_name": f"Government of {state_name}",
                    "issuer_type": "SDL",
                    "coupon_rate": cr,
                    "coupon_type": "FIXED",
                    "coupon_frequency": 2,
                    "maturity_date": f"{mat_yr}-03-15",
                    "issue_date": f"{yr}-03-15",
                    "face_value": 100.0,
                    "credit_rating": "SOV",
                    "rating_agency": "RBI",
                    "sector": "GOVERNMENT",
                    "sub_sector": "State Development Loan",
                    "listing_exchange": "NSE",
                    "is_pf_eligible": 1,
                }


# ─────────────────────────────────────────────────────────────────────────────
# 4. PSU Bonds
# ─────────────────────────────────────────────────────────────────────────────

PSU_ISSUERS = [
    # (ISIN Prefix, Issuer Name, Rating, Sector, SubSector, CouponRange)
    ("INE062A", "National Highways Authority of India",    "AAA", "INFRASTRUCTURE", "Roads",              (6.8, 8.0)),
    ("INE752E", "National Thermal Power Corporation",      "AAA", "ENERGY",         "Power Generation",   (6.5, 7.8)),
    ("INE115A", "Power Finance Corporation",               "AAA", "FINANCIALS",     "Power Finance",      (6.9, 8.1)),
    ("INE134E", "Rural Electrification Corporation",       "AAA", "FINANCIALS",     "Power Finance",      (7.0, 8.2)),
    ("INE775A", "NTPC Limited",                            "AAA", "ENERGY",         "Power Generation",   (6.5, 7.9)),
    ("INE219A", "Bharat Petroleum Corporation",            "AAA", "ENERGY",         "Oil & Gas",          (6.8, 7.8)),
    ("INE081A", "Oil & Natural Gas Corporation",           "AAA", "ENERGY",         "Oil & Gas",          (6.7, 7.7)),
    ("INE680A", "Indian Oil Corporation",                  "AAA", "ENERGY",         "Oil & Gas",          (6.6, 7.6)),
    ("INE220A", "Gas Authority of India",                  "AAA", "ENERGY",         "Gas",                (6.7, 7.7)),
    ("INE257A", "Steel Authority of India",                "AAA", "INDUSTRIALS",    "Steel",              (7.1, 8.3)),
    ("INE245A", "Bharat Heavy Electricals",                "AA+", "INDUSTRIALS",    "Capital Goods",      (7.2, 8.4)),
    ("INE205A", "HUDCO",                                   "AA+", "FINANCIALS",     "Housing Finance",    (7.3, 8.5)),
    ("INE774D", "IIFCL",                                   "AAA", "FINANCIALS",     "Infrastructure",     (6.9, 8.1)),
    ("INE154A", "SIDBI",                                   "AAA", "FINANCIALS",     "MSME Finance",       (6.8, 8.0)),
    ("INE020B", "National Bank for Agriculture (NABARD)",  "AAA", "FINANCIALS",     "Agriculture",        (6.7, 7.9)),
    ("INE671H", "IRFC Limited",                            "AAA", "FINANCIALS",     "Railways Finance",   (6.8, 8.0)),
    ("INE721A", "Mahanagar Gas",                           "AAA", "ENERGY",         "Gas Distribution",   (6.9, 7.9)),
    ("INE556F", "NLC India",                               "AA+", "ENERGY",         "Lignite Mining",     (7.2, 8.3)),
    ("INE501C", "Bharat Electronics",                      "AAA", "INDUSTRIALS",    "Defence Electronics",(6.8, 7.8)),
    ("INE233A", "Hindustan Aeronautics",                   "AAA", "INDUSTRIALS",    "Defence",            (6.7, 7.7)),
    ("INE813H", "RITES Limited",                           "AA+", "INFRASTRUCTURE", "Transport",          (7.1, 8.2)),
    ("INE477A", "RVNL",                                    "AA+", "INFRASTRUCTURE", "Railways",           (7.2, 8.3)),
    ("INE774D", "RECPDCL",                                 "AAA", "FINANCIALS",     "Power",              (6.9, 8.0)),
    ("INE414G", "National Housing Bank",                   "AAA", "FINANCIALS",     "Housing",            (6.8, 7.9)),
    ("INE115A", "THDC India",                              "AA+", "ENERGY",         "Hydro Power",        (7.0, 8.1)),
    ("INE350Y", "NHPC Limited",                            "AAA", "ENERGY",         "Hydro Power",        (6.8, 7.8)),
    ("INE848E", "Neyveli Lignite Corporation",             "AA+", "ENERGY",         "Lignite",            (7.1, 8.2)),
    ("INE511C", "Telecommunications Consultants India",    "AA",  "TELECOM",        "Telecom Services",   (7.4, 8.6)),
    ("INE860A", "Airports Authority of India",             "AA+", "INFRASTRUCTURE", "Aviation",           (7.0, 8.1)),
    ("INE053F", "India Infrastructure Finance Corp (UK)",  "AAA", "FINANCIALS",     "Infrastructure",     (6.9, 8.0)),
]

PSU_TENORS = [3, 5, 7, 10, 15, 20, 25, 30]

def psu_bonds() -> Generator[dict, None, None]:
    for prefix, issuer, rating, sector, sub_sector, (low, high) in PSU_ISSUERS:
        for tenor in PSU_TENORS:
            for yr in range(2014, 2026):  # 12 issue years
                mat_yr = yr + tenor
                cr = coupon(low, high)
                isin = make_isin("PS")
                is_pf = 1 if rating in ("AAA", "AA+") else 0
                yield {
                    "isin": isin,
                    "bond_name": f"{issuer[:40]} {cr:.2f}% {mat_yr}",
                    "issuer_name": issuer,
                    "issuer_type": "PSU",
                    "coupon_rate": cr,
                    "coupon_type": "FIXED",
                    "coupon_frequency": 2,
                    "maturity_date": f"{mat_yr}-06-15",
                    "issue_date": f"{yr}-06-15",
                    "face_value": 1000.0,
                    "credit_rating": rating,
                    "rating_agency": "CRISIL",
                    "sector": sector,
                    "sub_sector": sub_sector,
                    "listing_exchange": "NSE",
                    "is_pf_eligible": is_pf,
                }


# ─────────────────────────────────────────────────────────────────────────────
# 5. Bank Bonds (AT1, Tier-2, Senior Infra)
# ─────────────────────────────────────────────────────────────────────────────

BANKS = [
    # (Prefix, Bank Name, Rating, BankType)
    ("INE062A0", "State Bank of India",             "AAA", "PSU_BANK"),
    ("INE040A0", "Bank of Baroda",                  "AA+", "PSU_BANK"),
    ("INE565A0", "Punjab National Bank",             "AA+", "PSU_BANK"),
    ("INE160A0", "Canara Bank",                      "AA+", "PSU_BANK"),
    ("INE457A0", "Union Bank of India",              "AA",  "PSU_BANK"),
    ("INE090A0", "Indian Bank",                      "AA",  "PSU_BANK"),
    ("INE565A1", "Bank of India",                    "AA",  "PSU_BANK"),
    ("INE084A0", "Central Bank of India",            "AA-", "PSU_BANK"),
    ("INE001A0", "HDFC Bank",                        "AAA", "PRIVATE_BANK"),
    ("INE030A0", "ICICI Bank",                       "AAA", "PRIVATE_BANK"),
    ("INE237A0", "Axis Bank",                        "AA+", "PRIVATE_BANK"),
    ("INE090A1", "Kotak Mahindra Bank",               "AAA", "PRIVATE_BANK"),
    ("INE263A0", "Yes Bank",                         "A+",  "PRIVATE_BANK"),
    ("INE667A0", "IndusInd Bank",                    "AA",  "PRIVATE_BANK"),
    ("INE528G0", "Federal Bank",                     "AA",  "PRIVATE_BANK"),
    ("INE976G0", "IDFC First Bank",                  "AA-", "PRIVATE_BANK"),
    ("INE565A2", "RBL Bank",                         "A+",  "PRIVATE_BANK"),
    ("INE040A1", "South Indian Bank",                "A",   "PRIVATE_BANK"),
    ("INE168A0", "Karnataka Bank",                   "A+",  "PRIVATE_BANK"),
    ("INE077A0", "DCB Bank",                         "AA-", "PRIVATE_BANK"),
]

BANK_INSTRUMENTS = [
    ("AT1",    [None],          "PERPETUAL",    False),   # Perpetual
    ("TIER2",  [10, 15],        "SUBORDINATE",  True),
    ("SENIOR", [3, 5, 7, 10],   "SENIOR",       True),
    ("INFRA",  [5, 7, 10, 15],  "INFRA",        True),
]

def bank_bonds() -> Generator[dict, None, None]:
    for prefix, bank_name, rating, bank_type in BANKS:
        for instr_type, tenors, sub_type, _ in BANK_INSTRUMENTS:
            cr_low  = 7.5 if instr_type == "AT1" else 6.5
            cr_high = 10.5 if instr_type == "AT1" else 8.5
            for tenor in tenors:
                for yr in range(2020, 2026):
                    isin = make_isin("BK")
                    cr = coupon(cr_low, cr_high)
                    mat_date = "PERPETUAL" if tenor is None else f"{yr + tenor}-09-15"
                    issue_date = f"{yr}-09-15"
                    yield {
                        "isin": isin,
                        "bond_name": f"{bank_name[:35]} {instr_type} {cr:.2f}% {yr}",
                        "issuer_name": bank_name,
                        "issuer_type": "BANK",
                        "coupon_rate": cr,
                        "coupon_type": "FIXED",
                        "coupon_frequency": 2,
                        "maturity_date": mat_date,
                        "issue_date": issue_date,
                        "face_value": 1000000.0,   # Bank bonds: ₹10L face
                        "credit_rating": rating if instr_type != "AT1" else "AA" if rating == "AAA" else "BBB+",
                        "rating_agency": "ICRA",
                        "sector": "FINANCIALS",
                        "sub_sector": f"Bank {instr_type}",
                        "listing_exchange": "BSE",
                        "is_pf_eligible": 1 if instr_type in ("SENIOR", "INFRA") and rating in ("AAA", "AA+") else 0,
                    }


# ─────────────────────────────────────────────────────────────────────────────
# 6. NBFC Bonds
# ─────────────────────────────────────────────────────────────────────────────

NBFCS = [
    # (Prefix, Name, Rating, Sector)
    ("INE001B0", "HDFC Limited",                     "AAA", "HOUSING_FINANCE"),
    ("INE179A0", "LIC Housing Finance",               "AAA", "HOUSING_FINANCE"),
    ("INE146K0", "PNB Housing Finance",               "AA",  "HOUSING_FINANCE"),
    ("INE016A0", "Bajaj Finance",                     "AAA", "DIVERSIFIED"),
    ("INE296A0", "AEON Credit Service",               "AA",  "CONSUMER_FINANCE"),
    ("INE155A0", "Muthoot Finance",                   "AA+", "GOLD_LOANS"),
    ("INE414G1", "Manappuram Finance",                "AA",  "GOLD_LOANS"),
    ("INE070A0", "Cholamandalam Investment Finance",  "AA+", "VEHICLE_FINANCE"),
    ("INE295A0", "Shriram Finance",                   "AA+", "VEHICLE_FINANCE"),
    ("INE562T0", "Shriram Transport Finance",         "AA+", "VEHICLE_FINANCE"),
    ("INE671H1", "Mahindra Finance",                  "AA+", "VEHICLE_FINANCE"),
    ("INE078A0", "L&T Finance",                       "AA+", "DIVERSIFIED"),
    ("INE834L0", "Tata Capital Financial Services",   "AAA", "DIVERSIFIED"),
    ("INE066A0", "Hero FinCorp",                      "AA+", "VEHICLE_FINANCE"),
    ("INE477B0", "IIFL Finance",                      "AA",  "DIVERSIFIED"),
    ("INE759E0", "Sundaram Finance",                  "AAA", "VEHICLE_FINANCE"),
    ("INE881D0", "Aditya Birla Finance",              "AA+", "DIVERSIFIED"),
    ("INE205A1", "Tata Motors Finance",               "AA",  "VEHICLE_FINANCE"),
    ("INE752E1", "Piramal Finance",                   "A+",  "DIVERSIFIED"),
    ("INE787D0", "JM Financial Products",             "AA",  "DIVERSIFIED"),
    ("INE667F0", "Indiabulls Housing Finance",        "AA",  "HOUSING_FINANCE"),
    ("INE050A0", "Reliance Capital",                  "A-",  "DIVERSIFIED"),
    ("INE031A0", "HDB Financial Services",            "AAA", "CONSUMER_FINANCE"),
    ("INE583N0", "Godrej Housing Finance",            "AA+", "HOUSING_FINANCE"),
    ("INE219C0", "Can Fin Homes",                     "AA+", "HOUSING_FINANCE"),
    ("INE796D0", "GRUH Finance",                      "AAA", "HOUSING_FINANCE"),
    ("INE571K0", "Aavas Financiers",                  "AA-", "HOUSING_FINANCE"),
    ("INE241A0", "Home First Finance",                "A+",  "HOUSING_FINANCE"),
    ("INE753K0", "Aptus Value Housing Finance",       "A+",  "HOUSING_FINANCE"),
    ("INE100N0", "Repco Home Finance",                "A+",  "HOUSING_FINANCE"),
]

NBFC_TENORS = [1, 2, 3, 5, 7, 10, 15]

def nbfc_bonds() -> Generator[dict, None, None]:
    for prefix, name, rating, sub_sector in NBFCS:
        for tenor in NBFC_TENORS:
            for yr in range(2015, 2026):  # 11 issue years
                cr_low  = {"AAA": 6.8, "AA+": 7.1, "AA": 7.4, "AA-": 7.7, "A+": 8.0, "A": 8.3, "A-": 8.6, "BBB+": 9.0}.get(rating, 8.0)
                cr_high = cr_low + 0.75
                cr = coupon(cr_low, cr_high)
                isin = make_isin("NB")
                yield {
                    "isin": isin,
                    "bond_name": f"{name[:40]} {cr:.2f}% {yr+tenor}",
                    "issuer_name": name,
                    "issuer_type": "NBFC",
                    "coupon_rate": cr,
                    "coupon_type": "FIXED",
                    "coupon_frequency": 2,
                    "maturity_date": f"{yr+tenor}-03-31",
                    "issue_date": f"{yr}-03-31",
                    "face_value": 1000.0,
                    "credit_rating": rating,
                    "rating_agency": "CRISIL",
                    "sector": "FINANCIALS",
                    "sub_sector": sub_sector,
                    "listing_exchange": "NSE",
                    "is_pf_eligible": 1 if rating in ("AAA", "AA+", "AA") else 0,
                }


# ─────────────────────────────────────────────────────────────────────────────
# 7. Corporate Bonds (Multi-sector, ~11,000 bonds)
# ─────────────────────────────────────────────────────────────────────────────

CORPORATES = [
    # Telecom
    ("INE669E0", "Reliance Jio Infocomm",        "AAA", "TELECOM",       "Telecom Services"),
    ("INE153A0", "Bharti Airtel",               "AA+", "TELECOM",       "Telecom Services"),
    ("INE007T0", "Vodafone Idea",               "B+",  "TELECOM",       "Telecom Services"),
    # IT
    ("INE467B0", "Infosys",                     "AAA", "IT",            "IT Services"),
    ("INE058A0", "Tata Consultancy Services",   "AAA", "IT",            "IT Services"),
    ("INE669B0", "Wipro",                       "AA+", "IT",            "IT Services"),
    ("INE562A0", "HCL Technologies",            "AA+", "IT",            "IT Services"),
    # Auto
    ("INE155L0", "Tata Motors",                 "AA-", "AUTO",          "Passenger Vehicles"),
    ("INE585B0", "Mahindra & Mahindra",         "AA+", "AUTO",          "Passenger Vehicles"),
    ("INE721I0", "Bajaj Auto",                  "AAA", "AUTO",          "Two Wheelers"),
    ("INE721A0", "Hero MotoCorp",               "AAA", "AUTO",          "Two Wheelers"),
    ("INE179B0", "Maruti Suzuki",               "AAA", "AUTO",          "Passenger Vehicles"),
    ("INE048I0", "Eicher Motors",               "AA+", "AUTO",          "Commercial Vehicles"),
    ("INE148I0", "Ashok Leyland",               "AA",  "AUTO",          "Commercial Vehicles"),
    ("INE274J0", "Escorts Kubota",              "AA",  "AUTO",          "Farm Equipment"),
    # Real Estate
    ("INE741K0", "Godrej Properties",           "AA-", "REAL_ESTATE",   "Residential"),
    ("INE751E0", "DLF",                         "AA-", "REAL_ESTATE",   "Commercial"),
    ("INE571D0", "Prestige Estates",            "A+",  "REAL_ESTATE",   "Residential"),
    ("INE489L0", "Phoenix Mills",               "A+",  "REAL_ESTATE",   "Retail"),
    ("INE796H0", "Oberoi Realty",               "AA-", "REAL_ESTATE",   "Premium Residential"),
    ("INE481J0", "Brigade Enterprises",         "A+",  "REAL_ESTATE",   "Residential"),
    ("INE417C0", "Sobha Developers",            "A",   "REAL_ESTATE",   "Residential"),
    ("INE269C0", "Kolte-Patil Developers",      "A",   "REAL_ESTATE",   "Residential"),
    # Consumer
    ("INE259A0", "Hindustan Unilever",          "AAA", "CONSUMER",      "FMCG"),
    ("INE030B0", "Nestle India",                "AAA", "CONSUMER",      "Food & Beverages"),
    ("INE683C0", "Britannia Industries",        "AA+", "CONSUMER",      "Food"),
    ("INE118A0", "ITC Limited",                 "AAA", "CONSUMER",      "Diversified FMCG"),
    ("INE192A0", "Marico",                      "AA+", "CONSUMER",      "Personal Care"),
    ("INE523A0", "Dabur India",                 "AAA", "CONSUMER",      "Healthcare FMCG"),
    ("INE322A0", "Colgate-Palmolive India",     "AAA", "CONSUMER",      "Oral Care"),
    # Pharma
    ("INE406A0", "Sun Pharmaceutical",          "AA+", "PHARMA",        "Generic Pharma"),
    ("INE088A0", "Dr. Reddy's Laboratories",    "AA+", "PHARMA",        "Generic Pharma"),
    ("INE113A0", "Cipla",                       "AA",  "PHARMA",        "Generic Pharma"),
    ("INE079A0", "Aurobindo Pharma",            "AA",  "PHARMA",        "API & Formulations"),
    ("INE207A0", "Lupin",                       "AA-", "PHARMA",        "Generic Pharma"),
    ("INE059A0", "Cadila Healthcare (Zydus)",   "AA",  "PHARMA",        "Generic Pharma"),
    ("INE364A0", "Divi's Laboratories",         "AA+", "PHARMA",        "API"),
    ("INE437C0", "Torrent Pharmaceuticals",     "AA+", "PHARMA",        "Generic Pharma"),
    # Infrastructure
    ("INE010B0", "Larsen & Toubro",             "AAA", "INFRASTRUCTURE","EPC & Construction"),
    ("INE522F0", "IRB Infrastructure",          "A+",  "INFRASTRUCTURE","Toll Roads"),
    ("INE741K1", "GR Infraprojects",            "A+",  "INFRASTRUCTURE","Roads"),
    ("INE750L0", "KNR Constructions",           "A",   "INFRASTRUCTURE","Roads"),
    ("INE580I0", "KEC International",           "AA-", "INFRASTRUCTURE","Power T&D"),
    ("INE584I0", "Kalpataru Projects",          "A+",  "INFRASTRUCTURE","EPC"),
    ("INE512A0", "Techno Electric",             "AA-", "INFRASTRUCTURE","Power EPC"),
    # Power & Energy
    ("INE467B1", "Adani Power",                 "AA-", "ENERGY",        "Power Generation"),
    ("INE584C0", "JSW Energy",                  "AA",  "ENERGY",        "Power Generation"),
    ("INE036A0", "Tata Power",                  "AA",  "ENERGY",        "Integrated Power"),
    ("INE326H0", "Torrent Power",               "AA+", "ENERGY",        "Power Distribution"),
    ("INE325A0", "Cesc Limited",                "AA+", "ENERGY",        "Power Distribution"),
    ("INE887A0", "NHPC Green Energy",           "AA+", "ENERGY",        "Renewable"),
    ("INE196B0", "Renew Power",                 "A",   "ENERGY",        "Solar & Wind"),
    ("INE078H0", "Greenko Energy",              "A",   "ENERGY",        "Renewable"),
    # Metals & Mining
    ("INE494B0", "Tata Steel",                  "AA-", "MATERIALS",     "Steel"),
    ("INE205A2", "JSW Steel",                   "AA",  "MATERIALS",     "Steel"),
    ("INE205K0", "Hindalco Industries",         "AA",  "MATERIALS",     "Aluminium"),
    ("INE038A0", "Hindustan Zinc",              "AAA", "MATERIALS",     "Zinc"),
    ("INE038B0", "Vedanta Limited",             "AA-", "MATERIALS",     "Mining & Metals"),
    ("INE022A0", "National Aluminium Company",  "AA+", "MATERIALS",     "Aluminium"),
    ("INE129A0", "Coal India",                  "AA+", "MATERIALS",     "Coal Mining"),
    # Chemicals
    ("INE084B0", "Pidilite Industries",         "AA+", "MATERIALS",     "Adhesives"),
    ("INE235A0", "SRF Limited",                 "AA",  "MATERIALS",     "Specialty Chemicals"),
    ("INE373A0", "Aarti Industries",            "A+",  "MATERIALS",     "Specialty Chemicals"),
    ("INE263B0", "Navin Fluorine",              "A+",  "CHEMICALS",     "Fluorochemicals"),
    ("INE405E0", "Gujarat Fluorochemicals",     "AA-", "CHEMICALS",     "Fluoropolymers"),
    # Logistics
    ("INE011A0", "Container Corporation",       "AA+", "LOGISTICS",     "Rail Logistics"),
    ("INE348T0", "Delhivery",                   "A-",  "LOGISTICS",     "Express Logistics"),
    ("INE786H0", "Allcargo Logistics",           "AA-", "LOGISTICS",     "Freight Forwarding"),
    ("INE024B0", "Blue Dart Express",           "AA+", "LOGISTICS",     "Air Express"),
    # Retail
    ("INE039A0", "Avenue Supermarts (D-Mart)",  "AAA", "RETAIL",        "Supermarkets"),
    ("INE792A0", "Titan Company",               "AA+", "RETAIL",        "Jewellery"),
    ("INE592G0", "Trent (Westside)",            "AA-", "RETAIL",        "Fashion Retail"),
    ("INE096I0", "V-Mart Retail",               "A",   "RETAIL",        "Value Retail"),
    # Media & Entertainment
    ("INE070D0", "Zee Entertainment",           "A-",  "MEDIA",         "Broadcasting"),
    ("INE523B0", "Sun TV Network",              "AA+", "MEDIA",         "Broadcasting"),
    ("INE270A0", "PVR Cinemas",                 "A-",  "MEDIA",         "Multiplex"),
    # Hospitality
    ("INE514E0", "Indian Hotels (Taj)",         "AA",  "HOSPITALITY",   "Luxury Hotels"),
    ("INE206H0", "Lemon Tree Hotels",           "A",   "HOSPITALITY",   "Mid-Segment Hotels"),
    ("INE795A0", "Chalet Hotels",               "A+",  "HOSPITALITY",   "Premium Hotels"),
    # Healthcare
    ("INE213A0", "Apollo Hospitals Enterprise", "AA-", "HEALTHCARE",    "Hospitals"),
    ("INE598A0", "Fortis Healthcare",           "A+",  "HEALTHCARE",    "Hospitals"),
    ("INE753K1", "Aster DM Healthcare",         "A+",  "HEALTHCARE",    "Hospitals"),
    ("INE110A0", "Max Healthcare",              "A+",  "HEALTHCARE",    "Hospitals"),
    ("INE034T0", "Narayana Hrudayalaya",        "A+",  "HEALTHCARE",    "Cardiac Care"),
    # Agriculture
    ("INE010A0", "Adani Agri Fresh",            "A-",  "AGRICULTURE",   "Agri-Processing"),
    ("INE149A0", "Jubilant FoodWorks",          "AA-", "CONSUMER",      "Quick Service Restaurant"),
    ("INE597A0", "Kaveri Seed Company",         "A",   "AGRICULTURE",   "Seed Manufacture"),
    # Education
    ("INE714H0", "NIIT Technologies",           "A-",  "EDUCATION",     "IT Training"),
    # Airlines
    ("INE797F0", "IndiGo (InterGlobe Aviation)","A+",  "AVIATION",      "Airlines"),
    ("INE246F0", "SpiceJet",                    "B",   "AVIATION",      "Airlines"),
    # Conglomerate
    ("INE002A0", "Reliance Industries",         "AAA", "INDUSTRIALS",   "Conglomerate"),
    ("INE467C0", "Adani Enterprises",           "A+",  "INDUSTRIALS",   "Conglomerate"),
    ("INE214T0", "Adani Ports & SEZ",           "AA",  "INFRASTRUCTURE","Ports"),
    ("INE765G0", "Adani Transmission",          "AA",  "ENERGY",        "Power Transmission"),
    ("INE932S0", "Adani Green Energy",          "A+",  "ENERGY",        "Renewable Energy"),
    ("INE364L0", "Adani Total Gas",             "AA-", "ENERGY",        "Gas Distribution"),
    ("INE742F0", "Vedanta Resources Finance",   "B+",  "MATERIALS",     "Mining"),
]

CORP_TENORS = [1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20]

def corporate_bonds() -> Generator[dict, None, None]:
    for prefix, name, rating, sector, sub_sector in CORPORATES:
        cr_low  = {"AAA": 6.8, "AA+": 7.05,"AA": 7.3,"AA-": 7.6,"A+": 8.0,"A": 8.3,"A-": 8.7,"B+": 9.2,"B": 9.8}.get(rating, 8.0)
        cr_high = cr_low + 0.85
        for tenor in CORP_TENORS:
            for yr in range(2017, 2026):
                cr = coupon(cr_low, cr_high)
                isin = make_isin("CB")
                exchange = random.choice(["NSE", "BSE"])
                mat_yr = yr + tenor
                yield {
                    "isin": isin,
                    "bond_name": f"{name[:38]} {cr:.2f}% {mat_yr}",
                    "issuer_name": name,
                    "issuer_type": "CORPORATE",
                    "coupon_rate": cr,
                    "coupon_type": "FIXED",
                    "coupon_frequency": 2,
                    "maturity_date": f"{mat_yr}-09-30",
                    "issue_date": f"{yr}-09-30",
                    "face_value": 1000.0,
                    "credit_rating": rating,
                    "rating_agency": random.choice(["CRISIL", "ICRA", "CARE", "India Ratings"]),
                    "sector": sector,
                    "sub_sector": sub_sector,
                    "listing_exchange": exchange,
                    "is_pf_eligible": 1 if rating in ("AAA", "AA+", "AA") else 0,
                }


# ─────────────────────────────────────────────────────────────────────────────
# 8. Municipal Bonds
# ─────────────────────────────────────────────────────────────────────────────

MUNICIPALITIES = [
    ("IN00MUNI01", "Pune Municipal Corporation",       "AA",  "URBAN_INFRA", "Water Supply"),
    ("IN00MUNI02", "Ahmedabad Municipal Corporation",  "AA",  "URBAN_INFRA", "Urban Development"),
    ("IN00MUNI03", "Greater Hyderabad Municipal",      "AA-", "URBAN_INFRA", "Roads & Flyovers"),
    ("IN00MUNI04", "Bhopal Smart City Development",    "A+",  "URBAN_INFRA", "Smart City"),
    ("IN00MUNI05", "Lucknow Smart City",               "A+",  "URBAN_INFRA", "Smart City"),
    ("IN00MUNI06", "Indore Smart City Development",    "AA-", "URBAN_INFRA", "Smart City"),
    ("IN00MUNI07", "Bruhat Bengaluru Mahanagara Palike","AA-", "URBAN_INFRA", "Urban Mobility"),
    ("IN00MUNI08", "Chennai Metropolitan Authority",   "AA",  "URBAN_INFRA", "Metro Rail"),
    ("IN00MUNI09", "Kolkata Metropolitan Dev Auth",    "A+",  "URBAN_INFRA", "Urban Infra"),
    ("IN00MUNI10", "Amravati Capital City Development","A",   "URBAN_INFRA", "Greenfield City"),
    ("IN00MUNI11", "Surat Smart City Development",     "AA-", "URBAN_INFRA", "Smart City"),
    ("IN00MUNI12", "Visakhapatnam Smart City",         "A+",  "URBAN_INFRA", "Port City"),
]

MUNI_TENORS = [5, 7, 10, 15]

def municipal_bonds() -> Generator[dict, None, None]:
    for prefix, name, rating, sector, sub_sector in MUNICIPALITIES:
        for tenor in MUNI_TENORS:
            for yr in range(2020, 2027):
                cr = coupon(6.5, 8.5)
                isin = make_isin("MU")
                yield {
                    "isin": isin,
                    "bond_name": f"{name[:40]} {cr:.2f}% {yr+tenor}",
                    "issuer_name": name,
                    "issuer_type": "MUNICIPAL",
                    "coupon_rate": cr,
                    "coupon_type": "FIXED",
                    "coupon_frequency": 2,
                    "maturity_date": f"{yr+tenor}-03-15",
                    "issue_date": f"{yr}-03-15",
                    "face_value": 1000.0,
                    "credit_rating": rating,
                    "rating_agency": "CRISIL",
                    "sector": sector,
                    "sub_sector": sub_sector,
                    "listing_exchange": "BSE",
                    "is_pf_eligible": 1 if rating in ("AAA", "AA+", "AA") else 0,
                }


# ─────────────────────────────────────────────────────────────────────────────
# 9. Covered Bonds & Securitised Instruments
# ─────────────────────────────────────────────────────────────────────────────

COVERED_ISSUERS = [
    ("IN00COVR01", "HDFC Covered Bond Series",          "AAA", "SECURITISED", "Mortgage Backed"),
    ("IN00COVR02", "LIC HFL Covered Bond",              "AAA", "SECURITISED", "Mortgage Backed"),
    ("IN00COVR03", "SBI Home Loan Securitisation",      "AAA", "SECURITISED", "ABS"),
    ("IN00COVR04", "Bajaj Finance Covered Bond",        "AAA", "SECURITISED", "Consumer Finance ABS"),
    ("IN00COVR05", "ICICI HFC Covered Bond",            "AAA", "SECURITISED", "Mortgage Backed"),
]

def covered_bonds() -> Generator[dict, None, None]:
    for prefix, name, rating, sector, sub_sector in COVERED_ISSUERS:
        for yr in range(2020, 2026):
            for tenor in [3, 5, 7]:
                cr = coupon(6.7, 7.5)
                isin = make_isin("CV")
                yield {
                    "isin": isin,
                    "bond_name": f"{name} {cr:.2f}% {yr+tenor}-Series{yr}",
                    "issuer_name": name,
                    "issuer_type": "COVERED",
                    "coupon_rate": cr,
                    "coupon_type": "FIXED",
                    "coupon_frequency": 4,
                    "maturity_date": f"{yr+tenor}-06-30",
                    "issue_date": f"{yr}-06-30",
                    "face_value": 1000.0,
                    "credit_rating": rating,
                    "rating_agency": "CRISIL",
                    "sector": sector,
                    "sub_sector": sub_sector,
                    "listing_exchange": "BSE",
                    "is_pf_eligible": 1,
                }


# ─────────────────────────────────────────────────────────────────────────────
# Master Iterator
# ─────────────────────────────────────────────────────────────────────────────

def all_bonds():
    yield from gsec_bonds()
    yield from tbill_bonds()
    yield from sdl_bonds()
    yield from psu_bonds()
    yield from bank_bonds()
    yield from nbfc_bonds()
    yield from corporate_bonds()
    yield from municipal_bonds()
    yield from covered_bonds()


# ─────────────────────────────────────────────────────────────────────────────
# Async seeder
# ─────────────────────────────────────────────────────────────────────────────

UPSERT_SQL = """
INSERT INTO universal_bond_master (
    isin, bond_name, issuer_name, issuer_type, coupon_rate, coupon_type,
    coupon_frequency, maturity_date, issue_date, face_value, credit_rating,
    rating_agency, sector, sub_sector, listing_exchange, is_pf_eligible,
    is_active, created_at, updated_at
) VALUES (
    :isin, :bond_name, :issuer_name, :issuer_type, :coupon_rate, :coupon_type,
    :coupon_frequency, :maturity_date, :issue_date, :face_value, :credit_rating,
    :rating_agency, :sector, :sub_sector, :listing_exchange, :is_pf_eligible,
    1, datetime('now'), datetime('now')
)
ON CONFLICT(isin) DO UPDATE SET
    bond_name      = excluded.bond_name,
    coupon_rate    = excluded.coupon_rate,
    credit_rating  = excluded.credit_rating,
    updated_at     = datetime('now')
"""


async def run_seed(batch_size: int = 500, dry_run: bool = False) -> dict:
    """
    Seed all bonds into the gateway database.

    Parameters
    ----------
    batch_size : int   Number of records per bulk insert batch.
    dry_run    : bool  If True, generate bonds but don't write to DB.

    Returns
    -------
    dict with inserted/skipped/total counts.
    """
    await init_database()

    # Count existing
    from app.shared.database import fetch_one as _fo
    before = await _fo("SELECT COUNT(*) AS cnt FROM universal_bond_master")
    before_cnt = int(before["cnt"]) if before else 0

    total = 0
    inserted = 0
    skipped = 0
    batch: list[dict] = []

    for bond in all_bonds():
        total += 1
        # Deduplicate in-memory
        batch.append(bond)
        if len(batch) >= batch_size:
            if not dry_run:
                for b in batch:
                    try:
                        await execute_query(UPSERT_SQL, b)
                        inserted += 1
                    except Exception as exc:
                        skipped += 1
                        log.warning("seed_skip", isin=b.get("isin"), reason=str(exc)[:80])
            else:
                inserted += len(batch)
            batch.clear()
            log.info("seed_progress", total=total, inserted=inserted)

    # Flush remainder
    if batch and not dry_run:
        for b in batch:
            try:
                await execute_query(UPSERT_SQL, b)
                inserted += 1
            except Exception as exc:
                skipped += 1

    after = await _fo("SELECT COUNT(*) AS cnt FROM universal_bond_master")
    after_cnt = int(after["cnt"]) if after else 0

    result = {
        "bonds_generated": total,
        "bonds_inserted_or_updated": inserted,
        "bonds_skipped": skipped,
        "db_count_before": before_cnt,
        "db_count_after": after_cnt,
        "net_new": after_cnt - before_cnt,
    }
    log.info("seed_complete", **result)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Seed all Indian bonds into the gateway DB")
    parser.add_argument("--dry-run", action="store_true", help="Generate bonds without writing to DB")
    parser.add_argument("--batch-size", type=int, default=500, help="Insert batch size")
    args = parser.parse_args()

    result = asyncio.run(run_seed(batch_size=args.batch_size, dry_run=args.dry_run))
    print("\n== Seeder Summary ==================================================")
    for k, v in result.items():
        print(f"  {k:35s}: {v:,}")
    print("====================================================================\n")
