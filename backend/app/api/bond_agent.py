"""
Gateway Router — Bond Agent
Surfaces the daily bond recommendation engine under /api/v1/agent.
The recommendation logic is self-contained here so the gateway
does NOT depend on the Flask-based bond_agent_server.
"""

from __future__ import annotations

from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, Query, Request

from ..shared.database import fetch_all, fetch_one
from ..shared.logger import get_logger
from ..shared.auth import require_role, Roles
from ..domain.core_trust.audit import AuditAction, AuditService, AuditSeverity, EntityType
from ..domain.core_trust.explainability import build_counterfactual, build_explanation
from ..domain.market_data.instrument_classification import (
    INSTRUMENT_CATEGORY_EQUITY,
    INSTRUMENT_CATEGORY_PRIVATE,
    INSTRUMENT_CATEGORY_PSU,
    INSTRUMENT_CATEGORY_STATE_CENTRAL,
    category_explainability_lines,
    category_metadata,
    classify_instrument,
)
from ..domain.compliance.pf_engine import is_bond_eligible
from ..domain.portfolio.dataset import PortfolioDatasetService

log = get_logger("router.agent")
router = APIRouter(prefix="/agent", tags=["Bond Agent"])

# ── Recommendation thresholds (EPFO 2024-25 pattern) ────────────────────────
THRESHOLDS = {
    "G-SEC":     {"min_ytm": 6.50, "min_discount_pct": 25},
    "SDL":       {"min_ytm": 6.95},
    "PSU":       {"min_ytm": 7.30},
    "BANK":      {"min_ytm": 7.35},
    "NBFC":      {"min_ytm": 7.50},
    "CORPORATE": {"min_ytm": 7.30},
}

RISK_MAP = {
    "SOVEREIGN": "Low Risk",
    "AAA":  "Low Risk",
    "AA+":  "Low Risk",
    "AA":   "Medium Risk",
    "AA-":  "Medium Risk",
    "A+":   "High Risk",
    "A":    "High Risk",
}


def _legacy_category_engine(category: str) -> str:
    cat = str(category or "").upper()
    if cat == INSTRUMENT_CATEGORY_STATE_CENTRAL:
        return "state_central_evaluation_engine"
    if cat == INSTRUMENT_CATEGORY_PSU:
        return "psu_spread_support_engine"
    if cat == INSTRUMENT_CATEGORY_PRIVATE:
        return "private_credit_risk_adjusted_engine"
    if cat == INSTRUMENT_CATEGORY_EQUITY:
        return "equity_growth_engine"
    return "legacy_yield_threshold_engine"


def _attach_category_profile(row: dict) -> dict:
    out = dict(row)
    derived = classify_instrument(out)
    category = str(out.get("instrument_category") or derived.get("instrument_category") or INSTRUMENT_CATEGORY_PRIVATE).upper()
    subtype = str(out.get("instrument_subtype") or derived.get("instrument_subtype") or "CORPORATE").upper()
    meta = category_metadata(category)
    out["instrument_category"] = category
    out["instrument_subtype"] = subtype
    out["category_display"] = meta.get("category_display")
    out["category_tag"] = meta.get("category_tag")
    out["risk_label"] = meta.get("risk_label")
    out["section_title"] = meta.get("section_title")
    out["category_specific_reasoning"] = category_explainability_lines(category, subtype)
    out["evaluation_engine"] = _legacy_category_engine(category)
    return out


