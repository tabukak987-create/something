"""
PF Compliance Engine
====================
Provident Fund rule checks used by allocation and recommendation workflows.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.domain.portfolio.institutional_financial_core import validate_sector_rating_consistency

RATING_ORDER = {
    "SOVEREIGN": 10,
    "SOV": 10,
    "AAA": 9,
    "AA+": 8,
    "AA": 7,
    "AA-": 6,
    "A+": 5,
    "A": 4,
    "A-": 3,
    "BBB+": 2,
    "BBB": 1,
    "NR": 0,
}

ASSET_LIMITS = {
    "GOVERNMENT_SECURITIES": (45.0, 65.0),
    "DEBT_INSTRUMENTS": (20.0, 45.0),
    "MONEY_MARKET": (0.0, 10.0),
    "EQUITY_ETF": (5.0, 15.0),
}

ISSUER_LIMITS = {
    "CORPORATE": 15.0,
    "PSU": 15.0,
    "BANK": 15.0,
    "NBFC": 15.0,
    "G-SEC": 100.0,
    "SDL": 100.0,
}

SECTOR_LIMITS = {
    "STATE_GOVERNMENT": 30.0,
    "CORPORATE": 25.0,
    "PSU": 40.0,
}

ALLOWED_ISSUER_TYPES = {
    "G-SEC",
    "SDL",
    "SOVEREIGN",
    "PSU",
    "CORPORATE",
    "INFRA",
    "BANK",
    "BANK_TIER_II",
}

STRICT_PF_TARGET = {
    "AAA_GSEC_SDL": 60.0,
    "AA+": 30.0,
    "AA": 10.0,
}

STRICT_LIMITS = {
    "issuer_max_pct": 10.0,
    "tier_1_2_max_pct": 5.0,
    "aa_plus_max_pct": 45.0,
    "aa_max_pct": 40.0,
}


def _build_limit_violation(
    *,
    label: str,
    actual: float,
    limit: float,
    mode: str,
) -> tuple[str, dict[str, Any]]:
    displayed_value = round(actual, 2)
    displayed_limit = round(limit, 2)
    if mode == "max":
        delta = actual - limit
        near_boundary = actual > limit and displayed_value <= displayed_limit
    else:
        delta = limit - actual
        near_boundary = actual < limit and displayed_value >= displayed_limit

    message = (
        f"{label} breach | Actual Value (full precision): {actual:.10f}% | "
        f"Displayed Value (rounded): {displayed_value:.2f}% | "
        f"Limit: {limit:.10f}% | Delta: {delta:.10f}%"
    )
    if near_boundary:
        message += " | Value exceeds limit due to precision beyond displayed rounding."

    detail = {
        "label": label,
        "mode": mode,
        "actual_value_precise": actual,
        "displayed_value_rounded": displayed_value,
        "limit_precise": limit,
        "displayed_limit_rounded": displayed_limit,
        "delta_precise": delta,
        "near_boundary_rounding": near_boundary,
    }
    return message, detail


def _rating_key(raw: str | None) -> str:
    r = (raw or "NR").upper().strip()
    if "/" in r:
        r = r.split("/")[0].strip()
    if r in ("SOV", "G-SEC", "GSEC"):
        return "SOVEREIGN"
    return r


def _to_asset_bucket(row: dict[str, Any]) -> str:
    issuer_type = (row.get("issuer_type") or "").upper()
    category = (row.get("allocation_bucket") or row.get("category") or "").upper()
    if category in ASSET_LIMITS:
        return category
    if issuer_type in ("G-SEC", "SDL", "SOVEREIGN", "CENTRAL_GOVT", "STATE_GOVT"):
        return "GOVERNMENT_SECURITIES"
    if issuer_type in ("CORPORATE", "PSU", "BANK", "BANK_TIER_II", "INFRA", "NBFC"):
        return "DEBT_INSTRUMENTS"
    if issuer_type in ("CP", "CD", "MONEY_MARKET"):
        return "MONEY_MARKET"
    return "DEBT_INSTRUMENTS"


def _is_equity_related_position(position: dict[str, Any]) -> bool:
    issuer_type = str(position.get("issuer_type") or "").upper()
    category = str(position.get("allocation_bucket") or position.get("category") or "").upper()
    sector = str(position.get("sector") or "").upper()
    name = str(position.get("issuer_name") or position.get("issuer") or position.get("isin") or "").upper()
    if "EQUITY" in category or "EQUITY" in sector:
        return True
    if issuer_type in {"MUTUAL_FUND", "ETF"}:
        return True
    if any(token in name for token in ("BLUE CHIP", "BALANCED ADVANTAGE", "FLEXICAP", "LARGE CAP", "MID CAP", "SMALL CAP", "INDEX", "MUTUAL FUND", "FUND", "ETF")):
        return True
    return False


def _bond_only_positions(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [p for p in positions if not _is_equity_related_position(p)]


def _years_to_maturity(maturity_date: str | None) -> float:
    if not maturity_date:
        return 99.0
    try:
        d = date.fromisoformat(str(maturity_date)[:10])
        return max(0.0, (d - date.today()).days / 365.25)
    except Exception:
        return 99.0


def _strict_rating_bucket(position: dict[str, Any]) -> str:
    issuer_type = str(position.get("issuer_type") or "").upper()
    rating = _rating_key(position.get("credit_rating") or position.get("rating"))
    if issuer_type in {"G-SEC", "SDL", "SOVEREIGN", "CENTRAL_GOVT", "STATE_GOVT"} or rating in {"SOVEREIGN", "AAA"}:
        return "AAA_GSEC_SDL"
    if rating == "AA+":
        return "AA+"
    if rating == "AA":
        return "AA"
    return "A_AND_BELOW"


def _strict_rating_distribution(positions: list[dict[str, Any]]) -> dict[str, float]:
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
        if "TIER" in issuer_type:
            sums["TIER_1_2"] += value
        sums[_strict_rating_bucket(p)] += value
    return {k: (v / total) * 100.0 for k, v in sums.items()}


def validate_strict_pf_pattern(
    positions: list[dict[str, Any]],
    target: dict[str, float] | None = None,
    precision_audit: list[dict[str, Any]] | None = None,
) -> tuple[bool, list[str], dict[str, float]]:
    target = target or STRICT_PF_TARGET
    dist = _strict_rating_distribution(positions)
    violations: list[str] = []

    if dist["AAA_GSEC_SDL"] + 1e-9 < target["AAA_GSEC_SDL"]:
        msg, detail = _build_limit_violation(
            label="Strict target AAA/G-Sec/SDL minimum",
            actual=dist["AAA_GSEC_SDL"],
            limit=target["AAA_GSEC_SDL"],
            mode="min",
        )
        violations.append(msg)
        if precision_audit is not None:
            precision_audit.append(detail)
    if dist["AA+"] - 1e-9 > target["AA+"]:
        msg, detail = _build_limit_violation(
            label="Strict target AA+ maximum",
            actual=dist["AA+"],
            limit=target["AA+"],
            mode="max",
        )
        violations.append(msg)
        if precision_audit is not None:
            precision_audit.append(detail)
    if dist["AA"] - 1e-9 > target["AA"]:
        msg, detail = _build_limit_violation(
            label="Strict target AA maximum",
            actual=dist["AA"],
            limit=target["AA"],
            mode="max",
        )
        violations.append(msg)
        if precision_audit is not None:
            precision_audit.append(detail)
    if dist["A_AND_BELOW"] > 0.0:
        msg, detail = _build_limit_violation(
            label="Strict target A and below exposure",
            actual=dist["A_AND_BELOW"],
            limit=0.0,
            mode="max",
        )
        violations.append(msg)
        if precision_audit is not None:
            precision_audit.append(detail)

    if dist["AA+"] > STRICT_LIMITS["aa_plus_max_pct"]:
        msg, detail = _build_limit_violation(
            label="AA+ tactical hard cap",
            actual=dist["AA+"],
            limit=STRICT_LIMITS["aa_plus_max_pct"],
            mode="max",
        )
        violations.append(msg)
        if precision_audit is not None:
            precision_audit.append(detail)
    if dist["AA"] > STRICT_LIMITS["aa_max_pct"]:
        msg, detail = _build_limit_violation(
            label="AA tactical hard cap",
            actual=dist["AA"],
            limit=STRICT_LIMITS["aa_max_pct"],
            mode="max",
        )
        violations.append(msg)
        if precision_audit is not None:
            precision_audit.append(detail)
    if dist["TIER_1_2"] > STRICT_LIMITS["tier_1_2_max_pct"]:
        msg, detail = _build_limit_violation(
            label="Tier I/II exposure",
            actual=dist["TIER_1_2"],
            limit=STRICT_LIMITS["tier_1_2_max_pct"],
            mode="max",
        )
        violations.append(msg)
        if precision_audit is not None:
            precision_audit.append(detail)

    total = sum(float(p.get("market_value") or p.get("allocation_amount") or 0.0) for p in positions) or 1.0
    by_issuer: dict[str, float] = {}
    for p in positions:
        issuer = (p.get("issuer_name") or p.get("issuer") or p.get("isin") or "UNKNOWN").upper()
        by_issuer[issuer] = by_issuer.get(issuer, 0.0) + float(p.get("market_value") or p.get("allocation_amount") or 0.0)
    for issuer, value in by_issuer.items():
        pct = (value / total) * 100.0
        if pct > STRICT_LIMITS["issuer_max_pct"]:
            msg, detail = _build_limit_violation(
                label=f"Issuer {issuer} strict exposure",
                actual=pct,
                limit=STRICT_LIMITS["issuer_max_pct"],
                mode="max",
            )
            violations.append(msg)
            if precision_audit is not None:
                precision_audit.append(detail)

    return len(violations) == 0, violations, {k: round(v, 4) for k, v in dist.items()}


def validate_rating(bond: dict[str, Any], min_rating: str = "AA") -> tuple[bool, str | None]:
    if _is_equity_related_position(bond):
        return True, None
    rating = _rating_key(bond.get("credit_rating") or bond.get("rating"))
    if RATING_ORDER.get(rating, 0) < RATING_ORDER.get(min_rating, 0):
        return False, f"Rating {rating} below minimum {min_rating}"
    return True, None


def validate_sector_limits(
    positions: list[dict[str, Any]],
    precision_audit: list[dict[str, Any]] | None = None,
) -> tuple[bool, list[str]]:
    bond_positions = _bond_only_positions(positions)
    total = sum(float(p.get("market_value") or p.get("allocation_amount") or 0.0) for p in bond_positions) or 1.0
    violations: list[str] = []
    for sector, limit in SECTOR_LIMITS.items():
        exposure = sum(
            float(p.get("market_value") or p.get("allocation_amount") or 0.0)
            for p in bond_positions
            if _normalize_sector(p) == sector
        )
        pct = (exposure / total) * 100.0
        if pct > limit:
            msg, detail = _build_limit_violation(
                label=f"Sector {sector} exposure",
                actual=pct,
                limit=limit,
                mode="max",
            )
            violations.append(msg)
            if precision_audit is not None:
                precision_audit.append(detail)
    return len(violations) == 0, violations


def _normalize_sector(position: dict[str, Any]) -> str:
    issuer_type = (position.get("issuer_type") or "").upper()
    sector = (position.get("sector") or "").upper()
    if issuer_type in {"SDL", "STATE-GOVT", "STATE_GOVERNMENT"} or sector in {"STATE_GOVERNMENT", "SDL"}:
        return "STATE_GOVERNMENT"
    if issuer_type == "PSU" or sector == "PSU":
        return "PSU"
    if issuer_type in {"CORPORATE", "BANK", "NBFC"} or sector in {"CORPORATE", "BANKING", "NBFC", "INFRASTRUCTURE"}:
        return "CORPORATE"
    return sector or "CORPORATE"


def validate_issuer_limits(
    positions: list[dict[str, Any]],
    precision_audit: list[dict[str, Any]] | None = None,
) -> tuple[bool, list[str]]:
    bond_positions = _bond_only_positions(positions)
    total = sum(float(p.get("market_value") or p.get("allocation_amount") or 0.0) for p in bond_positions) or 1.0
    by_issuer: dict[str, float] = {}
    issuer_meta: dict[str, str] = {}
    for p in bond_positions:
        issuer = (p.get("issuer_name") or p.get("issuer") or p.get("isin") or "UNKNOWN").upper()
        by_issuer[issuer] = by_issuer.get(issuer, 0.0) + float(
            p.get("market_value") or p.get("allocation_amount") or 0.0
        )
        issuer_meta[issuer] = (p.get("issuer_type") or "CORPORATE").upper()

    violations: list[str] = []
    for issuer, value in by_issuer.items():
        issuer_type = issuer_meta.get(issuer, "CORPORATE")
        limit = ISSUER_LIMITS.get(issuer_type, ISSUER_LIMITS["CORPORATE"])
        pct = (value / total) * 100.0
        if pct > limit:
            msg, detail = _build_limit_violation(
                label=f"Issuer {issuer} ({issuer_type}) exposure",
                actual=pct,
                limit=limit,
                mode="max",
            )
            violations.append(msg)
            if precision_audit is not None:
                precision_audit.append(detail)
    return len(violations) == 0, violations


def validate_classification_consistency(positions: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    violations: list[str] = []
    for p in positions:
        if _is_equity_related_position(p):
            continue
        errs = validate_sector_rating_consistency(
            rating=p.get("credit_rating") or p.get("rating"),
            sector=p.get("sector") or _normalize_sector(p),
        )
        for err in errs:
            violations.append(f"{p.get('isin')}: {err}")
    return len(violations) == 0, violations


def validate_duration(
    positions: list[dict[str, Any]],
    precision_audit: list[dict[str, Any]] | None = None,
) -> tuple[bool, list[str]]:
    violations: list[str] = []
    bond_positions = _bond_only_positions(positions)
    total = sum(float(p.get("market_value") or p.get("allocation_amount") or 0.0) for p in bond_positions) or 1.0
    weighted_duration = 0.0
    for p in bond_positions:
        amount = float(p.get("market_value") or p.get("allocation_amount") or 0.0)
        duration = float(p.get("modified_duration") or p.get("duration") or 0.0)
        years = float(p.get("years_to_maturity") or _years_to_maturity(p.get("maturity_date")))
        if years > 15.0:
            violations.append(f"Bond {p.get('isin')} maturity {years:.2f}Y exceeds 15Y")
        weighted_duration += duration * amount

    portfolio_duration = weighted_duration / total
    if portfolio_duration < 5.0 or portfolio_duration > 7.0:
        if portfolio_duration < 5.0:
            msg, detail = _build_limit_violation(
                label="Portfolio duration minimum",
                actual=portfolio_duration,
                limit=5.0,
                mode="min",
            )
        else:
            msg, detail = _build_limit_violation(
                label="Portfolio duration maximum",
                actual=portfolio_duration,
                limit=7.0,
                mode="max",
            )
        violations.append(msg + "Y")
        if precision_audit is not None:
            precision_audit.append(detail)
    return len(violations) == 0, violations


def validate_asset_allocation(
    positions: list[dict[str, Any]],
    precision_audit: list[dict[str, Any]] | None = None,
) -> tuple[bool, list[str], dict[str, float]]:
    total = sum(float(p.get("market_value") or p.get("allocation_amount") or 0.0) for p in positions) or 1.0
    bucket_values: dict[str, float] = {
        "GOVERNMENT_SECURITIES": 0.0,
        "DEBT_INSTRUMENTS": 0.0,
        "MONEY_MARKET": 0.0,
        "EQUITY_ETF": 0.0,
    }
    for p in positions:
        bucket = _to_asset_bucket(p)
        bucket_values[bucket] = bucket_values.get(bucket, 0.0) + float(
            p.get("market_value") or p.get("allocation_amount") or 0.0
        )

    bucket_pct = {k: (v / total) * 100.0 for k, v in bucket_values.items()}
    violations: list[str] = []
    for bucket, (mn, mx) in ASSET_LIMITS.items():
        pct = bucket_pct.get(bucket, 0.0)
        if pct < mn:
            msg, detail = _build_limit_violation(
                label=f"Asset bucket {bucket} minimum",
                actual=pct,
                limit=mn,
                mode="min",
            )
            violations.append(msg)
            if precision_audit is not None:
                precision_audit.append(detail)
        elif pct > mx:
            msg, detail = _build_limit_violation(
                label=f"Asset bucket {bucket} maximum",
                actual=pct,
                limit=mx,
                mode="max",
            )
            violations.append(msg)
            if precision_audit is not None:
                precision_audit.append(detail)
    return len(violations) == 0, violations, bucket_pct


def is_bond_eligible(bond: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    issuer_type = (bond.get("issuer_type") or "").upper()
    sector = (bond.get("sector") or "").upper()
    listed = (bond.get("listing_exchange") or "").upper() not in ("", "UNLISTED", "RESTRICTED")

    if issuer_type not in ALLOWED_ISSUER_TYPES:
        errors.append(f"Issuer type {issuer_type or 'UNKNOWN'} not PF-eligible")

    ok_rating, rating_err = validate_rating(bond, min_rating="AA")
    if not ok_rating and rating_err:
        errors.append(rating_err)

    if not listed:
        errors.append("Unlisted or restricted security")

    # Explicitly reject below investment grade.
    if RATING_ORDER.get(_rating_key(bond.get("credit_rating") or bond.get("rating")), 0) <= RATING_ORDER["BBB"]:
        errors.append("Below investment grade")

    # Bank Tier II must satisfy AA floor (already checked above); keep explicit reason for audit.
    if issuer_type == "BANK_TIER_II" and RATING_ORDER.get(_rating_key(bond.get("credit_rating") or bond.get("rating")), 0) < RATING_ORDER["AA"]:
        errors.append("Bank Tier II requires AA or above")

    # Optional conservative filter for restricted sectors.
    if sector == "RESTRICTED":
        errors.append("Restricted sector")

    return len(errors) == 0, errors


def validate_portfolio(
    positions: list[dict[str, Any]],
    strict_mode: bool = False,
) -> dict[str, Any]:
    precision_audit: list[dict[str, Any]] = []
    rating_ok = True
    rating_violations: list[str] = []
    for p in positions:
        ok, err = validate_rating(p, min_rating="AA")
        if not ok and err:
            rating_ok = False
            rating_violations.append(f"{p.get('isin')}: {err}")

    sec_ok, sec_v = validate_sector_limits(positions, precision_audit=precision_audit)
    issuer_ok, issuer_v = validate_issuer_limits(positions, precision_audit=precision_audit)
    dur_ok, dur_v = validate_duration(positions, precision_audit=precision_audit)
    alloc_ok, alloc_v, alloc_pct = validate_asset_allocation(positions, precision_audit=precision_audit)
    cls_ok, cls_v = validate_classification_consistency(positions)

    strict_ok = True
    strict_v: list[str] = []
    strict_dist: dict[str, float] = {}
    if strict_mode:
        strict_ok, strict_v, strict_dist = validate_strict_pf_pattern(positions, precision_audit=precision_audit)

    all_violations = rating_violations + sec_v + issuer_v + dur_v + alloc_v + cls_v + strict_v
    has_concentration_breach = len(sec_v) > 0 or len(issuer_v) > 0
    return {
        "pf_compliant": rating_ok and sec_ok and issuer_ok and dur_ok and alloc_ok and cls_ok and strict_ok,
        "checks": {
            "validate_rating": rating_ok,
            "validate_sector_limits": sec_ok,
            "validate_issuer_limits": issuer_ok,
            "validate_duration": dur_ok,
            "validate_asset_allocation": alloc_ok,
            "validate_classification_consistency": cls_ok,
            "validate_strict_pf_pattern": strict_ok,
        },
        "strict_mode": strict_mode,
        "asset_allocation_pct": {k: round(v, 4) for k, v in alloc_pct.items()},
        "strict_rating_distribution": strict_dist,
        "strict_target": STRICT_PF_TARGET if strict_mode else {},
        "portfolio_rating_cap": "AA" if has_concentration_breach else "AAA",
        "violations": all_violations,
        "precision_audit": precision_audit,
    }
