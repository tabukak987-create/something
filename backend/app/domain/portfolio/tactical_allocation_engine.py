"""Tactical allocation helpers for time-based yield capture under PF constraints."""

from __future__ import annotations

from datetime import date
from typing import Any


PF_YEAR_END_TARGET = {
    "AAA_GSEC_SDL": 60.0,
    "AA+": 30.0,
    "AA": 10.0,
}

TACTICAL_BOUNDS = {
    "AAA_GSEC_SDL_MIN": 40.0,
    "AA+_MAX": 45.0,
    "AA_MAX": 15.0,
    "AA_ABSOLUTE_MAX": 40.0,
    "TIER_1_2_MAX": 5.0,
}

APPROVED_HIGH_YIELD_ISSUERS = (
    "TATA",
    "LIC",
    "HDFC",
    "HDB",
    "MAHINDRA",
    "L&T",
    "BAJAJ",
    "ADITYA BIRLA",
    "RELIANCE",
    "KOTAK",
    "ICICI",
    "SBI",
)


def _financial_year_start(as_of: date, fy_start_month: int = 4) -> date:
    year = as_of.year if as_of.month >= fy_start_month else as_of.year - 1
    return date(year, fy_start_month, 1)


def _canonical_rating(raw: str | None) -> str:
    value = (raw or "").upper().strip()
    if value in {"SOV", "SOVEREIGN", "G-SEC", "GSEC", "SDL"}:
        return "SOVEREIGN"
    if "/" in value:
        value = value.split("/")[0].strip()
    if value in {"AAA", "AA+", "AA", "AA-", "A+", "A", "A-"}:
        return value
    if "AAA" in value:
        return "AAA"
    if "AA+" in value:
        return "AA+"
    if value.startswith("AA"):
        return "AA"
    if value.startswith("A"):
        return "A"
    return "NR"


def _rating_rank(raw: str | None) -> int:
    canonical = _canonical_rating(raw)
    ranks = {
        "SOVEREIGN": 10,
        "AAA": 9,
        "AA+": 8,
        "AA": 7,
        "AA-": 6,
        "A+": 5,
        "A": 4,
        "A-": 3,
        "NR": 0,
    }
    return ranks.get(canonical, 0)


def calculate_year_progress(as_of: date | None = None, fy_start_month: int = 4) -> dict[str, Any]:
    today = as_of or date.today()
    start = _financial_year_start(today, fy_start_month=fy_start_month)
    fy_end = date(start.year + 1, fy_start_month, 1)
    month_index = ((today.year - start.year) * 12 + (today.month - start.month)) + 1
    month_index = min(max(month_index, 1), 12)

    if month_index <= 4:
        phase = "EARLY"
    elif month_index <= 8:
        phase = "MID"
    else:
        phase = "LATE"

    return {
        "as_of": today.isoformat(),
        "fy_start": start.isoformat(),
        "fy_end_exclusive": fy_end.isoformat(),
        "month_index": month_index,
        "months_remaining": 12 - month_index,
        "days_remaining": max((fy_end - today).days, 0),
        "current_year_progress": round(month_index / 12.0, 6),
        "progress_pct": round((month_index / 12.0) * 100.0, 2),
        "phase": phase,
    }


def _phase_from_days_remaining(days_remaining: int) -> str:
    # Keep quarter-like behavior while using a direct time-to-year-end signal.
    if days_remaining > 240:
        return "EARLY"
    if days_remaining > 120:
        return "MID"
    return "LATE"


def determine_allocation_phase(year_progress: dict[str, Any]) -> dict[str, Any]:
    days_remaining_raw = year_progress.get("days_remaining")
    phase = str(year_progress.get("phase") or "MID").upper()
    if days_remaining_raw is not None:
        try:
            phase = _phase_from_days_remaining(int(days_remaining_raw))
        except (TypeError, ValueError):
            phase = str(year_progress.get("phase") or "MID").upper()
    if phase == "EARLY":
        return {
            "phase": "EARLY",
            "strategy_mode": "Tactical Yield Capture",
            "weights": {
                "yield_spread": 0.42,
                "liquidity": 0.18,
                "rating_quality": 0.17,
                "duration_fit": 0.13,
                "market_trend": 0.10,
            },
            "prefer_high_yield": True,
            "prefer_safety": False,
        }
    if phase == "LATE":
        return {
            "phase": "LATE",
            "strategy_mode": "Compliance Convergence",
            "weights": {
                "yield_spread": 0.24,
                "liquidity": 0.20,
                "rating_quality": 0.31,
                "duration_fit": 0.15,
                "market_trend": 0.10,
            },
            "prefer_high_yield": False,
            "prefer_safety": True,
        }
    return {
        "phase": "MID",
        "strategy_mode": "Balanced Tactical Allocation",
        "weights": {
            "yield_spread": 0.33,
            "liquidity": 0.20,
            "rating_quality": 0.22,
            "duration_fit": 0.15,
            "market_trend": 0.10,
        },
        "prefer_high_yield": True,
        "prefer_safety": True,
    }


