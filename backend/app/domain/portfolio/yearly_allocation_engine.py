"""Yearly allocation tracking for inflow-aware institutional recommendation workflows."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from app.shared.database import IS_SQLITE, execute_query, fetch_all, fetch_one

TARGET_RANGES = {
    "GOVT": {"min": 45.0, "max": 65.0, "mid": 55.0},
    "PSU_PRIVATE_DEBT": {"min": 35.0, "max": 55.0, "mid": 45.0},
    "EQUITY": {"min": 5.0, "max": 15.0, "mid": 10.0},
}

CATEGORY_WEIGHT_KEYS = ("SDS", "CENTRAL_STATE", "PSU", "PRIVATE", "EQUITY")


def _financial_year(as_of: date) -> str:
    start_year = as_of.year if as_of.month >= 4 else as_of.year - 1
    return f"{start_year}-{start_year + 1}"


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


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


def _blank_category_totals() -> dict[str, float]:
    return {key: 0.0 for key in CATEGORY_WEIGHT_KEYS}


def _encode_weight_map(values: dict[str, float]) -> str:
    return json.dumps({key: round(_safe_float(values.get(key)), 4) for key in CATEGORY_WEIGHT_KEYS}, sort_keys=True)


def _decode_weight_map(value: Any) -> dict[str, float]:
    if isinstance(value, dict):
        raw = value
    elif not value:
        raw = {}
    else:
        try:
            raw = json.loads(str(value))
        except Exception:
            raw = {}
    out = _blank_category_totals()
    for key in CATEGORY_WEIGHT_KEYS:
        out[key] = _safe_float(raw.get(key))
    return out


def _portfolio_category_key(position: dict[str, Any]) -> str:
    explicit = str(
        position.get("portfolio_category")
        or position.get("portfolio_category_key")
        or position.get("portfolio_bucket")
        or ""
    ).upper().strip()
    if explicit in CATEGORY_WEIGHT_KEYS:
        return explicit
    legacy = str(position.get("instrument_category") or position.get("category") or position.get("allocation_bucket") or "").upper().strip()
    if legacy in {"SDS", "STATIC", "LOCKED", "NON_TRADABLE", "NON-TRADABLE"}:
        return "SDS"
    if legacy in {"CENTRAL_STATE", "STATE_CENTRAL", "GOVT", "CENTRAL", "STATE"}:
        return "CENTRAL_STATE"
    if legacy == "PSU":
        return "PSU"
    if legacy == "PRIVATE":
        return "PRIVATE"
    if legacy == "EQUITY":
        return "EQUITY"
    return "PRIVATE"


def _bucket_for_position(position: dict[str, Any]) -> str:
    portfolio_category = _portfolio_category_key(position)
    if portfolio_category in {"SDS", "CENTRAL_STATE"}:
        return "GOVT"
    if portfolio_category in {"PSU", "PRIVATE"}:
        return "PSU_PRIVATE_DEBT"
    if portfolio_category == "EQUITY":
        return "EQUITY"

    issuer_type = str(position.get("issuer_type") or "").upper()
    sector = str(position.get("sector") or "").upper()
    category = str(position.get("allocation_bucket") or position.get("category") or "").upper()
    name = str(position.get("issuer_name") or position.get("instrument_name") or position.get("bond_name") or "").upper()

    if "EQUITY" in category or "EQUITY" in sector or any(token in name for token in ("FUND", "ETF", "EQUITY", "BLUE CHIP", "BALANCED ADVANTAGE", "FLEXICAP", "LARGE CAP", "MID CAP", "SMALL CAP", "INDEX")):
        return "EQUITY"
    if issuer_type in {"G-SEC", "SDL", "SOVEREIGN", "CENTRAL_GOVT", "STATE_GOVT", "T-BILL"}:
        return "GOVT"
    if any(token in category for token in ("GOV", "SOV", "SDL", "G-SEC", "GSEC", "TREASURY", "TBILL")):
        return "GOVT"
    if any(token in sector for token in ("GOV", "SOV", "SDL", "G-SEC", "GSEC", "TREASURY", "TBILL")):
        return "GOVT"
    if any(token in category for token in ("PSU", "PRIVATE", "CORPORATE", "BANK", "NBFC")):
        return "PSU_PRIVATE_DEBT"
    if any(token in sector for token in ("PSU", "PRIVATE", "CORPORATE", "BANK", "NBFC")):
        return "PSU_PRIVATE_DEBT"
    return "PSU_PRIVATE_DEBT"


def _sheet_bucket_to_internal(category_bucket: str | None) -> str:
    text = str(category_bucket or "").upper()
    if "EQUITY" in text or "AUM ONLY" in text or "10%" in text:
        return "EQUITY"
    if any(k in text for k in ("CENTRAL", "STATE", "GOV", "SDL", "SOV", "G-SEC", "GSEC", "45% TO 65%")):
        return "GOVT"
    return "PSU_PRIVATE_DEBT"


async def _sheet_bucket_totals() -> dict[str, float]:
    try:
        rows = await fetch_all(
            """
            SELECT category_bucket, SUM(COALESCE(basis_value, 0)) AS basis
            FROM portfolio_sheet_allocations
            GROUP BY category_bucket
            """,
            {},
        )
    except Exception:
        return {}

    totals = {"GOVT": 0.0, "PSU_PRIVATE_DEBT": 0.0, "EQUITY": 0.0}
    for row in rows:
        bucket = _sheet_bucket_to_internal(row.get("category_bucket"))
        totals[bucket] += _safe_float(row.get("basis"))

    if not any(totals.values()):
        return {}
    return totals


def _bucket_totals(positions: list[dict[str, Any]]) -> dict[str, float]:
    totals = {"GOVT": 0.0, "PSU_PRIVATE_DEBT": 0.0, "EQUITY": 0.0}
    for row in positions:
        bucket = _bucket_for_position(row)
        totals[bucket] += _safe_float(
            row.get("investment_value")
            or row.get("allocation_amount")
            or row.get("market_value")
        )
    return totals


def _pct(totals: dict[str, float], key: str, denominator: float) -> float:
    if denominator <= 0.0:
        return 0.0
    return round((totals.get(key, 0.0) / denominator) * 100.0, 4)


def _category_totals(positions: list[dict[str, Any]]) -> dict[str, float]:
    totals = _blank_category_totals()
    for row in positions:
        key = _portfolio_category_key(row)
        totals[key] += _safe_float(
            row.get("book_value")
            or row.get("investment_value")
            or row.get("current_value")
            or row.get("allocation_amount")
        )
    return totals


async def _ensure_tables() -> None:
    await execute_query(
        """
        CREATE TABLE IF NOT EXISTS yearly_allocation_state (
            portfolio_id TEXT NOT NULL,
            fiscal_year TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            total_invested_amount REAL NOT NULL DEFAULT 0,
            govt_amount REAL NOT NULL DEFAULT 0,
            psu_private_amount REAL NOT NULL DEFAULT 0,
            equity_amount REAL NOT NULL DEFAULT 0,
            govt_pct REAL NOT NULL DEFAULT 0,
            psu_private_pct REAL NOT NULL DEFAULT 0,
            equity_pct REAL NOT NULL DEFAULT 0,
            category_amounts_json TEXT,
            category_percentages_json TEXT,
            category_source TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (portfolio_id, fiscal_year)
        )
        """
    )
    for column_name, column_type in (
        ("category_amounts_json", "TEXT"),
        ("category_percentages_json", "TEXT"),
        ("category_source", "TEXT"),
    ):
        if not await _table_has_column("yearly_allocation_state", column_name):
            await execute_query(f"ALTER TABLE yearly_allocation_state ADD COLUMN {column_name} {column_type}", {})
    await execute_query(
        """
        CREATE TABLE IF NOT EXISTS yearly_allocation_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            portfolio_id TEXT NOT NULL,
            fiscal_year TEXT NOT NULL,
            event_date TEXT NOT NULL,
            inflow_amount REAL NOT NULL,
            selected_bucket TEXT,
            strategy_phase TEXT,
            govt_allocated REAL NOT NULL DEFAULT 0,
            psu_private_allocated REAL NOT NULL DEFAULT 0,
            equity_allocated REAL NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await execute_query(
        "CREATE INDEX IF NOT EXISTS idx_yearly_alloc_events_pf_fy ON yearly_allocation_events(portfolio_id, fiscal_year)"
    )


