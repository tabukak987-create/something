from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.domain.market_data.instrument_classification import (
    PORTFOLIO_CATEGORY_CENTRAL_STATE,
    PORTFOLIO_CATEGORY_EQUITY,
    PORTFOLIO_CATEGORY_PRIVATE,
    PORTFOLIO_CATEGORY_PSU,
    PORTFOLIO_CATEGORY_SDS,
    category_metadata,
    classify_instrument,
    portfolio_category_from_record,
)


STATE_NAMES = {
    "ANDHRA PRADESH", "ARUNACHAL PRADESH", "ASSAM", "BIHAR", "CHHATTISGARH",
    "GOA", "GUJARAT", "HARYANA", "HIMACHAL PRADESH", "JHARKHAND", "KARNATAKA",
    "KERALA", "MADHYA PRADESH", "MAHARASHTRA", "MANIPUR", "MEGHALAYA", "MIZORAM",
    "NAGALAND", "ODISHA", "PUNJAB", "RAJASTHAN", "SIKKIM", "TAMIL NADU",
    "TELANGANA", "TRIPURA", "UTTAR PRADESH", "UTTARAKHAND", "WEST BENGAL",
    "DELHI", "JAMMU AND KASHMIR", "LADAKH", "PUDUCHERRY",
}

PSU_KEYWORDS = (
    "NTPC", "REC", "RURAL ELECTRIFICATION", "PFC", "POWER FINANCE",
    "NABARD", "NHAI", "IRFC", "HUDCO", "POWER GRID", "PGCIL", "GAIL",
    "ONGC", "SBI", "BANK OF BARODA", "CANARA BANK", "UNION BANK",
)

BANK_NBFC_KEYWORDS = (
    "BANK", "NBFC", "FINANCE", "FINANCIAL", "HOUSING FINANCE", "CAPITAL",
)

MAX_MATURITY_YEAR = 2099
BOND_PORTFOLIO_CATEGORIES = {
    PORTFOLIO_CATEGORY_CENTRAL_STATE,
    PORTFOLIO_CATEGORY_PSU,
    PORTFOLIO_CATEGORY_PRIVATE,
}


@dataclass
class BondValidationResult:
    is_valid: bool
    normalized: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_excel_serial_date(value: Any) -> Optional[date]:
    """Parse Excel serial date values (1900 date system) into calendar dates."""
    val = _to_float(value)
    if val is None or val <= 0:
        return None

    whole_days = int(math.floor(val))
    # Guardrail to avoid interpreting ordinary numeric fields as dates.
    if whole_days < 20000 or whole_days > 90000:
        return None

    # Excel leap-year bug compatibility: serial 60 maps to 1900-02-29 (nonexistent).
    if whole_days == 60:
        whole_days = 59
    elif whole_days > 60:
        whole_days -= 1

    try:
        parsed = date(1899, 12, 31) + timedelta(days=whole_days)
    except Exception:
        return None

    if parsed.year < 1950 or parsed.year > MAX_MATURITY_YEAR:
        return None
    return parsed


def parse_flexible_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        serial_dt = _parse_excel_serial_date(value)
        if serial_dt is not None:
            return serial_dt

    text = str(value).strip()
    if not text:
        return None
    compact = text.replace(",", "")

    # ISO date-time strings and similar inputs.
    try:
        if re.search(r"\d{4}-\d{2}-\d{2}[ T]", compact):
            return datetime.fromisoformat(compact.replace("Z", "+00:00")).date()
    except Exception:
        pass

    # Numeric strings are often Excel serial dates in workbook ingestion flows.
    if re.fullmatch(r"-?\d+(?:\.\d+)?", compact):
        serial_dt = _parse_excel_serial_date(compact)
        if serial_dt is not None:
            return serial_dt

    fmts = (
        "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d/%m/%y", "%d.%m.%Y",
        "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y",
        "%Y/%m/%d", "%Y.%m.%d", "%m/%d/%Y", "%m-%d-%Y",
        "%d-%b-%Y", "%d-%B-%Y", "%Y%m%d",
    )
    for fmt in fmts:
        try:
            return datetime.strptime(compact, fmt).date()
        except ValueError:
            continue

    # Handles values like "Jan 2046" or "2046"
    m = re.search(r"\b([A-Za-z]{3,9})[\s\-/]+(\d{4})\b", compact)
    if m:
        month = m.group(1)
        year = int(m.group(2))
        try:
            return datetime.strptime(f"1 {month} {year}", "%d %b %Y").date()
        except ValueError:
            try:
                return datetime.strptime(f"1 {month} {year}", "%d %B %Y").date()
            except ValueError:
                pass

    m = re.search(r"\b(\d{4})\b", compact)
    if m:
        return date(int(m.group(1)), 12, 31)

    return None