def calculate_rating_distribution(positions: list[dict[str, Any]]) -> dict[str, float]:
    total = sum(float(p.get("market_value") or p.get("allocation_amount") or 0.0) for p in positions) or 1.0
    sums = {
        "AAA_GSEC_SDL": 0.0,
        "AA+": 0.0,
        "AA": 0.0,
        "A_AND_BELOW": 0.0,
        "TIER_1_2": 0.0,
    }

    for p in positions:
        value = float(p.get("market_value") or p.get("allocation_amount") or 0.0)
        issuer_type = str(p.get("issuer_type") or "").upper()
        rating = _canonical_rating(p.get("credit_rating") or p.get("rating"))

        if "TIER" in issuer_type:
            sums["TIER_1_2"] += value

        if issuer_type in {"G-SEC", "SDL", "SOVEREIGN", "CENTRAL_GOVT", "STATE_GOVT"} or rating in {"SOVEREIGN", "AAA"}:
            sums["AAA_GSEC_SDL"] += value
        elif rating == "AA+":
            sums["AA+"] += value
        elif rating == "AA":
            sums["AA"] += value
        else:
            sums["A_AND_BELOW"] += value

    return {k: round((v / total) * 100.0, 4) for k, v in sums.items()}


def calculate_target_gap(
    current_distribution: dict[str, float],
    target_distribution: dict[str, float] | None = None,
) -> dict[str, float]:
    target = target_distribution or PF_YEAR_END_TARGET
    return {
        "AAA_GSEC_SDL": round(target["AAA_GSEC_SDL"] - float(current_distribution.get("AAA_GSEC_SDL") or 0.0), 4),
        "AA+": round(target["AA+"] - float(current_distribution.get("AA+") or 0.0), 4),
        "AA": round(target["AA"] - float(current_distribution.get("AA") or 0.0), 4),
    }


def is_approved_high_yield_issuer(issuer_name: str | None) -> bool:
    name = (issuer_name or "").upper()
    return any(group in name for group in APPROVED_HIGH_YIELD_ISSUERS)


def parse_dual_ratings(row: dict[str, Any]) -> tuple[str, str]:
    r1 = _canonical_rating(row.get("credit_rating") or row.get("rating") or row.get("rating_1"))
    r2_raw = row.get("rating_2")
    if not r2_raw:
        combined = str(row.get("credit_rating") or row.get("rating") or "")
        if "/" in combined:
            parts = [p.strip() for p in combined.split("/") if p.strip()]
            if len(parts) > 1:
                r2_raw = parts[1]
    r2 = _canonical_rating(r2_raw or r1)
    return r1, r2


def dual_rating_allowed(row: dict[str, Any]) -> bool:
    r1, r2 = parse_dual_ratings(row)
    rank1 = _rating_rank(r1)
    rank2 = _rating_rank(r2)
    aa_rank = _rating_rank("AA")
    # Allowed patterns:
    # 1) both ratings >= AA
    # 2) one rating AAA/SOVEREIGN and the other >= AA
    if rank1 >= aa_rank and rank2 >= aa_rank:
        return True
    if (r1 in {"AAA", "SOVEREIGN"} and rank2 >= aa_rank) or (r2 in {"AAA", "SOVEREIGN"} and rank1 >= aa_rank):
        return True
    return False


def rating_aa_or_above(row: dict[str, Any]) -> bool:
    r1, r2 = parse_dual_ratings(row)
    aa_rank = _rating_rank("AA")
    return _rating_rank(r1) >= aa_rank and _rating_rank(r2) >= aa_rank


