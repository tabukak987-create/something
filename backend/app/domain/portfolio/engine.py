from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import math
from typing import Any, Dict, List, Optional

from app.shared.database import IS_SQLITE, execute_query, fetch_all
from app.domain.market_data.instrument_classification import INSTRUMENT_TYPE_SDS, classify_instrument, portfolio_category_from_record
from app.domain.portfolio.institutional_financial_core import solve_ytm_percent
from app.domain.core_trust.data_governance import confidence_for_record, record_calculation_trace, validate_source_context


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str):
            value = value.strip().replace(",", "")
            if value == "":
                return default
        return float(value)
    except Exception:
        return default


def _to_date(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw[:10], fmt).date()
        except ValueError:
            continue
    return None


def _year_fraction(start: date, end: date, convention: str) -> float:
    if end <= start:
        return 0.0
    conv = (convention or "ACT/365").upper().replace(" ", "")
    days = (end - start).days
    if conv in {"30/360", "30E/360"}:
        return days / 360.0
    return days / 365.0


def _coupon_dates(maturity: date, frequency: int, as_of: date) -> tuple[Optional[date], Optional[date]]:
    if maturity <= as_of:
        return maturity, maturity
    months = max(1, int(12 / max(1, frequency)))
    d = maturity
    prev_d = None
    while d > as_of:
        prev_d = d
        year = d.year
        month = d.month - months
        while month <= 0:
            month += 12
            year -= 1
        day = min(d.day, 28)
        d = date(year, month, day)
    return d, prev_d


def _accrued_interest(coupon_rate: float, notional: float, maturity: Optional[date], frequency: int, convention: str, as_of: date) -> float:
    if not maturity or maturity <= as_of or frequency <= 0:
        return 0.0
    last_coupon, next_coupon = _coupon_dates(maturity, frequency, as_of)
    if not last_coupon or not next_coupon or next_coupon <= last_coupon:
        return 0.0
    period_coupon = (coupon_rate / 100.0) * notional / frequency
    frac = _year_fraction(last_coupon, as_of, convention) / max(_year_fraction(last_coupon, next_coupon, convention), 1e-9)
    return max(0.0, period_coupon * frac)


def _solve_ytm(coupon_rate: float, price_per_100: float, maturity: Optional[date], frequency: int, as_of: date) -> Optional[float]:
    if not maturity or maturity <= as_of or price_per_100 <= 0:
        return None
    years = (maturity - as_of).days / 365.25
    n = max(1, int(round(years * max(1, frequency))))
    c = coupon_rate / 100.0 * 100.0 / max(1, frequency)

    def f(y: float) -> float:
        r = y / max(1, frequency)
        if r <= -0.99:
            return 1e9
        pv = 0.0
        for t in range(1, n + 1):
            pv += c / ((1 + r) ** t)
        pv += 100.0 / ((1 + r) ** n)
        return pv - price_per_100

    lo, hi = -0.5, 1.5
    flo, fhi = f(lo), f(hi)
    if flo * fhi > 0:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        fmid = f(mid)
        if abs(fmid) < 1e-9:
            break
        if flo * fmid <= 0:
            hi = mid
            fhi = fmid
        else:
            lo = mid
            flo = fmid
    return round(((lo + hi) / 2) * 100.0, 6)


def _normalize_price_per_100(price: float, face_value: float, quantity: float) -> float:
    """Convert a holding-level rupee price to a per-100 basis when needed."""
    if price <= 0:
        return 0.0
    unit_face_value = face_value / quantity if quantity > 0 else face_value
    if unit_face_value > 0:
        return round((price / unit_face_value) * 100.0, 6)
    return round(price, 6)