def normalize_yield_percent(value: Any) -> Optional[float]:
    val = _to_float(value)
    if val is None:
        return None
    if abs(val) < 1:
        return val * 100.0
    return val


def classify_issuer(issuer_name: str, rating: Optional[str] = None) -> Dict[str, Any]:
    issuer = (issuer_name or "").upper()
    rating_up = (rating or "").upper()

    if "GOVERNMENT OF INDIA" in issuer or re.search(r"\bGOI\b", issuer):
        return {
            "issuer_category": "CENTRAL_GOVERNMENT",
            "sector": "CENTRAL_GOVERNMENT",
            "government_backed": True,
            "psu_flag": False,
            "issuer_type": "G-SEC",
        }

    if "GOVERNMENT OF" in issuer:
        for state in STATE_NAMES:
            if state in issuer:
                return {
                    "issuer_category": "STATE_GOVERNMENT",
                    "sector": "STATE_GOVERNMENT",
                    "government_backed": True,
                    "psu_flag": False,
                    "issuer_type": "SDL",
                }

    if "SGS" in issuer or "STATE DEVELOPMENT LOAN" in issuer:
        if any(state in issuer for state in STATE_NAMES):
            return {
                "issuer_category": "STATE_GOVERNMENT",
                "sector": "STATE_GOVERNMENT",
                "government_backed": True,
                "psu_flag": False,
                "issuer_type": "SDL",
            }

    if any(key in issuer for key in PSU_KEYWORDS):
        return {
            "issuer_category": "PSU",
            "sector": "PSU",
            "government_backed": True,
            "psu_flag": True,
            "issuer_type": "PSU",
        }

    if any(key in issuer for key in BANK_NBFC_KEYWORDS):
        return {
            "issuer_category": "CORPORATE",
            "sector": "CORPORATE",
            "government_backed": False,
            "psu_flag": False,
            "issuer_type": "CORPORATE",
        }

    # Sovereign ratings cannot map to corporate sectors.
    if rating_up.startswith("SOV"):
        return {
            "issuer_category": "STATE_GOVERNMENT",
            "sector": "STATE_GOVERNMENT",
            "government_backed": True,
            "psu_flag": False,
            "issuer_type": "SDL",
        }

    return {
        "issuer_category": "CORPORATE",
        "sector": "CORPORATE",
        "government_backed": False,
        "psu_flag": False,
        "issuer_type": "CORPORATE",
    }


def validate_maturity_window(maturity_dt: Optional[date], current_dt: Optional[date] = None) -> List[str]:
    errors: List[str] = []
    if maturity_dt is None:
        errors.append("Maturity date is missing or unparseable")
        return errors

    current_dt = current_dt or date.today()
    if maturity_dt.year < current_dt.year:
        errors.append(f"Maturity year {maturity_dt.year} is in the past")
    if maturity_dt.year > MAX_MATURITY_YEAR:
        errors.append(f"Maturity year {maturity_dt.year} exceeds {MAX_MATURITY_YEAR}")
    return errors


def validate_coupon_start_before_maturity(coupon_start: Optional[date], maturity_dt: Optional[date]) -> List[str]:
    if coupon_start and maturity_dt and coupon_start >= maturity_dt:
        return ["Coupon start date must be before maturity date"]
    return []


def _maturity_optional(record: Dict[str, Any]) -> bool:
    text = " ".join(
        str(record.get(field) or "")
        for field in ("bond_name", "issuer_name", "category", "sector", "instrument_name", "particulars")
    ).upper()
    isin = str(record.get("isin") or "").upper()
    ip_dates = str(record.get("ip_dates") or "").upper()
    if isin.startswith("INF") or "NAV" in ip_dates:
        return True
    return any(
        token in text
        for token in (
            "PERPETUAL",
            "MUTUAL FUND",
            "ETF",
            "EQUITY",
            "FUND",
            "BLUECHIP",
            "FLEXICAP",
            "BALANCED ADVANTAGE",
            "DIRECT GROWTH",
        )
    )


def validate_yield_range(ytm_percent: Optional[float]) -> List[str]:
    if ytm_percent is None:
        return ["YTM is missing"]
    if ytm_percent < 0.1 or ytm_percent > 25.0:
        return [f"YTM {ytm_percent:.4f}% is outside allowed range 0.1%-25%"]
    return []


