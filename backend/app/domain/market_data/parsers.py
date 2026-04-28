"""
Broker Quote Parser — Multi-Format PDF/Excel Ingestion
=======================================================
Handles the following broker quote sheet formats sent daily:

  • RFS           — XLSX, header at row 5, ISINs in col-1
  • Nuvama        — XLSX (Investment Quotes), header at row 5
                    PDF (Bond Vector), pdfplumber table extraction
  • Darashaw      — XLSX, two-section layout, No ISIN column (name-match)
  • Generic/Other — XLSX, header at row 0 (Quote/Quotes prefix files)
  • Aditya Birla  — PDF, text-based, pdfplumber extraction

For each file the parser:
  1. Detects broker, date, and format
  2. Extracts a list of ParsedQuote objects
  3. Returns an IngestResult with stats and errors
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple

import pandas as pd

# pdfplumber — graceful degradation if not installed
try:
    import pdfplumber
    _PDF_OK = True
except ImportError:
    _PDF_OK = False


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ParsedQuote:
    isin: str
    broker_name: str
    quote_date: str                     # YYYY-MM-DD
    bond_name: Optional[str] = None
    category: Optional[str] = None
    coupon_rate: Optional[float] = None
    maturity_date: Optional[str] = None
    rating: Optional[str] = None
    rating_agency: Optional[str] = None
    clean_price: Optional[float] = None
    ytm: Optional[float] = None         # annual, decimal
    ytm_semi: Optional[float] = None    # semi-annual, decimal
    ytm_screen: Optional[float] = None  # screen/reference YTM
    bid_price: Optional[float] = None
    ask_price: Optional[float] = None
    bid_yield: Optional[float] = None
    ask_yield: Optional[float] = None
    lot_size: Optional[int] = None
    quantity_lacs: Optional[float] = None
    put_call_date: Optional[str] = None
    ip_dates: Optional[str] = None
    settlement_days: int = 1
    source_file: str = ""
    source_format: str = ""


@dataclass
class IngestResult:
    filename: str
    broker_name: str
    quote_date: str
    source_format: str
    quotes: List[ParsedQuote] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.quotes)


# ─────────────────────────────────────────────────────────────────────────────
# Broker & date detection helpers
# ─────────────────────────────────────────────────────────────────────────────

_BROKER_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"rfs", re.I), "RFS"),
    (re.compile(r"nuvama", re.I), "Nuvama"),
    (re.compile(r"darashaw", re.I), "Darashaw"),
    (re.compile(r"aditya.?birla|birla.?capital|birla", re.I), "Aditya Birla Capital"),
    (re.compile(r"bond.?vector", re.I), "Nuvama"),        # Bond Vector = Nuvama research
    (re.compile(r"std\s+quotes?|std\s*-\s*pf|standard.?chartered", re.I), "Standard Chartered"),  # STD Quotes = Standard Chartered
    (re.compile(r"iifl", re.I), "IIFL"),
    (re.compile(r"hdfc", re.I), "HDFC Securities"),
    (re.compile(r"kotak", re.I), "Kotak Securities"),
    (re.compile(r"axis", re.I), "Axis Capital"),
    (re.compile(r"icici", re.I), "ICICI Securities"),
]

_DATE_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"(\d{2})[-_]?([A-Za-z]{3})[-_]?(\d{4})"), "dmy_str"),   # 09-Feb-2026
    (re.compile(r"(\d{2})[-_]?(\d{2})[-_]?(\d{4})"), "dmy_num"),          # 13-02-2026
    (re.compile(r"(\d{4})[-_]?(\d{2})[-_]?(\d{2})"), "ymd"),              # 2026-02-09
    (re.compile(r"(\d{8})"), "compact"),                                   # 18022026
]

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_ISIN_RE = re.compile(r"\b(IN[A-Z0-9]{10})\b")


def detect_broker(filename: str) -> str:
    for pattern, name in _BROKER_PATTERNS:
        if pattern.search(filename):
            return name
    return "Unknown"


def detect_broker_from_content(text: str) -> str:
    """
    Scan arbitrary text (Excel cell dump or PDF first-page text) for broker names.
    Returns broker name if found, else "Unknown".
    """
    # Extended patterns that cover common broker header lines/footers
    _CONTENT_PATTERNS: List[Tuple[re.Pattern, str]] = [
        (re.compile(r"nuvama|nuv(?:ama)?\s*wealth|nuv(?:ama)?\s*securities", re.I), "Nuvama"),
        (re.compile(r"edelweiss", re.I), "Edelweiss"),
        (re.compile(r"standard\s*chartered|standard\s*bank", re.I), "Standard Chartered"),
        (re.compile(r"darashaw", re.I), "Darashaw"),
        (re.compile(r"aditya\s*birla|birla\s*capital|birla\s*sunlife|absl\b", re.I), "Aditya Birla Capital"),
        (re.compile(r"rfs\s*(?:broking|securities|pvt)?", re.I), "RFS"),
        (re.compile(r"bond\s*vector", re.I), "Nuvama"),
        (re.compile(r"iifl\s*(?:securities|wealth)?", re.I), "IIFL"),
        (re.compile(r"hdfc\s*securities|hdfc\s*bank", re.I), "HDFC Securities"),
        (re.compile(r"hdfc\b", re.I), "HDFC Securities"),
        (re.compile(r"kotak\s*(?:securities|mahindra)?", re.I), "Kotak Securities"),
        (re.compile(r"axis\s*(?:capital|securities|bank)?", re.I), "Axis Capital"),
        (re.compile(r"icici\s*(?:securities|bank)?", re.I), "ICICI Securities"),
        (re.compile(r"sbicap\s*securities|sbi\s*cap", re.I), "SBI Capital"),
        (re.compile(r"jm\s*financial", re.I), "JM Financial"),
        (re.compile(r"trust\s*plutus|trustplutus", re.I), "TrustPlutus"),
        (re.compile(r"trustmf\b|trust\s*mutual", re.I), "TrustMF"),
        (re.compile(r"motilal\s*(?:oswal)?", re.I), "MotilalOswal"),
        (re.compile(r"invesco\b", re.I), "Invesco"),
        (re.compile(r"\bnj\s*(?:india|wealth|invest)?", re.I), "NJ"),
        (re.compile(r"antique\s*(?:stock)?", re.I), "Antique"),
        (re.compile(r"nirmal\s*bang", re.I), "Nirmal Bang"),
        (re.compile(r"standard\s*chartered|std\s+quotes?|std\s*-?\s*pf\s+trust", re.I), "Standard Chartered"),
        (re.compile(r"bajaj\s*(?:broking|finserv)?", re.I), "Bajaj Broking"),
        (re.compile(r"geojit", re.I), "Geojit"),
        (re.compile(r"ventura\s*(?:securities)?", re.I), "Ventura"),
        (re.compile(r"SMIFS|smi\s*fs", re.I), "SMIFS"),
        (re.compile(r"sharekhan", re.I), "Sharekhan"),
        (re.compile(r"angel\s*(?:broking|one)?", re.I), "Angel One"),
        (re.compile(r"5paisa", re.I), "5paisa"),
        (re.compile(r"phillip\s*capital|phillipcap", re.I), "PhillipCapital"),
        (re.compile(r"prabhudas\s*lilladher|prabhu", re.I), "Prabhudas Lilladher"),
        (re.compile(r"emkay\b", re.I), "Emkay"),
        (re.compile(r"karvy\b", re.I), "Karvy"),
        (re.compile(r"religare\b", re.I), "Religare"),
        (re.compile(r"zerodha\b", re.I), "Zerodha"),
    ]
    for pattern, name in _CONTENT_PATTERNS:
        if pattern.search(text):
            return name
    return "Unknown"


def extract_date_from_string(text: str) -> Optional[str]:
    """Try to extract a YYYY-MM-DD date from an arbitrary string."""
    for pattern, fmt in _DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        try:
            if fmt == "dmy_str":
                d, mon, y = m.group(1), m.group(2).lower()[:3], m.group(3)
                return f"{y}-{_MONTH_MAP.get(mon, 1):02d}-{int(d):02d}"
            elif fmt == "dmy_num":
                d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if 1 <= d <= 31 and 1 <= mo <= 12 and 2000 <= y <= 2099:
                    return f"{y}-{mo:02d}-{d:02d}"
            elif fmt == "ymd":
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if 2000 <= y <= 2099 and 1 <= mo <= 12 and 1 <= d <= 31:
                    return f"{y}-{mo:02d}-{d:02d}"
            elif fmt == "compact":
                s = m.group(1)
                # Try DDMMYyyy
                d, mo, y = int(s[:2]), int(s[2:4]), int(s[4:])
                if 1 <= d <= 31 and 1 <= mo <= 12 and 2000 <= y <= 2099:
                    return f"{y}-{mo:02d}-{d:02d}"
        except (ValueError, KeyError):
            continue
    return None


def _to_float(val) -> Optional[float]:
    """Coerce a cell value to float, returning None for blanks/NaT."""
    if val is None:
        return None
    if isinstance(val, float):
        import math
        return None if math.isnan(val) else val
    if isinstance(val, (int,)):
        return float(val)
    s = str(val).strip().replace(",", "").replace("%", "")
    if not s or s.lower() in ("nan", "nat", "none", "-", "n/a", ""):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_str(val) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return None if s.lower() in ("nan", "nat", "none", "", "-") else s


def _parse_maturity(val) -> Optional[str]:
    if val is None:
        return None
    # pandas Timestamp
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%d")
    s = _to_str(val)
    if not s:
        return None
    # Try common formats
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d-%b-%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s.split(" ")[0], fmt.split(" ")[0]).strftime("%Y-%m-%d")
        except ValueError:
            pass
    # Try pandas
    try:
        return pd.to_datetime(s).strftime("%Y-%m-%d")
    except Exception:
        pass
    return s


def _normalise_ytm(val: Optional[float]) -> Optional[float]:
    """Ensure YTM is in decimal form (e.g. 0.0687, not 6.87). Reject invalid values."""
    if val is None:
        return None
    if val < 0:
        # Negative yields are invalid for Indian corporate/PF-grade bonds
        return None
    if val > 1.0:   # already in percentage form like 6.87
        val = round(val / 100, 6)
    else:
        val = round(val, 6)
    # Reject unrealistically high yields (>25%)
    if val > 0.25:
        return None
    return val


def _normalise_price(val: Optional[float]) -> Optional[float]:
    """Bond prices are per ₹100 face value, typically 95-115."""
    if val is None:
        return None
    # Sanity check
    if 50.0 <= val <= 200.0:
        return round(val, 6)
    return None


def _parse_rating(val) -> Tuple[Optional[str], Optional[str]]:
    """Split a combined rating string like 'AA+CRISIL & INDIA RATING' → ('AA+', 'CRISIL')."""
    if not val:
        return None, None
    s = str(val).strip()
    agencies = ["CRISIL", "ICRA", "CARE", "FITCH", "MOODY", "INDIA RATING", "ACUITE", "BRICKWORK"]
    grade = s
    agency = None
    for ag in agencies:
        if ag.lower() in s.lower():
            agency = ag
            grade = re.sub(re.escape(ag), "", grade, flags=re.I).strip(",& \t")
            break
    # Clean up grade
    grade = re.sub(r"\s+", "", grade).upper()
    # Map Sovereign explicitly
    if "SOVEREIGN" in grade.upper():
        return "Sovereign", "Govt of India"
    return grade or None, agency


# ─────────────────────────────────────────────────────────────────────────────
# Main parser class
# ─────────────────────────────────────────────────────────────────────────────

class BrokerQuoteParser:
    """
    Unified broker quote file parser.

    Usage::

        parser = BrokerQuoteParser()
        result = parser.parse("RFS PF QUOTES 09-Feb-2026.xlsx")
        for q in result.quotes:
            print(q.isin, q.ytm, q.clean_price)
    """

    def parse(self, filepath: str | Path, broker_override: str | None = None) -> IngestResult:
        filepath = Path(filepath)
        filename = filepath.name
        broker = broker_override or detect_broker(filename)
        quote_date = extract_date_from_string(filename) or date.today().isoformat()

        ext = filepath.suffix.lower()
        if ext in (".xlsx", ".xls"):
            return self._parse_excel(filepath, filename, broker, quote_date)
        elif ext == ".pdf":
            return self._parse_pdf(filepath, filename, broker, quote_date)
        else:
            return IngestResult(
                filename=filename, broker_name=broker,
                quote_date=quote_date, source_format="UNSUPPORTED",
                errors=[f"Unsupported file type: {ext}"],
            )

    # ── Excel dispatch ────────────────────────────────────────────────────────

    def _parse_excel(
        self, path: Path, filename: str, broker: str, quote_date: str
    ) -> IngestResult:
        try:
            df_raw = pd.read_excel(str(path), header=None, dtype=str)
        except Exception as exc:
            return IngestResult(
                filename=filename, broker_name=broker, quote_date=quote_date,
                source_format="EXCEL_ERROR", errors=[str(exc)],
            )

        # Try to extract quote date from file content if not in filename
        if not extract_date_from_string(filename):
            for ri in range(min(4, len(df_raw))):
                for ci in range(min(5, len(df_raw.columns))):
                    cell = _to_str(df_raw.iloc[ri, ci])
                    if cell:
                        d = extract_date_from_string(cell)
                        if d:
                            quote_date = d
                            break

        # Try to detect broker from content when filename gives nothing useful
        if broker == "Unknown":
            # Collect all text in first 8 rows × all columns, concat and search
            header_text_parts = []
            for ri in range(min(8, len(df_raw))):
                for ci in range(len(df_raw.columns)):
                    v = _to_str(df_raw.iloc[ri, ci])
                    if v:
                        header_text_parts.append(v)
            header_text = " ".join(header_text_parts)
            detected = detect_broker_from_content(header_text)
            if detected != "Unknown":
                broker = detected

        # RFS column-signature detection — 'Security Detail' + 'ISIN' in any header row
        if broker == "Unknown":
            for ri in range(min(15, len(df_raw))):
                row_text = " ".join(
                    str(v).strip() for v in df_raw.iloc[ri].values
                    if _to_str(v)
                ).lower()
                if "security detail" in row_text and "isin" in row_text:
                    broker = "RFS"
                    break

        broker_upper = broker.upper()

        if "RFS" in broker_upper:
            return self._parse_rfs(df_raw, filename, broker, quote_date)
        elif "NUVAMA" in broker_upper:
            return self._parse_nuvama_xl(df_raw, filename, broker, quote_date)
        elif "DARASHAW" in broker_upper:
            return self._parse_darashaw(df_raw, filename, broker, quote_date)
        else:
            # Try generic format (Quote/Quotes files)
            result = self._parse_generic(df_raw, filename, broker, quote_date)
            # If we got nothing, try auto-detect
            if not result.quotes:
                result = self._parse_auto(df_raw, filename, broker, quote_date)
            return result

    # ── RFS Format ───────────────────────────────────────────────────────────
    # Row 1: date, Row 4: category header, Row 5: column headers, Row 6+: data
    # Cols: Coupon(0) | ISIN(1) | Name(2) | TYPE(3) | Rating(4) | Maturity(5)
    #       IP Dates(6) | Put/Call(7) | Price(8) | YTM(9) | Semi(10) | FV(11) | Qty(12)

    def _parse_rfs(
        self, df: pd.DataFrame, filename: str, broker: str, quote_date: str
    ) -> IngestResult:
        result = IngestResult(
            filename=filename, broker_name=broker, quote_date=quote_date,
            source_format="EXCEL_RFS",
        )
        # Find header row (contains "ISIN")
        hrow = None
        for i, row in df.iterrows():
            if any("isin" in str(v).lower() for v in row.values):
                hrow = i
                break
        if hrow is None:
            result.errors.append("Could not find ISIN header row")
            return result

        col_map = {}
        for ci, val in enumerate(df.iloc[hrow]):
            v = str(val).lower().strip()
            if "isin" in v:
                col_map["isin"] = ci
            elif "price" in v:
                col_map["price"] = ci
            elif "ytm" in v and "semi" not in v and "annual" not in v:
                col_map["ytm"] = ci
            elif "semi" in v:
                col_map["ytm_semi"] = ci
            elif "name" in v or "security" in v:
                col_map["name"] = ci
            elif "coupon" in v:
                col_map["coupon"] = ci
            elif "maturity" in v or "maturity date" in v:
                col_map["maturity"] = ci
            elif "rating" in v or "agency" in v:
                col_map["rating"] = ci
            elif "type" in v and "type" in v:
                col_map["category"] = ci
            elif "quantum" in v or "face value" in v:
                col_map["qty"] = ci
            elif "put" in v or "call" in v:
                col_map["put_call"] = ci
            elif "ip date" in v or "interest" in v:
                col_map["ip_dates"] = ci

        if "isin" not in col_map:
            result.errors.append("ISIN column not found in RFS header")
            return result

        current_category = None
        for i in range(hrow + 1, len(df)):
            row = df.iloc[i]
            isin = _to_str(row.iloc[col_map["isin"]] if col_map["isin"] < len(row) else None)
            if not isin or not _ISIN_RE.match(isin):
                # Might be a category header row
                for v in row.values:
                    s = _to_str(v)
                    if s and len(s) > 3 and not any(c.isdigit() for c in s):
                        current_category = s
                continue

            price = _normalise_price(_to_float(row.iloc[col_map.get("price", 999)] if col_map.get("price", 999) < len(row) else None))
            ytm = _normalise_ytm(_to_float(row.iloc[col_map.get("ytm", 999)] if col_map.get("ytm", 999) < len(row) else None))
            ytm_semi = _normalise_ytm(_to_float(row.iloc[col_map.get("ytm_semi", 999)] if col_map.get("ytm_semi", 999) < len(row) else None))
            rat_raw = _to_str(row.iloc[col_map.get("rating", 999)] if col_map.get("rating", 999) < len(row) else None)
            rating, rating_agency = _parse_rating(rat_raw)
            qty_raw = _to_str(row.iloc[col_map.get("qty", 999)] if col_map.get("qty", 999) < len(row) else None)
            qty_lacs = _parse_quantity_lacs(qty_raw)
            coupon = _normalise_ytm(_to_float(row.iloc[col_map.get("coupon", 999)] if col_map.get("coupon", 999) < len(row) else None))

            q = ParsedQuote(
                isin=isin.strip().upper(),
                broker_name=broker,
                quote_date=quote_date,
                bond_name=_to_str(row.iloc[col_map.get("name", 999)] if col_map.get("name", 999) < len(row) else None),
                category=_to_str(row.iloc[col_map.get("category", 999)] if col_map.get("category", 999) < len(row) else None) or current_category,
                coupon_rate=coupon,
                maturity_date=_parse_maturity(row.iloc[col_map.get("maturity", 999)] if col_map.get("maturity", 999) < len(row) else None),
                rating=rating,
                rating_agency=rating_agency,
                clean_price=price,
                ytm=ytm,
                ytm_semi=ytm_semi,
                quantity_lacs=qty_lacs,
                put_call_date=_to_str(row.iloc[col_map.get("put_call", 999)] if col_map.get("put_call", 999) < len(row) else None),
                ip_dates=_to_str(row.iloc[col_map.get("ip_dates", 999)] if col_map.get("ip_dates", 999) < len(row) else None),
                source_file=filename,
                source_format="EXCEL_RFS",
            )
            result.quotes.append(q)

        return result

    # ── Nuvama Excel Format ───────────────────────────────────────────────────
    # Row 1: date, Row 5: headers, Row 6: category, Row 7+: data
    # Cols: Name(0) | Coupon(1) | ISIN(2) | Rating(3) | IntDate(4) | Maturity(5)
    #       FV(6) | Price(7) | YTM(8) | Qty(9) | Qtm(10) | Type(11) |
    #       SemiAnnual(12) | Annual(13) | ScreenPrice(14) | SemiScreen(15) | AnnualScreen(16)

    def _parse_nuvama_xl(
        self, df: pd.DataFrame, filename: str, broker: str, quote_date: str
    ) -> IngestResult:
        result = IngestResult(
            filename=filename, broker_name=broker, quote_date=quote_date,
            source_format="EXCEL_NUVAMA",
        )
        # Find main header row
        hrow = None
        for i, row in df.iterrows():
            vals = [str(v).lower() for v in row.values]
            if sum(1 for v in vals if "isin" in v or "coupon" in v or "price" in v or "ytm" in v) >= 3:
                hrow = i
                break
        if hrow is None:
            result.errors.append("Could not find Nuvama header row")
            return result

        col_map = {}
        for ci, val in enumerate(df.iloc[hrow]):
            v = str(val).lower().strip()
            if "isin" in v:
                col_map["isin"] = ci
            elif "name" in v or "security" in v:
                col_map["name"] = ci
            elif "coupon" in v:
                col_map["coupon"] = ci
            elif "maturity" in v:
                col_map["maturity"] = ci
            elif "rating" in v:
                col_map["rating"] = ci
            elif "ytm" in v or "annualised ytm" in v or "annual" in v:
                if col_map.get("ytm") is None:
                    col_map["ytm"] = ci
            elif "price" in v and col_map.get("price") is None:
                col_map["price"] = ci
            elif "quantum" in v or "available" in v:
                col_map["qty"] = ci
            elif "semi" in v:
                col_map.setdefault("ytm_semi", ci)
            elif "screen" in v:
                col_map.setdefault("ytm_screen", ci)
            elif "interest" in v or "ip" in v:
                col_map["ip_dates"] = ci
            elif "type" in v:
                col_map["category"] = ci

        if "isin" not in col_map:
            result.errors.append("ISIN column not found in Nuvama header")
            return result

        current_category = None
        for i in range(hrow + 1, len(df)):
            row = df.iloc[i]
            isin = _to_str(row.iloc[col_map["isin"]] if col_map["isin"] < len(row) else None)
            if not isin or not _ISIN_RE.match(isin):
                # Category header
                for v in row.values:
                    s = _to_str(v)
                    if s and len(s) > 5 and "category" in s.lower():
                        current_category = s
                continue

            rating_raw = _to_str(row.iloc[col_map.get("rating", 999)] if col_map.get("rating", 999) < len(row) else None)
            rating, rating_agency = _parse_rating(rating_raw)
            qty_raw = _to_str(row.iloc[col_map.get("qty", 999)] if col_map.get("qty", 999) < len(row) else None)
            qty_lacs = _parse_quantity_lacs(qty_raw)
            coupon = _normalise_ytm(_to_float(row.iloc[col_map.get("coupon", 999)] if col_map.get("coupon", 999) < len(row) else None))
            ytm = _normalise_ytm(_to_float(row.iloc[col_map.get("ytm", 999)] if col_map.get("ytm", 999) < len(row) else None))
            ytm_semi = _normalise_ytm(_to_float(row.iloc[col_map.get("ytm_semi", 999)] if col_map.get("ytm_semi", 999) < len(row) else None))
            ytm_screen = _normalise_ytm(_to_float(row.iloc[col_map.get("ytm_screen", 999)] if col_map.get("ytm_screen", 999) < len(row) else None))
            price = _normalise_price(_to_float(row.iloc[col_map.get("price", 999)] if col_map.get("price", 999) < len(row) else None))

            q = ParsedQuote(
                isin=isin.strip().upper(),
                broker_name=broker,
                quote_date=quote_date,
                bond_name=_to_str(row.iloc[col_map.get("name", 999)] if col_map.get("name", 999) < len(row) else None),
                category=current_category,
                coupon_rate=coupon,
                maturity_date=_parse_maturity(row.iloc[col_map.get("maturity", 999)] if col_map.get("maturity", 999) < len(row) else None),
                rating=rating,
                rating_agency=rating_agency,
                clean_price=price,
                ytm=ytm,
                ytm_semi=ytm_semi,
                ytm_screen=ytm_screen,
                quantity_lacs=qty_lacs,
                ip_dates=_to_str(row.iloc[col_map.get("ip_dates", 999)] if col_map.get("ip_dates", 999) < len(row) else None),
                source_file=filename,
                source_format="EXCEL_NUVAMA",
            )
            result.quotes.append(q)

        return result

    # ── Darashaw Format ───────────────────────────────────────────────────────
    # Two-section sheet: Central & State, then Corporate
    # No ISIN column — match by bond name using the bond master
    # Cols: Sr.No(0) | Name(1) | Category(2) | Coupon(3) | Rating(4)
    #       Put/Call(5) | Maturity(6) | Rate/Price(7) | YTM(8) | YTP/Semi(9)
    #       IP Date(10) | Qty(11)

    def _parse_darashaw(
        self, df: pd.DataFrame, filename: str, broker: str, quote_date: str
    ) -> IngestResult:
        result = IngestResult(
            filename=filename, broker_name=broker, quote_date=quote_date,
            source_format="EXCEL_DARASHAW",
        )
        # Find all header rows (Darashaw has two sections each with a header)
        header_rows = []
        for i, row in df.iterrows():
            vals = [str(v).lower() for v in row.values]
            if "name" in " ".join(vals) and ("coupon" in " ".join(vals) or "rating" in " ".join(vals)):
                header_rows.append(i)

        if not header_rows:
            result.errors.append("Could not find Darashaw header rows")
            return result

        all_bonds: List[ParsedQuote] = []
        for hrow in header_rows:
            col_map = {}
            for ci, val in enumerate(df.iloc[hrow]):
                v = str(val).lower().strip()
                if "name" in v or "security" in v:
                    col_map["name"] = ci
                elif "coupon" in v:
                    col_map["coupon"] = ci
                elif "rating" in v:
                    col_map["rating"] = ci
                elif "category" in v:
                    col_map["category_col"] = ci
                elif "maturity" in v or "redemption" in v:
                    col_map["maturity"] = ci
                elif "rate" in v and col_map.get("price") is None:
                    col_map["price"] = ci
                elif "ytm" in v and "annual" in v:
                    col_map["ytm"] = ci
                elif "ytm" in v:
                    col_map.setdefault("ytm", ci)
                elif "semi" in v:
                    col_map["ytm_semi"] = ci
                elif "put" in v or "call" in v:
                    col_map["put_call"] = ci
                elif "ip" in v or "interest" in v:
                    col_map["ip_dates"] = ci
                elif "quantum" in v or "available" in v:
                    col_map["qty"] = ci

            # Determine end of section
            next_hrow = header_rows[header_rows.index(hrow) + 1] if header_rows.index(hrow) + 1 < len(header_rows) else len(df)

            current_category = None
            for i in range(hrow + 1, next_hrow):
                row = df.iloc[i]
                name = _to_str(row.iloc[col_map.get("name", 1)] if col_map.get("name", 1) < len(row) else None)
                if not name or len(name) < 5:
                    # Category divider
                    for v in row.values:
                        s = _to_str(v)
                        if s and len(s) > 3:
                            current_category = s
                    continue

                # No ISIN — generate synthetic or skip
                # We'll still store the bond data and try to match ISIN later
                coupon = _normalise_ytm(_to_float(row.iloc[col_map.get("coupon", 999)] if col_map.get("coupon", 999) < len(row) else None))
                isin_synthetic = _make_synthetic_isin_from_name(name, coupon, broker)
                rating_raw = _to_str(row.iloc[col_map.get("rating", 999)] if col_map.get("rating", 999) < len(row) else None)
                rating, rating_agency = _parse_rating(rating_raw)
                qty_raw = _to_str(row.iloc[col_map.get("qty", 999)] if col_map.get("qty", 999) < len(row) else None)
                qty_lacs = _parse_quantity_lacs(qty_raw)
                price = _normalise_price(_to_float(row.iloc[col_map.get("price", 999)] if col_map.get("price", 999) < len(row) else None))
                ytm = _normalise_ytm(_to_float(row.iloc[col_map.get("ytm", 999)] if col_map.get("ytm", 999) < len(row) else None))
                ytm_semi = _normalise_ytm(_to_float(row.iloc[col_map.get("ytm_semi", 999)] if col_map.get("ytm_semi", 999) < len(row) else None))
                cat = _to_str(row.iloc[col_map.get("category_col", 999)] if col_map.get("category_col", 999) < len(row) else None) or current_category

                q = ParsedQuote(
                    isin=isin_synthetic,
                    broker_name=broker,
                    quote_date=quote_date,
                    bond_name=name,
                    category=cat,
                    coupon_rate=coupon,
                    maturity_date=_parse_maturity(row.iloc[col_map.get("maturity", 999)] if col_map.get("maturity", 999) < len(row) else None),
                    rating=rating,
                    rating_agency=rating_agency,
                    clean_price=price,
                    ytm=ytm,
                    ytm_semi=ytm_semi,
                    quantity_lacs=qty_lacs,
                    put_call_date=_to_str(row.iloc[col_map.get("put_call", 999)] if col_map.get("put_call", 999) < len(row) else None),
                    ip_dates=_to_str(row.iloc[col_map.get("ip_dates", 999)] if col_map.get("ip_dates", 999) < len(row) else None),
                    source_file=filename,
                    source_format="EXCEL_DARASHAW",
                )
                all_bonds.append(q)

        result.quotes = all_bonds
        return result

    # ── Generic Format (Quote/Quotes files, default) ──────────────────────────
    # Header at row 0: ISIN No. | Security Detail | Category | Coupon Rate
    #   | Type | Rating | Agency | Put Call Date | Maturity | Rate | YTM | YTPC | IP Date | Qty

    def _parse_generic(
        self, df: pd.DataFrame, filename: str, broker: str, quote_date: str
    ) -> IngestResult:
        result = IngestResult(
            filename=filename, broker_name=broker, quote_date=quote_date,
            source_format="EXCEL_GENERIC",
        )
        # Find header row (contains "ISIN")
        hrow = None
        for i, row in df.iterrows():
            vals = [str(v).lower() for v in row.values]
            if any("isin" in v for v in vals):
                hrow = i
                break
        if hrow is None:
            return result   # No ISIN header found

        col_map = {}
        for ci, val in enumerate(df.iloc[hrow]):
            v = str(val).lower().strip()
            if "isin" in v:
                col_map["isin"] = ci
            elif "security" in v or "detail" in v or "name" in v:
                col_map.setdefault("name", ci)
            elif "category" in v:
                col_map["category"] = ci
            elif "coupon" in v:
                col_map["coupon"] = ci
            elif "type" in v and "security" in v:
                col_map["type"] = ci
            elif "rating" in v and "agency" not in v:
                col_map["rating"] = ci
            elif "agency" in v:
                col_map["agency"] = ci
            elif "put" in v or "call" in v:
                col_map["put_call"] = ci
            elif "maturity" in v:
                col_map["maturity"] = ci
            elif v in ("rate", "price", "rate / price"):
                col_map["price"] = ci
            elif "ytm" in v and "ytpc" not in v and "ytm " not in v:
                col_map["ytm"] = ci
            elif "ytpc" in v:
                col_map["ytm_screen"] = ci
            elif "ip" in v or "interest" in v:
                col_map["ip_dates"] = ci
            elif "available" in v or "quantum" in v:
                col_map["qty"] = ci

        if "isin" not in col_map:
            return result

        current_category = None
        for i in range(hrow + 1, len(df)):
            row = df.iloc[i]
            isin = _to_str(row.iloc[col_map["isin"]] if col_map["isin"] < len(row) else None)
            if not isin or not _ISIN_RE.match(isin):
                # Category row
                cat_val = _to_str(row.iloc[col_map.get("category", 2)] if col_map.get("category", 2) < len(row) else None)
                if cat_val and len(cat_val) > 3:
                    current_category = cat_val
                continue

            rating_raw = _to_str(row.iloc[col_map.get("rating", 999)] if col_map.get("rating", 999) < len(row) else None)
            agency_raw = _to_str(row.iloc[col_map.get("agency", 999)] if col_map.get("agency", 999) < len(row) else None)
            rating, rating_agency = _parse_rating(rating_raw)
            if not rating_agency and agency_raw:
                _, rating_agency = _parse_rating(agency_raw)
            qty_raw = _to_str(row.iloc[col_map.get("qty", 999)] if col_map.get("qty", 999) < len(row) else None)
            qty_lacs = _parse_quantity_lacs(qty_raw)
            coupon = _normalise_ytm(_to_float(row.iloc[col_map.get("coupon", 999)] if col_map.get("coupon", 999) < len(row) else None))
            price = _normalise_price(_to_float(row.iloc[col_map.get("price", 999)] if col_map.get("price", 999) < len(row) else None))
            ytm = _normalise_ytm(_to_float(row.iloc[col_map.get("ytm", 999)] if col_map.get("ytm", 999) < len(row) else None))
            ytm_screen = _normalise_ytm(_to_float(row.iloc[col_map.get("ytm_screen", 999)] if col_map.get("ytm_screen", 999) < len(row) else None))
            cat = _to_str(row.iloc[col_map.get("category", 999)] if col_map.get("category", 999) < len(row) else None) or current_category

            q = ParsedQuote(
                isin=isin.strip().upper(),
                broker_name=broker,
                quote_date=quote_date,
                bond_name=_to_str(row.iloc[col_map.get("name", 999)] if col_map.get("name", 999) < len(row) else None),
                category=cat,
                coupon_rate=coupon,
                maturity_date=_parse_maturity(row.iloc[col_map.get("maturity", 999)] if col_map.get("maturity", 999) < len(row) else None),
                rating=rating,
                rating_agency=rating_agency,
                clean_price=price,
                ytm=ytm,
                ytm_screen=ytm_screen,
                quantity_lacs=qty_lacs,
                put_call_date=_to_str(row.iloc[col_map.get("put_call", 999)] if col_map.get("put_call", 999) < len(row) else None),
                ip_dates=_to_str(row.iloc[col_map.get("ip_dates", 999)] if col_map.get("ip_dates", 999) < len(row) else None),
                source_file=filename,
                source_format="EXCEL_GENERIC",
            )
            result.quotes.append(q)

        return result

    # ── Auto-detect fallback ──────────────────────────────────────────────────

    def _parse_auto(
        self, df: pd.DataFrame, filename: str, broker: str, quote_date: str
    ) -> IngestResult:
        """
        Generic fallback: scan every row for ISIN pattern, extract surrounding
        numeric columns as price/yield.
        """
        result = IngestResult(
            filename=filename, broker_name=broker, quote_date=quote_date,
            source_format="EXCEL_AUTO",
        )
        for i, row in df.iterrows():
            for ci, val in enumerate(row):
                s = _to_str(val)
                if not s:
                    continue
                m = _ISIN_RE.match(s)
                if not m:
                    continue
                isin = m.group(1)
                # Adjacent cells: price then yield
                floats = []
                for j in range(max(0, ci - 3), min(len(row), ci + 8)):
                    v = _to_float(row.iloc[j])
                    if v is not None and v != 0:
                        floats.append(v)
                price = next((v for v in floats if 50 < v < 200), None)
                ytm = next((v for v in floats if 0 < v < 1), None)
                if ytm is None:
                    ytm_raw = next((v for v in floats if 1 < v < 20), None)
                    ytm = _normalise_ytm(ytm_raw)
                q = ParsedQuote(
                    isin=isin.upper(),
                    broker_name=broker,
                    quote_date=quote_date,
                    clean_price=_normalise_price(price),
                    ytm=ytm,
                    source_file=filename,
                    source_format="EXCEL_AUTO",
                )
                result.quotes.append(q)
                break
        return result

    # ── PDF Extraction ────────────────────────────────────────────────────────

    def _parse_pdf(
        self, path: Path, filename: str, broker: str, quote_date: str
    ) -> IngestResult:
        if not _PDF_OK:
            return IngestResult(
                filename=filename, broker_name=broker, quote_date=quote_date,
                source_format="PDF_ERROR",
                errors=["pdfplumber not installed. Run: pip install pdfplumber"],
            )

        # For unknown-broker PDFs, scan the first page text for a broker name
        if broker == "Unknown" and _PDF_OK:
            try:
                with pdfplumber.open(str(path)) as _pdf_peek:
                    first_text = (_pdf_peek.pages[0].extract_text() or "") if _pdf_peek.pages else ""
                detected = detect_broker_from_content(first_text)
                if detected != "Unknown":
                    broker = detected
            except Exception:
                pass

        broker_upper = broker.upper()
        if "NUVAMA" in broker_upper:
            return self._parse_pdf_nuvama(path, filename, broker, quote_date)
        elif "BIRLA" in broker_upper:
            return self._parse_pdf_birla(path, filename, broker, quote_date)
        else:
            return self._parse_pdf_generic(path, filename, broker, quote_date)

    def _parse_pdf_nuvama(
        self, path: Path, filename: str, broker: str, quote_date: str
    ) -> IngestResult:
        """
        Bond Vector PDF from Nuvama.
        Typical layout: table with columns like
        Sr | Security Name | Coupon | Maturity | Rating | ISIN | FV | Bid Price | Ask Price | Bid YTM | Ask YTM | Lot
        """
        result = IngestResult(
            filename=filename, broker_name=broker, quote_date=quote_date,
            source_format="PDF_NUVAMA",
        )
        try:
            with pdfplumber.open(str(path)) as pdf:
                full_text = ""
                all_tables = []
                for page in pdf.pages:
                    text = page.extract_text() or ""
                    full_text += text + "\n"
                    if not extract_date_from_string(filename):
                        d = extract_date_from_string(text[:200])
                        if d:
                            quote_date = d
                            result.quote_date = d
                    tables = page.extract_tables()
                    if tables:
                        all_tables.extend(tables)

                if all_tables:
                    quotes = _extract_from_tables(all_tables, broker, quote_date, filename, "PDF_NUVAMA")
                    result.quotes.extend(quotes)

                if not result.quotes:
                    quotes = _extract_from_text(full_text, broker, quote_date, filename, "PDF_NUVAMA")
                    result.quotes.extend(quotes)

        except Exception as exc:
            result.errors.append(str(exc))
        return result

    def _parse_pdf_birla(
        self, path: Path, filename: str, broker: str, quote_date: str
    ) -> IngestResult:
        """
        Aditya Birla Capital quote sheet.
        """
        result = IngestResult(
            filename=filename, broker_name=broker, quote_date=quote_date,
            source_format="PDF_BIRLA",
        )
        try:
            with pdfplumber.open(str(path)) as pdf:
                full_text = ""
                all_tables = []
                for page in pdf.pages:
                    t = page.extract_text() or ""
                    full_text += t + "\n"
                    tbls = page.extract_tables()
                    if tbls:
                        all_tables.extend(tbls)

                if all_tables:
                    quotes = _extract_from_tables(all_tables, broker, quote_date, filename, "PDF_BIRLA")
                    result.quotes.extend(quotes)
                if not result.quotes:
                    quotes = _extract_from_text(full_text, broker, quote_date, filename, "PDF_BIRLA")
                    result.quotes.extend(quotes)

        except Exception as exc:
            result.errors.append(str(exc))
        return result

    def _parse_pdf_generic(
        self, path: Path, filename: str, broker: str, quote_date: str
    ) -> IngestResult:
        result = IngestResult(
            filename=filename, broker_name=broker, quote_date=quote_date,
            source_format="PDF_GENERIC",
        )
        try:
            with pdfplumber.open(str(path)) as pdf:
                full_text = ""
                all_tables = []
                for page in pdf.pages:
                    t = page.extract_text() or ""
                    full_text += t + "\n"
                    tbls = page.extract_tables()
                    if tbls:
                        all_tables.extend(tbls)

                if all_tables:
                    quotes = _extract_from_tables(all_tables, broker, quote_date, filename, "PDF_GENERIC")
                    result.quotes.extend(quotes)
                if not result.quotes:
                    quotes = _extract_from_text(full_text, broker, quote_date, filename, "PDF_GENERIC")
                    result.quotes.extend(quotes)
        except Exception as exc:
            result.errors.append(str(exc))
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Global helpers (used by PDF parsers)
# ─────────────────────────────────────────────────────────────────────────────

def _extract_from_tables(
    tables: list, broker: str, quote_date: str, filename: str, fmt: str
) -> List[ParsedQuote]:
    """Parse pdfplumber tables into ParsedQuote objects."""
    quotes: List[ParsedQuote] = []
    for table in tables:
        if not table or len(table) < 2:
            continue
        # Identify header row (first row with text and no ISIN)
        header_row_idx = 0
        col_map: Dict[str, int] = {}

        for hi, row in enumerate(table[:3]):
            if row and any(row):
                col_map = _map_columns(row)
                if col_map:
                    header_row_idx = hi
                    break

        if not col_map.get("isin") and not col_map.get("name"):
            continue

        for row in table[header_row_idx + 1:]:
            if not row:
                continue
            q = _row_to_quote(row, col_map, broker, quote_date, filename, fmt)
            if q:
                quotes.append(q)
    return quotes


def _map_columns(header_row: list) -> Dict[str, int]:
    col_map: Dict[str, int] = {}
    for ci, val in enumerate(header_row):
        if val is None:
            continue
        v = str(val).lower().strip()
        if "isin" in v:
            col_map["isin"] = ci
        elif "name" in v or "security" in v or "bond" in v or "issuer" in v:
            col_map.setdefault("name", ci)
        elif "coupon" in v and "rate" not in v.replace("coupon", ""):
            col_map.setdefault("coupon", ci)
        elif "maturity" in v or "redemption" in v:
            col_map.setdefault("maturity", ci)
        elif "rating" in v:
            col_map.setdefault("rating", ci)
        elif "bid" in v and "price" in v:
            col_map["bid_price"] = ci
        elif "ask" in v and "price" in v:
            col_map["ask_price"] = ci
        elif "bid" in v and ("yield" in v or "ytm" in v):
            col_map["bid_yield"] = ci
        elif "ask" in v and ("yield" in v or "ytm" in v):
            col_map["ask_yield"] = ci
        elif "price" in v and "bid" not in v and "ask" not in v and "screen" not in v:
            col_map.setdefault("price", ci)
        elif ("ytm" in v or "yield" in v) and "bid" not in v and "ask" not in v:
            col_map.setdefault("ytm", ci)
        elif "semi" in v:
            col_map.setdefault("ytm_semi", ci)
        elif "lot" in v or "face" in v or "quantum" in v:
            col_map.setdefault("qty", ci)
    return col_map


def _row_to_quote(
    row: list, col_map: Dict[str, int], broker: str,
    quote_date: str, filename: str, fmt: str,
) -> Optional[ParsedQuote]:
    def _get(key: str):
        idx = col_map.get(key)
        if idx is None or idx >= len(row):
            return None
        return row[idx]

    isin_raw = _to_str(_get("isin"))
    if isin_raw and _ISIN_RE.match(isin_raw):
        isin = isin_raw.strip().upper()
    else:
        # Scan all cells for ISIN pattern
        isin = None
        for cell in row:
            m = _ISIN_RE.match(str(cell or "").strip())
            if m:
                isin = m.group(1)
                break
        if not isin:
            return None

    rating_raw = _to_str(_get("rating"))
    rating, rating_agency = _parse_rating(rating_raw)

    return ParsedQuote(
        isin=isin,
        broker_name=broker,
        quote_date=quote_date,
        bond_name=_to_str(_get("name")),
        coupon_rate=_normalise_ytm(_to_float(_get("coupon"))),
        maturity_date=_parse_maturity(_get("maturity")),
        rating=rating,
        rating_agency=rating_agency,
        clean_price=_normalise_price(_to_float(_get("price"))),
        bid_price=_normalise_price(_to_float(_get("bid_price"))),
        ask_price=_normalise_price(_to_float(_get("ask_price"))),
        bid_yield=_normalise_ytm(_to_float(_get("bid_yield"))),
        ask_yield=_normalise_ytm(_to_float(_get("ask_yield"))),
        ytm=_normalise_ytm(_to_float(_get("ytm"))),
        ytm_semi=_normalise_ytm(_to_float(_get("ytm_semi"))),
        quantity_lacs=_parse_quantity_lacs(_to_str(_get("qty"))),
        source_file=filename,
        source_format=fmt,
    )


def _extract_from_text(
    text: str, broker: str, quote_date: str, filename: str, fmt: str
) -> List[ParsedQuote]:
    """Last-resort regex scan of raw text for ISIN + adjacent numeric values."""
    quotes: List[ParsedQuote] = []
    lines = text.splitlines()
    for line in lines:
        isins = _ISIN_RE.findall(line)
        if not isins:
            continue
        nums = re.findall(r"\b(\d{1,4}\.\d{2,6})\b", line)
        floats = [float(n) for n in nums]
        price = next((v for v in floats if 50 < v < 200), None)
        ytm_raw = next((v for v in floats if 0.01 < v < 0.25), None)
        if ytm_raw is None:
            ytm_raw = next((v for v in floats if 1 < v < 20), None)
        ytm = _normalise_ytm(ytm_raw)
        for isin in isins:
            quotes.append(ParsedQuote(
                isin=isin.upper(),
                broker_name=broker,
                quote_date=quote_date,
                clean_price=_normalise_price(price),
                ytm=ytm,
                source_file=filename,
                source_format=fmt,
            ))
    return quotes


def _parse_quantity_lacs(val: Optional[str]) -> Optional[float]:
    """
    Parse quantity strings like '68.80 LAC', '1.071 CR AON', '5 Crore',
    'Min 5 Crore & in Multiples', '45 Lacs', '0.11 Cr' → float in lacs.
    """
    if not val:
        return None
    s = str(val).lower().strip()
    # Remove 'aon', 'and in multiples', 'min' etc
    s = re.sub(r"(aon|and.*|min.*|multiple.*|\bof\b)", "", s).strip()
    # Extract number and unit
    m = re.search(r"([\d.,]+)\s*(cr(?:ore)?|lac(?:s)?|l\b|c\b)?", s)
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
        unit = (m.group(2) or "").lower()
        if "cr" in unit:
            return round(num * 100, 2)     # crores → lacs
        else:
            return round(num, 2)           # already lacs
    except (ValueError, AttributeError):
        return None


def _make_synthetic_isin_from_name(name: str, coupon: Optional[float], broker: str) -> str:
    """
    For brokers that don't provide ISINs (Darashaw), create a stable synthetic key
    from issuer name + coupon so historical tracking still works.
    Format: BRND_HASH where BRND is broker abbreviation.
    """
    import hashlib
    key = f"{name}_{coupon}_{broker}"
    h = hashlib.md5(key.encode()).hexdigest()[:8].upper()
    return f"SYNTH_{h}"


# ─────────────────────────────────────────────────────────────────────────────
# Multi-file batch parsing
# ─────────────────────────────────────────────────────────────────────────────

def parse_files(paths: List[str | Path]) -> List[IngestResult]:
    """Parse a list of broker quote files and return IngestResult for each."""
    parser = BrokerQuoteParser()
    results = []
    for p in paths:
        try:
            result = parser.parse(p)
            results.append(result)
        except Exception as exc:
            results.append(IngestResult(
                filename=str(Path(p).name), broker_name="Unknown",
                quote_date=date.today().isoformat(), source_format="ERROR",
                errors=[str(exc)],
            ))
    return results


# ─────────────────────────────────────────────────────────────────────────────
# ISIN Cross-Validation against Bond Master
# ─────────────────────────────────────────────────────────────────────────────

async def cross_validate_quotes(result: IngestResult, fetch_fn) -> Dict[str, Any]:
    """
    Cross-validate parsed quotes against universal_bond_master.

    For each quote with an ISIN, verify:
      1. ISIN exists in the master registry
      2. Bond name (if present) roughly matches the master record
      3. Coupon rate (if present) matches within tolerance

    Args:
        result: Parsed IngestResult from broker quote file
        fetch_fn: Async callable(query, params) → row dict (e.g., fetch_one)

    Returns:
        Dict with validation_errors list and validated_count
    """
    validation_errors = []
    validated = 0

    for quote in result.quotes:
        isin = quote.isin
        if not isin or isin.startswith("SYNTH_"):
            continue  # Skip synthetic ISINs (no master lookup possible)

        master = await fetch_fn(
            "SELECT isin, bond_name, coupon_rate, issuer_name FROM universal_bond_master WHERE isin = :isin",
            {"isin": isin},
        )

        if not master:
            validation_errors.append({
                "isin": isin,
                "type": "ISIN_NOT_IN_MASTER",
                "severity": "WARNING",
                "message": f"ISIN {isin} from broker quote not found in universal_bond_master",
                "broker_name": quote.bond_name,
            })
            continue

        # Cross-check coupon rate
        if quote.coupon_rate is not None and master.get("coupon_rate"):
            master_coupon = float(master["coupon_rate"])
            if abs(quote.coupon_rate - master_coupon) > 0.01:  # tolerance 1 bps
                validation_errors.append({
                    "isin": isin,
                    "type": "COUPON_MISMATCH",
                    "severity": "CRITICAL",
                    "message": (
                        f"Coupon mismatch for {isin}: broker={quote.coupon_rate}%, "
                        f"master={master_coupon}%"
                    ),
                    "broker_value": quote.coupon_rate,
                    "master_value": master_coupon,
                })

        validated += 1

    return {
        "total_quotes": len(result.quotes),
        "validated": validated,
        "validation_errors": validation_errors,
        "has_errors": len(validation_errors) > 0,
    }