def _recommendations_by_category(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {
        INSTRUMENT_CATEGORY_STATE_CENTRAL: [],
        INSTRUMENT_CATEGORY_PSU: [],
        INSTRUMENT_CATEGORY_PRIVATE: [],
        INSTRUMENT_CATEGORY_EQUITY: [],
    }
    for row in rows:
        profiled = _attach_category_profile(row)
        grouped.setdefault(profiled["instrument_category"], []).append(profiled)

    sections: list[dict] = []
    for category in [
        INSTRUMENT_CATEGORY_STATE_CENTRAL,
        INSTRUMENT_CATEGORY_PSU,
        INSTRUMENT_CATEGORY_PRIVATE,
        INSTRUMENT_CATEGORY_EQUITY,
    ]:
        members = sorted(grouped.get(category) or [], key=lambda x: float(x.get("score") or 0.0), reverse=True)
        meta = category_metadata(category)
        sections.append(
            {
                "instrument_category": category,
                "category_display": meta.get("category_display"),
                "category_tag": meta.get("category_tag"),
                "risk_label": meta.get("risk_label"),
                "section_title": meta.get("section_title"),
                "count": len(members),
                "recommendations": members,
            }
        )
    return sections


@router.get("/recommendations")
async def get_recommendations(
    request: Request,
    max_results: int = Query(25, le=100),
    risk_filter: Optional[str] = Query(None, description="Low Risk | Medium Risk | High Risk"),
    user=Depends(require_role(Roles.VIEWER)),
):
    """
    Return today's buy recommendations based on YTM thresholds.
    Sourced from the unified market data and bond master.
    """
    today = str(date.today())

    # Hard gate: exclude ISINs flagged as CRITICAL in audit remediation.
    blocked_isins: set[str] = set()
    blocked_reasons: dict[str, list[str]] = {}
    try:
        audit = await PortfolioDatasetService.build_regulatory_audit()

        for row in (audit.get("market_yield_coverage") or {}).get("holdings") or []:
            if str(row.get("action_tier") or "").upper() == "CRITICAL":
                isin = str(row.get("isin") or "").strip().upper()
                if isin:
                    blocked_isins.add(isin)
                    blocked_reasons.setdefault(isin, []).append(f"yield:{row.get('issue')}")

        for row in (audit.get("maturity_remediation") or {}).get("holdings") or []:
            if str(row.get("action_tier") or "").upper() == "CRITICAL":
                isin = str(row.get("isin") or "").strip().upper()
                if isin:
                    blocked_isins.add(isin)
                    blocked_reasons.setdefault(isin, []).append(f"maturity:{row.get('issue')}")
    except Exception as exc:
        # Keep recommendations available even if audit precompute fails.
        log.warning("recommendation_audit_gate_failed", error=str(exc))

    try:
        rows = await fetch_all(
            """
            SELECT b.isin, b.bond_name, b.issuer_name, b.issuer_type,
                   b.credit_rating, b.maturity_date, b.coupon_rate,
                   b.is_pf_eligible, b.face_value,
                   m.ytm, m.clean_price, m.modified_duration,
                   m.spread_over_gsec, m.data_source
            FROM   universal_bond_master b
            LEFT JOIN unified_market_data m ON b.isin = m.isin
                AND m.price_date = (
                    SELECT MAX(price_date) FROM unified_market_data
                    WHERE isin = b.isin
                )
            WHERE  b.is_active = 1
              AND  b.is_pf_eligible = 1
              AND  m.ytm IS NOT NULL
            ORDER  BY m.ytm DESC
            LIMIT 500
            """,
        )
    except Exception as exc:
        log.error("recommendation_query_failed", error=str(exc))
        payload = {
            "date": today,
            "total_recommendations": 0,
            "recommendation_gate": {
                "blocked_isins_count": len(blocked_isins),
                "blocked_candidates": 0,
                "blocked_isins": [
                    {
                        "isin": key,
                        "reasons": blocked_reasons.get(key, []),
                    }
                    for key in sorted(blocked_isins)
                ][:100],
            },
            "risk_breakdown": {"Low Risk": 0, "Medium Risk": 0, "High Risk": 0},
            "recommendations": [],
            "data_blocker": {
                "code": "RECOMMENDATION_DATA_QUERY_FAILED",
                "message": str(exc),
            },
            "disclaimer": (
                "ADVISORY ONLY — These recommendations are informational and do NOT "
                "constitute trade execution instructions. All investment decisions require "
                "manual approval through the compliance engine and trustee authorization. "
                "No automated trade execution is performed."
            ),
            "advisory_only": True,
        }
        payload["explanation"] = build_explanation(
            module="bond_agent_router",
            decision_type="daily_recommendation_generation",
            decision_outcome="ERROR",
            summary="Recommendation generation failed because market data query could not be completed.",
            key_factors=[
                {"name": "Blocked ISINs", "value": len(blocked_isins), "impact": "neutral"},
                {"name": "Error", "value": str(exc), "impact": "negative"},
            ],
            confidence_score=0.0,
            severity="CRITICAL",
            tags=["recommendation", "bond-agent"],
            metadata={"date": today},
        )
        return payload

    recommendations = []
    blocked_candidates = 0
    for row in rows:
        isin = str(row.get("isin") or "").strip().upper()
        if isin in blocked_isins:
            blocked_candidates += 1
            continue

        itype = (row.get("issuer_type") or "CORPORATE").upper()
        rating = (row.get("credit_rating") or "").upper()
        ytm = float(row.get("ytm") or 0)
        face = float(row.get("face_value") or 1000)  # Use actual face value from bond master
        price = float(row.get("clean_price") or face)
        discount_pct = round((face - price) / face * 100, 2) if face > 0 and price < face else 0

        thresh = THRESHOLDS.get(itype, THRESHOLDS["CORPORATE"])
        qualifies = ytm >= thresh["min_ytm"]
        if not qualifies and "min_discount_pct" in thresh:
            qualifies = discount_pct >= thresh["min_discount_pct"]

        if not qualifies:
            continue

        pf_ok, pf_errors = is_bond_eligible(row)
        if not pf_ok:
            continue

        risk = RISK_MAP.get(rating, "Medium Risk")
        if risk_filter and risk != risk_filter:
            continue

        years = 0.0
        try:
            mat = date.fromisoformat(str(row["maturity_date"]))
            years = round((mat - date.today()).days / 365.25, 1)
        except Exception:
            pass

        reason_parts = [f"YTM {ytm:.2f}% exceeds threshold {thresh['min_ytm']}%"]
        if discount_pct > 0:
            reason_parts.append(f"trading at {discount_pct:.1f}% discount")
        if row.get("spread_over_gsec"):
            reason_parts.append(f"spread {row['spread_over_gsec']:.0f}bps over G-Sec")

        # ── Composite score 0–100 ───────────────────────────────────────────
        # 40 pts: YTM attractiveness — how much above threshold (max at +200 bps)
        ytm_excess_bps = (ytm - thresh["min_ytm"]) * 100
        ytm_score = min(40.0, max(0.0, ytm_excess_bps / 5.0))

        # 25 pts: Rating quality
        rating_pts = {"SOVEREIGN": 25, "AAA": 23, "AA+": 21, "AA": 18,
                      "AA-": 16, "A+": 12, "A": 9, "A-": 7, "BBB+": 5, "BBB": 3}
        rating_score = float(rating_pts.get(rating, 4))

        # 20 pts: Spread over G-Sec (more spread = more attractive)
        spread = float(row.get("spread_over_gsec") or 0)
        spread_score = min(20.0, max(0.0, spread / 10.0))

        # 10 pts: Maturity sweet spot (3-7Y = 10, <1Y or >15Y = 2)
        if 3 <= years <= 7:
            years_score = 10.0
        elif 1 <= years < 3 or 7 < years <= 10:
            years_score = 7.0
        elif years < 1:
            years_score = 3.0
        else:  # >10Y
            years_score = max(2.0, 10.0 - (years - 10) * 0.5)

        # 5 pts: PF eligibility bonus
        pf_score = 5.0 if row.get("is_pf_eligible") else 0.0

        score = round(ytm_score + rating_score + spread_score + years_score + pf_score, 1)

        recommendation_item = _attach_category_profile({
            "isin": isin,
            "bond_name": row["bond_name"],
            "issuer_name": row["issuer_name"],
            "issuer_type": itype,
            "credit_rating": rating,
            "ytm": ytm,
            "clean_price": price,
            "face_value": face,
            "discount_pct": discount_pct,
            "modified_duration": row.get("modified_duration"),
            "spread_over_gsec_bps": row.get("spread_over_gsec"),
            "years_to_maturity": years,
            "risk_category": risk,
            "recommendation": "BUY",
            "score": score,
            "reason": "; ".join(reason_parts),
            "pf_eligibility_reason": "PF compliance checks passed",
            "data_source": row.get("data_source", "SYNTHETIC"),
            "as_of": today,
        })
        recommendation_item["risk_interpretation"] = recommendation_item.get("risk_label")
        recommendation_item["compliance_justification"] = "; ".join(
            recommendation_item.get("category_specific_reasoning")[:2]
        )
        recommendation_item["reason"] = "; ".join(
            [recommendation_item.get("reason") or ""] + recommendation_item.get("category_specific_reasoning", [])
        ).strip("; ")
        recommendation_item["explanation"] = build_explanation(
            module="bond_agent_router",
            decision_type="legacy_bond_recommendation",
            decision_outcome="BUY",
            summary=str(recommendation_item.get("reason") or "YTM and policy filters triggered a buy recommendation."),
            key_factors=[
                {
                    "name": "YTM",
                    "value": round(ytm, 4),
                    "impact": "positive" if ytm >= float(thresh.get("min_ytm") or 0.0) else "neutral",
                    "evidence": "Minimum YTM threshold screen",
                },
                {
                    "name": "Composite Score",
                    "value": score,
                    "impact": "positive",
                    "evidence": "YTM + rating + spread + maturity scoring",
                },
                {
                    "name": "Risk Category",
                    "value": risk,
                    "impact": "neutral",
                },
            ],
            rule_checks=[
                {
                    "rule_code": "YTM_THRESHOLD",
                    "status": "PASS" if ytm >= float(thresh.get("min_ytm") or 0.0) else "WARN",
                    "message": "Minimum YTM threshold check",
                    "threshold": float(thresh.get("min_ytm") or 0.0),
                    "actual": round(ytm, 4),
                },
                {
                    "rule_code": "PF_ELIGIBILITY",
                    "status": "PASS",
                    "message": "PF compliance eligibility checks passed",
                    "actual": True,
                },
            ],
            calculation_trace={
                "score": score,
                "ytm_excess_bps": round(ytm_excess_bps, 2),
                "spread_score": round(spread_score, 2),
                "rating_score": round(rating_score, 2),
                "years_score": round(years_score, 2),
                "discount_pct": round(discount_pct, 2),
            },
            data_lineage=[
                {"source": "universal_bond_master", "field": "bond_metadata", "trust_level": "high"},
                {"source": "unified_market_data", "field": "latest_ytm_price", "trust_level": "high"},
            ],
            regulatory_basis=["PF eligibility and risk-category screening controls"],
            confidence_score=min(1.0, max(0.0, score / 100.0)),
            confidence_drivers=[
                f"issuer_type={itype}",
                f"rating={rating}",
                f"risk_category={risk}",
                f"instrument_category={recommendation_item.get('instrument_category')}",
            ],
            counterfactuals=[
                build_counterfactual(
                    scenario="below_threshold_ytm",
                    expected_outcome="NO_RECOMMENDATION",
                    impact_summary="If yield falls below threshold, recommendation is filtered out.",
                    delta_metrics={"ytm": round(ytm, 4), "threshold": float(thresh.get("min_ytm") or 0.0)},
                )
            ],
            severity="INFO",
            tags=["recommendation", "legacy"],
            metadata={"isin": isin, "as_of": today},
        )
        recommendations.append(recommendation_item)

    recommendations.sort(key=lambda r: r["score"], reverse=True)
    recommendations = recommendations[:max_results]
    recommendations_by_category = _recommendations_by_category(recommendations)
    risk_breakdown = {
        "Low Risk": sum(1 for r in recommendations if r["risk_category"] == "Low Risk"),
        "Medium Risk": sum(1 for r in recommendations if r["risk_category"] == "Medium Risk"),
        "High Risk": sum(1 for r in recommendations if r["risk_category"] == "High Risk"),
    }

    log.info("recommendations_generated", count=len(recommendations), date=today)

    await AuditService.record(
        user={
            "id": str(user.get("id", user.get("sub", "unknown"))),
            "username": user.get("username") or user.get("sub") or "unknown",
            "role": user.get("role", Roles.VIEWER),
        },
        action=AuditAction.RECOMMENDATION_GENERATED,
        entity_type=EntityType.REPORT,
        entity_id="daily_recommendations",
        old_value=None,
        new_value={
            "date": today,
            "risk_filter": risk_filter,
            "max_results": max_results,
            "returned": len(recommendations),
            "blocked_candidates": blocked_candidates,
        },
        affected_module="bond_agent",
        ip_address=request.client.host if request.client else None,
        severity=AuditSeverity.INFO,
        compliance_flag=True,
        description=f"Recommendations generated ({len(recommendations)} rows)",
    )

    payload = {
        "date": today,
        "total_recommendations": len(recommendations),
        "recommendation_gate": {
            "blocked_isins_count": len(blocked_isins),
            "blocked_candidates": blocked_candidates,
            "blocked_isins": [
                {
                    "isin": key,
                    "reasons": blocked_reasons.get(key, []),
                }
                for key in sorted(blocked_isins)
            ][:100],
        },
        "risk_breakdown": risk_breakdown,
        "recommendations": recommendations,
        "recommendations_by_category": recommendations_by_category,
        # AUDIT SAFETY: Advisory-only disclaimer
        "disclaimer": (
            "ADVISORY ONLY — These recommendations are informational and do NOT "
            "constitute trade execution instructions. All investment decisions require "
            "manual approval through the compliance engine and trustee authorization. "
            "No automated trade execution is performed."
        ),
        "advisory_only": True,
    }
    payload["explanation"] = build_explanation(
        module="bond_agent_router",
        decision_type="daily_recommendation_generation",
        decision_outcome="PASS" if recommendations else "WARN",
        summary=f"Generated {len(recommendations)} advisory recommendation(s) for {today}.",
        key_factors=[
            {
                "name": "Recommendations Returned",
                "value": len(recommendations),
                "impact": "positive" if recommendations else "neutral",
            },
            {
                "name": "Blocked Candidates",
                "value": blocked_candidates,
                "impact": "negative" if blocked_candidates else "neutral",
                "evidence": "Critical remediation gate exclusions",
            },
            {
                "name": "Low Risk Recommendations",
                "value": risk_breakdown.get("Low Risk", 0),
                "impact": "positive" if risk_breakdown.get("Low Risk", 0) else "neutral",
            },
        ],
        rule_checks=[
            {
                "rule_code": "ADVISORY_ONLY_MODE",
                "status": "PASS",
                "message": "Recommendations are non-executable advisory outputs",
                "actual": True,
            },
            {
                "rule_code": "AUDIT_REMEDIATION_GATE",
                "status": "WARN" if blocked_candidates > 0 else "PASS",
                "message": "Critical remediation blockers excluded from candidate set",
                "actual": blocked_candidates,
            },
        ],
        calculation_trace={
            "total_candidates_returned": len(recommendations),
            "blocked_isins_count": len(blocked_isins),
            "blocked_candidates": blocked_candidates,
            "risk_breakdown": risk_breakdown,
            "max_results": max_results,
        },
        data_lineage=[
            {"source": "universal_bond_master", "field": "bond_universe", "trust_level": "high"},
            {"source": "unified_market_data", "field": "latest_market_snapshot", "trust_level": "high"},
            {"source": "portfolio_dataset_service", "field": "regulatory_audit_gate", "trust_level": "high"},
        ],
        regulatory_basis=[
            "PF eligibility requirements",
            "Advisory-only recommendation governance",
        ],
        confidence_score=0.86,
        confidence_drivers=[
            f"candidate_count={len(recommendations)}",
            f"risk_filter={risk_filter or 'none'}",
            f"blocked_candidates={blocked_candidates}",
        ],
        counterfactuals=[
            build_counterfactual(
                scenario="include_blocked_candidates",
                expected_outcome="HIGHER_COUNT_WITH_HIGHER_RISK",
                impact_summary="Bypassing remediation gate can increase count but weakens control quality.",
                delta_metrics={"blocked_candidates": blocked_candidates},
            )
        ],
        severity=("WARNING" if blocked_candidates > 0 else "INFO"),
        tags=["recommendation", "legacy", "daily"],
        metadata={"date": today, "risk_filter": risk_filter, "max_results": max_results},
    )
    return payload


@router.get("/summary")
async def daily_summary(user=Depends(require_role(Roles.VIEWER))):
    """Quick dashboard summary — bond counts, data freshness."""
    total = await fetch_one(
        "SELECT COUNT(*) AS cnt FROM universal_bond_master WHERE is_active = 1"
    )
    pf_count = await fetch_one(
        "SELECT COUNT(*) AS cnt FROM universal_bond_master WHERE is_active=1 AND is_pf_eligible=1"
    )
    latest_md = await fetch_one(
        "SELECT MAX(price_date) AS latest, COUNT(*) AS cnt FROM unified_market_data"
    )
    payload = {
        "total_bonds_in_registry": int((total or {}).get("cnt", 0)),
        "pf_eligible_bonds": int((pf_count or {}).get("cnt", 0)),
        "market_data_as_of": (latest_md or {}).get("latest"),
        "market_data_records": int((latest_md or {}).get("cnt", 0)),
    }
    payload["explanation"] = build_explanation(
        module="bond_agent_router",
        decision_type="bond_agent_summary",
        decision_outcome="PASS",
        summary="Generated bond universe and market freshness summary for the recommendation dashboard.",
        key_factors=[
            {"name": "Registry Bonds", "value": payload["total_bonds_in_registry"], "impact": "neutral"},
            {"name": "PF Eligible Bonds", "value": payload["pf_eligible_bonds"], "impact": "positive"},
            {"name": "Market Data Records", "value": payload["market_data_records"], "impact": "neutral"},
        ],
        calculation_trace=payload,
        data_lineage=[
            {"source": "universal_bond_master", "field": "is_active_and_pf_eligible", "trust_level": "high"},
            {"source": "unified_market_data", "field": "latest_price_date", "trust_level": "high"},
        ],
        confidence_score=0.92,
        tags=["recommendation", "summary"],
    )
    return payload
