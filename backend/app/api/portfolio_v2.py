"""
Portfolio Router — Institutional Grade
========================================
All calculations are sourced from the institutional raw-data pipeline.
No frontend calculations. All server-side.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from typing import Optional
from pydantic import BaseModel
import json
import os
import uuid
from datetime import datetime, date
from pathlib import Path

try:
    import openpyxl
except Exception:
    openpyxl = None

from ..shared.database import fetch_all, fetch_one, execute_query
from ..shared.logger import get_logger
from ..shared.auth import require_role, Roles
from ..shared.time_utils import utcnow_iso
from ..services.institutional_financial_core import solve_ytm_percent
from ..domain.portfolio.dataset import PortfolioDatasetService
from ..services.institutional_financial_core import parse_flexible_date
from ..domain.core_trust.audit import AuditAction, AuditService, AuditSeverity, EntityType
from ..services.instrument_classification import classify_instrument
from ..services.instrument_classification import (
    PORTFOLIO_CATEGORY_CENTRAL_STATE,
    PORTFOLIO_CATEGORY_EQUITY,
    PORTFOLIO_CATEGORY_PRIVATE,
    PORTFOLIO_CATEGORY_PSU,
    PORTFOLIO_CATEGORY_SDS,
    portfolio_category_from_record,
)
from ..domain.portfolio.engine import (
    build_portfolio_view,
    build_portfolio_field_audit,
    ensure_portfolio_core_schema,
)
from ..domain.portfolio.analytics import compute_full_portfolio_analytics

log = get_logger("router.portfolio")
router = APIRouter(prefix="/portfolio", tags=["Portfolio Management"])
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
WORKBOOK_PATH = Path(os.getenv("PORTFOLIO_WORKBOOK") or str(WORKSPACE_ROOT / "data" / "portfolio_upload.xlsx"))
SUMMARY_WORKBOOK_PATH = Path(os.getenv("PORTFOLIO_SUMMARY_WORKBOOK") or str(WORKSPACE_ROOT / "data" / "portfolio_main.xlsx"))
SUMMARY_WORKBOOK_FALLBACK_PATH = Path(
    os.getenv("PORTFOLIO_SUMMARY_WORKBOOK_FALLBACK")
    or str(WORKSPACE_ROOT / "data" / "portfolio_main_fallback.xlsx")
)


def _state_payload(status: str, data: dict | None = None, message: str | None = None) -> dict:
    return {
        "status": status,
        "data": data if isinstance(data, dict) else {},
        "message": message,
    }


def _normalize_text(value: object) -> str:
    return str(value or "").strip().upper()


def _is_sds_like_row(row: dict) -> bool:
    try:
        return classify_instrument(row).get("type") == "SDS"
    except Exception:
        category = _normalize_text(row.get("workbook_category") or row.get("category"))
        issuer_name = _normalize_text(row.get("issuer_name"))
        bond_name = _normalize_text(row.get("bond_name"))
        sds_tokens = ("STATE DEVELOPMENT SAVINGS", "SDS", "SAVINGS BOND", "NON-TRADABLE", "LOCKED", "STATIC", "FRSB")
        return any(token in category for token in sds_tokens) or any(token in issuer_name for token in sds_tokens) or any(token in bond_name for token in sds_tokens)


def _row_weight(row: dict) -> float:
    for key in ("current_value", "market_value", "investment_value", "book_value"):
        try:
            value = float(row.get(key) or 0.0)
            if value > 0:
                return value
        except Exception:
            continue
    return 0.0


def _row_purchase_value(row: dict) -> float:
    for key in ("investment_value", "book_value", "purchase_value", "purchase_price"):
        try:
            value = float(row.get(key) or 0.0)
            if value > 0:
                return value
        except Exception:
            continue
    return 0.0


def _weighted_average(rows: list[dict], value_key: str, weight_key: str = "current_value") -> float:
    numerator = 0.0
    denominator = 0.0
    for row in rows:
        try:
            value = float(row.get(value_key) or 0.0)
            weight = float(row.get(weight_key) or _row_weight(row) or 0.0)
        except Exception:
            continue
        if weight <= 0:
            continue
        numerator += value * weight
        denominator += weight
    return (numerator / denominator) if denominator else 0.0


def _equity_cagr_pct(row: dict) -> float:
    purchase_price = float(row.get("purchase_price") or row.get("avg_purchase_price") or row.get("book_price") or 0.0)
    current_price = float(row.get("current_price") or row.get("nav") or row.get("market_price") or row.get("current_value") or 0.0)
    if purchase_price <= 0 or current_price <= 0:
        return 0.0
    years = float(row.get("time_period") or row.get("years_held") or row.get("holding_period_years") or 0.0)
    if years <= 0:
        purchase_date = parse_flexible_date(row.get("purchase_date"))
        years = max(0.0, (date.today() - purchase_date).days / 365.25) if purchase_date else 0.0
    if years <= 0:
        return 0.0
    try:
        return ((current_price / purchase_price) ** (1.0 / years) - 1.0) * 100.0
    except Exception:
        return 0.0


def _build_structured_portfolio_groups(rows: list[dict]) -> dict:
    groups = {
        "sds": [],
        "bonds": {
            "central_state": [],
            "psu": [],
            "private": [],
        },
        "equity": [],
    }

    for row in rows:
        classification = classify_instrument(row)
        portfolio_category = portfolio_category_from_record(row, classification)
        if portfolio_category == PORTFOLIO_CATEGORY_SDS:
            groups["sds"].append(row)
        elif portfolio_category == PORTFOLIO_CATEGORY_EQUITY:
            groups["equity"].append(row)
        elif portfolio_category == PORTFOLIO_CATEGORY_CENTRAL_STATE:
            groups["bonds"]["central_state"].append(row)
        elif portfolio_category == PORTFOLIO_CATEGORY_PSU:
            groups["bonds"]["psu"].append(row)
        else:
            groups["bonds"]["private"].append(row)

    def _latest_stamp(rows_for_group: list[dict]) -> str | None:
        stamps = []
        for row in rows_for_group:
            stamp = str(row.get("timestamp_effective") or row.get("timestamp_ingested") or row.get("last_updated") or "").strip()
            if stamp:
                stamps.append(stamp)
        return max(stamps) if stamps else None

    def _summarize(rows_for_group: list[dict], group_type: str, sub_type: str | None = None) -> dict:
        total_current = sum(_row_weight(row) for row in rows_for_group)
        total_invested = sum(_row_purchase_value(row) for row in rows_for_group)
        summary = {
            "type": group_type,
            "sub_type": sub_type,
            "count": len(rows_for_group),
            "current_value": round(total_current, 6),
            "invested_value": round(total_invested, 6),
            "last_updated": _latest_stamp(rows_for_group),
        }

        if group_type == "SDS":
            summary.update({
                "type_label": "Government-backed",
                "liquidity": "Locked / Non-tradable",
                "interest_rate": round(_weighted_average(rows_for_group, "interest_rate", "current_value"), 6),
            })
            return summary

        if group_type == "EQUITY":
            absolute_return = total_current - total_invested
            summary.update({
                "absolute_return": round(absolute_return, 6),
                "absolute_return_pct": round((absolute_return / total_invested * 100.0), 6) if total_invested else 0.0,
                "cagr_pct": round(_weighted_average([{**row, "cagr_pct": _equity_cagr_pct(row)} for row in rows_for_group], "cagr_pct", "investment_value"), 6),
            })
            return summary

        bond_rows = [row for row in rows_for_group if _normalize_text(row.get("type") or row.get("instrument_type")) != "SDS"]
        bond_weight = sum(_row_weight(row) for row in bond_rows)
        coupon_weighted_yield = round(_weighted_average(rows_for_group, "interest_rate", "current_value"), 6) if total_current else 0.0
        summary.update({
            "weighted_ytm": round(_weighted_average(bond_rows, "ytm", "current_value"), 6) if bond_weight else 0.0,
            "coupon_weighted_yield": coupon_weighted_yield,
            "weighted_modified_duration": round(_weighted_average(bond_rows, "modified_duration", "current_value"), 6) if bond_weight else 0.0,
            "weighted_avg_maturity": round(_weighted_average(bond_rows, "years_to_maturity", "current_value"), 6) if bond_weight else 0.0,
        })
        return summary

    return {
        "sds": {
            "summary": _summarize(groups["sds"], "SDS", "STATIC"),
            "holdings": groups["sds"],
        },
        "bonds": {
            "summary": _summarize(groups["bonds"]["central_state"] + groups["bonds"]["psu"] + groups["bonds"]["private"], "BOND"),
            "central_state": {"summary": _summarize(groups["bonds"]["central_state"], "BOND", "CENTRAL_STATE"), "holdings": groups["bonds"]["central_state"]},
            "psu": {"summary": _summarize(groups["bonds"]["psu"], "BOND", "PSU"), "holdings": groups["bonds"]["psu"]},
            "private": {"summary": _summarize(groups["bonds"]["private"], "BOND", "PRIVATE"), "holdings": groups["bonds"]["private"]},
            "central": {"summary": _summarize(groups["bonds"]["central_state"], "BOND", "CENTRAL_STATE"), "holdings": groups["bonds"]["central_state"]},
            "state": {"summary": _summarize(groups["bonds"]["central_state"], "BOND", "CENTRAL_STATE"), "holdings": groups["bonds"]["central_state"]},
        },
        "equity": {
            "summary": _summarize(groups["equity"], "EQUITY"),
            "holdings": groups["equity"],
        },
    }


def _content_type_for_workbook(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return "application/vnd.ms-excel"


async def _load_workbook_portfolio_snapshot(trust_id: str = "default") -> dict:
    trust_id = str(trust_id or "default").strip() or "default"
    workbook_path = (
        SUMMARY_WORKBOOK_PATH
        if SUMMARY_WORKBOOK_PATH.exists()
        else SUMMARY_WORKBOOK_FALLBACK_PATH
        if SUMMARY_WORKBOOK_FALLBACK_PATH.exists()
        else WORKBOOK_PATH
    )
    parsed = {}
    if workbook_path.exists():
        parsed = await PortfolioDatasetService.parse_upload(
            workbook_path.read_bytes(),
            filename=workbook_path.name,
            content_type=_content_type_for_workbook(workbook_path),
            portfolio_id=trust_id,
            trust_id=trust_id,
        )
    accepted = parsed.get("accepted") or []
    if not accepted:
        latest_upload = await fetch_one(
            """
            SELECT batch_id, trust_id, portfolio_id, file_name, mime_type, raw_content
            FROM portfolio_upload_batches
            WHERE trust_id = :trust_id OR portfolio_id = :trust_id
            ORDER BY id DESC
            LIMIT 1
            """,
            {"trust_id": trust_id},
        )
        if latest_upload and latest_upload.get("raw_content"):
            try:
                raw_content = latest_upload.get("raw_content")
                if isinstance(raw_content, memoryview):
                    raw_content = raw_content.tobytes()
                elif not isinstance(raw_content, (bytes, bytearray)):
                    raw_content = bytes(raw_content)

                fallback_filename = str(latest_upload.get("file_name") or "uploaded_portfolio.xlsx")
                fallback_content_type = str(latest_upload.get("mime_type") or _content_type_for_workbook(Path(fallback_filename)))
                parsed = await PortfolioDatasetService.parse_upload(
                    raw_content,
                    filename=fallback_filename,
                    content_type=fallback_content_type,
                    batch_id=str(latest_upload.get("batch_id") or ""),
                    strict=False,
                    portfolio_id=str(latest_upload.get("portfolio_id") or latest_upload.get("trust_id") or "default"),
                    trust_id=str(latest_upload.get("trust_id") or "default"),
                )
                accepted = parsed.get("accepted") or []
            except Exception as exc:
                log.warning("portfolio_snapshot_batch_fallback_failed", error=str(exc))

        if not accepted:
            return {}

    total_value = 0.0
    total_face_value = 0.0
    weighted_ytm_sum_all = 0.0
    weighted_ytm_sum_ex_sds = 0.0
    weighted_ytm_weight_ex_sds = 0.0
    weighted_coupon_sum_all = 0.0
    weighted_duration_sum = 0.0
    weighted_duration_weight_ex_sds = 0.0
    rows_without_ytm = 0
    distinct_isins: set[str] = set()
    holdings: list[dict] = []

    def _safe_row_number(value: object) -> int:
        try:
            return int(float(value or 0))
        except Exception:
            return 0

    today = date.today()
    for row in accepted:
        current_value = float(row.get("current_value") or 0.0)
        face_value = float(row.get("face_value") or 0.0)
        coupon_rate = float(row.get("coupon_rate") or 0.0)
        classification = classify_instrument(row)
        portfolio_category = portfolio_category_from_record(row, classification)
        sds_like_row = portfolio_category == PORTFOLIO_CATEGORY_SDS or classification.get("type") == "SDS"
        raw_ytm = row.get("ytm_at_purchase")
        ytm_value = None
        if raw_ytm not in (None, ""):
            try:
                ytm_value = float(raw_ytm)
            except Exception:
                ytm_value = None
        maturity_dt = parse_flexible_date(row.get("maturity_date"))
        years_to_maturity = max(0.0, (maturity_dt - today).days / 365.25) if maturity_dt else 0.0
        purchase_dt = parse_flexible_date(row.get("purchase_date"))
        purchase_date_iso = purchase_dt.isoformat() if purchase_dt else None
        isin = str(row.get("isin") or "").strip().upper()
        source_row_number = _safe_row_number(row.get("_row_number") or row.get("source_row_number"))
        source_sheet_name = str(row.get("_source_sheet_name") or row.get("source_sheet_name") or "").strip()
        source_reference = str(row.get("source_reference") or workbook_path.name).strip() or workbook_path.name
        source_name = str(row.get("source_name") or workbook_path.stem or "workbook_upload").strip() or "workbook_upload"
        source_type = str(row.get("source_type") or "WORKBOOK").strip() or "WORKBOOK"
        ingestion_method = str(row.get("ingestion_method") or "workbook_snapshot").strip() or "workbook_snapshot"
        timestamp_ingested = str(row.get("timestamp_ingested") or utcnow_iso())
        timestamp_effective = str(row.get("timestamp_effective") or timestamp_ingested)
        workbook_category = str(row.get("category") or "").strip()
        workbook_sector = str(row.get("sector") or "").strip()
        latest_rating = str(row.get("credit_rating") or row.get("credit_rating_1") or row.get("credit_rating_2") or "").strip()
        rating_agency = str(row.get("rating_agency") or row.get("rating_agency_1") or row.get("rating_agency_2") or "").strip()

        if isin:
            distinct_isins.add(isin)

        if not sds_like_row and ytm_value is None and current_value > 0.0 and face_value > 0.0 and coupon_rate > 0.0 and years_to_maturity > 0.0:
            derived_ytm = solve_ytm_percent(
                clean_price=current_value,
                face_value=face_value,
                coupon_rate_pct=coupon_rate,
                years_to_maturity=years_to_maturity,
                frequency=2,
            )
            if derived_ytm is not None:
                ytm_value = float(derived_ytm)

        if sds_like_row:
            ytm_value = None

        total_value += current_value
        total_face_value += face_value
        if current_value > 0:
            weighted_coupon_sum_all += coupon_rate * current_value
            if ytm_value is not None:
                weighted_ytm_sum_all += ytm_value * current_value
                if not sds_like_row:
                    weighted_ytm_sum_ex_sds += ytm_value * current_value
                    weighted_ytm_weight_ex_sds += current_value
            elif not sds_like_row:
                rows_without_ytm += 1
            if maturity_dt and maturity_dt > today and not sds_like_row:
                weighted_duration_sum += years_to_maturity * current_value
                weighted_duration_weight_ex_sds += current_value

        normalized_row = dict(row)
        normalized_row.update({
            "source_row_number": source_row_number,
            "source_sheet_name": source_sheet_name,
            "source_reference": source_reference,
            "source_name": source_name,
            "source_type": source_type,
            "ingestion_method": ingestion_method,
            "timestamp_ingested": timestamp_ingested,
            "timestamp_effective": timestamp_effective,
            "purchase_date": purchase_date_iso,
            "quantity": float(row.get("quantity") or 0.0),
            "face_value": face_value,
            "purchase_price": float(row.get("purchase_price") or row.get("buy_price") or 0.0),
            "current_price": float(row.get("current_price") or row.get("nav") or current_value or 0.0),
            "current_value": current_value,
            "market_value": current_value,
            "investment_value": current_value,
            "book_value": current_value,
            "interest_rate": coupon_rate,
            "workbook_category": workbook_category,
            "workbook_sector": workbook_sector,
            "type": classification.get("type"),
            "sub_type": classification.get("sub_type"),
            "instrument_type": classification.get("type"),
            "instrument_subtype": classification.get("sub_type"),
            "instrument_category": classification.get("instrument_category"),
            "portfolio_category": portfolio_category,
            "portfolio_category_label": classification.get("portfolio_category_label"),
            "portfolio_category_key": portfolio_category,
            "portfolio_bucket": portfolio_category,
            "credit_rating": latest_rating,
            "rating": latest_rating,
            "rating_agency": rating_agency,
            "years_to_maturity": years_to_maturity,
            "ytm": ytm_value,
            "weighted_ytm": ytm_value,
            "provenance_json": json.dumps({
                "source_name": source_name,
                "source_type": source_type,
                "ingestion_method": ingestion_method,
                "source_reference": source_reference,
                "source_sheet_name": source_sheet_name,
                "source_row_number": source_row_number,
                "purchase_date": purchase_date_iso,
                "timestamp_ingested": timestamp_ingested,
                "timestamp_effective": timestamp_effective,
            }, ensure_ascii=False),
        })
        normalized_row.pop("_row_number", None)
        normalized_row.pop("_source_sheet_name", None)
        holdings.append(normalized_row)

    accrued_interest_total = 0.0
    if openpyxl is not None and workbook_path.suffix.lower() in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
        try:
            workbook = openpyxl.load_workbook(workbook_path, data_only=True, read_only=True)
            try:
                worksheet = workbook[workbook.sheetnames[0]]
                header_row_index = None
                headers = []
                for candidate_index, candidate_row in enumerate(worksheet.iter_rows(min_row=1, max_row=min(10, worksheet.max_row), values_only=True), start=1):
                    normalized = [str(value).strip().lower() if value is not None else "" for value in candidate_row]
                    if any(token in normalized for token in ("isin no/loan code", "closing facevalue", "accrued interest", "int. for the year")):
                        header_row_index = candidate_index
                        headers = normalized
                        break
                if header_row_index is not None:
                    interest_index = next((index for index, header in enumerate(headers) if header == "accrued interest"), None)
                    if interest_index is None:
                        interest_index = next((index for index, header in enumerate(headers) if header == "int. for the year"), None)
                    if interest_index is not None:
                        for data_row in worksheet.iter_rows(min_row=header_row_index + 1, values_only=True):
                            if not data_row or not any(value is not None and str(value).strip() != "" for value in data_row):
                                continue
                            try:
                                accrued_interest_total += float(data_row[interest_index] or 0.0)
                            except Exception:
                                continue
            finally:
                workbook.close()
        except Exception:
            accrued_interest_total = 0.0

    weighted_ytm_yield_bearing = (weighted_ytm_sum_ex_sds / weighted_ytm_weight_ex_sds) if weighted_ytm_weight_ex_sds else 0.0
    weighted_ytm_all_rows = (weighted_ytm_sum_all / total_value) if total_value else 0.0
    weighted_coupon_yield = (weighted_coupon_sum_all / total_value) if total_value else 0.0
    weighted_modified_duration = (weighted_duration_sum / weighted_duration_weight_ex_sds) if weighted_duration_weight_ex_sds else 0.0
    current_value_crores = total_value / 10000000.0
    face_value_crores = total_face_value / 10000000.0

    holdings.sort(key=lambda row: (_safe_row_number(row.get("source_row_number")), str(row.get("isin") or "")))
    structured_groups = _build_structured_portfolio_groups(holdings)

    return {
        "summary": {
            "current_value": total_value,
            "total_market_value": total_value,
            "invested_value": total_value,
            "total_invested": total_value,
            "aum_crores": current_value_crores,
            "invested_crores": current_value_crores,
            "face_value_crores": face_value_crores,
            "total_clean_value": total_value,
            "total_accrued_interest": accrued_interest_total,
            "unrealized_gain": 0.0,
            "unrealized_gain_pct": 0.0,
            "pnl": 0.0,
            "pnl_percent": 0.0,
            "weighted_ytm": weighted_ytm_yield_bearing,
            "coupon_weighted_yield": weighted_coupon_yield,
            "weighted_modified_duration": weighted_modified_duration,
            "weighted_duration": weighted_modified_duration,
            "weighted_avg_maturity": weighted_modified_duration,
            "holdings_count": len(holdings),
            "position_count": len(distinct_isins),
            "rows_without_ytm": rows_without_ytm,
            "portfolio_ytm_yield_bearing_pct": weighted_ytm_yield_bearing,
            "portfolio_ytm_all_rows_pct": weighted_ytm_all_rows,
            "portfolio_coupon_weighted_yield_pct": weighted_coupon_yield,
            "workbook_total_market_value": total_value,
            "workbook_total_invested": total_value,
            "workbook_face_value_crores": face_value_crores,
            "workbook_weighted_ytm": weighted_ytm_yield_bearing,
            "workbook_weighted_ytm_all_rows": weighted_ytm_all_rows,
            "workbook_coupon_weighted_yield": weighted_coupon_yield,
            "workbook_weighted_ytm_source": "derived_excluding_sdl",
            "workbook_weighted_modified_duration": weighted_modified_duration,
            "workbook_total_rows": parsed.get("summary", {}).get("total_rows", 0),
            "workbook_accepted_rows": parsed.get("summary", {}).get("accepted_rows", 0),
            "workbook_rejected_rows": parsed.get("summary", {}).get("rejected_rows", 0),
            "workbook_unique_isins": len(distinct_isins),
        },
        "holdings": holdings,
        "grouped": structured_groups,
        "sds": structured_groups.get("sds", {}),
        "bonds": structured_groups.get("bonds", {}),
        "equity": structured_groups.get("equity", {}),
        "generated_at": today.isoformat(),
        "validation_report": {
            "status": "PASS" if rows_without_ytm == 0 else "WARN",
            "rows_without_ytm": rows_without_ytm,
            "accepted_rows": len(holdings),
            "distinct_isin": len(distinct_isins),
        },
    }


async def _load_workbook_summary(trust_id: str = "default") -> dict:
    snapshot = await _load_workbook_portfolio_snapshot(trust_id=trust_id)
    return snapshot.get("summary", {}) if snapshot else {}


async def _refresh_trade_holding_snapshot(portfolio_id: str, isin: str) -> None:
    lots = await fetch_all(
        """
        SELECT purchase_date, quantity, purchase_price, face_value, source_name, source_type,
               ingestion_method, source_reference, source_sheet_name, source_row_number,
               timestamp_ingested, timestamp_effective, provenance_json
        FROM portfolio_positions
        WHERE portfolio_id = :pid AND isin = :isin
        ORDER BY COALESCE(purchase_date, ''), COALESCE(source_row_number, 0), id
        """,
        {"pid": portfolio_id, "isin": isin},
    )
    if not lots:
        await execute_query(
            "DELETE FROM portfolio_holdings WHERE portfolio_id = :pid AND isin = :isin",
            {"pid": portfolio_id, "isin": isin},
        )
        return

    instrument = await fetch_one(
        "SELECT rating_latest, rating_agency FROM instrument_master WHERE isin = :isin",
        {"isin": isin},
    ) or {}

    total_quantity = 0.0
    total_purchase_basis = 0.0
    total_face_value = 0.0
    earliest_purchase_date: Optional[str] = None
    earliest_purchase_dt: Optional[date] = None
    latest_lot = lots[-1]
    for lot in lots:
        lot_quantity = float(lot.get("quantity") or 0.0)
        lot_purchase_price = float(lot.get("purchase_price") or 0.0)
        lot_face_value = float(lot.get("face_value") or 0.0)
        total_quantity += lot_quantity
        total_purchase_basis += lot_quantity * lot_purchase_price
        total_face_value += lot_face_value
        lot_purchase_raw = lot.get("purchase_date")
        lot_purchase_dt = parse_flexible_date(lot_purchase_raw)
        lot_purchase_date = lot_purchase_dt.isoformat() if lot_purchase_dt else (str(lot_purchase_raw or "").strip() or None)
        if lot_purchase_dt is not None:
            if earliest_purchase_dt is None or lot_purchase_dt < earliest_purchase_dt:
                earliest_purchase_dt = lot_purchase_dt
                earliest_purchase_date = lot_purchase_dt.isoformat()
        elif lot_purchase_date and earliest_purchase_dt is None and (earliest_purchase_date is None or lot_purchase_date < earliest_purchase_date):
            earliest_purchase_date = lot_purchase_date

    weighted_purchase_price = total_purchase_basis / total_quantity if total_quantity else 0.0
    normalized_purchase_date = earliest_purchase_dt.isoformat() if earliest_purchase_dt else earliest_purchase_date
    provenance_payload = {}
    try:
        provenance_payload = json.loads(latest_lot.get("provenance_json") or "{}") or {}
    except Exception:
        provenance_payload = {}
    provenance_payload.update({
        "source_name": latest_lot.get("source_name"),
        "source_type": latest_lot.get("source_type"),
        "ingestion_method": latest_lot.get("ingestion_method"),
        "source_reference": latest_lot.get("source_reference"),
        "source_sheet_name": latest_lot.get("source_sheet_name"),
        "source_row_number": latest_lot.get("source_row_number"),
        "purchase_date": normalized_purchase_date,
        "timestamp_ingested": latest_lot.get("timestamp_ingested"),
        "timestamp_effective": latest_lot.get("timestamp_effective"),
    })
    payload = {
        "pid": portfolio_id,
        "isin": isin,
        "purchase_date": normalized_purchase_date,
        "quantity": total_quantity,
        "purchase_price": weighted_purchase_price,
        "face_value": total_face_value,
        "credit_rating": instrument.get("rating_latest"),
        "credit_rating_2": None,
        "rating_agency": instrument.get("rating_agency"),
        "rating_agency_2": None,
        "source_name": latest_lot.get("source_name"),
        "source_type": latest_lot.get("source_type"),
        "ingestion_method": latest_lot.get("ingestion_method"),
        "source_reference": latest_lot.get("source_reference"),
        "source_sheet_name": latest_lot.get("source_sheet_name"),
        "source_row_number": latest_lot.get("source_row_number"),
        "timestamp_ingested": latest_lot.get("timestamp_ingested"),
        "timestamp_effective": latest_lot.get("timestamp_effective"),
        "provenance_json": json.dumps(provenance_payload, default=str),
    }
    existing = await fetch_one(
        "SELECT id FROM portfolio_holdings WHERE portfolio_id = :pid AND isin = :isin",
        {"pid": portfolio_id, "isin": isin},
    )
    if existing:
        await execute_query(
            """UPDATE portfolio_holdings
               SET purchase_date = :purchase_date,
                   quantity = :quantity,
                   purchase_price = :purchase_price,
                   face_value = :face_value,
                   source_name = :source_name,
                   source_type = :source_type,
                   ingestion_method = :ingestion_method,
                   source_reference = :source_reference,
                   source_sheet_name = :source_sheet_name,
                   source_row_number = :source_row_number,
                   timestamp_ingested = :timestamp_ingested,
                   timestamp_effective = :timestamp_effective,
                   provenance_json = :provenance_json
               WHERE portfolio_id = :pid AND isin = :isin""",
            payload,
        )
    else:
        await execute_query(
            """INSERT INTO portfolio_holdings
               (portfolio_id, isin, purchase_date, quantity, purchase_price, face_value,
                source_name, source_type, ingestion_method, source_reference,
                source_sheet_name, source_row_number, timestamp_ingested, timestamp_effective, provenance_json)
               VALUES (:pid, :isin, :purchase_date, :quantity, :purchase_price, :face_value,
                :source_name, :source_type, :ingestion_method, :source_reference,
                :source_sheet_name, :source_row_number, :timestamp_ingested, :timestamp_effective, :provenance_json)""",
            payload,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# TRUST DIRECTORY
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/trusts")
async def list_trust_contexts(
    limit: int = Query(25, ge=1, le=100),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Return trust ids discovered from portfolio activity for the selector UI."""
    try:
        limit_value = int(limit)
    except Exception:
        limit_value = 25
    limit_value = max(1, min(limit_value, 100))

    try:
        rows = await fetch_all(
            """
            WITH trust_sources AS (
                SELECT
                    COALESCE(NULLIF(TRIM(trust_id), ''), NULLIF(TRIM(portfolio_id), ''), 'default') AS trust_id,
                    timestamp_ingested AS observed_at
                FROM portfolio_positions
                UNION ALL
                SELECT
                    COALESCE(NULLIF(TRIM(portfolio_id), ''), 'default') AS trust_id,
                    timestamp_ingested AS observed_at
                FROM portfolio_holdings
                UNION ALL
                SELECT
                    COALESCE(NULLIF(TRIM(trust_id), ''), NULLIF(TRIM(portfolio_id), ''), 'default') AS trust_id,
                    uploaded_at AS observed_at
                FROM portfolio_upload_batches
            )
            SELECT
                trust_id,
                COUNT(*) AS usage_count,
                MAX(observed_at) AS last_seen_at
            FROM trust_sources
            WHERE trust_id IS NOT NULL AND TRIM(trust_id) <> ''
            GROUP BY trust_id
            ORDER BY CASE WHEN trust_id = 'default' THEN 0 ELSE 1 END, last_seen_at DESC, trust_id ASC
            LIMIT :limit
            """,
            {"limit": limit},
        )
    except Exception as exc:
        log.warning("trust_directory_lookup_failed", error=str(exc))
        rows = []

    trusts = [
        {
            "trust_id": "default",
            "label": "Default trust",
            "usage_count": 0,
            "last_seen_at": None,
        }
    ]
    seen = {"default"}
    for row in rows:
        trust_id = str(row.get("trust_id") or "default").strip() or "default"
        if trust_id in seen:
            continue
        seen.add(trust_id)
        trusts.append(
            {
                "trust_id": trust_id,
                "label": trust_id,
                "usage_count": int(row.get("usage_count") or 0),
                "last_seen_at": row.get("last_seen_at"),
            }
        )

    return {
        "trusts": trusts[:limit_value],
        "count": len(trusts[:limit_value]),
    }

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN PORTFOLIO ENDPOINT — Full institutional analytics
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/")
async def get_portfolio(
    request: Request,
    trust_id: str = Query("default", description="Trust identifier"),
    user=Depends(require_role(Roles.VIEWER)),
):
    """
    Workbook-backed portfolio tab payload.
    The attached workbook snapshot is the source of truth for the UI.
    """
    try:
        snapshot = await _load_workbook_portfolio_snapshot(trust_id=trust_id)
        summary = dict(snapshot.get("summary", {}) or {})
        holdings = list(snapshot.get("holdings", []) or [])
        allocations = snapshot.get("allocations", {}) or {}
        validation_report = snapshot.get("validation_report", {}) or {}
        generated_at = snapshot.get("generated_at", "")

        try:
            await AuditService.record(
                user=user,
                action=AuditAction.PORTFOLIO_VIEW,
                entity_type=EntityType.PORTFOLIO,
                entity_id=str(trust_id or "default"),
                old_value=None,
                new_value={
                    "holdings_count": len(holdings),
                    "generated_at": generated_at,
                },
                affected_module="portfolio_v2",
                ip_address=(request.client.host if request.client else None),
                severity=AuditSeverity.INFO,
                compliance_flag=True,
                description="Portfolio snapshot viewed",
                metadata={
                    "trust_id": trust_id,
                    "route": "/portfolio/",
                },
            )
        except Exception:
            # Compliance logging must not block read-only portfolio access.
            pass

        return {
            "summary": summary,
            "holdings": holdings,
            "allocations": allocations,
            "grouped": snapshot.get("grouped", {}) or {},
            "primary_table": holdings,
            "positions": holdings,
            "expanded": {},
            "validation_report": validation_report,
            "generated_at": generated_at,
        }
    except Exception as e:
        log.error("portfolio_analytics_failed", error=str(e))
        raise HTTPException(500, f"Portfolio analytics computation failed: {str(e)}")