def strong_issuer_allowed(row: dict[str, Any]) -> bool:
    issuer_type = str(row.get("issuer_type") or "").upper()
    # Sovereign/PSU style buckets are structurally acceptable for safety allocation.
    if issuer_type in {"G-SEC", "SDL", "SOVEREIGN", "CENTRAL_GOVT", "STATE_GOVT", "PSU"}:
        return True

    # Corporates and bank capital instruments must be top-tier names.
    if issuer_type in {"CORPORATE", "NBFC", "INFRA", "BANK", "BANK_TIER_II"}:
        return is_approved_high_yield_issuer(row.get("issuer_name") or row.get("issuer"))

    return False


def tactical_filter_universe(
    bonds: list[dict[str, Any]],
    phase: str,
    current_distribution: dict[str, float],
) -> list[dict[str, Any]]:
    phase_u = (phase or "MID").upper()
    out: list[dict[str, Any]] = []
    for b in bonds:
        issuer_type = str(b.get("issuer_type") or "").upper()
        rating = _canonical_rating(b.get("credit_rating") or b.get("rating"))
        if rating in {"NR", "A", "A+", "A-", "AA-"} and issuer_type not in {"G-SEC", "SDL", "SOVEREIGN"}:
            continue
        if not rating_aa_or_above(b):
            continue
        if not dual_rating_allowed(b):
            continue
        if not strong_issuer_allowed(b):
            continue

        if rating in {"AA+", "AA"}:
            if not is_approved_high_yield_issuer(b.get("issuer_name") or b.get("issuer")):
                continue

        if phase_u == "LATE":
            # Late phase should converge to safer buckets.
            if rating == "AA" and float(current_distribution.get("AA") or 0.0) >= PF_YEAR_END_TARGET["AA"]:
                continue
            if rating == "AA+" and float(current_distribution.get("AA+") or 0.0) >= PF_YEAR_END_TARGET["AA+"]:
                continue

        out.append(b)
    return out


def tactical_bounds_violations(distribution: dict[str, float]) -> list[str]:
    violations: list[str] = []
    if float(distribution.get("AAA_GSEC_SDL") or 0.0) < TACTICAL_BOUNDS["AAA_GSEC_SDL_MIN"]:
        violations.append("AAA/G-Sec/SDL below tactical minimum 40%")
    if float(distribution.get("AA+") or 0.0) > TACTICAL_BOUNDS["AA+_MAX"]:
        violations.append("AA+ exceeds tactical maximum 45%")
    aa_pct = float(distribution.get("AA") or 0.0)
    if aa_pct > TACTICAL_BOUNDS["AA_MAX"]:
        violations.append("AA exceeds tactical maximum 15%")
    if aa_pct > TACTICAL_BOUNDS["AA_ABSOLUTE_MAX"]:
        violations.append("AA exceeds absolute risk cap 40%")
    if float(distribution.get("TIER_1_2") or 0.0) > TACTICAL_BOUNDS["TIER_1_2_MAX"]:
        violations.append("Tier I/II exceeds 5%")
    if float(distribution.get("A_AND_BELOW") or 0.0) > 0.0:
        violations.append("A and below exposure detected")
    return violations


def detect_risk_signals(
    positions: list[dict[str, Any]],
    tactical_distribution: dict[str, float],
    pf_violations: list[str] | None = None,
) -> dict[str, Any]:
    pf_violations = pf_violations or []
    downgrade_count = 0
    low_liquidity_count = 0
    spread_stress_count = 0

    for p in positions:
        if _rating_rank(p.get("credit_rating") or p.get("rating")) < _rating_rank("AA"):
            downgrade_count += 1
        if float(p.get("liquidity_score") or 100.0) < 35.0:
            low_liquidity_count += 1
        if float(p.get("spread_over_gsec") or p.get("spread_over_gsec_bps") or 0.0) > 250.0:
            spread_stress_count += 1

    tactical_violations = tactical_bounds_violations(tactical_distribution)
    safe_override = bool(
        downgrade_count > 0
        or low_liquidity_count > 0
        or spread_stress_count > 0
        or pf_violations
        or tactical_violations
    )

    return {
        "safe_override": safe_override,
        "risk_flags": {
            "credit_downgrade": downgrade_count,
            "liquidity_drop": low_liquidity_count,
            "spread_widening": spread_stress_count,
            "pf_violations": len(pf_violations),
            "tactical_violations": tactical_violations,
        },
    }