async def _table_has_column(table_name: str, column_name: str) -> bool:
    if IS_SQLITE:
        rows = await fetch_all(f"PRAGMA table_info({table_name})")
        return any(str(row.get("name") or "").lower() == column_name.lower() for row in rows)

    rows = await fetch_all(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = :table_name
          AND column_name = :column_name
        LIMIT 1
        """,
        {"table_name": table_name, "column_name": column_name},
    )
    return bool(rows)


def _duration_metrics(coupon_rate: float, ytm: float, maturity: Optional[date], frequency: int, as_of: date) -> tuple[float, float]:
    if not maturity or maturity <= as_of:
        return 0.0, 0.0
    years = (maturity - as_of).days / 365.25
    n = max(1, int(round(years * max(1, frequency))))
    r = (ytm / 100.0) / max(1, frequency)
    c = coupon_rate / 100.0 * 100.0 / max(1, frequency)
    pv_total = 0.0
    mac_num = 0.0
    for t in range(1, n + 1):
        cf = c
        if t == n:
            cf += 100.0
        pv = cf / ((1 + r) ** t)
        pv_total += pv
        mac_num += (t / max(1, frequency)) * pv
    if pv_total <= 0:
        return 0.0, 0.0
    macaulay = mac_num / pv_total
    modified = macaulay / (1 + r)
    return round(macaulay, 6), round(modified, 6)


def _aggregate_lot_positions(holdings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    status_rank = {"HEALTHY": 0, "MONITOR": 1, "RISK": 2}
    confidence_rank = {"low": 0, "medium": 1, "high": 2}

    def _sort_key(lot: dict[str, Any]) -> tuple[str, int, int]:
        return (
            str(lot.get("purchase_date") or ""),
            int(lot.get("source_row_number") or 0),
            int(lot.get("holding_id") or 0),
        )

    groups: dict[str, dict[str, Any]] = {}
    for lot in holdings:
        isin = str(lot.get("isin") or "").strip().upper()
        if not isin:
            continue

        market_value = _to_float(lot.get("market_value") or lot.get("current_value") or 0.0)
        investment_value = _to_float(lot.get("investment_value") or lot.get("book_value") or 0.0)
        clean_price = _to_float(lot.get("clean_price") or lot.get("current_price") or lot.get("market_price") or 0.0)
        dirty_price = _to_float(lot.get("dirty_price") or 0.0)
        accrued_interest = _to_float(lot.get("accrued_interest") or 0.0)
        if dirty_price <= 0 and clean_price > 0:
            dirty_price = clean_price

        group = groups.setdefault(
            isin,
            {
                "isin": isin,
                "lot_count": 0,
                "lot_details": [],
                "quantity": 0.0,
                "face_value": 0.0,
                "face_value_total": 0.0,
                "book_value": 0.0,
                "market_value": 0.0,
                "current_value": 0.0,
                "clean_value": 0.0,
                "investment_value": 0.0,
                "accrued_interest": 0.0,
                "clean_price_weighted": 0.0,
                "dirty_price_weighted": 0.0,
                "ytm_weighted": 0.0,
                "modified_duration_weighted": 0.0,
                "macaulay_duration_weighted": 0.0,
                "dv01_total": 0.0,
                "spread_weighted": 0.0,
                "years_to_maturity_weighted": 0.0,
                "_weight_total": 0.0,
                "_status_rank": 0,
                "_confidence_rank": 2,
            },
        )

        lot_copy = dict(lot)
        lot_copy["unique_lot_id"] = lot_copy.get("unique_lot_id") or lot_copy.get("holding_id") or lot_copy.get("holding_key")
        lot_copy["purchase_date"] = lot_copy.get("purchase_date") or lot.get("issue_date") or lot.get("first_purchase_date")
        lot_copy["market_value"] = round(market_value, 6)
        lot_copy["current_value"] = round(market_value, 6)
        lot_copy["book_value"] = round(investment_value, 6)
        lot_copy["investment_value"] = round(investment_value, 6)
        lot_copy["clean_price"] = round(clean_price, 6)
        lot_copy["dirty_price"] = round(dirty_price, 6)
        lot_copy["accrued_interest"] = round(accrued_interest, 6)
        lot_copy["ytm"] = round(_to_float(lot_copy.get("ytm")), 6)
        lot_copy["modified_duration"] = round(_to_float(lot_copy.get("modified_duration")), 6)
        lot_copy["macaulay_duration"] = round(_to_float(lot_copy.get("macaulay_duration")), 6)

        group["lot_count"] += 1
        group["quantity"] += _to_float(lot.get("quantity"))
        group["face_value"] += _to_float(lot.get("face_value"))
        group["face_value_total"] += _to_float(lot.get("face_value_total") or lot.get("face_value"))
        group["book_value"] += investment_value
        group["market_value"] += market_value
        group["current_value"] += market_value
        group["clean_value"] += _to_float(lot.get("clean_value") or market_value)
        group["investment_value"] += investment_value
        group["accrued_interest"] += accrued_interest
        group["clean_price_weighted"] += clean_price * market_value
        group["dirty_price_weighted"] += dirty_price * market_value
        group["ytm_weighted"] += _to_float(lot.get("ytm")) * market_value
        group["modified_duration_weighted"] += _to_float(lot.get("modified_duration")) * market_value
        group["macaulay_duration_weighted"] += _to_float(lot.get("macaulay_duration")) * market_value
        group["dv01_total"] += _to_float(lot.get("dv01"))
        group["spread_weighted"] += _to_float(lot.get("spread_over_gsec_bps")) * market_value
        group["years_to_maturity_weighted"] += _to_float(lot.get("years_to_maturity")) * market_value
        group["_weight_total"] += market_value
        group["_status_rank"] = max(group["_status_rank"], status_rank.get(str(lot.get("status") or "HEALTHY").upper(), 0))
        group["_confidence_rank"] = min(group["_confidence_rank"], confidence_rank.get(str(lot.get("confidence_level") or "low").lower(), 0))
        group["lot_details"].append(lot_copy)

    aggregated: list[dict[str, Any]] = []
    for isin, group in groups.items():
        weight = group["_weight_total"] or group["market_value"] or group["investment_value"] or 1.0
        lots = sorted(group["lot_details"], key=_sort_key)
        group["lot_details"] = lots
        group["holding_key"] = group.get("holding_key") or lots[0].get("holding_key") or lots[0].get("unique_lot_id") or isin
        group["clean_price"] = round(group["clean_price_weighted"] / weight, 6) if weight else 0.0
        group["dirty_price"] = round(group["dirty_price_weighted"] / weight, 6) if weight else 0.0
        group["ytm"] = round(group["ytm_weighted"] / weight, 6) if weight else 0.0
        group["modified_duration"] = round(group["modified_duration_weighted"] / weight, 6) if weight else 0.0
        group["macaulay_duration"] = round(group["macaulay_duration_weighted"] / weight, 6) if weight else 0.0
        group["spread_over_gsec_bps"] = round(group["spread_weighted"] / weight, 6) if weight else 0.0
        group["years_to_maturity"] = round(group["years_to_maturity_weighted"] / weight, 6) if weight else 0.0
        group["current_price"] = group["clean_price"]
        group["status"] = "RISK" if group["_status_rank"] >= 2 else ("MONITOR" if group["_status_rank"] >= 1 else "HEALTHY")
        group["confidence_level"] = "high" if group["_confidence_rank"] >= 2 else ("medium" if group["_confidence_rank"] >= 1 else "low")
        group["source_name"] = lots[0].get("source_name") or group.get("source_name") or "—"
        group["source_type"] = lots[0].get("source_type") or group.get("source_type") or "unknown"
        group["source_reference"] = " · ".join([str(lot.get("source_reference") or "").strip() for lot in lots if lot.get("source_reference")]) or group.get("source_reference") or ""
        group["source_row_number"] = lots[0].get("source_row_number") or group.get("source_row_number")
        aggregated.append(group)

    return sorted(aggregated, key=lambda row: (-(row.get("market_value") or 0.0), str(row.get("isin") or "")))


_RATING_SCORE = {
    "SOVEREIGN": 1.0,
    "SOV": 1.0,
    "AAA": 2.0,
    "AA+": 3.0,
    "AA": 4.0,
    "AA-": 5.0,
    "A+": 6.0,
    "A": 7.0,
    "A-": 8.0,
    "BBB+": 9.0,
    "BBB": 10.0,
    "BBB-": 11.0,
    "BB+": 12.0,
    "BB": 13.0,
}


def _rating_score(raw_rating: Any) -> float:
    rating = str(raw_rating or "NR").upper().strip()
    return _RATING_SCORE.get(rating, _RATING_SCORE["A"])


def _rating_label_from_score(score: float) -> str:
    if score <= 1.5:
        return "SOV"
    if score <= 2.5:
        return "AAA"
    if score <= 3.5:
        return "AA+"
    if score <= 4.5:
        return "AA"
    if score <= 5.5:
        return "AA-"
    if score <= 6.5:
        return "A+"
    if score <= 7.5:
        return "A"
    if score <= 8.5:
        return "A-"
    if score <= 9.5:
        return "BBB+"
    if score <= 10.5:
        return "BBB"
    if score <= 11.5:
        return "BBB-"
    return "NR"


async def ensure_portfolio_core_schema() -> None:
    # Raw-only core tables for portfolio trust layer.
    await execute_query(
        """
        CREATE TABLE IF NOT EXISTS instrument_master (
            isin TEXT PRIMARY KEY,
            instrument_name TEXT NOT NULL,
            issuer TEXT NOT NULL,
            coupon_rate REAL NOT NULL,
            maturity_date TEXT NOT NULL,
            put_date TEXT,
            call_date TEXT,
            sector TEXT,
            category TEXT,
            instrument_category TEXT,
            rating_latest TEXT,
            rating_agency TEXT,
            workbook_rating_1 TEXT,
            workbook_rating_2 TEXT,
            workbook_rating_agency_1 TEXT,
            workbook_rating_agency_2 TEXT,
            payment_frequency INTEGER NOT NULL DEFAULT 2,
            day_count_convention TEXT NOT NULL DEFAULT 'ACT/365',
            source_name TEXT,
            source_type TEXT,
            ingestion_method TEXT,
            source_reference TEXT,
            timestamp_ingested TEXT,
            timestamp_effective TEXT,
            provenance_json TEXT
        )
        """,
        {},
    )
    await execute_query(
        """
        CREATE TABLE IF NOT EXISTS portfolio_holdings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            portfolio_id TEXT NOT NULL,
            isin TEXT NOT NULL,
            purchase_date TEXT,
            quantity REAL NOT NULL,
            purchase_price REAL NOT NULL,
            face_value REAL NOT NULL DEFAULT 1000,
            instrument_category TEXT,
            credit_rating TEXT,
            credit_rating_2 TEXT,
            rating_agency TEXT,
            rating_agency_2 TEXT,
            source_name TEXT,
            source_type TEXT,
            ingestion_method TEXT,
            source_reference TEXT,
            source_sheet_name TEXT,
            source_row_number INTEGER,
            timestamp_ingested TEXT,
            timestamp_effective TEXT,
            provenance_json TEXT,
            FOREIGN KEY(isin) REFERENCES instrument_master(isin)
        )
        """,
        {},
    )
    try:
        if not await _table_has_column("portfolio_holdings", "face_value"): 
            await execute_query("ALTER TABLE portfolio_holdings ADD COLUMN face_value REAL NOT NULL DEFAULT 1000", {})
    except Exception:
        pass
    for column_name, column_type in (
        ("sector", "TEXT"),
        ("credit_rating", "TEXT"),
        ("credit_rating_2", "TEXT"),
        ("rating_agency", "TEXT"),
        ("rating_agency_2", "TEXT"),
        ("source_name", "TEXT"),
        ("source_type", "TEXT"),
        ("ingestion_method", "TEXT"),
        ("source_reference", "TEXT"),
        ("source_sheet_name", "TEXT"),
        ("source_row_number", "INTEGER"),
        ("timestamp_ingested", "TEXT"),
        ("timestamp_effective", "TEXT"),
        ("provenance_json", "TEXT"),
    ):
        try:
            await execute_query(f"ALTER TABLE portfolio_holdings ADD COLUMN {column_name} {column_type}", {})
        except Exception:
            pass
    await execute_query("CREATE INDEX IF NOT EXISTS idx_ph_pid_isin ON portfolio_holdings(portfolio_id, isin)", {})
    await execute_query(
        """
        CREATE TABLE IF NOT EXISTS portfolio_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            unique_lot_id TEXT,
            trust_id TEXT NOT NULL,
            portfolio_id TEXT NOT NULL,
            isin TEXT,
            category TEXT,
            instrument_category TEXT,
            instrument_name TEXT,
            coupon REAL,
            maturity TEXT,
            quantity REAL,
            face_value REAL,
            book_value REAL,
            current_value REAL,
            market_price REAL,
            clean_price REAL,
            ytm REAL,
            accrued_interest REAL,
            purchase_price REAL,
            purchase_date TEXT,
            issuer_name TEXT,
            credit_rating TEXT,
            credit_rating_2 TEXT,
            rating_agency TEXT,
            rating_agency_2 TEXT,
            sector TEXT,
            source_name TEXT,
            source_type TEXT,
            ingestion_method TEXT,
            source_reference TEXT,
            source_sheet_name TEXT,
            source_row_number INTEGER,
            timestamp_ingested TEXT,
            timestamp_effective TEXT,
            provenance_json TEXT
        )
        """,
        {},
    )
    for column_name, column_type in (
        ("unique_lot_id", "TEXT"),
        ("clean_price", "REAL"),
        ("ytm", "REAL"),
        ("current_value", "REAL"),
        ("accrued_interest", "REAL"),
        ("sector", "TEXT"),
        ("source_sheet_name", "TEXT"),
        ("source_row_number", "INTEGER"),
    ):
        if not await _table_has_column("portfolio_positions", column_name):
            await execute_query(f"ALTER TABLE portfolio_positions ADD COLUMN {column_name} {column_type}", {})
    await execute_query("CREATE INDEX IF NOT EXISTS idx_pp_trust_ref_row ON portfolio_positions(trust_id, source_reference, source_sheet_name, source_row_number)", {})
    await execute_query("CREATE INDEX IF NOT EXISTS idx_pp_trust_isin ON portfolio_positions(trust_id, isin)", {})
    await execute_query(
        """
        CREATE TABLE IF NOT EXISTS market_prices (
            isin TEXT NOT NULL,
            date TEXT NOT NULL,
            price REAL NOT NULL,
            yield REAL,
            source TEXT,
            source_name TEXT,
            source_type TEXT,
            ingestion_method TEXT,
            source_reference TEXT,
            timestamp_ingested TEXT,
            timestamp_effective TEXT,
            provenance_json TEXT,
            FOREIGN KEY(isin) REFERENCES instrument_master(isin),
            UNIQUE(isin, date, source)
        )
        """,
        {},
    )
    await execute_query("CREATE INDEX IF NOT EXISTS idx_mp_isin_date ON market_prices(isin, date DESC)", {})
    await execute_query(
        """
        CREATE TABLE IF NOT EXISTS cashflows (
            isin TEXT NOT NULL,
            payment_date TEXT NOT NULL,
            coupon_amount REAL NOT NULL,
            principal_amount REAL NOT NULL,
            source_name TEXT,
            source_type TEXT,
            ingestion_method TEXT,
            source_reference TEXT,
            timestamp_ingested TEXT,
            timestamp_effective TEXT,
            provenance_json TEXT,
            FOREIGN KEY(isin) REFERENCES instrument_master(isin),
            UNIQUE(isin, payment_date)
        )
        """,
        {},
    )
    await execute_query("CREATE INDEX IF NOT EXISTS idx_cf_isin_date ON cashflows(isin, payment_date)", {})

    # Add workbook-rating columns to existing databases without requiring a fresh schema.
    for table_name, column_name, column_type in (
        ("instrument_master", "workbook_rating_1", "TEXT"),
        ("instrument_master", "workbook_rating_2", "TEXT"),
        ("instrument_master", "workbook_rating_agency_1", "TEXT"),
        ("instrument_master", "workbook_rating_agency_2", "TEXT"),
        ("portfolio_holdings", "credit_rating", "TEXT"),
        ("portfolio_holdings", "credit_rating_2", "TEXT"),
        ("portfolio_holdings", "rating_agency", "TEXT"),
        ("portfolio_holdings", "rating_agency_2", "TEXT"),
        ("portfolio_positions", "credit_rating_2", "TEXT"),
        ("portfolio_positions", "rating_agency", "TEXT"),
        ("portfolio_positions", "rating_agency_2", "TEXT"),
    ):
        if not await _table_has_column(table_name, column_name):
            await execute_query(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}", {})
    for column_name, column_type in (
        ("instrument_category", "TEXT"),
        ("workbook_rating_1", "TEXT"),
        ("workbook_rating_2", "TEXT"),
        ("workbook_rating_agency_1", "TEXT"),
        ("workbook_rating_agency_2", "TEXT"),
    ):
        if not await _table_has_column("instrument_master", column_name):
            await execute_query(f"ALTER TABLE instrument_master ADD COLUMN {column_name} {column_type}", {})
    if not await _table_has_column("universal_bond_master", "instrument_category"):
        await execute_query("ALTER TABLE universal_bond_master ADD COLUMN instrument_category TEXT", {})
    if not await _table_has_column("portfolio_positions", "instrument_category"):
        await execute_query("ALTER TABLE portfolio_positions ADD COLUMN instrument_category TEXT", {})
    if not await _table_has_column("portfolio_holdings", "instrument_category"):
        await execute_query("ALTER TABLE portfolio_holdings ADD COLUMN instrument_category TEXT", {})


def _merge_source_sources(*sources: dict[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for src in sources:
        if src and validate_source_context(src) == []:
            merged.update(src)
    if not merged:
        for src in sources:
            if src:
                return dict(src)
    return merged


MAIN_PORTFOLIO_SOURCE_REFERENCE = "portfolio_main.xlsx"


def _looks_like_workbook_source_reference(source_reference: str) -> bool:
    value = str(source_reference or "").strip().lower()
    return value.endswith((".xlsx", ".xlsm", ".xltx", ".xltm", ".xls"))


async def resolve_current_portfolio_source_reference(portfolio_id: str = "default") -> Optional[str]:
    portfolio_key = str(portfolio_id or "default").strip() or "default"

    try:
        exact_rows = await fetch_all(
            """
            SELECT
                source_reference,
                MAX(COALESCE(timestamp_ingested, timestamp_effective, '')) AS last_seen_at,
                COUNT(*) AS row_count
            FROM portfolio_positions
            WHERE trust_id = :pid
              AND source_reference = :main_source_reference
            GROUP BY source_reference
            LIMIT 1
            """,
            {"pid": portfolio_key, "main_source_reference": MAIN_PORTFOLIO_SOURCE_REFERENCE},
        )
        if exact_rows:
            source_reference = str(exact_rows[0].get("source_reference") or "").strip()
            if source_reference:
                return source_reference

        rows = await fetch_all(
            """
            SELECT
                source_reference,
                MAX(COALESCE(timestamp_ingested, timestamp_effective, '')) AS last_seen_at,
                                COUNT(*) AS row_count,
                                CASE
                                        WHEN LOWER(COALESCE(source_reference, '')) LIKE '%.xlsx'
                                            OR LOWER(COALESCE(source_reference, '')) LIKE '%.xlsm'
                                            OR LOWER(COALESCE(source_reference, '')) LIKE '%.xltx'
                                            OR LOWER(COALESCE(source_reference, '')) LIKE '%.xltm'
                                            OR LOWER(COALESCE(source_reference, '')) LIKE '%.xls'
                                        THEN 0
                                        ELSE 1
                                END AS workbook_rank
            FROM portfolio_positions
            WHERE trust_id = :pid
              AND COALESCE(NULLIF(TRIM(source_reference), ''), '') <> ''
            GROUP BY source_reference
                        ORDER BY workbook_rank ASC,
                                         last_seen_at DESC,
                     row_count DESC,
                     source_reference ASC
            LIMIT 1
            """,
            {"pid": portfolio_key},
        )
        if rows:
            for row in rows:
                source_reference = str(row.get("source_reference") or "").strip()
                if source_reference and _looks_like_workbook_source_reference(source_reference):
                    return source_reference
            source_reference = str(rows[0].get("source_reference") or "").strip()
            if source_reference:
                return source_reference

        rows = await fetch_all(
            """
            SELECT
                source_reference,
                MAX(COALESCE(timestamp_ingested, timestamp_effective, '')) AS last_seen_at,
                                COUNT(*) AS row_count,
                                CASE
                                        WHEN LOWER(COALESCE(source_reference, '')) LIKE '%.xlsx'
                                            OR LOWER(COALESCE(source_reference, '')) LIKE '%.xlsm'
                                            OR LOWER(COALESCE(source_reference, '')) LIKE '%.xltx'
                                            OR LOWER(COALESCE(source_reference, '')) LIKE '%.xltm'
                                            OR LOWER(COALESCE(source_reference, '')) LIKE '%.xls'
                                        THEN 0
                                        ELSE 1
                                END AS workbook_rank
            FROM portfolio_holdings
            WHERE COALESCE(portfolio_id, 'default') = :pid
              AND COALESCE(NULLIF(TRIM(source_reference), ''), '') <> ''
            GROUP BY source_reference
                        ORDER BY workbook_rank ASC,
                                         last_seen_at DESC,
                     row_count DESC,
                     source_reference ASC
            LIMIT 1
            """,
            {"pid": portfolio_key},
        )
        if rows:
            for row in rows:
                source_reference = str(row.get("source_reference") or "").strip()
                if source_reference and _looks_like_workbook_source_reference(source_reference):
                    return source_reference
            source_reference = str(rows[0].get("source_reference") or "").strip()
            if source_reference:
                return source_reference
    except Exception:
        return None

    return None


@dataclass
class ValidationFlags:
    stale_price_flag: bool
    missing_rating_flag: bool
    calculation_error_flag: bool


async def build_portfolio_view(portfolio_id: str = "default", stale_price_days: int = 3) -> Dict[str, Any]:
    await ensure_portfolio_core_schema()
    as_of = date.today()
    current_source_reference = await resolve_current_portfolio_source_reference(portfolio_id)

    rows = await fetch_all(
        """
        WITH base_holdings AS (
            SELECT
                p.id AS holding_id,
                p.unique_lot_id,
                p.portfolio_id,
                p.isin,
                p.purchase_date,
                p.quantity,
                p.purchase_price,
                p.face_value,
                p.book_value,
                p.current_value AS holding_current_value,
                p.market_price AS holding_market_price,
                p.coupon AS coupon_rate,
                p.maturity AS maturity_date,
                p.ytm AS workbook_ytm,
                p.issuer_name,
                p.credit_rating,
                p.credit_rating_2,
                p.rating_agency,
                p.rating_agency_2,
                p.sector,
                NULL AS category,
                NULL AS instrument_category,
                p.accrued_interest AS workbook_accrued_interest,
                NULL AS workbook_accrued_days,
                NULL AS workbook_interest_received,
                p.source_name,
                p.source_type,
                p.ingestion_method,
                p.source_reference,
                p.source_sheet_name,
                p.source_row_number,
                p.timestamp_ingested,
                p.timestamp_effective
            FROM portfolio_positions p
            WHERE p.trust_id = :pid
                            AND (COALESCE(CAST(:current_source_reference AS TEXT), '') = '' OR p.source_reference = :current_source_reference)
            UNION ALL
            SELECT
                h.id AS holding_id,
                NULL AS unique_lot_id,
                h.portfolio_id,
                h.isin,
                h.purchase_date,
                h.quantity,
                h.purchase_price,
                h.face_value,
                NULL AS book_value,
                NULL AS holding_current_value,
                NULL AS holding_market_price,
                NULL AS coupon_rate,
                NULL AS maturity_date,
                NULL AS workbook_ytm,
                NULL AS issuer_name,
                h.credit_rating,
                h.credit_rating_2,
                h.rating_agency,
                h.rating_agency_2,
                NULL AS sector,
                NULL AS category,
                NULL AS instrument_category,
                NULL AS workbook_accrued_interest,
                NULL AS workbook_accrued_days,
                NULL AS workbook_interest_received,
                h.source_name,
                h.source_type,
                h.ingestion_method,
                h.source_reference,
                NULL AS source_sheet_name,
                NULL AS source_row_number,
                h.timestamp_ingested,
                h.timestamp_effective
            FROM portfolio_holdings h
            WHERE h.portfolio_id = :pid
                            AND (COALESCE(CAST(:current_source_reference AS TEXT), '') = '' OR h.source_reference = :current_source_reference)
              AND NOT EXISTS (SELECT 1 FROM portfolio_positions p WHERE p.trust_id = :pid)
        ),
        latest_market_prices AS (
            SELECT *
            FROM (
                SELECT
                    mp.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY mp.isin
                        ORDER BY mp.date DESC,
                                 COALESCE(mp.timestamp_effective, mp.timestamp_ingested, mp.date) DESC,
                                 COALESCE(mp.source, '') ASC
                    ) AS rn
                FROM market_prices mp
            ) ranked
            WHERE rn = 1
        )
        SELECT
            h.holding_id AS holding_id,
            h.unique_lot_id,
            i.isin,
            i.instrument_name,
            i.issuer,
            COALESCE(h.coupon_rate, i.coupon_rate, 0.0) AS coupon_rate,
            COALESCE(h.maturity_date, i.maturity_date) AS maturity_date,
            i.put_date,
            i.call_date,
            i.sector,
            i.category,
            NULL AS instrument_category,
            i.rating_latest,
            i.rating_agency,
            i.workbook_rating_1,
            i.workbook_rating_2,
            i.workbook_rating_agency_1,
            i.workbook_rating_agency_2,
            i.payment_frequency,
            i.day_count_convention,
            i.source_name AS instrument_source_name,
            i.source_type AS instrument_source_type,
            i.ingestion_method AS instrument_ingestion_method,
            i.source_reference AS instrument_source_reference,
            i.timestamp_ingested AS instrument_timestamp_ingested,
            i.timestamp_effective AS instrument_timestamp_effective,
            h.quantity AS total_quantity,
            h.purchase_date AS purchase_date,
            h.purchase_price AS avg_purchase_price,
            h.face_value AS face_value,
            h.book_value AS holding_book_value,
            h.holding_current_value AS holding_current_value,
            h.holding_market_price AS holding_market_price,
            h.workbook_ytm AS workbook_ytm,
            h.purchase_date AS first_purchase_date,
            NULL AS holding_category,
            NULL AS holding_instrument_category,
            h.credit_rating AS holding_credit_rating,
            h.credit_rating_2 AS holding_credit_rating_2,
            h.rating_agency AS holding_rating_agency,
            h.rating_agency_2 AS holding_rating_agency_2,
            h.workbook_accrued_interest AS workbook_accrued_interest,
            h.workbook_accrued_days AS workbook_accrued_days,
            h.workbook_interest_received AS workbook_interest_received,
            h.source_name AS holding_source_name,
            h.source_type AS holding_source_type,
            h.ingestion_method AS holding_ingestion_method,
            h.source_reference AS holding_source_reference,
            h.source_sheet_name AS holding_source_sheet_name,
            h.source_row_number AS holding_source_row_number,
            h.timestamp_ingested AS holding_timestamp_ingested,
            h.timestamp_effective AS holding_timestamp_effective,
            mp.price AS current_price,
            mp.yield AS market_yield,
            mp.date AS price_date,
            mp.source AS price_source,
            mp.source_name AS market_source_name,
            mp.source_type AS market_source_type,
            mp.ingestion_method AS market_ingestion_method,
            mp.source_reference AS market_source_reference,
            mp.timestamp_ingested AS market_timestamp_ingested,
            mp.timestamp_effective AS market_timestamp_effective
        FROM base_holdings h
        INNER JOIN instrument_master i ON i.isin = h.isin
        LEFT JOIN latest_market_prices mp ON mp.isin = i.isin
        ORDER BY i.instrument_name, h.holding_id ASC
        """,
        {"pid": portfolio_id, "current_source_reference": current_source_reference},
    )

    income_rows = await fetch_all(
        """
        WITH base_isins AS (
            SELECT DISTINCT isin FROM portfolio_positions WHERE trust_id = :pid
              AND (COALESCE(CAST(:current_source_reference AS TEXT), '') = '' OR source_reference = :current_source_reference)
            UNION
            SELECT DISTINCT isin FROM portfolio_holdings
            WHERE portfolio_id = :pid
              AND (COALESCE(CAST(:current_source_reference AS TEXT), '') = '' OR source_reference = :current_source_reference)
              AND NOT EXISTS (SELECT 1 FROM portfolio_positions p WHERE p.trust_id = :pid)
        )
        SELECT DISTINCT c.isin, c.coupon_amount, c.principal_amount, c.payment_date
        FROM cashflows c
        INNER JOIN base_isins h ON h.isin = c.isin
        """,
        {"pid": portfolio_id, "current_source_reference": current_source_reference},
    )
    income_by_isin: dict[str, list[dict[str, Any]]] = {}
    for row in income_rows:
        income_by_isin.setdefault(str(row.get("isin") or ""), []).append(row)

    holdings: List[Dict[str, Any]] = []
    expanded: Dict[str, Dict[str, Any]] = {}
    total_invested = 0.0
    total_current = 0.0
    total_clean = 0.0
    total_face = 0.0
    total_accrued = 0.0
    weighted_ytm_numer = 0.0
    weighted_coupon_numer = 0.0
    weighted_duration_numer = 0.0
    weighted_maturity_numer = 0.0
    weighted_credit_numer = 0.0
    yield_weight_total = 0.0
    validation = {
        "stale_price_count": 0,
        "missing_rating_count": 0,
        "calculation_error_count": 0,
        "missing_source_count": 0,
        "source_confidence_low_count": 0,
    }

    for r in rows:
        holding_id = r.get("holding_id")
        quantity = _to_float(r.get("total_quantity"))
        purchase_price = _to_float(r.get("avg_purchase_price"))
        lot_current_value = _to_float(r.get("holding_current_value"))
        lot_book_value = _to_float(r.get("holding_book_value"))
        lot_market_price = _to_float(r.get("holding_market_price"))
        clean_price = lot_market_price if lot_market_price > 0 else _to_float(r.get("current_price"))
        face_value = _to_float(r.get("face_value"), 1000.0)
        coupon = _to_float(r.get("coupon_rate"))
        maturity = _to_date(r.get("maturity_date"))
        frequency = int(_to_float(r.get("payment_frequency"), 2.0)) or 2
        day_count = str(r.get("day_count_convention") or "ACT/365")
        classification = classify_instrument(r)
        is_sds = classification.get("type") == INSTRUMENT_TYPE_SDS
        portfolio_category = str(
            r.get("portfolio_category")
            or classification.get("portfolio_category")
            or portfolio_category_from_record(r, classification)
            or ""
        ).strip()

        position_face_value = face_value if face_value > 0 else 0.0
        if lot_book_value > 0:
            invested_value = lot_book_value
        else:
            invested_value = (purchase_price / 100.0) * position_face_value if purchase_price > 0 and position_face_value > 0 else 0.0

        clean_value = lot_book_value if lot_book_value > 0 else ((clean_price * quantity) if clean_price > 0 and quantity > 0 else 0.0)

        workbook_accrued_interest = r.get("workbook_accrued_interest")
        accrued_interest = _to_float(workbook_accrued_interest, 0.0) if workbook_accrued_interest is not None else _accrued_interest(coupon, position_face_value, maturity, frequency, day_count, as_of)
        accrued_per_100 = (accrued_interest / position_face_value * 100.0) if position_face_value > 0 else 0.0
        dirty_price = clean_price + accrued_per_100 if clean_price > 0 else 0.0
        current_value = lot_current_value if lot_current_value > 0 else (lot_book_value if lot_book_value > 0 else ((dirty_price * quantity) if dirty_price > 0 and quantity > 0 else clean_value))
        pnl = current_value - invested_value
        pnl_percent = (pnl / invested_value * 100.0) if invested_value else 0.0

        years_to_maturity = 0.0 if is_sds else (max(0.0, (maturity - as_of).days / 365.25) if maturity else 0.0)
        workbook_ytm = _to_float(r.get("workbook_ytm"), 0.0)
        computed_ytm = None if is_sds else (
            solve_ytm_percent(
                clean_price=current_value,
                face_value=position_face_value,
                coupon_rate_pct=coupon,
                years_to_maturity=years_to_maturity,
                frequency=frequency,
            )
            if current_value > 0 and position_face_value > 0 and years_to_maturity > 0
            else None
        )
        market_ytm = 0.0 if is_sds else _to_float(r.get("market_yield"), 0.0)
        ytm = 0.0 if is_sds else (workbook_ytm if workbook_ytm > 0 else (market_ytm if market_ytm > 0 else (computed_ytm or 0.0)))

        macaulay, modified = (0.0, 0.0) if is_sds else _duration_metrics(coupon, ytm, maturity, frequency, as_of)

        all_cashflows = income_by_isin.get(str(r.get("isin") or ""), [])
        income_rows = [x for x in all_cashflows if str(x.get("payment_date") or "") <= as_of.isoformat()]
        interest_received = sum(_to_float(x.get("coupon_amount")) for x in income_rows)
        interest_for_year = sum(
            _to_float(x.get("coupon_amount"))
            for x in income_rows
            if str(x.get("payment_date", "")).startswith(str(as_of.year))
        )

        price_date = _to_date(r.get("price_date"))
        stale_price = (price_date is None) or ((as_of - price_date).days > stale_price_days)
        latest_rating = str(r.get("rating_latest") or r.get("workbook_rating_1") or r.get("workbook_rating_2") or "").strip()
        rating_agency = str(r.get("rating_agency") or r.get("workbook_rating_agency_1") or r.get("workbook_rating_agency_2") or "").strip()
        workbook_rating_1 = str(r.get("workbook_rating_1") or r.get("holding_credit_rating") or "").strip()
        workbook_rating_2 = str(r.get("workbook_rating_2") or r.get("holding_credit_rating_2") or "").strip()
        workbook_rating_agency_1 = str(r.get("workbook_rating_agency_1") or r.get("holding_rating_agency") or "").strip()
        workbook_rating_agency_2 = str(r.get("workbook_rating_agency_2") or r.get("holding_rating_agency_2") or "").strip()
        missing_rating = not latest_rating and not rating_agency and not workbook_rating_1 and not workbook_rating_2
        workbook_category = str(r.get("category") or r.get("holding_category") or "").strip()
        instrument_category = str(
            r.get("instrument_category")
            or r.get("holding_instrument_category")
            or classify_instrument(r).get("instrument_category")
            or ""
        ).strip()
        workbook_accrued_interest = _to_float(r.get("workbook_accrued_interest"))
        workbook_accrued_days = _to_float(r.get("workbook_accrued_days"))
        workbook_interest_received = _to_float(r.get("workbook_interest_received"))
        display_accrued_interest = workbook_accrued_interest if workbook_accrued_interest > 0 else accrued_interest
        source_bundle = {
            "holding": {
                "source_name": r.get("holding_source_name"),
                "source_type": r.get("holding_source_type"),
                "ingestion_method": r.get("holding_ingestion_method"),
                "source_reference": r.get("holding_source_reference"),
                "timestamp_ingested": r.get("holding_timestamp_ingested"),
                "timestamp_effective": r.get("holding_timestamp_effective"),
            },
            "instrument": {
                "source_name": r.get("instrument_source_name"),
                "source_type": r.get("instrument_source_type"),
                "ingestion_method": r.get("instrument_ingestion_method"),
                "source_reference": r.get("instrument_source_reference"),
                "timestamp_ingested": r.get("instrument_timestamp_ingested"),
                "timestamp_effective": r.get("instrument_timestamp_effective"),
            },
            "market": {
                "source_name": r.get("market_source_name"),
                "source_type": r.get("market_source_type"),
                "ingestion_method": r.get("market_ingestion_method"),
                "source_reference": r.get("market_source_reference"),
                "timestamp_ingested": r.get("market_timestamp_ingested"),
                "timestamp_effective": r.get("market_timestamp_effective"),
            },
        }
        provenance_errors = [
            validate_source_context(bundle)
            for bundle in source_bundle.values()
        ]
        provenance_errors = [err for sub in provenance_errors for err in sub]
        source_confidence = confidence_for_record(_merge_source_sources(
            source_bundle["market"],
            source_bundle["instrument"],
            source_bundle["holding"],
        ))
        calc_error = bool(
            math.isnan(invested_value)
            or math.isnan(current_value)
            or (clean_price > 0 and ytm <= -50)
        )

        flags = ValidationFlags(
            stale_price_flag=stale_price,
            missing_rating_flag=missing_rating,
            calculation_error_flag=calc_error,
        )
        if flags.stale_price_flag:
            validation["stale_price_count"] += 1
        if flags.missing_rating_flag:
            validation["missing_rating_count"] += 1
        if flags.calculation_error_flag:
            validation["calculation_error_count"] += 1
        if provenance_errors:
            validation["missing_source_count"] += 1
        if source_confidence == "low":
            validation["source_confidence_low_count"] += 1

        yield_spread = None
        if not is_sds and clean_price > 0 and ytm > 0 and computed_ytm is not None:
            yield_spread = round(ytm - computed_ytm, 6)
            if abs(yield_spread) > 3.0:
                validation["calculation_error_count"] += 1
                flags.calculation_error_flag = True

        total_invested += invested_value
        total_current += current_value
        total_clean += clean_value
        total_face += position_face_value
        total_accrued += display_accrued_interest
        if current_value > 0:
            weighted_coupon_numer += coupon * current_value
            if not is_sds:
                weighted_ytm_numer += ytm * current_value
                weighted_duration_numer += modified * current_value
                weighted_maturity_numer += years_to_maturity * current_value
                yield_weight_total += current_value
            weighted_credit_numer += _rating_score(latest_rating) * current_value

        holdings.append(
            {
                "holding_id": holding_id,
                "unique_lot_id": r.get("unique_lot_id") or holding_id,
                "isin": str(r.get("isin") or "").strip().upper(),
                "instrument_name": r.get("instrument_name"),
                "issuer_name": r.get("issuer_name") or r.get("issuer"),
                "purchase_date": r.get("purchase_date"),
                "quantity": quantity,
                "purchase_price": purchase_price,
                "face_value": face_value,
                "book_value": invested_value,
                "market_price": clean_price,
                "clean_price": clean_price,
                "dirty_price": dirty_price,
                "current_value": current_value,
                "investment_value": invested_value,
                "accrued_interest": accrued_interest,
                "ytm": ytm,
                "modified_duration": modified,
                "macaulay_duration": macaulay,
                "instrument_type": classification.get("type"),
                "sub_type": classification.get("sub_type"),
                "sector": r.get("sector"),
                "category": workbook_category or r.get("category"),
                "instrument_category": instrument_category,
                "portfolio_category": portfolio_category,
                "portfolio_category_label": classification.get("portfolio_category_label"),
                "credit_rating": latest_rating or workbook_rating_1,
                "credit_rating_2": workbook_rating_2,
                "rating_agency": rating_agency,
                "rating_agency_2": workbook_rating_agency_2,
                "stale_price_flag": stale_price,
                "missing_rating_flag": missing_rating,
                "calculation_error_flag": calc_error,
                "source_confidence": source_confidence,
                "yield_spread": yield_spread,
                "source_name": r.get("holding_source_name"),
                "source_type": r.get("holding_source_type"),
                "ingestion_method": r.get("holding_ingestion_method"),
                "source_reference": r.get("holding_source_reference"),
                "source_sheet_name": r.get("holding_source_sheet_name"),
                "source_row_number": r.get("holding_source_row_number"),
                "timestamp_ingested": r.get("holding_timestamp_ingested"),
                "timestamp_effective": r.get("holding_timestamp_effective"),
                "current_price": r.get("current_price"),
                "market_yield": r.get("market_yield"),
            }
        )

    positions = _aggregate_lot_positions(holdings)

    total_pnl = total_current - total_invested
    report = {
        "status": "PASS" if validation["stale_price_count"] == 0 and validation["calculation_error_count"] == 0 and validation["missing_source_count"] == 0 and validation["source_confidence_low_count"] == 0 else "FAIL",
        **validation,
    }

    yield_weight = yield_weight_total or 0.0
    weighted_ytm = round(weighted_ytm_numer / yield_weight, 6) if yield_weight else 0.0
    weighted_coupon_yield = round(weighted_coupon_numer / total_current, 6) if total_current else 0.0
    weighted_modified_duration = round(weighted_duration_numer / yield_weight, 6) if yield_weight else 0.0
    weighted_avg_maturity = round(weighted_maturity_numer / yield_weight, 6) if yield_weight else 0.0
    weighted_credit_score = round(weighted_credit_numer / total_current, 6) if total_current else 0.0
    portfolio_rating = _rating_label_from_score(weighted_credit_score)

    try:
        await record_calculation_trace(
            entity_type="PORTFOLIO",
            entity_id=portfolio_id,
            calculation_name="build_portfolio_view",
            formula_version="portfolio_institutional_engine.v1",
            input_values_snapshot={
                "portfolio_id": portfolio_id,
                "stale_price_days": stale_price_days,
                "holdings_count": len(holdings),
            },
            output_values_snapshot={
                "summary": {
                    "invested_value": round(total_invested, 6),
                    "current_value": round(total_current, 6),
                    "pnl": round(total_pnl, 6),
                    "weighted_ytm": weighted_ytm,
                    "coupon_weighted_yield": weighted_coupon_yield,
                    "weighted_modified_duration": weighted_modified_duration,
                    "portfolio_rating": portfolio_rating,
                },
                "validation_report": report,
            },
            source_snapshot={
                "portfolio_id": portfolio_id,
                "traceability_status": report["status"],
            },
            actor="system",
        )
    except Exception:
        pass

    return {
        "as_of": as_of.isoformat(),
        "summary": {
            "invested_value": round(total_invested, 6),
            "current_value": round(total_current, 6),
            "pnl": round(total_pnl, 6),
            "pnl_percent": round((total_pnl / total_invested * 100.0), 6) if total_invested else 0.0,
            "unrealized_gain": round(total_pnl, 6),
            "unrealized_gain_pct": round((total_pnl / total_invested * 100.0), 6) if total_invested else 0.0,
            "holdings_count": len(holdings),
            "position_count": len(positions),
            "aum_crores": round(total_current / 1e7, 6),
            "total_market_value": round(total_current, 6),
            "total_invested": round(total_invested, 6),
            "invested_crores": round(total_invested / 1e7, 6),
            "face_value_crores": round(total_face / 1e7, 6),
            "total_clean_value": round(total_clean, 6),
            "total_accrued_interest": round(total_accrued, 6),
            "weighted_ytm": weighted_ytm,
            "coupon_weighted_yield": weighted_coupon_yield,
            "portfolio_coupon_weighted_yield_pct": weighted_coupon_yield,
            "weighted_modified_duration": weighted_modified_duration,
            "weighted_duration": weighted_modified_duration,
            "weighted_avg_maturity": weighted_avg_maturity,
            "portfolio_rating": portfolio_rating,
            "credit_quality_rating": portfolio_rating,
            "data_traceability_status": report["status"],
        },
        "holdings": holdings,
        "positions": positions,
        "expanded": expanded,
        "validation_report": report,
    }


def build_portfolio_field_audit() -> Dict[str, Any]:
    mapping_table = [
        {"sheet_column": "Particulars", "meaning": "Bond/security label", "formula_in_sheet": "Input / display label", "system_field": "bond_name", "status": "MAPPED"},
        {"sheet_column": "Issuer", "meaning": "Issuer or borrower name", "formula_in_sheet": "Input / display label", "system_field": "issuer_name", "status": "MAPPED"},
        {"sheet_column": "Rate of Int", "meaning": "Coupon rate", "formula_in_sheet": "Input / rate field", "system_field": "coupon_rate", "status": "MAPPED"},
        {"sheet_column": "Purchase Date", "meaning": "Trade or acquisition date", "formula_in_sheet": "Input / date field", "system_field": "purchase_date", "status": "MAPPED"},
        {"sheet_column": "Maturity Date", "meaning": "Redemption date", "formula_in_sheet": "Input / date field", "system_field": "maturity_date", "status": "MAPPED"},
        {"sheet_column": "No of Units", "meaning": "Quantity held", "formula_in_sheet": "Input / quantity field", "system_field": "quantity", "status": "MAPPED"},
        {"sheet_column": "ISIN No/Loan Code", "meaning": "Security identifier", "formula_in_sheet": "Input / unique key", "system_field": "isin", "status": "MAPPED"},
        {"sheet_column": "Put Dt", "meaning": "Put/call optionality date", "formula_in_sheet": "Input / date field", "system_field": "put_date", "status": "MAPPED"},
        {"sheet_column": "Call Dt", "meaning": "Call option date", "formula_in_sheet": "Input / date field", "system_field": "call_date", "status": "MAPPED"},
        {"sheet_column": "Category", "meaning": "Portfolio classification", "formula_in_sheet": "Input / classification", "system_field": "category", "status": "MAPPED"},
        {"sheet_column": "Sector", "meaning": "Issuer sector", "formula_in_sheet": "Input / classification", "system_field": "sector", "status": "MAPPED"},
        {"sheet_column": "Rating Agency1", "meaning": "Primary rating agency", "formula_in_sheet": "Input / supporting metadata", "system_field": "rating_agency", "status": "MAPPED"},
        {"sheet_column": "Rating 1", "meaning": "Primary credit rating", "formula_in_sheet": "Input / supporting metadata", "system_field": "credit_rating", "status": "MAPPED"},
        {"sheet_column": "Rating Agency2", "meaning": "Secondary rating agency", "formula_in_sheet": "Input / supporting metadata", "system_field": "rating_agency", "status": "MAPPED"},
        {"sheet_column": "Rating 2", "meaning": "Secondary credit rating", "formula_in_sheet": "Input / supporting metadata", "system_field": "credit_rating", "status": "MAPPED"},
        {"sheet_column": "IP Dates", "meaning": "Interest payment schedule", "formula_in_sheet": "Input / semi-structured schedule text", "system_field": "ip_dates", "status": "MAPPED"},
        {"sheet_column": "Closing FaceValue", "meaning": "Closing face value", "formula_in_sheet": "Derived from holdings x face value", "system_field": "face_value", "status": "DERIVED"},
        {"sheet_column": "Closing Investment", "meaning": "Closing invested amount", "formula_in_sheet": "Derived from quantity x purchase price", "system_field": "book_value", "status": "DERIVED"},
        {"sheet_column": "Int. Recd.", "meaning": "Interest received to date", "formula_in_sheet": "Derived from cashflows", "system_field": "cashflows.coupon_amount", "status": "DERIVED"},
        {"sheet_column": "Accrued Interest", "meaning": "Interest accrued since last coupon", "formula_in_sheet": "Derived at valuation time", "system_field": "accrued_interest", "status": "DERIVED"},
        {"sheet_column": "Accrued Days", "meaning": "Days since last coupon", "formula_in_sheet": "Derived date arithmetic", "system_field": "calculation_only", "status": "DERIVED"},
        {"sheet_column": "Int. for the year", "meaning": "Coupon income in current year", "formula_in_sheet": "Derived from cashflows", "system_field": "cashflows.coupon_amount", "status": "DERIVED"},
        {"sheet_column": "stale_price_flag", "meaning": "Validation flag for stale prices", "formula_in_sheet": "Backend computed", "system_field": "validation.flags.stale_price_flag", "status": "INTERNAL"},
        {"sheet_column": "missing_rating_flag", "meaning": "Validation flag for missing ratings", "formula_in_sheet": "Backend computed", "system_field": "validation.flags.missing_rating_flag", "status": "INTERNAL"},
        {"sheet_column": "calculation_error_flag", "meaning": "Validation flag for calculation anomalies", "formula_in_sheet": "Backend computed", "system_field": "validation.flags.calculation_error_flag", "status": "INTERNAL"},
        {"sheet_column": "yield_spread", "meaning": "Model-versus-market yield spread", "formula_in_sheet": "Backend computed", "system_field": "spread_over_gsec_bps", "status": "INTERNAL"},
        {"sheet_column": "duration", "meaning": "Duration", "formula_in_sheet": "Backend computed", "system_field": "modified_duration", "status": "INTERNAL"},
        {"sheet_column": "modified_duration", "meaning": "Modified duration", "formula_in_sheet": "Backend computed", "system_field": "modified_duration", "status": "INTERNAL"},
        {"sheet_column": "Category (duplicate column)", "meaning": "Duplicate workbook column", "formula_in_sheet": "Rejected as duplicate", "system_field": None, "status": "INVALID_OR_DUPLICATE"},
    ]

    return {
        "classification": {
            "CORE": [entry["sheet_column"] for entry in mapping_table if entry["status"] == "MAPPED"],
            "DERIVED": [entry["sheet_column"] for entry in mapping_table if entry["status"] == "DERIVED"],
            "INTERNAL": [entry["sheet_column"] for entry in mapping_table if entry["status"] == "INTERNAL"],
            "INVALID_OR_DUPLICATE": [entry["sheet_column"] for entry in mapping_table if entry["status"] == "INVALID_OR_DUPLICATE"],
        },
        "mapping_table": mapping_table,
        "removed_from_storage": {
            "closing_face_value": "Derived from positions/prices, recompute on read.",
            "closing_investment": "Derived from quantity and purchase price, recompute on read.",
            "interest_received": "Derived from cashflows, not persisted in holdings.",
            "accrued_interest": "Day-count derived at valuation time.",
            "accrued_days": "Intermediate date math, compute at request time.",
            "interest_for_year": "Aggregated from cashflows per valuation date.",
            "pnl": "Derived from current and invested value.",
            "current_value": "Derived from latest market price and quantity.",
        },
        "summary": {
            "sheet_columns_total": len(mapping_table),
            "mapped_columns": sum(1 for entry in mapping_table if entry["status"] == "MAPPED"),
            "derived_columns": sum(1 for entry in mapping_table if entry["status"] == "DERIVED"),
            "internal_columns": sum(1 for entry in mapping_table if entry["status"] == "INTERNAL"),
            "invalid_or_duplicate_columns": sum(1 for entry in mapping_table if entry["status"] == "INVALID_OR_DUPLICATE"),
        },
        "structural_issues_detected": [
            "Raw + derived data mixed in the same holdings table.",
            "Duplicate semantic fields (Category appears twice in source sheet).",
            "Non-normalized ratings split across multiple agency columns.",
            "Portfolio rows historically stored one row per ISIN, preventing multi-lot fidelity.",
        ],
    }