@router.get("/expanded/{isin}")
async def get_portfolio_expanded(
    isin: str,
    trust_id: str = Query("default", description="Trust identifier"),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Expanded portfolio row details for click-through view."""
    result = await build_portfolio_view(portfolio_id=trust_id)
    expanded = result.get("expanded", {}) or {}
    details = [value for key, value in expanded.items() if str(key).upper().startswith(f"{isin.upper()}:")]
    if not details:
        raise HTTPException(404, f"ISIN not found in portfolio: {isin}")
    return {
        "isin": isin.upper(),
        "details": details,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# SECTOR / RATING BREAKDOWN
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/analytics")
async def portfolio_analytics(
    trust_id: str = Query("default", description="Trust identifier"),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Full portfolio analytics summary — sector, rating, maturity, risk metrics."""
    result = await compute_full_portfolio_analytics(portfolio_id=trust_id)
    snapshot = await _load_workbook_portfolio_snapshot(trust_id=trust_id)
    snapshot_summary = snapshot.get("summary", {}) if snapshot else {}
    summary = dict(result.get("summary", {}) or {})
    workbook_weighted_ytm = snapshot_summary.get("weighted_ytm")
    if workbook_weighted_ytm is not None:
        summary["weighted_ytm"] = round(float(workbook_weighted_ytm), 4)
        summary["workbook_weighted_ytm"] = round(float(workbook_weighted_ytm), 4)
        if snapshot_summary.get("portfolio_ytm_yield_bearing_pct") is not None:
            summary["portfolio_ytm_yield_bearing_pct"] = round(float(snapshot_summary.get("portfolio_ytm_yield_bearing_pct") or 0.0), 4)
        if snapshot_summary.get("portfolio_ytm_all_rows_pct") is not None:
            summary["portfolio_ytm_all_rows_pct"] = round(float(snapshot_summary.get("portfolio_ytm_all_rows_pct") or 0.0), 4)
        summary["workbook_weighted_ytm_source"] = snapshot_summary.get("workbook_weighted_ytm_source") or "derived_excluding_sdl"
    workbook_coupon_weighted_yield = snapshot_summary.get("coupon_weighted_yield")
    if workbook_coupon_weighted_yield is not None:
        summary["coupon_weighted_yield"] = round(float(workbook_coupon_weighted_yield), 4)
        summary["workbook_coupon_weighted_yield"] = round(float(workbook_coupon_weighted_yield), 4)
        summary["portfolio_coupon_weighted_yield_pct"] = round(float(workbook_coupon_weighted_yield), 4)
    return {
        "summary": summary,
        "allocations": result.get("allocations", {}) or {},
        "validation_report": result.get("validation_report", {}),
        "generated_at": result.get("as_of", ""),
    }


@router.get("/sector-breakdown")
async def sector_breakdown(
    trust_id: str = Query("default", description="Trust identifier"),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Real sector and rating allocation from actual portfolio holdings."""
    result = await build_portfolio_view(portfolio_id=trust_id)
    holdings = result.get("holdings", [])
    expanded = result.get("expanded", {})
    total_mv = sum(float(h.get("current_value") or 0.0) for h in holdings)
    sector_map = {}
    rating_map = {}
    for h in holdings:
        holding_key = h.get("holding_key") or str(h.get("isin") or "")
        sec = (expanded.get(holding_key, {}).get("sector") or "UNKNOWN")
        rat = (h.get("rating") or "NR")
        mv = float(h.get("current_value") or 0.0)
        sector_map[sec] = sector_map.get(sec, 0.0) + mv
        rating_map[rat] = rating_map.get(rat, 0.0) + mv

    sectors = [
        {"name": k, "value": round(v, 6), "pct": round((v / total_mv * 100.0), 6) if total_mv else 0.0}
        for k, v in sorted(sector_map.items(), key=lambda x: -x[1])
    ]
    ratings = [
        {"name": k, "value": round(v, 6), "pct": round((v / total_mv * 100.0), 6) if total_mv else 0.0}
        for k, v in sorted(rating_map.items(), key=lambda x: -x[1])
    ]
    return {
        "sectors": sectors,
        "ratings": ratings,
        "total_market_value": round(total_mv, 6),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# CONCENTRATION ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/concentration")
async def concentration_risk(
    trust_id: str = Query("default", description="Trust identifier"),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Concentration risk — top issuer, sector, rating exposures + maturity ladder."""
    result = await build_portfolio_view(portfolio_id=trust_id)
    holdings = result.get("holdings", [])
    expanded = result.get("expanded", {})
    total_mv = sum(float(h.get("current_value") or 0.0) for h in holdings)
    issuer_map = {}
    sector_map = {}
    rating_map = {}
    for h in holdings:
        holding_key = h.get("holding_key") or str(h.get("isin") or "")
        info = expanded.get(holding_key, {})
        issuer = info.get("issuer") or "UNKNOWN"
        sector = info.get("sector") or "UNKNOWN"
        rating = h.get("rating") or "NR"
        mv = float(h.get("current_value") or 0.0)
        issuer_map[issuer] = issuer_map.get(issuer, 0.0) + mv
        sector_map[sector] = sector_map.get(sector, 0.0) + mv
        rating_map[rating] = rating_map.get(rating, 0.0) + mv

    top_issuers = [
        {"name": n, "value": round(v, 6), "pct": round((v / total_mv * 100.0), 6) if total_mv else 0.0}
        for n, v in sorted(issuer_map.items(), key=lambda x: -x[1])[:10]
    ]
    top_sectors = [
        {"name": n, "value": round(v, 6), "pct": round((v / total_mv * 100.0), 6) if total_mv else 0.0}
        for n, v in sorted(sector_map.items(), key=lambda x: -x[1])
    ]
    ratings = [
        {"name": n, "value": round(v, 6), "pct": round((v / total_mv * 100.0), 6) if total_mv else 0.0}
        for n, v in sorted(rating_map.items(), key=lambda x: -x[1])
    ]

    payload = {
        "total_market_value": round(total_mv, 6),
        "top_issuers": top_issuers,
        "top_sectors": top_sectors,
        "rating_breakdown": ratings,
        "maturity_ladder": [],
        "top_issuer_exposure_pct": top_issuers[0]["pct"] if top_issuers else 0,
        "top_sector_exposure_pct": top_sectors[0]["pct"] if top_sectors else 0,
        "threshold_alerts": [],
    }
    if not holdings or total_mv <= 0:
        return _state_payload("no_data", payload, "No portfolio data available. Run Full Scan to generate risk metrics.")
    return _state_payload("ok", payload)


# ═══════════════════════════════════════════════════════════════════════════════
# HEALTH SCORE
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/health")
async def portfolio_health(
    trust_id: str = Query("default", description="Trust identifier"),
    user=Depends(require_role(Roles.VIEWER)),
):
    """100-point health score with component breakdown, strengths, weaknesses, suggestions."""
    result = await build_portfolio_view(portfolio_id=trust_id)
    summary = dict(result.get("summary", {}))
    health = result.get("health", {}) or {}
    report = result.get("validation_report", {})
    holding_count = len(result.get("holdings", []))
    stale_count = int(report.get("stale_price_count") or 0)
    missing_rating_count = int(report.get("missing_rating_count") or 0)
    calc_error_count = int(report.get("calculation_error_count") or 0)
    total_penalty = stale_count * 8 + missing_rating_count * 5 + calc_error_count * 15
    total_score = max(0, 100 - total_penalty)
    grade = "A"
    if total_score < 90:
        grade = "B"
    if total_score < 80:
        grade = "C"
    if total_score < 70:
        grade = "D"
    if total_score < 60:
        grade = "F"

    return {
        "health": {
            "total_score": total_score,
            "grade": grade,
            "components": {
                "holdings_coverage": max(0, 100 - (0 if holding_count > 0 else 100)),
                "market_data_freshness": max(0, 100 - stale_count * 10),
                "rating_completeness": max(0, 100 - missing_rating_count * 10),
                "calculation_integrity": max(0, 100 - calc_error_count * 20),
            },
        },
        "summary": {
            "score": total_score,
            "grade": grade,
            "portfolio_rating": health.get("grade") or summary.get("portfolio_rating") or "NR",
            "weighted_ytm": summary.get("weighted_ytm") or 0.0,
            "weighted_duration": summary.get("weighted_duration") or summary.get("weighted_modified_duration") or 0.0,
            "aum_crores": summary.get("aum_crores") or 0.0,
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# MATURITY LADDER
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/maturity-ladder")
async def maturity_ladder(
    trust_id: str = Query("default", description="Trust identifier"),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Month-by-month maturity schedule for the next 36 months."""
    rows = await fetch_all(
        """
        WITH active_positions AS (
            SELECT p.isin, p.quantity, p.face_value
            FROM portfolio_positions p
            WHERE p.trust_id = :pid
            UNION ALL
            SELECT h.isin, h.quantity, h.face_value
            FROM portfolio_holdings h
            WHERE h.portfolio_id = :pid
              AND NOT EXISTS (SELECT 1 FROM portfolio_positions p WHERE p.trust_id = :pid)
        )
        SELECT
            i.maturity_date,
            SUM(ap.quantity * COALESCE(ap.face_value, ubm.face_value, 100.0)) AS maturity_face_value
        FROM active_positions ap
        INNER JOIN instrument_master i ON i.isin = ap.isin
        LEFT JOIN universal_bond_master ubm ON ubm.isin = ap.isin
        GROUP BY i.maturity_date
        ORDER BY i.maturity_date ASC
        """,
        {"pid": trust_id},
    )

    ladder = []
    buckets = {
        "<1Y": 0.0,
        "1-3Y": 0.0,
        "3-5Y": 0.0,
        "5-7Y": 0.0,
        ">7Y": 0.0,
    }
    today = date.today()
    for row in rows:
        maturity_raw = str(row.get("maturity_date") or "")
        if len(maturity_raw) < 10:
            continue
        month = maturity_raw[:7]
        value = float(row.get("maturity_face_value") or 0.0)
        ladder.append({"month": month, "maturity_face_value": round(value, 6)})
        try:
            mat_dt = date.fromisoformat(maturity_raw[:10])
            years = (mat_dt - today).days / 365.25
            if years < 1:
                buckets["<1Y"] += value
            elif years < 3:
                buckets["1-3Y"] += value
            elif years < 5:
                buckets["3-5Y"] += value
            elif years < 7:
                buckets["5-7Y"] += value
            else:
                buckets[">7Y"] += value
        except Exception:
            continue

    maturity_buckets = [{"name": k, "value": round(v, 6)} for k, v in buckets.items()]
    return {
        "maturity_ladder": ladder,
        "maturity_buckets": maturity_buckets,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# CASH FLOW SCHEDULE
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/cash-flows")
async def cash_flows(
    trust_id: str = Query("default", description="Trust identifier"),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Coupon and principal cash flow schedule (monthly aggregation + detail)."""
    rows = await fetch_all(
        """
        WITH active_positions AS (
            SELECT p.isin, p.quantity
            FROM portfolio_positions p
            WHERE p.trust_id = :pid
            UNION ALL
            SELECT h.isin, h.quantity
            FROM portfolio_holdings h
            WHERE h.portfolio_id = :pid
              AND NOT EXISTS (SELECT 1 FROM portfolio_positions p WHERE p.trust_id = :pid)
        )
        SELECT
            c.payment_date,
            SUM(c.coupon_amount * ap.quantity) AS coupon_total,
            SUM(c.principal_amount * ap.quantity) AS principal_total
        FROM cashflows c
        INNER JOIN active_positions ap ON ap.isin = c.isin
        GROUP BY c.payment_date
        ORDER BY c.payment_date ASC
        """,
        {"pid": trust_id},
    )

    detail = []
    monthly = {}
    for row in rows:
        payment_date = str(row.get("payment_date") or "")
        coupon_total = float(row.get("coupon_total") or 0.0)
        principal_total = float(row.get("principal_total") or 0.0)
        total = coupon_total + principal_total
        detail.append(
            {
                "payment_date": payment_date,
                "coupon_total": round(coupon_total, 6),
                "principal_total": round(principal_total, 6),
                "total_cash_flow": round(total, 6),
            }
        )
        month = payment_date[:7] if len(payment_date) >= 7 else "UNKNOWN"
        bucket = monthly.setdefault(month, {"coupon_total": 0.0, "principal_total": 0.0, "total_cash_flow": 0.0})
        bucket["coupon_total"] += coupon_total
        bucket["principal_total"] += principal_total
        bucket["total_cash_flow"] += total

    monthly_schedule = [
        {
            "month": month,
            "coupon_total": round(vals["coupon_total"], 6),
            "principal_total": round(vals["principal_total"], 6),
            "total_cash_flow": round(vals["total_cash_flow"], 6),
        }
        for month, vals in sorted(monthly.items(), key=lambda x: x[0])
    ]

    return {
        "monthly_schedule": monthly_schedule,
        "detail": detail,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# BOND HEALTH STATUS
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/bond-health")
async def bond_health(
    trust_id: str = Query("default", description="Trust identifier"),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Per-bond health check: HEALTHY / MONITOR / RISK with issue details."""
    result = await build_portfolio_view(portfolio_id=trust_id)
    expanded = result.get("expanded", {})
    holdings = result.get("holdings", [])
    today = date.today()
    out = []
    for h in holdings:
        holding_key = h.get("holding_key") or str(h.get("isin") or "")
        details = expanded.get(holding_key, {})
        issues = []
        if h.get("stale_price_flag"):
            issues.append("STALE_PRICE")
        if h.get("missing_rating_flag"):
            issues.append("MISSING_RATING")
        if h.get("calculation_error_flag"):
            issues.append("CALCULATION_ERROR")
        maturity_raw = str(details.get("maturity_date") or "")
        try:
            years = (date.fromisoformat(maturity_raw[:10]) - today).days / 365.25
            if years < 0.5:
                issues.append("NEAR_MATURITY")
        except Exception:
            pass

        status = "HEALTHY"
        if len(issues) >= 1:
            status = "MONITOR"
        if "CALCULATION_ERROR" in issues or len(issues) >= 3:
            status = "RISK"
        out.append(
            {
                "isin": h.get("isin"),
                "issuer": details.get("issuer") or "UNKNOWN",
                "status": status,
                "issues": issues,
            }
        )

    counts = {"HEALTHY": 0, "MONITOR": 0, "RISK": 0}
    for row in out:
        counts[row["status"]] += 1
    return {
        "summary": counts,
        "bonds": out,
        "as_of": result.get("as_of"),
    }


@router.get("/regulatory-audit")
async def regulatory_audit(user=Depends(require_role(Roles.AUDITOR))):
    """Regulatory investment-pattern and concentration audit built from live holdings."""
    try:
        return await PortfolioDatasetService.build_regulatory_audit()
    except Exception as e:
        log.error("regulatory_audit_failed", error=str(e))
        raise HTTPException(500, f"Regulatory audit generation failed: {str(e)}")


@router.get("/trust-layer-audit")
async def trust_layer_audit(user=Depends(require_role(Roles.AUDITOR))):
    """Field-level audit classification and derived-storage removal report."""
    try:
        return build_portfolio_field_audit()
    except Exception as e:
        log.error("trust_layer_audit_failed", error=str(e))
        raise HTTPException(500, f"Trust layer audit generation failed: {str(e)}")


@router.get("/excel-system-validation")
async def excel_system_validation(
    trust_id: str = Query("default", description="Trust identifier"),
    tolerance_pct: float = Query(0.1, ge=0.0, le=10.0, description="Tolerance percentage"),
    user=Depends(require_role(Roles.AUDITOR)),
):
    """Validate workbook rows vs persisted positions at row, bond, and portfolio levels."""
    if not WORKBOOK_PATH.exists():
        raise HTTPException(404, f"Workbook file not found: {WORKBOOK_PATH}")
    try:
        return await PortfolioDatasetService.build_excel_system_validation_report(
            workbook_content=WORKBOOK_PATH.read_bytes(),
            workbook_filename=WORKBOOK_PATH.name,
            workbook_content_type="application/vnd.ms-excel",
            trust_id=trust_id,
            tolerance_pct=tolerance_pct,
            source_reference=WORKBOOK_PATH.name,
        )
    except Exception as e:
        log.error("excel_system_validation_failed", error=str(e), trust_id=trust_id)
        raise HTTPException(500, f"Validation report generation failed: {str(e)}")


# ═══════════════════════════════════════════════════════════════════════════════
# TRADE LOG & EXECUTION
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/trades")
async def trade_history(
    limit: int = Query(100, le=500),
    trust_id: str = Query("default", description="Trust identifier"),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Return complete trade execution log."""
    try:
        await execute_query("ALTER TABLE trade_log ADD COLUMN portfolio_id TEXT", {})
    except Exception:
        pass
    rows = await fetch_all(
        "SELECT * FROM trade_log WHERE COALESCE(portfolio_id, 'default') = :pid ORDER BY COALESCE(trade_date, '') DESC LIMIT :limit",
        {"limit": limit, "pid": trust_id},
    )
    return {"trades": rows, "count": len(rows)}


class TradeIn(BaseModel):
    isin: str
    action: str  # BUY | SELL
    quantity: int
    price: float
    date: str
    broker: Optional[str] = None
    notes: Optional[str] = None
    trust_id: Optional[str] = "default"


@router.post("/trade")
async def execute_trade(trade: TradeIn, user=Depends(require_role(Roles.PORTFOLIO))):
    """Execute a trade with mandatory pre-trade compliance check for BUY orders."""
    trade_isin = str(trade.isin or "").strip().upper()
    trade_action = str(trade.action or "").strip().upper()
    trade_date_dt = parse_flexible_date(trade.date)
    if trade_date_dt is None:
        raise HTTPException(400, "Invalid trade date. Use a valid date (e.g. YYYY-MM-DD, DD/MM/YYYY, or Excel serial).")
    trade_date_iso = trade_date_dt.isoformat()
    trade_reference = (trade.notes or "").strip() or "trade_execution"

    log.info("execute_trade", isin=trade_isin, action=trade_action, qty=trade.quantity)

    portfolio_id = (trade.trust_id or "default").strip() or "default"

    # ── Pre-trade compliance gate (BUY orders only) ──────────────────
    compliance_result = None
    compliance_overall = None
    compliance_score = None
    compliance_rules_checked = 0
    if trade_action == "BUY":
        from ..api.compliance import PreTradeRequest, pre_trade_check
        face_value_cr = (trade.quantity * (trade.price or 100)) / 1e7  # convert to crores
        try:
            compliance_result = await pre_trade_check(
                PreTradeRequest(
                    isin=trade_isin,
                    face_value_cr=face_value_cr,
                    trust_id=portfolio_id,
                    portfolio_name=portfolio_id,
                ),
                user=user,
            )
            compliance_payload = compliance_result if isinstance(compliance_result, dict) else {}
            compliance_overall = (
                getattr(compliance_result, "overall", None)
                or compliance_payload.get("overall")
                or "UNKNOWN"
            )
            compliance_score = getattr(compliance_result, "score", None)
            if compliance_score is None:
                compliance_score = compliance_payload.get("score")
            compliance_rules_checked = getattr(compliance_result, "rules_checked", None)
            if compliance_rules_checked is None:
                compliance_rules_checked = compliance_payload.get("rules_checked") or 0
            compliance_violations = getattr(compliance_result, "violations", None) or compliance_payload.get("violations") or []
            if compliance_overall == "FAIL":
                fails = [
                    v for v in compliance_violations
                    if (v.get("status") if isinstance(v, dict) else getattr(v, "status", None)) == "FAIL"
                ]
                fail_msgs = "; ".join(
                    (v.get("message") if isinstance(v, dict) else getattr(v, "message", ""))
                    for v in fails[:3]
                )
                # AUDIT SAFETY: Compliance failures are HARD BLOCKS.
                # No override mechanism allowed — all trades must pass compliance.
                raise HTTPException(
                    400,
                    f"Pre-trade compliance FAILED: {fail_msgs}. "
                    f"Trade blocked. Resolve compliance violations before proceeding."
                )
        except HTTPException:
            raise
        except Exception as e:
            log.error("compliance_check_failed", isin=trade_isin, error=str(e))
            raise HTTPException(
                status_code=500,
                detail=f"Pre-trade compliance check failed for {trade_isin}: {str(e)}. Trade blocked for safety.",
            )

    await ensure_portfolio_core_schema()
    try:
        await execute_query("ALTER TABLE trade_log ADD COLUMN portfolio_id TEXT", {})
    except Exception:
        pass

    existing = await fetch_one(
        """
        SELECT SUM(quantity) AS total_qty
        FROM portfolio_positions
        WHERE portfolio_id = :pid AND isin = :isin
        """,
        {"pid": portfolio_id, "isin": trade_isin},
    )
    issuer_row = await fetch_one("SELECT issuer FROM instrument_master WHERE isin = :isin", {"isin": trade_isin})
    issuer_name = issuer_row["issuer"] if issuer_row else trade_isin
    face_value = 1000.0

    if trade_action == "BUY":
        if not issuer_row:
            master = await fetch_one("SELECT * FROM universal_bond_master WHERE isin = :isin", {"isin": trade_isin})
            if not master:
                raise HTTPException(404, "ISIN not found in master")
            issuer_name = master["issuer_name"]
            face_value = float(master.get("face_value") or 1000.0)
            await execute_query(
                """
                INSERT INTO instrument_master (
                    isin, instrument_name, issuer, coupon_rate, maturity_date,
                    put_date, call_date, sector, category, rating_latest, rating_agency,
                    payment_frequency, day_count_convention
                ) VALUES (
                    :isin, :instrument_name, :issuer, :coupon_rate, :maturity_date,
                    NULL, NULL, :sector, :category, :rating_latest, :rating_agency,
                    2, 'ACT/365'
                )
                ON CONFLICT(isin) DO NOTHING
                """,
                {
                    "isin": trade_isin,
                    "instrument_name": master.get("bond_name") or master.get("issuer_name") or trade_isin,
                    "issuer": master.get("issuer_name") or trade_isin,
                    "coupon_rate": master.get("coupon_rate") or 0.0,
                    "maturity_date": master.get("maturity_date") or trade_date_iso,
                    "sector": master.get("sector") or "",
                    "category": master.get("issuer_type") or "",
                    "rating_latest": master.get("credit_rating") or "",
                    "rating_agency": master.get("rating_agency") or "",
                },
            )
        else:
            master = await fetch_one("SELECT face_value FROM universal_bond_master WHERE isin = :isin", {"isin": trade_isin}) or {}
            face_value = float(master.get("face_value") or 1000.0)

        timestamp_ingested = utcnow_iso()

        await execute_query(
            """
            INSERT INTO portfolio_positions (
                unique_lot_id, trust_id, portfolio_id, isin, quantity, face_value,
                book_value, market_price, clean_price, accrued_interest,
                purchase_price, purchase_date, issuer_name, source_name,
                source_type, ingestion_method, source_reference,
                timestamp_ingested, timestamp_effective, provenance_json
            ) VALUES (
                :unique_lot_id, :trust_id, :portfolio_id, :isin, :quantity, :face_value,
                :book_value, :market_price, :clean_price, :accrued_interest,
                :purchase_price, :purchase_date, :issuer_name, :source_name,
                :source_type, :ingestion_method, :source_reference,
                :timestamp_ingested, :timestamp_effective, :provenance_json
            )
            """,
            {
                "unique_lot_id": f"trade:{portfolio_id}:{trade_isin}:{trade_date_iso}:{uuid.uuid4().hex}",
                "trust_id": portfolio_id,
                "portfolio_id": portfolio_id,
                "isin": trade_isin,
                "quantity": trade.quantity,
                "face_value": face_value,
                "book_value": (trade.price / 100.0) * face_value * trade.quantity,
                "market_price": (trade.price / 100.0) * face_value,
                "clean_price": (trade.price / 100.0) * face_value,
                "accrued_interest": 0.0,
                "issuer_name": issuer_name,
                "purchase_date": trade_date_iso,
                "purchase_price": trade.price,
                "source_name": "Trade Log",
                "source_type": "client_provided",
                "ingestion_method": "manual_trade",
                "source_reference": trade_reference,
                "timestamp_ingested": timestamp_ingested,
                "timestamp_effective": trade_date_iso,
                "provenance_json": json.dumps({
                    "source_name": "Trade Log",
                    "source_type": "client_provided",
                    "ingestion_method": "manual_trade",
                    "source_reference": trade_reference,
                    "purchase_date": trade_date_iso,
                    "timestamp_ingested": timestamp_ingested,
                    "timestamp_effective": trade_date_iso,
                }, default=str),
            },
        )
        await _refresh_trade_holding_snapshot(portfolio_id, trade_isin)

    elif trade_action == "SELL":
        total_qty = float((existing or {}).get("total_qty") or 0.0)
        if total_qty < trade.quantity:
            raise HTTPException(400, "Insufficient holdings")
        lots = await fetch_all(
            """
            SELECT id, quantity
            FROM portfolio_positions
            WHERE portfolio_id = :pid AND isin = :isin
            ORDER BY purchase_date ASC, id ASC
            """,
            {"pid": portfolio_id, "isin": trade_isin},
        )
        remaining = float(trade.quantity)
        for lot in lots:
            if remaining <= 0:
                break
            lot_qty = float(lot.get("quantity") or 0.0)
            if lot_qty <= remaining + 1e-12:
                await execute_query("DELETE FROM portfolio_positions WHERE id = :id", {"id": lot["id"]})
                remaining -= lot_qty
            else:
                new_qty = lot_qty - remaining
                remaining = 0.0
                await execute_query(
                    "UPDATE portfolio_positions SET quantity = :qty, book_value = COALESCE(book_value, 0) * (:qty / quantity) WHERE id = :id",
                    {"qty": new_qty, "id": lot["id"]},
                )
        if remaining > 1e-9:
            raise HTTPException(500, "Sell allocation failed due to inconsistent lots")
        await _refresh_trade_holding_snapshot(portfolio_id, trade_isin)

    else:
        raise HTTPException(400, "Action must be BUY or SELL")

    if trade_action == "BUY":
        try:
            market_timestamp = utcnow_iso()
            await execute_query(
                """
                INSERT INTO market_prices (
                    isin, date, price, yield, source,
                    source_name, source_type, ingestion_method, source_reference,
                    timestamp_ingested, timestamp_effective, provenance_json
                ) VALUES (
                    :isin, :date, :price, NULL, 'TRADE',
                    :source_name, :source_type, :ingestion_method, :source_reference,
                    :timestamp_ingested, :timestamp_effective, :provenance_json
                )
                ON CONFLICT(isin, date, source) DO UPDATE SET price = excluded.price
                """,
                {
                    "isin": trade_isin,
                    "date": trade_date_iso,
                    "price": trade.price,
                    "source_name": "Trade Log",
                    "source_type": "client_provided",
                    "ingestion_method": "manual_upload",
                    "source_reference": trade_reference,
                    "timestamp_ingested": market_timestamp,
                    "timestamp_effective": trade_date_iso,
                    "provenance_json": json.dumps({
                        "source_name": "Trade Log",
                        "source_type": "client_provided",
                        "ingestion_method": "manual_upload",
                        "source_reference": trade_reference,
                        "purchase_date": trade_date_iso,
                        "timestamp_ingested": market_timestamp,
                        "timestamp_effective": trade_date_iso,
                    }, default=str),
                },
            )
        except Exception:
            pass

    # Log to trade_log
    await execute_query(
        """
        INSERT INTO trade_log (portfolio_id, isin, issuer_name, action, quantity, price, total_value, trade_date, broker, notes)
        VALUES (:pid, :isin, :iss, :action, :qty, :price, :total, :date, :broker, :notes)
        """,
        {
            "pid": portfolio_id,
            "isin": trade_isin, "iss": issuer_name,
            "action": trade_action, "qty": trade.quantity,
            "price": trade.price, "total": trade.price * trade.quantity,
            "date": trade_date_iso, "broker": trade.broker,
            "notes": trade.notes,
        },
    )

    return {
        "status": "success",
        "message": f"Trade {trade_action} executed for {trade_isin}",
        "compliance": {
            "overall": compliance_overall if compliance_overall is not None else "N/A",
            "score": compliance_score,
            "rules_checked": compliance_rules_checked,
        } if trade_action == "BUY" else None,
    }

