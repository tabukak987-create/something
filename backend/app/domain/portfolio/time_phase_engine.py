"""Time-phased bucket selection for inflow-driven allocation strategy."""

from __future__ import annotations

from datetime import date
from typing import Any

from .yearly_allocation_engine import TARGET_RANGES, allocation_gap_vs_target


def _financial_year_end(as_of: date) -> date:
    if as_of.month >= 4:
        return date(as_of.year + 1, 3, 31)
    return date(as_of.year, 3, 31)


def determine_time_phase(as_of: date) -> dict[str, Any]:
    fy_end = _financial_year_end(as_of)
    days_to_year_end = max((fy_end - as_of).days, 0)
    if days_to_year_end > 240:
        return {
            "phase": "EARLY",
            "days_to_year_end": days_to_year_end,
            "description": "Start-4 months: prioritize yield in AA+/PSU while preserving guardrails",
            "prefer_high_yield": True,
            "prefer_safety": False,
        }
    if days_to_year_end > 120:
        return {
            "phase": "MID",
            "days_to_year_end": days_to_year_end,
            "description": "Balanced phase: close bucket gaps while maintaining carry",
            "prefer_high_yield": True,
            "prefer_safety": True,
        }
    return {
        "phase": "LATE",
        "days_to_year_end": days_to_year_end,
        "description": "Year-end phase: prioritize compliance convergence and safety",
        "prefer_high_yield": False,
        "prefer_safety": True,
    }


def _bucket_tilt_order(phase: str) -> list[str]:
    phase_u = (phase or "MID").upper()
    if phase_u == "EARLY":
        return ["PSU_PRIVATE_DEBT", "GOVT", "EQUITY"]
    if phase_u == "LATE":
        return ["GOVT", "PSU_PRIVATE_DEBT", "EQUITY"]
    return ["GOVT", "PSU_PRIVATE_DEBT", "EQUITY"]


def _under_allocated_buckets(current_pct: dict[str, float]) -> list[str]:
    out: list[str] = []
    for bucket, limits in TARGET_RANGES.items():
        if float(current_pct.get(bucket) or 0.0) < float(limits["min"]):
            out.append(bucket)
    return out


def select_preferred_bucket(
    current_pct: dict[str, float],
    phase_state: dict[str, Any],
    inflow_amount: float,
) -> dict[str, Any]:
    phase = str(phase_state.get("phase") or "MID").upper()
    gaps = allocation_gap_vs_target(current_pct)
    under_allocated = _under_allocated_buckets(current_pct)
    tilt_order = _bucket_tilt_order(phase)

    # Year-end correction: force allocations to buckets below required min bounds.
    if phase == "LATE" and under_allocated:
        forced = sorted(
            under_allocated,
            key=lambda b: float(TARGET_RANGES[b]["min"]) - float(current_pct.get(b) or 0.0),
            reverse=True,
        )[0]
        return {
            "selected_bucket": forced,
            "selection_mode": "year_end_correction",
            "allocation_reason": f"{forced} is below min target; late-phase correction applied",
            "gaps": gaps,
            "under_allocated": under_allocated,
            "inflow_amount": float(inflow_amount or 0.0),
        }

    # Outside forced-correction mode, prefer the largest positive target-gap bucket,
    # then tie-break by phase tilt.
    positive_gap = [b for b, g in gaps.items() if g > 0.0]
    if positive_gap:
        selected = sorted(
            positive_gap,
            key=lambda b: (float(gaps[b]), -tilt_order.index(b) if b in tilt_order else -99),
            reverse=True,
        )[0]
        return {
            "selected_bucket": selected,
            "selection_mode": "gap_based",
            "allocation_reason": f"Selected by largest target-gap in {phase} phase",
            "gaps": gaps,
            "under_allocated": under_allocated,
            "inflow_amount": float(inflow_amount or 0.0),
        }

    # If all buckets are near/above midpoint, follow the phase tilt order.
    selected = tilt_order[0]
    return {
        "selected_bucket": selected,
        "selection_mode": "phase_tilt",
        "allocation_reason": f"All buckets near target; applied {phase} phase tilt",
        "gaps": gaps,
        "under_allocated": under_allocated,
        "inflow_amount": float(inflow_amount or 0.0),
    }


def bucket_for_bond(row: dict[str, Any]) -> str:
    category = str(row.get("category") or row.get("allocation_bucket") or "").upper()
    issuer_type = str(row.get("issuer_type") or "").upper()
    sector = str(row.get("sector") or "").upper()

    if "EQUITY" in category or "EQUITY" in sector:
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


def filter_universe_by_bucket(universe: list[dict[str, Any]], selected_bucket: str) -> list[dict[str, Any]]:
    if not universe:
        return []
    return [row for row in universe if bucket_for_bond(row) == selected_bucket]