def validate_sector_rating_consistency(rating: Optional[str], sector: Optional[str]) -> List[str]:
    rating_up = (rating or "").upper()
    sector_up = (sector or "").upper()
    if rating_up.startswith("SOV") and sector_up not in {"CENTRAL_GOVERNMENT", "STATE_GOVERNMENT", "GOVERNMENT_SECURITIES", "G-SEC", "SDL", "SOVEREIGN"}:
        return ["SOVEREIGN rated bond cannot be classified outside central/state government sectors"]
    return []


def validate_bond_record(
    record: Dict[str, Any],
    current_dt: Optional[date] = None,
    *,
    strict: bool = False,
    known_isins: Optional[set[str]] = None,
) -> BondValidationResult:
    normalized = dict(record)
    errors: List[str] = []
    warnings: List[str] = []
    classification = classify_instrument(record)
    portfolio_category = portfolio_category_from_record(record, classification)
    category_meta = category_metadata(portfolio_category)

    for key, value in classification.items():
        if normalized.get(key) in (None, ""):
            normalized[key] = value
    if normalized.get("portfolio_category") in (None, ""):
        normalized["portfolio_category"] = portfolio_category
    if normalized.get("portfolio_category_label") in (None, ""):
        normalized["portfolio_category_label"] = category_meta.get("category_display", portfolio_category)
    if normalized.get("portfolio_category_key") in (None, ""):
        normalized["portfolio_category_key"] = portfolio_category
    if normalized.get("portfolio_bucket") in (None, ""):
        normalized["portfolio_bucket"] = portfolio_category
    if not normalized.get("category"):
        normalized["category"] = category_meta.get("category_display", portfolio_category)

    is_bond = portfolio_category in BOND_PORTFOLIO_CATEGORIES

    isin = str(record.get("isin") or "").strip().upper()
    if strict and not isin:
        errors.append("ISIN is missing")
    if strict and known_isins is not None and isin and isin not in known_isins:
        warnings.append(f"Unknown ISIN {isin}")

    quantity = _to_float(record.get("quantity"))
    if strict and (quantity is None or quantity <= 0):
        errors.append("Quantity must be greater than zero")

    maturity_dt = parse_flexible_date(record.get("maturity_date"))
    normalized["maturity_date"] = maturity_dt.isoformat() if maturity_dt else None
    if is_bond:
        if maturity_dt is None and not _maturity_optional(record):
            errors.extend(validate_maturity_window(maturity_dt, current_dt=current_dt))
        elif maturity_dt is not None:
            errors.extend(validate_maturity_window(maturity_dt, current_dt=current_dt))
            if strict:
                current_dt = current_dt or date.today()
                if maturity_dt <= current_dt:
                    errors.append("Maturity date must be in the future")

    coupon_start_dt = parse_flexible_date(record.get("coupon_start_date") or record.get("issue_date") or record.get("purchase_date"))
    if is_bond:
        errors.extend(validate_coupon_start_before_maturity(coupon_start_dt, maturity_dt))

    ytm_norm = normalize_yield_percent(record.get("ytm") if record.get("ytm") is not None else record.get("ytm_at_purchase"))
    if ytm_norm is not None:
        normalized["ytm"] = ytm_norm
        normalized["ytm_at_purchase"] = ytm_norm
    if is_bond and ytm_norm is not None:
        errors.extend(validate_yield_range(ytm_norm))

    coupon = _to_float(record.get("coupon_rate") or record.get("coupon"))
    if coupon is not None:
        normalized["coupon_rate"] = coupon
        if coupon < 0 or coupon > 25:
            errors.append(f"Coupon {coupon:.4f}% is outside allowed range 0%-25%")
    elif is_bond:
        warnings.append("Coupon rate is missing")

    price = _to_float(record.get("clean_price") or record.get("purchase_price") or record.get("price"))
    if price is not None:
        normalized["clean_price"] = price
        if price <= 0:
            errors.append("Price must be positive")

    issuer_name = str(record.get("issuer_name") or "").strip()
    if not issuer_name:
        errors.append("Issuer name is missing")

    rating = str(record.get("credit_rating_1") or record.get("credit_rating") or record.get("rating") or "").strip()
    rating_agency = str(record.get("rating_agency_1") or record.get("rating_agency") or "").strip()
    if rating:
        normalized["credit_rating"] = rating
        normalized["credit_rating_1"] = rating
        normalized["rating"] = rating
    if rating_agency:
        normalized["rating_agency"] = rating_agency
        normalized["rating_agency_1"] = rating_agency
    cls = classify_issuer(issuer_name, rating=rating)
    for k, v in cls.items():
        if normalized.get(k) in (None, ""):
            normalized[k] = v

    sector_value = str(normalized.get("sector") or "").strip().upper()
    classified_sector = str(cls.get("sector") or "").strip().upper()
    if classified_sector and (not sector_value or sector_value in {"SOVEREIGN", "GOVERNMENT", "GOVT"}):
        normalized["sector"] = classified_sector

    if is_bond and not rating and cls.get("issuer_type") not in {"G-SEC", "SDL"} and not _maturity_optional(record):
        errors.append("Credit rating is missing")

    if is_bond:
        errors.extend(validate_sector_rating_consistency(rating=rating, sector=normalized.get("sector")))

    if is_bond:
        if maturity_dt is None and not _maturity_optional(record):
            errors.extend(validate_maturity_window(maturity_dt, current_dt=current_dt))
        elif maturity_dt is not None:
            errors.extend(validate_maturity_window(maturity_dt, current_dt=current_dt))
            if strict:
                current_dt = current_dt or date.today()
                if maturity_dt <= current_dt:
                    errors.append("Maturity date must be in the future")

    return BondValidationResult(is_valid=len(errors) == 0, normalized=normalized, errors=errors, warnings=warnings)


