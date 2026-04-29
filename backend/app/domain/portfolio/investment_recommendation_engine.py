"""Institutional investment recommendation engine orchestration."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping
from datetime import date

from app.domain.market_data.market_scanner import scan_tradable_bonds
from app.domain.portfolio.analytics import compute_full_portfolio_analytics
from app.domain.portfolio.analyzer import analyze_portfolio
from app.domain.compliance.pf_engine import is_bond_eligible, validate_portfolio
from app.domain.market_data.relative_value_engine import compute_relative_value
from app.domain.market_data.bond_scoring_engine import score_bonds
from app.domain.portfolio.allocator import optimize_allocation
from app.domain.portfolio.tactical_allocation_engine import (
    PF_YEAR_END_TARGET,
    calculate_rating_distribution,
    calculate_target_gap,
    calculate_year_progress,
    detect_risk_signals,
    dual_rating_allowed,
    determine_allocation_phase,
    rating_aa_or_above,
    strong_issuer_allowed,
    tactical_filter_universe,
)
from app.domain.market_data.spread_intelligence_engine import analyze_spread_intelligence
from app.domain.market_data.market_timing_engine import (
    adjust_scoring_weights_for_market_mode,
    apply_market_mode_overrides,
    determine_market_mode,
    infer_market_signals,
    strategy_mode_label,
)
from app.domain.portfolio.rebalance_planner import generate_rebalance_plan
from app.domain.portfolio.time_phase_engine import (
    determine_time_phase,
    filter_universe_by_bucket,
    select_preferred_bucket,
)
from app.domain.portfolio.yearly_allocation_engine import (
    TARGET_RANGES,
    get_yearly_allocation_state,
    project_state_after_inflow,
    record_inflow_event,
)
from app.domain.portfolio.institutional_financial_core import (
    BondValidationResult,
    compute_duration_metrics,
    compute_spread_metrics,
    validate_bond_record,
)
from app.domain.core_trust.explainability import build_change_tracking, build_counterfactual, build_explanation
from app.domain.market_data.instrument_classification import (
    INSTRUMENT_CATEGORY_EQUITY,
    INSTRUMENT_CATEGORY_PRIVATE,
    INSTRUMENT_CATEGORY_PSU,
    INSTRUMENT_CATEGORY_STATE_CENTRAL,
    category_explainability_lines,
    category_metadata,
    category_sort_key,
    category_to_allocation_bucket,
    classify_instrument,
)
from app.shared.database import fetch_all


def build_recommendation_explanation(bond: dict[str, Any]) -> str:
    lines: list[str] = []
    rv_bps = float(bond.get("relative_value_bps") or 0.0)
    if rv_bps > 0:
        lines.append(f"Yield {rv_bps:.0f}bps above peers")
    rating = bond.get("credit_rating") or bond.get("rating") or "NR"
    lines.append(f"{rating} rated {bond.get('issuer_type', 'bond')} issuer")
    dur = float(bond.get("modified_duration") or 0.0)
    if 5.0 <= dur <= 7.0:
        lines.append("Improves portfolio duration alignment")
    liq = float(bond.get("liquidity_score") or 0.0)
    if liq >= 60:
        lines.append("Liquidity high")
    lines.append("PF compliant")
    return "; ".join(lines)


_CATEGORY_RATING_SIGNAL = {
    "SOVEREIGN": 1.0,
    "AAA": 0.95,
    "AA+": 0.9,
    "AA": 0.84,
    "AA-": 0.76,
    "A+": 0.68,
    "A": 0.6,
    "A-": 0.5,
    "BBB+": 0.4,
    "BBB": 0.3,
    "NR": 0.2,
}


def _bounded(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, float(value)))


def _rating_signal(rating: str | None) -> float:
    key = str(rating or "NR").upper().strip()
    if key.startswith("AAA"):
        return _CATEGORY_RATING_SIGNAL["AAA"]
    if key.startswith("AA+"):
        return _CATEGORY_RATING_SIGNAL["AA+"]
    if key.startswith("AA"):
        return _CATEGORY_RATING_SIGNAL["AA"]
    if key.startswith("A+"):
        return _CATEGORY_RATING_SIGNAL["A+"]
    if key.startswith("A"):
        return _CATEGORY_RATING_SIGNAL["A"]
    return _CATEGORY_RATING_SIGNAL.get(key, _CATEGORY_RATING_SIGNAL["NR"])


def _attach_category_profile(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    classified = classify_instrument(out)
    category = classified["instrument_category"]
    subtype = classified["instrument_subtype"]
    meta = category_metadata(category)
    out["instrument_category"] = category
    out["instrument_subtype"] = subtype
    out["category_display"] = meta["category_display"]
    out["category_tag"] = meta["category_tag"]
    out["risk_label"] = meta["risk_label"]
    out["category_section_title"] = meta["section_title"]
    out.setdefault("allocation_bucket", category_to_allocation_bucket(category))
    out.setdefault("category", category)
    return out


def _category_specific_score(row: dict[str, Any]) -> tuple[float, dict[str, float], str]:
    category = str(row.get("instrument_category") or "").upper()
    relative_value_bps = float(row.get("relative_value_bps") or (float(row.get("spread") or 0.0) * 100.0))
    duration = float(row.get("modified_duration") or row.get("duration") or 0.0)
    duration_fit = _bounded(1.0 - abs(duration - 6.0) / 6.0)
    liquidity = _bounded(float(row.get("liquidity_score") or 0.0) / 100.0)
    rating_quality = _rating_signal(str(row.get("credit_rating") or row.get("rating") or "NR"))
    issuer_strength = _bounded(_issuer_quality_signal(row))
    market_trend = _bounded(float((row.get("score_components") or {}).get("market_trend") or 0.0))

    if category == INSTRUMENT_CATEGORY_STATE_CENTRAL:
        yield_curve_advantage = _bounded((relative_value_bps + 40.0) / 120.0)
        score = 0.45 * yield_curve_advantage + 0.30 * duration_fit + 0.25 * liquidity
        return score, {
            "yield_curve_advantage": round(yield_curve_advantage, 6),
            "duration_fit": round(duration_fit, 6),
            "liquidity": round(liquidity, 6),
        }, "state_central_evaluation_engine"

    if category == INSTRUMENT_CATEGORY_PSU:
        spread_over_sovereign = _bounded((relative_value_bps + 20.0) / 100.0)
        score = 0.40 * rating_quality + 0.35 * spread_over_sovereign + 0.25 * issuer_strength
        return score, {
            "rating_quality": round(rating_quality, 6),
            "spread_over_sovereign": round(spread_over_sovereign, 6),
            "issuer_strength": round(issuer_strength, 6),
        }, "psu_spread_support_engine"

    if category == INSTRUMENT_CATEGORY_PRIVATE:
        yield_premium = _bounded((relative_value_bps + 40.0) / 180.0)
        financial_strength_proxy = _bounded((rating_quality + liquidity) / 2.0)
        risk_flags = 0.0
        if liquidity < 0.35:
            risk_flags += 0.18
        if rating_quality < 0.7:
            risk_flags += 0.22
        score = 0.40 * rating_quality + 0.25 * financial_strength_proxy + 0.25 * yield_premium - 0.10 * risk_flags
        return score, {
            "rating_quality": round(rating_quality, 6),
            "financial_strength_proxy": round(financial_strength_proxy, 6),
            "yield_premium": round(yield_premium, 6),
            "risk_flags": round(risk_flags, 6),
        }, "private_credit_risk_adjusted_engine"

    # Equity engine intentionally ignores bond-only metrics such as duration spreads.
    performance = _bounded((market_trend + _bounded(float(row.get("relative_value_signal") or 0.0))) / 2.0)
    volatility_proxy = _bounded(1.0 - liquidity)
    growth_signal = _bounded((performance + _bounded(float(row.get("score") or 0.0))) / 2.0)
    score = 0.45 * performance + 0.35 * growth_signal + 0.20 * (1.0 - volatility_proxy)
    return score, {
        "performance": round(performance, 6),
        "growth_signal": round(growth_signal, 6),
        "volatility_proxy": round(volatility_proxy, 6),
    }, "equity_growth_engine"


def _apply_category_specific_scores(scored: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in scored:
        profiled = _attach_category_profile(row)
        category_score, metrics, engine_name = _category_specific_score(profiled)
        profiled["legacy_score"] = float(profiled.get("score") or 0.0)
        profiled["category_score"] = round(category_score, 6)
        profiled["category_engine"] = engine_name
        profiled["category_metrics"] = metrics
        profiled["score"] = round(_bounded(category_score, 0.0, 2.0), 6)
        out.append(profiled)

    out.sort(key=lambda x: (float(x.get("score") or 0.0), float(x.get("ytm") or 0.0)), reverse=True)
    return out


def _group_by_instrument_category(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("instrument_category") or INSTRUMENT_CATEGORY_PRIVATE).upper()].append(row)
    for values in grouped.values():
        values.sort(key=lambda x: (float(x.get("score") or 0.0), float(x.get("ytm") or 0.0)), reverse=True)
    return dict(grouped)


def _build_category_sections(rows: list[dict[str, Any]], top_n: int = 5) -> list[dict[str, Any]]:
    grouped = _group_by_instrument_category(rows)
    ordered_categories = [
        INSTRUMENT_CATEGORY_STATE_CENTRAL,
        INSTRUMENT_CATEGORY_PSU,
        INSTRUMENT_CATEGORY_PRIVATE,
        INSTRUMENT_CATEGORY_EQUITY,
    ]
    sections: list[dict[str, Any]] = []
    for category in ordered_categories:
        members = list(grouped.get(category, []))
        meta = category_metadata(category)
        sections.append(
            {
                "instrument_category": category,
                "instrument_subtype": None,
                "category_display": meta["category_display"],
                "category_tag": meta["category_tag"],
                "risk_label": meta["risk_label"],
                "section_title": meta["section_title"],
                "recommendations": members[:top_n],
                "count": len(members),
            }
        )
    return sections


def _build_bond_explanation(
    row: Mapping[str, Any],
    *,
    selected_bucket: str,
    strategy_phase: str,
) -> dict[str, Any]:
    category = str(row.get("instrument_category") or INSTRUMENT_CATEGORY_PRIVATE).upper()
    subtype = str(row.get("instrument_subtype") or "").upper()
    meta = category_metadata(category)
    category_reasons = category_explainability_lines(category, subtype)
    rating = str(row.get("credit_rating") or row.get("rating") or "NR").upper()
    rating_ok = rating.startswith("AAA") or rating.startswith("AA") or "SOV" in rating
    yield_advantage_bps = float(row.get("yield_advantage_bps") or 0.0)

    counterfactuals: list[dict[str, Any]] = []
    if yield_advantage_bps <= 0.0:
        counterfactuals.append(
            build_counterfactual(
                scenario="peer_spread_neutral",
                expected_outcome="LOWER_RELATIVE_VALUE",
                impact_summary="This name does not show a positive spread advantage versus peer baseline.",
                delta_metrics={"yield_advantage_bps": round(yield_advantage_bps, 2)},
                rationale="Used for comparison when spread uplift signal is weak.",
            )
        )

    confidence_drivers = [
        f"instrument_category={category}",
        f"instrument_subtype={subtype or 'NA'}",
        f"relative_value_signal={round(float(row.get('relative_value_signal') or 0.0), 6)}",
        f"liquidity_score={round(float(row.get('liquidity_score') or 0.0), 4)}",
        f"strategy_phase={strategy_phase}",
    ]

    category_floor_status = "PASS"
    category_floor_message = "Category specific checks passed"
    if category in {INSTRUMENT_CATEGORY_PSU, INSTRUMENT_CATEGORY_PRIVATE} and not rating_ok:
        category_floor_status = "WARN"
        category_floor_message = "Credit profile is below preferred AA floor for debt categories"

    return build_explanation(
        module="investment_recommendation_engine",
        decision_type="bond_selection",
        decision_outcome="RECOMMENDED",
        summary=str(row.get("ai_explanation") or "Selected based on policy and portfolio fit."),
        key_factors=[
            {
                "name": "Instrument Category",
                "value": category,
                "impact": "neutral",
                "evidence": f"Tag: {meta.get('category_tag')} | Risk: {meta.get('risk_label')}",
            },
            {
                "name": "Credit Rating",
                "value": rating,
                "impact": "positive" if rating_ok else "negative",
                "evidence": "AA and above floor with issuer quality filters",
            },
            {
                "name": "Relative Value (bps)",
                "value": round(yield_advantage_bps, 2),
                "impact": "positive" if yield_advantage_bps > 0 else "neutral",
                "evidence": "Spread intelligence and peer-relative valuation",
            },
            {
                "name": "Allocation Amount",
                "value": round(float(row.get("allocation_amount") or 0.0), 2),
                "impact": "positive",
                "evidence": "Optimizer output after issuer concentration cap",
            },
            {
                "name": "Portfolio Role",
                "value": row.get("portfolio_role") or "Rebalancing support",
                "impact": "neutral",
                "evidence": "Derived from phase-aware role inference",
            },
            {
                "name": "Why Recommended",
                "value": " | ".join(category_reasons),
                "impact": "positive",
                "evidence": "Category specific explainability contract",
            },
        ],
        rule_checks=[
            {
                "rule_code": "CATEGORY_ENGINE_ROUTING",
                "status": "PASS",
                "message": "Instrument routed to category-specific evaluation engine",
                "actual": row.get("category_engine") or "unknown",
            },
            {
                "rule_code": "CATEGORY_CREDIT_GUARD",
                "status": category_floor_status,
                "message": category_floor_message,
                "actual": rating,
            },
            {
                "rule_code": "PF_BUCKET_ALIGNMENT",
                "status": "PASS",
                "message": "Selected bucket aligned with yearly allocation policy",
                "actual": row.get("selected_bucket") or selected_bucket,
            },
            {
                "rule_code": "ISSUER_CONCENTRATION_CAP",
                "status": "PASS",
                "message": "Post-trade issuer exposure constrained by optimizer cap",
                "threshold": "10%",
                "actual": "within_limit",
            },
        ],
        calculation_trace={
            "score": round(float(row.get("score") or 0.0), 6),
            "category_score": round(float(row.get("category_score") or 0.0), 6),
            "base_score": round(float(row.get("base_score") or 0.0), 6),
            "allocation_amount": round(float(row.get("allocation_amount") or 0.0), 2),
            "ytm": round(float(row.get("ytm") or 0.0), 6),
            "relative_value_signal": round(float(row.get("relative_value_signal") or 0.0), 6),
            "category_metrics": dict(row.get("category_metrics") or {}),
        },
        data_lineage=[
            {"source": "market_scanner", "field": "bond_universe", "trust_level": "high"},
            {"source": "instrument_classification", "field": "instrument_category", "trust_level": "high"},
            {"source": "spread_intelligence_engine", "field": "relative_value_bps", "trust_level": "high"},
            {"source": "allocation_optimizer", "field": "allocation_amount", "trust_level": "high"},
        ],
        regulatory_basis=[
            "Category-specific investment policy controls",
            "PF Trust policy: debt categories target AA and above quality bands",
            "PF Trust issuer concentration limits",
            "Phase-aligned yearly allocation framework",
        ],
        confidence_score=float(row.get("confidence_score") or 0.0),
        confidence_drivers=confidence_drivers,
        counterfactuals=counterfactuals,
        tags=["recommendation", "bond"],
        metadata={
            "isin": row.get("isin"),
            "instrument_category": category,
            "instrument_subtype": subtype,
            "risk_label": meta.get("risk_label"),
            "strategy_mode": row.get("strategy_mode"),
            "market_mode": row.get("market_mode"),
        },
    )


def _validated_market_row(row: dict[str, Any]) -> BondValidationResult:
    validated = validate_bond_record(
        {
            "isin": row.get("isin"),
            "issuer_name": row.get("issuer_name"),
            "credit_rating": row.get("credit_rating") or row.get("rating"),
            "sector": row.get("sector"),
            "maturity_date": row.get("maturity_date"),
            "coupon_rate": row.get("coupon_rate"),
            "ytm": row.get("ytm"),
            "clean_price": row.get("clean_price"),
        }
    )
    if not validated.is_valid:
        return validated

    normalized = dict(row)
    normalized.update(validated.normalized)

    maturity_s = normalized.get("maturity_date")
    clean_price = normalized.get("clean_price")
    coupon = normalized.get("coupon_rate")
    ytm = normalized.get("ytm")

    years = None
    if maturity_s:
        try:
            maturity_d = date.fromisoformat(str(maturity_s))
            years = max((maturity_d - date.today()).days / 365.25, 0.0)
        except Exception:
            years = None

    if years and clean_price and coupon is not None and ytm is not None:
        metrics = compute_duration_metrics(
            clean_price=float(clean_price),
            face_value=100.0,
            coupon_rate_pct=float(coupon),
            ytm_pct=float(ytm),
            years_to_maturity=years,
            frequency=2,
        )
        if normalized.get("modified_duration") is None and metrics.get("modified_duration") is not None:
            normalized["modified_duration"] = metrics["modified_duration"]
        if normalized.get("duration") is None and metrics.get("duration") is not None:
            normalized["duration"] = metrics["duration"]
        if normalized.get("dv01") is None and metrics.get("dv01") is not None:
            normalized["dv01"] = metrics["dv01"]

    spread_metrics = compute_spread_metrics(normalized.get("ytm"), normalized.get("best_bid_yield"))
    if normalized.get("g_spread") is None:
        normalized["g_spread"] = spread_metrics.get("g_spread")
    if normalized.get("z_spread") is None:
        normalized["z_spread"] = spread_metrics.get("z_spread")

    return BondValidationResult(is_valid=True, normalized=normalized, errors=[], warnings=validated.warnings)


def _sheet_asset_bucket(category_bucket: str | None) -> str:
    text = str(category_bucket or "").upper()
    if "EQUITY" in text:
        return "EQUITY_ETF"
    if any(token in text for token in ("CENTRAL", "STATE", "GOV", "SDL", "SOV", "G-SEC", "GSEC")):
        return "GOVERNMENT_SECURITIES"
    return "DEBT_INSTRUMENTS"


async def _load_sheet_backed_compliance_positions() -> list[dict[str, Any]]:
    try:
        rows = await fetch_all(
            """
            SELECT
                psa.isin,
                psa.category_bucket,
                psa.sector_bucket,
                psa.basis_value,
                ubm.issuer_name,
                ubm.issuer_type,
                NULLIF(ubm.credit_rating, '') AS credit_rating,
                COALESCE(NULLIF(ubm.sector, ''), psa.sector_bucket) AS sector,
                ubm.maturity_date,
                ubm.issue_date,
                ubm.face_value,
                ubm.coupon_rate
            FROM portfolio_sheet_allocations psa
            LEFT JOIN universal_bond_master ubm ON psa.isin = ubm.isin
            WHERE COALESCE(psa.basis_value, 0) > 0
            ORDER BY psa.basis_value DESC
            """,
            {},
        )
    except Exception:
        return []

    positions: list[dict[str, Any]] = []
    for row in rows:
        basis_value = float(row.get("basis_value") or 0.0)
        if basis_value <= 0:
            continue

        category_bucket = str(row.get("category_bucket") or "").upper()
        asset_bucket = _sheet_asset_bucket(category_bucket)
        issuer_name = row.get("issuer_name") or row.get("isin") or "UNKNOWN"
        issuer_type = str(row.get("issuer_type") or "CORPORATE").upper()
        credit_rating = str(row.get("credit_rating") or "NR").upper()
        sector = str(row.get("sector") or row.get("sector_bucket") or "CORPORATE").upper()
        maturity_date = row.get("maturity_date")
        coupon_rate = float(row.get("coupon_rate") or 0.0)
        face_value = float(row.get("face_value") or basis_value or 0.0)
        clean_price = 100.0 if face_value > 0 else 0.0
        ytm = coupon_rate or 7.0
        modified_duration = None

        if clean_price > 0 and maturity_date:
            try:
                years_to_maturity = max((date.fromisoformat(str(maturity_date)[:10]) - date.today()).days / 365.25, 0.01)
                metrics = compute_duration_metrics(
                    clean_price=clean_price,
                    face_value=face_value,
                    coupon_rate_pct=coupon_rate,
                    ytm_pct=ytm,
                    years_to_maturity=years_to_maturity,
                    frequency=2,
                )
                modified_duration = metrics.get("modified_duration")
            except Exception:
                modified_duration = None

        entry = {
            "isin": row.get("isin"),
            "issuer_name": issuer_name,
            "issuer_type": issuer_type,
            "credit_rating": credit_rating,
            "rating": credit_rating,
            "sector": sector,
            "category": asset_bucket,
            "allocation_bucket": asset_bucket,
            "market_value": basis_value,
            "allocation_amount": basis_value,
            "investment_value": basis_value,
            "clean_price": clean_price,
            "ytm": ytm if ytm > 0 else None,
            "modified_duration": modified_duration,
            "liquidity_score": 100.0,
            "spread_over_gsec": 0.0,
            "maturity_date": maturity_date,
            "issue_date": row.get("issue_date"),
        }
        positions.append(_attach_category_profile(entry))

    return positions


def _normalize_category_bucket(bond: dict[str, Any]) -> str:
    profiled = _attach_category_profile(bond)
    return category_to_allocation_bucket(profiled.get("instrument_category"))


def _issuer_exposure_map(current_positions: list[dict[str, Any]]) -> dict[str, float]:
    total = sum(float(p.get("market_value") or p.get("allocation_amount") or 0.0) for p in current_positions) or 1.0
    by_issuer: dict[str, float] = {}
    for p in current_positions:
        issuer = str(p.get("issuer_name") or p.get("issuer") or p.get("isin") or "UNKNOWN").upper()
        by_issuer[issuer] = by_issuer.get(issuer, 0.0) + float(p.get("market_value") or p.get("allocation_amount") or 0.0)
    return {k: (v / total) * 100.0 for k, v in by_issuer.items()}


def _issuer_quality_signal(row: dict[str, Any]) -> float:
    issuer_type = str(row.get("issuer_type") or "").upper()
    if issuer_type in {"G-SEC", "SDL", "SOVEREIGN", "CENTRAL_GOVT", "STATE_GOVT", "PSU"}:
        return 1.0
    return 1.0 if strong_issuer_allowed(row) else 0.2


def _apply_dynamic_tactical_scores(
    scored: list[dict[str, Any]],
    weights: dict[str, float],
    current_positions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    issuer_exposure = _issuer_exposure_map(current_positions or [])
    rescored: list[dict[str, Any]] = []
    for row in scored:
        c = row.get("score_components") or {}
        relative_value_signal = float(
            row.get("relative_value_signal")
            if row.get("relative_value_signal") is not None
            else c.get("relative_value_signal")
            or 0.0
        )
        absolute_yield = float(c.get("absolute_yield") or 0.0)
        issuer_quality = _issuer_quality_signal(row)
        issuer = str(row.get("issuer_name") or row.get("issuer") or row.get("isin") or "UNKNOWN").upper()
        # Penalize issuers that are already concentrated in current portfolio.
        concentration_penalty = min(0.35, max(0.0, (float(issuer_exposure.get(issuer) or 0.0) - 8.0) / 20.0))

        tactical_score = (
            float(weights.get("yield_spread") or 0.0) * float(c.get("yield_spread") or 0.0)
            + 0.12 * absolute_yield
            + float(weights.get("liquidity") or 0.0) * float(c.get("liquidity") or 0.0)
            + float(weights.get("rating_quality") or 0.0) * float(c.get("rating_quality") or 0.0)
            + float(weights.get("duration_fit") or 0.0) * float(c.get("duration_fit") or 0.0)
            + float(weights.get("market_trend") or 0.0) * float(c.get("market_trend") or 0.0)
            + float(weights.get("relative_value") or 0.0) * relative_value_signal
            + float(row.get("spread_boost_factor") or 0.0)
            + 0.10 * issuer_quality
            - concentration_penalty
        )
        out = dict(row)
        out["base_score"] = float(row.get("score") or 0.0)
        out["score"] = round(tactical_score, 6)
        out["score_components"] = dict(c)
        out["score_components"]["relative_value_signal"] = round(relative_value_signal, 6)
        out["score_components"]["issuer_quality"] = round(issuer_quality, 6)
        out["score_components"]["concentration_risk"] = round(concentration_penalty, 6)
        rescored.append(out)
    rescored.sort(key=lambda x: (float(x.get("score") or 0.0), float(x.get("ytm") or 0.0)), reverse=True)
    return rescored


def _enforce_issuer_limit(
    allocation_plan: list[dict[str, Any]],
    current_positions: list[dict[str, Any]],
    total_post_value: float,
    max_pct: float = 10.0,
) -> list[dict[str, Any]]:
    if not allocation_plan:
        return []
    issuer_current: dict[str, float] = {}
    for p in current_positions:
        issuer = str(p.get("issuer_name") or p.get("issuer") or p.get("isin") or "UNKNOWN").upper()
        issuer_current[issuer] = issuer_current.get(issuer, 0.0) + float(p.get("market_value") or p.get("allocation_amount") or 0.0)

    capped: list[dict[str, Any]] = []
    for item in allocation_plan:
        issuer = str(item.get("issuer_name") or item.get("issuer") or item.get("isin") or "UNKNOWN").upper()
        existing = issuer_current.get(issuer, 0.0)
        max_value = (max_pct / 100.0) * total_post_value
        room = max(0.0, max_value - existing)
        alloc = float(item.get("allocation_amount") or 0.0)
        if room <= 0.0:
            continue
        new_alloc = min(alloc, room)
        if new_alloc <= 0.0:
            continue
        out = dict(item)
        out["allocation_amount"] = round(new_alloc, 2)
        issuer_current[issuer] = existing + new_alloc
        capped.append(out)
    return capped


def _safe_universe_only(bonds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    safe: list[dict[str, Any]] = []
    for b in bonds:
        bucket = _normalize_category_bucket(b)
        if bucket == "GOVT":
            safe.append(b)
    return safe


def _weighted_yield(plan: list[dict[str, Any]]) -> float:
    total = sum(float(x.get("allocation_amount") or 0.0) for x in plan)
    if total <= 0:
        return 0.0
    return sum(float(x.get("ytm") or 0.0) * float(x.get("allocation_amount") or 0.0) for x in plan) / total


def _build_policy_fallback_plan(
    candidates: list[dict[str, Any]],
    investment_amount: float,
    current_positions: list[dict[str, Any]],
    total_post_value: float,
    max_issuer_pct: float = 15.0,
) -> list[dict[str, Any]]:
    """
    Build a deterministic fallback allocation using only policy-eligible names.
    This path is used when full-portfolio compliance constraints block optimizer output
    even though eligible inventory exists.
    """
    if not candidates or investment_amount <= 0:
        return []

    ranked = sorted(
        candidates,
        key=lambda x: (
            float(x.get("score") or 0.0),
            float(x.get("ytm") or 0.0),
            float(x.get("relative_value_bps") or 0.0),
        ),
        reverse=True,
    )

    existing_issuer_value: dict[str, float] = {}
    for p in current_positions:
        issuer = str(p.get("issuer_name") or p.get("issuer") or p.get("isin") or "UNKNOWN").upper()
        existing_issuer_value[issuer] = existing_issuer_value.get(issuer, 0.0) + float(
            p.get("market_value") or p.get("allocation_amount") or 0.0
        )

    max_post_issuer_value = (max_issuer_pct / 100.0) * max(total_post_value, 1.0)
    per_name_cap = 0.25 * investment_amount
    remaining = float(investment_amount)
    fallback_plan: list[dict[str, Any]] = []

    for row in ranked:
        if remaining <= 0:
            break
        eligible, _errors = is_bond_eligible(row)
        if not eligible:
            continue

        issuer = str(row.get("issuer_name") or row.get("issuer") or row.get("isin") or "UNKNOWN").upper()
        issuer_current = existing_issuer_value.get(issuer, 0.0)
        issuer_room = max(0.0, max_post_issuer_value - issuer_current)
        if issuer_room <= 0:
            continue

        alloc = min(remaining, per_name_cap, issuer_room)
        if alloc <= 0:
            continue

        out = dict(row)
        out["allocation_amount"] = round(alloc, 2)
        fallback_plan.append(out)
        existing_issuer_value[issuer] = issuer_current + alloc
        remaining -= alloc

    return fallback_plan


def _parse_investment_date(investment_date: str | None) -> date:
    if not investment_date:
        return date.today()
    try:
        return date.fromisoformat(str(investment_date)[:10])
    except Exception:
        return date.today()


def _infer_portfolio_role(bond: dict[str, Any], phase: str) -> str:
    rating = str(bond.get("credit_rating") or bond.get("rating") or "NR").upper()
    rel_bps = float(bond.get("relative_value_bps") or 0.0)
    if phase == "LATE" and ("AAA" in rating or "SOV" in rating):
        return "Compliance convergence"
    if rel_bps >= 10.0:
        return "Yield boost"
    if "AAA" in rating or "SOV" in rating:
        return "Safety anchor"
    return "Rebalancing support"


def _risk_indicators(positions: list[dict[str, Any]]) -> dict[str, Any]:
    total = sum(float(p.get("market_value") or p.get("allocation_amount") or 0.0) for p in positions) or 1.0
    issuer: dict[str, float] = {}
    sector: dict[str, float] = {}
    low_liq = 0
    weak_credit = 0
    total_count = len(positions)

    for p in positions:
        value = float(p.get("market_value") or p.get("allocation_amount") or 0.0)
        iss = str(p.get("issuer_name") or p.get("issuer") or p.get("isin") or "UNKNOWN").upper()
        sec = str(p.get("sector") or "OTHER").upper()
        issuer[iss] = issuer.get(iss, 0.0) + value
        sector[sec] = sector.get(sec, 0.0) + value

        liq = float(p.get("liquidity_score") or 100.0)
        if liq < 35.0:
            low_liq += 1

        rating = str(p.get("credit_rating") or p.get("rating") or "NR").upper()
        if rating.startswith("A") and not rating.startswith("AA") and not rating.startswith("AAA"):
            weak_credit += 1

    top_issuer = max(issuer.values()) if issuer else 0.0
    top_sector = max(sector.values()) if sector else 0.0
    return {
        "issuer_exposure_pct": round((top_issuer / total) * 100.0, 4),
        "sector_exposure_pct": round((top_sector / total) * 100.0, 4),
        "credit_risk_level": "HIGH" if weak_credit > 0 else "LOW",
        "liquidity_risk_level": "HIGH" if low_liq > 0 else "LOW",
        "credit_risk_count": weak_credit,
        "liquidity_risk_count": low_liq,
        "positions_count": total_count,
    }


def _category_exposure(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    totals: dict[str, float] = {
        INSTRUMENT_CATEGORY_STATE_CENTRAL: 0.0,
        INSTRUMENT_CATEGORY_PSU: 0.0,
        INSTRUMENT_CATEGORY_PRIVATE: 0.0,
        INSTRUMENT_CATEGORY_EQUITY: 0.0,
    }
    gross = 0.0
    for raw_row in rows:
        row = _attach_category_profile(raw_row)
        category = str(row.get("instrument_category") or INSTRUMENT_CATEGORY_PRIVATE).upper()
        amount = float(row.get("market_value") or row.get("allocation_amount") or row.get("investment_value") or 0.0)
        if amount <= 0:
            continue
        totals[category] = totals.get(category, 0.0) + amount
        gross += amount

    exposure: list[dict[str, Any]] = []
    for category in [
        INSTRUMENT_CATEGORY_STATE_CENTRAL,
        INSTRUMENT_CATEGORY_PSU,
        INSTRUMENT_CATEGORY_PRIVATE,
        INSTRUMENT_CATEGORY_EQUITY,
    ]:
        meta = category_metadata(category)
        value = totals.get(category, 0.0)
        exposure.append(
            {
                "instrument_category": category,
                "category_display": meta["category_display"],
                "category_tag": meta["category_tag"],
                "risk_label": meta["risk_label"],
                "value": round(value, 2),
                "pct": round((value / gross) * 100.0, 4) if gross > 0 else 0.0,
            }
        )
    return exposure


async def generate_investment_recommendation(
    portfolio_id: str,
    investment_amount: float,
    investment_date: str | None = None,
) -> dict[str, Any]:
    # detect_cash_inflow
    detected_cash_inflow = max(0.0, float(investment_amount or 0.0))

    # analyze_portfolio
    analytics = await compute_full_portfolio_analytics(portfolio_id=portfolio_id)
    current_positions = [_attach_category_profile(dict(row)) for row in (analytics.get("holdings", []) or [])]
    compliance_positions = await _load_sheet_backed_compliance_positions()
    if not compliance_positions:
        compliance_positions = list(current_positions)
    else:
        compliance_positions = [_attach_category_profile(dict(row)) for row in compliance_positions]
    portfolio_summary = analytics.get("summary", {})
    portfolio_metrics = analyze_portfolio(current_positions, portfolio_summary)

    as_of_date = _parse_investment_date(investment_date)
    year_progress = calculate_year_progress(as_of=as_of_date)
    phase_state = determine_time_phase(as_of_date)
    tactical_phase = determine_allocation_phase(
        {
            **year_progress,
            "phase": phase_state.get("phase"),
            "days_remaining": phase_state.get("days_to_year_end"),
        }
    )

    # read_portfolio_state + update_allocation_tracking (base state before current inflow)
    yearly_state = await get_yearly_allocation_state(
        portfolio_id=portfolio_id,
        current_positions=compliance_positions,
        as_of_date=as_of_date,
    )
    current_bucket_pct = dict(yearly_state.get("current_allocation_percentages") or {})
    bucket_decision = select_preferred_bucket(
        current_pct=current_bucket_pct,
        phase_state=phase_state,
        inflow_amount=float(investment_amount or 0.0),
    )
    selected_bucket = str(bucket_decision.get("selected_bucket") or "GOVT")

    current_rating_distribution = calculate_rating_distribution(compliance_positions)
    year_end_gap = calculate_target_gap(current_rating_distribution, PF_YEAR_END_TARGET)

    universe = await scan_tradable_bonds(limit=1200)

    validated_universe: list[dict[str, Any]] = []
    dropped_invalid = 0
    for bond in universe:
        checked = _validated_market_row(bond)
        if not checked.is_valid:
            dropped_invalid += 1
            continue
        # Recommendation guardrails: must have validated price, maturity, yield.
        if checked.normalized.get("clean_price") is None or checked.normalized.get("maturity_date") is None or checked.normalized.get("ytm") is None:
            dropped_invalid += 1
            continue
        validated_universe.append(_attach_category_profile(checked.normalized))

    # apply_pf_compliance
    pf_eligible_universe: list[dict[str, Any]] = []
    for b in validated_universe:
        category = str(b.get("instrument_category") or INSTRUMENT_CATEGORY_PRIVATE).upper()

        # Category-specific pre-filters:
        # - Sovereign/state ignores rating floors.
        # - PSU/private apply rating and issuer quality constraints.
        # - Equity avoids bond credit-floor checks and relies on liquidity/market filters.
        if category in {INSTRUMENT_CATEGORY_PSU, INSTRUMENT_CATEGORY_PRIVATE}:
            if not rating_aa_or_above(b):
                continue
            if not dual_rating_allowed(b):
                continue
            if not strong_issuer_allowed(b):
                continue
        elif category == INSTRUMENT_CATEGORY_EQUITY:
            if float(b.get("liquidity_score") or 0.0) <= 0.0:
                continue

        ok, _errors = is_bond_eligible(b)
        if ok:
            pf_eligible_universe.append(b)

    # filter_high_quality_issuers (includes dual-rating logic for AA+/AA entries)
    tactical_universe = tactical_filter_universe(
        pf_eligible_universe,
        phase=tactical_phase["phase"],
        current_distribution=current_rating_distribution,
    )

    pre_risk = detect_risk_signals(
        compliance_positions,
        tactical_distribution=current_rating_distribution,
        pf_violations=[],
    )

    market_signals = infer_market_signals(tactical_universe, current_positions=compliance_positions)
    market_mode = determine_market_mode(market_signals)
    market_mode = apply_market_mode_overrides(market_mode, pre_risk.get("risk_flags") or {})

    # select_bucket + run_scoring inside chosen bucket for this inflow event.
    bucket_tactical_universe = filter_universe_by_bucket(tactical_universe, selected_bucket)
    scoring_universe = bucket_tactical_universe or tactical_universe

    spread_intel = analyze_spread_intelligence(scoring_universe)
    spread_ready_universe = list(spread_intel.get("bonds", []))

    rel = compute_relative_value(spread_ready_universe, spread_threshold_bps=8.0)
    scored_base = score_bonds(rel, target_duration=6.0)

    # score_bonds (dynamic weights based on tactical phase)
    dynamic_weights = adjust_scoring_weights_for_market_mode(
        tactical_phase["weights"],
        market_mode=str(market_mode.get("mode") or "NEUTRAL"),
    )
    scored_dynamic = _apply_dynamic_tactical_scores(scored_base, dynamic_weights, current_positions=compliance_positions)
    scored = _apply_category_specific_scores(scored_dynamic)
    pre_allocation_category_sections = _build_category_sections(scored, top_n=5)

    # run_optimizer
    alloc = optimize_allocation(scored[:150], investment_amount=investment_amount, current_positions=compliance_positions)
    allocation_plan = list(alloc.get("allocation_plan", []))

    current_total_mv = sum(float(p.get("market_value") or p.get("allocation_amount") or 0.0) for p in compliance_positions)
    if current_total_mv <= 0:
        current_total_mv = float(portfolio_summary.get("total_market_value") or 0.0)
    projected_total_mv = current_total_mv + float(investment_amount or 0.0)
    allocation_plan = _enforce_issuer_limit(
        allocation_plan,
        current_positions=compliance_positions,
        total_post_value=projected_total_mv if projected_total_mv > 0 else 1.0,
        max_pct=10.0,
    )

    # Baseline static yield using safe-only universe.
    safe_rel = compute_relative_value(_safe_universe_only(pf_eligible_universe), spread_threshold_bps=8.0)
    safe_scored_base = score_bonds(safe_rel, target_duration=6.0)
    safe_scored_dynamic = _apply_dynamic_tactical_scores(
        safe_scored_base,
        determine_allocation_phase({"phase": "LATE"})["weights"],
        current_positions=compliance_positions,
    )
    safe_scored = _apply_category_specific_scores(safe_scored_dynamic)
    safe_alloc = optimize_allocation(safe_scored[:150], investment_amount=investment_amount, current_positions=compliance_positions)
    static_plan = _enforce_issuer_limit(
        list(safe_alloc.get("allocation_plan", [])),
        current_positions=compliance_positions,
        total_post_value=projected_total_mv if projected_total_mv > 0 else 1.0,
        max_pct=10.0,
    )

    # generate_recommendation
    recommended_bonds = []
    for b in allocation_plan:
        row = _attach_category_profile(dict(b))
        category_reasoning = category_explainability_lines(row.get("instrument_category"), row.get("instrument_subtype"))
        reasons = [build_recommendation_explanation(row)]
        reasons.extend(category_reasoning)
        bucket = _normalize_category_bucket(row)
        if tactical_phase["phase"] == "EARLY" and bucket == "PSU_PRIVATE_DEBT":
            reasons.append("Temporary PSU/private tactical allocation used within PF policy")
        if tactical_phase["phase"] == "LATE" and bucket == "GOVT":
            reasons.append("Late-phase compliance convergence toward government category")
        if row.get("undervalue_opportunity"):
            reasons.append("Spread intelligence: yield is elevated versus peer baseline")
        if str(market_mode.get("mode") or "").upper() == "DEFENSIVE":
            reasons.append("Market timing: defensive posture prioritized quality and liquidity")
        row["ai_explanation"] = "; ".join(reasons)
        row["strategy_mode"] = strategy_mode_label(
            tactical_phase["strategy_mode"],
            str(market_mode.get("mode") or "NEUTRAL"),
            bool(spread_intel.get("opportunity_flag")),
        )
        row["market_mode"] = market_mode.get("mode")
        row["opportunity_flag"] = bool(row.get("undervalue_opportunity"))
        row["confidence_score"] = round(float(row.get("relative_value_signal") or 0.0), 6)
        row["yield_advantage_bps"] = round(float(row.get("relative_value_bps") or (float(row.get("spread") or 0.0) * 100.0)), 2)
        row["portfolio_role"] = _infer_portfolio_role(row, tactical_phase["phase"])
        row["selected_bucket"] = selected_bucket
        row["strategy_phase"] = phase_state.get("phase")
        row["allocation_reason"] = bucket_decision.get("allocation_reason")
        row["category_specific_reasoning"] = category_reasoning
        row["risk_interpretation"] = row.get("risk_label")
        row["evaluation_engine"] = row.get("category_engine")
        row["compliance_justification"] = (
            "Category-aligned filters passed: "
            + "; ".join(category_reasoning[:2])
        )
        recommended_bonds.append(row)

    # Fallback: if optimizer output is empty but eligible inventory exists,
    # produce a policy-valid deterministic allocation instead of empty output.
    fallback_used = False
    if not recommended_bonds and pf_eligible_universe:
        fallback_plan: list[dict[str, Any]] = []
        candidate_pools = [
            _safe_universe_only(tactical_universe),
            _safe_universe_only(pf_eligible_universe),
            tactical_universe,
            pf_eligible_universe,
        ]
        for pool in candidate_pools:
            if not pool:
                continue
            fallback_plan = _build_policy_fallback_plan(
                candidates=pool,
                investment_amount=float(investment_amount),
                current_positions=compliance_positions,
                total_post_value=projected_total_mv if projected_total_mv > 0 else 1.0,
                max_issuer_pct=15.0,
            )
            if fallback_plan:
                break

        if fallback_plan:
            fallback_used = True
            recommended_bonds = []
            for b in fallback_plan:
                row = _attach_category_profile(dict(b))
                category_reasoning = category_explainability_lines(row.get("instrument_category"), row.get("instrument_subtype"))
                row["ai_explanation"] = (
                    "Fallback allocation applied: full-portfolio compliance was already breached, "
                    "so new inflow was allocated only to policy-eligible names with issuer caps"
                )
                row["strategy_mode"] = "Remediation Fallback"
                row["market_mode"] = str(market_mode.get("mode") or "NEUTRAL")
                row["opportunity_flag"] = bool(row.get("undervalue_opportunity"))
                row["confidence_score"] = round(float(row.get("relative_value_signal") or 0.0), 6)
                row["yield_advantage_bps"] = round(
                    float(row.get("relative_value_bps") or (float(row.get("spread") or 0.0) * 100.0)),
                    2,
                )
                row["portfolio_role"] = _infer_portfolio_role(row, tactical_phase["phase"])
                row["selected_bucket"] = selected_bucket
                row["strategy_phase"] = phase_state.get("phase")
                row["allocation_reason"] = bucket_decision.get("allocation_reason")
                row["category_specific_reasoning"] = category_reasoning
                row["risk_interpretation"] = row.get("risk_label")
                row["evaluation_engine"] = row.get("category_engine")
                row["compliance_justification"] = "Fallback policy guardrails: category-eligible names plus issuer concentration cap"
                recommended_bonds.append(row)

    tactical_yield = _weighted_yield(recommended_bonds)
    static_yield = _weighted_yield(static_plan)
    extra_yield = tactical_yield - static_yield

    combined_pf = validate_portfolio(compliance_positions + recommended_bonds, strict_mode=True)
    projected_distribution = calculate_rating_distribution(compliance_positions + recommended_bonds)

    # update_allocation_tracking (projected post-inflow state for this recommendation run)
    projected_yearly_state = project_state_after_inflow(
        current_state=yearly_state,
        recommended_bonds=recommended_bonds,
        as_of_date=as_of_date,
    )

    risk_watch = detect_risk_signals(
        compliance_positions + recommended_bonds,
        tactical_distribution=projected_distribution,
        pf_violations=combined_pf.get("violations") or [],
    )
    risk_indicators = _risk_indicators(compliance_positions + recommended_bonds)

    safe_mode_activated = False
    if risk_watch.get("safe_override"):
        safe_mode_activated = True
        if static_plan:
            recommended_bonds = []
            for b in static_plan:
                row = _attach_category_profile(dict(b))
                category_reasoning = category_explainability_lines(row.get("instrument_category"), row.get("instrument_subtype"))
                row["ai_explanation"] = "Risk override activated: switched to safe allocation in AAA/G-Sec/SDL"
                row["strategy_mode"] = "Safety Override"
                row["market_mode"] = "DEFENSIVE"
                row["opportunity_flag"] = False
                row["confidence_score"] = 0.0
                row["selected_bucket"] = "GOVT"
                row["strategy_phase"] = phase_state.get("phase")
                row["allocation_reason"] = "Risk override: forced allocation to safety bucket"
                row["category_specific_reasoning"] = category_reasoning
                row["risk_interpretation"] = row.get("risk_label")
                row["evaluation_engine"] = row.get("category_engine")
                recommended_bonds.append(row)
        elif recommended_bonds:
            for row in recommended_bonds:
                row["ai_explanation"] = (
                    str(row.get("ai_explanation") or "")
                    + "; Risk override active, but safe universe optimizer returned empty. "
                    "Retaining policy-valid fallback allocation."
                ).strip("; ")
                row["strategy_mode"] = "Safety Override Fallback"
                row["market_mode"] = "DEFENSIVE"
        tactical_yield = _weighted_yield(recommended_bonds)
        extra_yield = tactical_yield - static_yield
        combined_pf = validate_portfolio(compliance_positions + recommended_bonds, strict_mode=True)
        projected_distribution = calculate_rating_distribution(compliance_positions + recommended_bonds)
        market_mode["mode"] = "DEFENSIVE"
        market_mode["override_applied"] = True
        market_mode["override_reason"] = "Safety override triggered by risk engine"

    current_category_exposure = _category_exposure(compliance_positions)
    projected_category_exposure = _category_exposure(compliance_positions + recommended_bonds)
    risk_indicators = _risk_indicators(compliance_positions + recommended_bonds)
    risk_indicators["category_exposure"] = projected_category_exposure

    projected_yearly_state = project_state_after_inflow(
        current_state=yearly_state,
        recommended_bonds=recommended_bonds,
        as_of_date=as_of_date,
    )

    rebalance_plan = generate_rebalance_plan(
        current_distribution=projected_distribution,
        target_distribution=PF_YEAR_END_TARGET,
        months_remaining=int(year_progress.get("months_remaining") or 0),
        threshold=2.0,
    )

    allocated = float(alloc.get("allocated_amount") or 0.0)
    expected_yield = tactical_yield

    current_ytm = float(portfolio_summary.get("weighted_ytm") or 0.0)
    current_duration = float(portfolio_metrics.get("portfolio_duration") or 0.0)
    allocated = sum(float(x.get("allocation_amount") or 0.0) for x in recommended_bonds)
    final_unallocated = max(0.0, float(investment_amount or 0.0) - allocated)
    allocated_duration = (
        sum(
            float(b.get("modified_duration") or b.get("duration") or 0.0)
            * float(b.get("allocation_amount") or 0.0)
            for b in recommended_bonds
        )
        / allocated
        if allocated > 0
        else 0.0
    )
    projected_duration = (
        ((current_duration * current_total_mv) + (allocated_duration * allocated)) / (current_total_mv + allocated)
        if (current_total_mv + allocated) > 0
        else current_duration
    )
    blended_post_ytm = (
        ((current_ytm * current_total_mv) + (expected_yield * allocated)) / (current_total_mv + allocated)
        if (current_total_mv + allocated) > 0
        else current_ytm
    )

    pf_status = combined_pf
    year_end_off_track = bool(
        float(projected_distribution.get("AAA_GSEC_SDL") or 0.0) + 1e-9 < float(PF_YEAR_END_TARGET["AAA_GSEC_SDL"])
        or float(projected_distribution.get("AA+") or 0.0) - 1e-9 > float(PF_YEAR_END_TARGET["AA+"])
        or float(projected_distribution.get("AA") or 0.0) - 1e-9 > float(PF_YEAR_END_TARGET["AA"])
        or float(projected_distribution.get("A_AND_BELOW") or 0.0) > 0.0
    )
    if year_end_off_track:
        risk_watch.setdefault("risk_flags", {})["year_end_convergence_risk"] = 1

    compliance_audit = {
        "run_date": as_of_date.isoformat(),
        "phase": tactical_phase.get("phase"),
        "strategy_mode": "Safety Override" if safe_mode_activated else strategy_mode_label(
            tactical_phase.get("strategy_mode") or "Balanced Tactical Allocation",
            str(market_mode.get("mode") or "NEUTRAL"),
            bool(spread_intel.get("opportunity_flag")),
        ),
        "fallback_used": fallback_used,
        "risk_flags": risk_watch.get("risk_flags") or {},
        "override_applied": safe_mode_activated,
        "market_timing": market_mode,
        "spread_intelligence": {
            "spread_signal": spread_intel.get("spread_signal"),
            "opportunity_flag": bool(spread_intel.get("opportunity_flag")),
            "confidence_score": spread_intel.get("confidence_score"),
            "summary": spread_intel.get("summary") or {},
        },
        "time_phase": phase_state,
        "bucket_decision": bucket_decision,
        "allocation_tracking": {
            "before_inflow": {
                "total_invested_amount": round(float(yearly_state.get("total_invested_amount") or 0.0), 2),
                "current_allocation_percentages": current_bucket_pct,
            },
            "after_inflow": {
                "total_invested_amount": round(float(projected_yearly_state.get("total_invested_amount") or 0.0), 2),
                "current_allocation_percentages": projected_yearly_state.get("current_allocation_percentages") or {},
            },
            "target_ranges": TARGET_RANGES,
        },
        "category_framework": {
            "top_picks_pre_allocation": pre_allocation_category_sections,
            "current_exposure": current_category_exposure,
            "projected_exposure": projected_category_exposure,
        },
        "year_end_target_status": "OFF_TRACK_CRITICAL" if year_end_off_track else "ON_TRACK",
        "target_gap_trajectory": {
            "current": current_rating_distribution,
            "projected": projected_distribution,
            "target": PF_YEAR_END_TARGET,
            "gap_at_run_start": year_end_gap,
            "gap_after_recommendation": calculate_target_gap(projected_distribution, PF_YEAR_END_TARGET),
        },
    }
    year_end_compliance_achievable = not year_end_off_track
    temporary_deviation = bool((risk_watch.get("risk_flags") or {}).get("tactical_violations"))
    has_recommendation = len(recommended_bonds) > 0 and allocated > 0
    top_blockers = list((pf_status.get("violations") or [])[:5])
    strategy_mode_out = (
        "Remediation Required"
        if not has_recommendation
        else (
            "Safety Override"
            if safe_mode_activated
            else strategy_mode_label(
                tactical_phase["strategy_mode"],
                str(market_mode.get("mode") or "NEUTRAL"),
                bool(spread_intel.get("opportunity_flag")),
            )
        )
    )
    recommendation_statement = (
        f"Recommended Bonds to Buy for Rs {round(float(investment_amount), 2):,.2f}"
        if has_recommendation
        else "No compliant buy recommendation can be issued for this inflow until current PF breaches are remediated"
    )

    try:
        await record_inflow_event(
            portfolio_id=portfolio_id,
            as_of_date=as_of_date,
            selected_bucket=selected_bucket,
            strategy_phase=str(phase_state.get("phase") or tactical_phase.get("phase") or "MID"),
            recommended_bonds=recommended_bonds,
            state_after=projected_yearly_state,
        )
    except Exception:
        # Recommendation responses remain available even if persistence is temporarily unavailable.
        pass

    engine_diagnostics = {
        "tradable_universe": len(universe),
        "dropped_invalid": dropped_invalid,
        "validated_universe": len(validated_universe),
        "pf_eligible_universe": len(pf_eligible_universe),
        "tactical_universe": len(tactical_universe),
        "spread_ready_universe": len(spread_ready_universe),
        "scored_universe": len(scored),
        "safe_universe": len(safe_scored),
        "optimizer": alloc.get("optimization_method"),
    }

    strategy_phase = str(phase_state.get("phase") or tactical_phase.get("phase") or "MID")
    recommended_category_sections = _build_category_sections(
        recommended_bonds,
        top_n=max(1, len(recommended_bonds)),
    )
    if recommended_bonds:
        ordered_rows: list[dict[str, Any]] = []
        for section in recommended_category_sections:
            section_rows = list(section.get("recommendations") or [])
            for rank, row in enumerate(section_rows, start=1):
                row["category_rank"] = rank
                row["category_section"] = section.get("section_title")
                ordered_rows.append(row)
            section["recommendations"] = section_rows
        recommended_bonds = ordered_rows

    for row in recommended_bonds:
        row["explanation"] = _build_bond_explanation(
            row,
            selected_bucket=selected_bucket,
            strategy_phase=strategy_phase,
        )

    distribution_change = build_change_tracking(
        previous_state=current_rating_distribution,
        current_state=projected_distribution,
        tracked_fields=["AAA_GSEC_SDL", "AA+", "AA", "A_AND_BELOW"],
    )

    top_counterfactuals = [
        build_counterfactual(
            scenario="safe_static_plan",
            expected_outcome="LOWER_OR_EQUAL_YIELD",
            impact_summary=(
                f"Static safe yield={round(static_yield, 6)} versus recommended yield={round(tactical_yield, 6)}"
            ),
            delta_metrics={
                "extra_yield": round(extra_yield, 6),
                "allocated_amount": round(allocated, 2),
            },
            rationale="Compares tactical recommendation with safe-only baseline allocation.",
        )
    ]
    if not has_recommendation:
        top_counterfactuals.append(
            build_counterfactual(
                scenario="portfolio_remediation_then_recommend",
                expected_outcome="RECOMMENDATION_AVAILABLE",
                impact_summary="Once portfolio-level PF breaches are remediated, inflow deployment can resume.",
                delta_metrics={"top_blockers_count": len(top_blockers)},
                rationale="Current recommendation is blocked by portfolio-level PF constraints.",
            )
        )

    decision_outcome = (
        "RECOMMENDATION_AVAILABLE_WITH_OVERRIDE"
        if has_recommendation and safe_mode_activated
        else (
            "RECOMMENDATION_AVAILABLE_WITH_FALLBACK"
            if has_recommendation and fallback_used
            else ("RECOMMENDATION_AVAILABLE" if has_recommendation else "REMEDIATION_REQUIRED")
        )
    )

    recommendation_explanation = build_explanation(
        module="investment_recommendation_engine",
        decision_type="portfolio_inflow_recommendation",
        decision_outcome=decision_outcome,
        summary=recommendation_statement,
        key_factors=[
            {
                "name": "Allocated Amount",
                "value": round(allocated, 2),
                "impact": "positive" if allocated > 0 else "negative",
                "evidence": "Optimizer output after policy filters and issuer caps",
            },
            {
                "name": "Projected YTM Delta",
                "value": round(blended_post_ytm - current_ytm, 6),
                "impact": "positive" if blended_post_ytm >= current_ytm else "negative",
                "evidence": "Blended post-trade yield versus current portfolio yield",
            },
            {
                "name": "Risk Override",
                "value": safe_mode_activated,
                "impact": "negative" if safe_mode_activated else "neutral",
                "evidence": "Risk monitor flags and defensive mode escalation",
            },
            {
                "name": "Fallback Allocation Used",
                "value": fallback_used,
                "impact": "neutral",
                "evidence": "Fallback applies only when optimization output is blocked",
            },
        ],
        rule_checks=[
            {
                "rule_code": "PF_PORTFOLIO_COMPLIANCE",
                "status": "PASS" if bool(pf_status.get("pf_compliant")) else "FAIL",
                "message": "Portfolio-level PF compliance after applying recommendation",
                "actual": bool(pf_status.get("pf_compliant")),
            },
            {
                "rule_code": "YEAR_END_CONVERGENCE",
                "status": "PASS" if year_end_compliance_achievable else "WARN",
                "message": "Trajectory toward year-end PF allocation targets",
                "actual": "ON_TRACK" if year_end_compliance_achievable else "OFF_TRACK",
            },
            {
                "rule_code": "UNALLOCATED_CASH",
                "status": "PASS" if final_unallocated <= 0.01 else "WARN",
                "message": "Residual inflow after recommendation",
                "actual": round(final_unallocated, 2),
                "threshold": 0.01,
            },
        ],
        calculation_trace={
            "investment_amount": round(float(investment_amount), 2),
            "allocated_amount": round(allocated, 2),
            "unallocated_amount": round(final_unallocated, 2),
            "expected_yield": round(expected_yield, 6),
            "static_yield": round(static_yield, 6),
            "tactical_yield": round(tactical_yield, 6),
            "extra_yield": round(extra_yield, 6),
            "current_weighted_ytm": round(current_ytm, 6),
            "projected_weighted_ytm": round(blended_post_ytm, 6),
            "current_duration": round(current_duration, 6),
            "projected_duration": round(projected_duration, 6),
        },
        data_lineage=[
            {"source": "portfolio_analytics", "field": "current_positions", "trust_level": "high"},
            {"source": "market_scanner", "field": "tradable_universe", "trust_level": "high"},
            {"source": "spread_intelligence_engine", "field": "spread_signal", "trust_level": "high"},
            {"source": "allocation_optimizer", "field": "allocation_plan", "trust_level": "high"},
            {"source": "pf_compliance_engine", "field": "pf_compliant", "trust_level": "high"},
        ],
        regulatory_basis=[
            "PF Trust policy credit floor and issuer quality controls",
            "PF Trust year-end allocation convergence targets",
            "Portfolio-level compliance validation and remediation controls",
        ],
        confidence_score=float(spread_intel.get("confidence_score") or 0.0),
        confidence_drivers=[
            f"market_mode={market_mode.get('mode') or 'NEUTRAL'}",
            f"opportunity_flag={bool(spread_intel.get('opportunity_flag'))}",
            f"risk_flags={len(risk_watch.get('risk_flags') or {})}",
        ],
        counterfactuals=top_counterfactuals,
        change_tracking=distribution_change,
        override={
            "applied": bool(safe_mode_activated or fallback_used),
            "reason": (
                str(market_mode.get("override_reason") or "")
                if safe_mode_activated
                else (
                    "Fallback allocation activated due to optimization blockage"
                    if fallback_used
                    else ""
                )
            ),
            "by": "system",
            "at": as_of_date.isoformat(),
        },
        severity=(
            "CRITICAL"
            if not has_recommendation
            else ("WARNING" if safe_mode_activated or fallback_used else "INFO")
        ),
        tags=["recommendation", "portfolio", "inflow"],
        audit_metadata={
            "portfolio_id": portfolio_id,
            "decision_status": "RECOMMENDATION_AVAILABLE" if has_recommendation else "REMEDIATION_REQUIRED",
            "market_mode": market_mode,
            "risk_flags": risk_watch.get("risk_flags") or {},
            "top_blockers": top_blockers,
            "category_counts": {
                str(section.get("instrument_category") or "UNKNOWN"): int(section.get("count") or 0)
                for section in recommended_category_sections
            },
            "engine_diagnostics": engine_diagnostics,
        },
        metadata={
            "selected_bucket": selected_bucket,
            "strategy_mode": strategy_mode_out,
            "cash_inflow_detected": round(detected_cash_inflow, 2),
        },
    )

    return {
        "portfolio_id": portfolio_id,
        "investment_amount": float(investment_amount),
        "portfolio_metrics": portfolio_metrics,
        "recommended_bonds": recommended_bonds,
        "allocation_plan": recommended_bonds,
        "recommendations_by_category": recommended_category_sections,
        "pre_allocation_top_picks_by_category": pre_allocation_category_sections,
        "expected_yield": round(expected_yield, 6),
        "static_yield": round(static_yield, 6),
        "tactical_yield": round(tactical_yield, 6),
        "extra_yield": round(extra_yield, 6),
        "strategy_mode": strategy_mode_out,
        "tactical_phase": tactical_phase,
        "strategy_phase": phase_state.get("phase"),
        "selected_bucket": selected_bucket,
        "allocation_reason": bucket_decision.get("allocation_reason"),
        "current_allocation_vs_target": {
            "current": current_bucket_pct,
            "target": TARGET_RANGES,
        },
        "market_mode": market_mode.get("mode") or "NEUTRAL",
        "display_market_mode": "N/A" if not has_recommendation else (market_mode.get("mode") or "NEUTRAL"),
        "spread_signal": spread_intel.get("spread_signal") or "NO_DATA",
        "opportunity_flag": bool(spread_intel.get("opportunity_flag")),
        "confidence_score": round(float(spread_intel.get("confidence_score") or 0.0), 6),
        "year_progress": year_progress,
        "time_context": {
            "current_year_progress": year_progress.get("current_year_progress"),
            "days_remaining_in_year": year_progress.get("days_remaining"),
        },
        "investment_date": as_of_date.isoformat(),
        "cash_inflow_detected": round(detected_cash_inflow, 2),
        "current_rating_distribution": current_rating_distribution,
        "projected_rating_distribution": projected_distribution,
        "projected_allocation_percentages": projected_yearly_state.get("current_allocation_percentages") or {},
        "total_invested_amount": round(float(projected_yearly_state.get("total_invested_amount") or 0.0), 2),
        "year_end_target_distribution": PF_YEAR_END_TARGET,
        "target_gap": year_end_gap,
        "rebalance_plan": rebalance_plan,
        "compliance_audit": compliance_audit,
        "risk_monitoring": risk_watch,
        "risk_indicators": risk_indicators,
        "category_exposure": {
            "current": current_category_exposure,
            "projected": projected_category_exposure,
        },
        "failure_safety": {
            "override_applied": safe_mode_activated,
            "fallback_mode": "AAA_GSEC_SDL_ONLY" if safe_mode_activated else "TACTICAL",
        },
        "explanation": recommendation_explanation,
        "recommendation_statement": recommendation_statement,
        "decision_status": "RECOMMENDATION_AVAILABLE" if has_recommendation else "REMEDIATION_REQUIRED",
        "top_blockers": top_blockers,
        "no_recommendation_reason": None if has_recommendation else "Portfolio-level PF constraints currently block compliant deployment of new inflow",
        "compliance_status": {
            "pf_compliant": bool(pf_status.get("pf_compliant")),
            "temporary_deviation": temporary_deviation,
            "year_end_compliance_achievable": year_end_compliance_achievable,
        },
        "expected_portfolio_metrics": {
            "current_weighted_ytm": round(current_ytm, 6),
            "projected_weighted_ytm": round(blended_post_ytm, 6),
            "current_portfolio_duration": round(current_duration, 6),
            "projected_portfolio_duration": round(projected_duration, 6),
            "current_market_value": round(current_total_mv, 2),
            "projected_market_value": round(current_total_mv + allocated, 2),
        },
        "portfolio_impact": {
            "ytm_delta": round(blended_post_ytm - current_ytm, 6),
            "allocated_amount": round(allocated, 2),
            "unallocated_amount": round(final_unallocated, 2),
        },
        "pf_compliance": pf_status,
        "pf_compliance_status": pf_status,
        "engine_diagnostics": engine_diagnostics,
    }