async def get_yearly_allocation_state(
    portfolio_id: str,
    current_positions: list[dict[str, Any]],
    as_of_date: date,
) -> dict[str, Any]:
    fy = _financial_year(as_of_date)
    totals = _bucket_totals(current_positions)
    category_totals = _category_totals(current_positions)
    sheet_totals = await _sheet_bucket_totals()
    source = "derived_from_holdings"
    if not any(totals.values()) and sheet_totals:
        totals = sheet_totals
        source = "derived_from_sheet_allocations"
    total_invested = sum(category_totals.values()) or sum(totals.values())
    category_source = "derived_from_holdings"
    if not any(category_totals.values()):
        category_source = "unavailable"
    state = {
        "portfolio_id": portfolio_id,
        "fiscal_year": fy,
        "as_of_date": as_of_date.isoformat(),
        "total_invested_amount": round(total_invested, 2),
        "bucket_amounts": totals,
        "current_allocation_percentages": {
            "GOVT": _pct(totals, "GOVT", total_invested),
            "PSU_PRIVATE_DEBT": _pct(totals, "PSU_PRIVATE_DEBT", total_invested),
            "EQUITY": _pct(totals, "EQUITY", total_invested),
        },
        "category_amounts": category_totals,
        "current_category_percentages": {
            key: _pct(category_totals, key, total_invested) for key in CATEGORY_WEIGHT_KEYS
        },
        "target_ranges": TARGET_RANGES,
        "source": source,
        "category_source": category_source,
    }
    try:
        await _ensure_tables()
        row = await fetch_one(
            """
            SELECT *
            FROM yearly_allocation_state
            WHERE portfolio_id = :portfolio_id AND fiscal_year = :fiscal_year
            """,
            {"portfolio_id": portfolio_id, "fiscal_year": fy},
        )
        if row and not any(totals.values()) and not any(category_totals.values()):
            persisted_category_amounts = _decode_weight_map(row.get("category_amounts_json"))
            persisted_category_percentages = _decode_weight_map(row.get("category_percentages_json"))
            return {
                "portfolio_id": portfolio_id,
                "fiscal_year": fy,
                "as_of_date": str(row.get("as_of_date") or as_of_date.isoformat()),
                "total_invested_amount": _safe_float(row.get("total_invested_amount")),
                "bucket_amounts": {
                    "GOVT": _safe_float(row.get("govt_amount")),
                    "PSU_PRIVATE_DEBT": _safe_float(row.get("psu_private_amount")),
                    "EQUITY": _safe_float(row.get("equity_amount")),
                },
                "current_allocation_percentages": {
                    "GOVT": _safe_float(row.get("govt_pct")),
                    "PSU_PRIVATE_DEBT": _safe_float(row.get("psu_private_pct")),
                    "EQUITY": _safe_float(row.get("equity_pct")),
                },
                "category_amounts": persisted_category_amounts,
                "current_category_percentages": persisted_category_percentages,
                "target_ranges": TARGET_RANGES,
                "source": "persistent_state",
                "category_source": str(row.get("category_source") or "persistent_state"),
            }
        await upsert_yearly_allocation_state(state)
    except Exception:
        # Keep recommendation flow resilient even when tracking persistence is unavailable.
        pass
    return state