def interpolate_benchmark_yield(curve_points: Iterable[Tuple[float, float]], tenor_years: float) -> Optional[float]:
    pts = sorted((float(t), float(y)) for t, y in curve_points)
    if not pts:
        return None
    if tenor_years <= pts[0][0]:
        return pts[0][1]
    if tenor_years >= pts[-1][0]:
        return pts[-1][1]

    for i in range(1, len(pts)):
        t0, y0 = pts[i - 1]
        t1, y1 = pts[i]
        if t0 <= tenor_years <= t1:
            w = (tenor_years - t0) / (t1 - t0)
            return y0 + w * (y1 - y0)
    return None


def compute_spread_metrics(bond_yield_pct: Optional[float], benchmark_yield_pct: Optional[float]) -> Dict[str, Optional[float]]:
    if bond_yield_pct is None or benchmark_yield_pct is None:
        return {"g_spread": None, "z_spread": None}
    spread = bond_yield_pct - benchmark_yield_pct
    return {"g_spread": spread, "z_spread": spread}


def solve_ytm_percent(
    clean_price: float,
    face_value: float,
    coupon_rate_pct: float,
    years_to_maturity: float,
    frequency: int = 2,
    max_iter: int = 200,
    tol: float = 1e-9,
) -> Optional[float]:
    if clean_price <= 0 or face_value <= 0 or years_to_maturity <= 0:
        return None

    n = max(1, int(round(years_to_maturity * frequency)))
    coupon = face_value * (coupon_rate_pct / 100.0) / frequency

    def pv(y: float) -> float:
        r = y / frequency
        total = 0.0
        for t in range(1, n + 1):
            cf = coupon if t < n else coupon + face_value
            total += cf / ((1.0 + r) ** t)
        return total

    low = -0.9 * frequency
    high = 2.0
    for _ in range(120):
        if pv(high) < clean_price:
            break
        high *= 1.5
        if high > 100.0:
            return None

    for _ in range(max_iter):
        mid = 0.5 * (low + high)
        diff = pv(mid) - clean_price
        if abs(diff) < tol:
            return mid * 100.0
        if diff > 0:
            low = mid
        else:
            high = mid

    return mid * 100.0


def compute_duration_metrics(
    clean_price: float,
    face_value: float,
    coupon_rate_pct: float,
    ytm_pct: float,
    years_to_maturity: float,
    frequency: int = 2,
) -> Dict[str, Optional[float]]:
    if clean_price <= 0 or face_value <= 0 or years_to_maturity <= 0:
        return {"duration": None, "modified_duration": None, "dv01": None}

    y = ytm_pct / 100.0
    n = max(1, int(round(years_to_maturity * frequency)))
    coupon = face_value * (coupon_rate_pct / 100.0) / frequency

    pv_sum = 0.0
    weighted = 0.0
    for t in range(1, n + 1):
        cf = coupon if t < n else coupon + face_value
        discount = (1.0 + y / frequency) ** t
        pv_cf = cf / discount
        pv_sum += pv_cf
        weighted += (t / frequency) * pv_cf

    if pv_sum <= 0:
        return {"duration": None, "modified_duration": None, "dv01": None}

    macaulay = weighted / pv_sum
    modified = macaulay / (1.0 + y / frequency)
    dv01 = modified * clean_price * 0.0001

    return {
        "duration": macaulay,
        "modified_duration": modified,
        "dv01": dv01,
    }