async def upsert_yearly_allocation_state(state: dict[str, Any]) -> None:
    bucket_amounts = state.get("bucket_amounts") or {}
    pcts = state.get("current_allocation_percentages") or {}
    category_amounts = state.get("category_amounts") or {}
    category_percentages = state.get("current_category_percentages") or {}
    await execute_query(
        """
        INSERT INTO yearly_allocation_state (
            portfolio_id, fiscal_year, as_of_date, total_invested_amount,
            govt_amount, psu_private_amount, equity_amount,
            govt_pct, psu_private_pct, equity_pct,
            category_amounts_json, category_percentages_json, category_source,
            updated_at
        ) VALUES (
            :portfolio_id, :fiscal_year, :as_of_date, :total_invested_amount,
            :govt_amount, :psu_private_amount, :equity_amount,
            :govt_pct, :psu_private_pct, :equity_pct,
            :category_amounts_json, :category_percentages_json, :category_source,
            CURRENT_TIMESTAMP
        )
        ON CONFLICT(portfolio_id, fiscal_year) DO UPDATE SET
            as_of_date = excluded.as_of_date,
            total_invested_amount = excluded.total_invested_amount,
            govt_amount = excluded.govt_amount,
            psu_private_amount = excluded.psu_private_amount,
            equity_amount = excluded.equity_amount,
            govt_pct = excluded.govt_pct,
            psu_private_pct = excluded.psu_private_pct,
            equity_pct = excluded.equity_pct,
            category_amounts_json = excluded.category_amounts_json,
            category_percentages_json = excluded.category_percentages_json,
            category_source = excluded.category_source,
            updated_at = CURRENT_TIMESTAMP
        """,
        {
            "portfolio_id": state.get("portfolio_id"),
            "fiscal_year": state.get("fiscal_year"),
            "as_of_date": state.get("as_of_date"),
            "total_invested_amount": _safe_float(state.get("total_invested_amount")),
            "govt_amount": _safe_float(bucket_amounts.get("GOVT")),
            "psu_private_amount": _safe_float(bucket_amounts.get("PSU_PRIVATE_DEBT")),
            "equity_amount": _safe_float(bucket_amounts.get("EQUITY")),
            "govt_pct": _safe_float(pcts.get("GOVT")),
            "psu_private_pct": _safe_float(pcts.get("PSU_PRIVATE_DEBT")),
            "equity_pct": _safe_float(pcts.get("EQUITY")),
            "category_amounts_json": _encode_weight_map(category_amounts),
            "category_percentages_json": _encode_weight_map(category_percentages),
            "category_source": str(state.get("category_source") or "derived_from_holdings"),
        },
    )


def project_state_after_inflow(
    current_state: dict[str, Any],
    recommended_bonds: list[dict[str, Any]],
    as_of_date: date,
) -> dict[str, Any]:
    amounts = {
        "GOVT": _safe_float((current_state.get("bucket_amounts") or {}).get("GOVT")),
        "PSU_PRIVATE_DEBT": _safe_float((current_state.get("bucket_amounts") or {}).get("PSU_PRIVATE_DEBT")),
        "EQUITY": _safe_float((current_state.get("bucket_amounts") or {}).get("EQUITY")),
    }
    for row in recommended_bonds:
        bucket = _bucket_for_position(row)
        amounts[bucket] += _safe_float(row.get("allocation_amount"))

    total_invested = sum(amounts.values())
    return {
        "portfolio_id": current_state.get("portfolio_id"),
        "fiscal_year": current_state.get("fiscal_year") or _financial_year(as_of_date),
        "as_of_date": as_of_date.isoformat(),
        "total_invested_amount": round(total_invested, 2),
        "bucket_amounts": amounts,
        "current_allocation_percentages": {
            "GOVT": _pct(amounts, "GOVT", total_invested),
            "PSU_PRIVATE_DEBT": _pct(amounts, "PSU_PRIVATE_DEBT", total_invested),
            "EQUITY": _pct(amounts, "EQUITY", total_invested),
        },
        "target_ranges": TARGET_RANGES,
        "source": "projected_with_new_inflow",
    }


async def record_inflow_event(
    *,
    portfolio_id: str,
    as_of_date: date,
    selected_bucket: str,
    strategy_phase: str,
    recommended_bonds: list[dict[str, Any]],
    state_after: dict[str, Any],
) -> None:
    await _ensure_tables()
    fy = state_after.get("fiscal_year") or _financial_year(as_of_date)
    event_amounts = {"GOVT": 0.0, "PSU_PRIVATE_DEBT": 0.0, "EQUITY": 0.0}
    for row in recommended_bonds:
        bucket = _bucket_for_position(row)
        event_amounts[bucket] += _safe_float(row.get("allocation_amount"))

    await execute_query(
        """
        INSERT INTO yearly_allocation_events (
            portfolio_id, fiscal_year, event_date, inflow_amount, selected_bucket, strategy_phase,
            govt_allocated, psu_private_allocated, equity_allocated
        ) VALUES (
            :portfolio_id, :fiscal_year, :event_date, :inflow_amount, :selected_bucket, :strategy_phase,
            :govt_allocated, :psu_private_allocated, :equity_allocated
        )
        """,
        {
            "portfolio_id": portfolio_id,
            "fiscal_year": fy,
            "event_date": as_of_date.isoformat(),
            "inflow_amount": round(sum(event_amounts.values()), 2),
            "selected_bucket": selected_bucket,
            "strategy_phase": strategy_phase,
            "govt_allocated": round(event_amounts["GOVT"], 2),
            "psu_private_allocated": round(event_amounts["PSU_PRIVATE_DEBT"], 2),
            "equity_allocated": round(event_amounts["EQUITY"], 2),
        },
    )
    await upsert_yearly_allocation_state(state_after)


def allocation_gap_vs_target(current_pct: dict[str, float]) -> dict[str, float]:
    return {
        bucket: round(float(TARGET_RANGES[bucket]["mid"]) - _safe_float(current_pct.get(bucket)), 4)
        for bucket in TARGET_RANGES
    }
