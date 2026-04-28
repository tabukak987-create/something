from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

from ..shared.market_config import RATING_SPREAD_BPS, get_synthetic_yield, interpolate_gsec_yield
from ..shared.time_utils import utcnow_iso
from .instrument_classification import classify_instrument
from app.domain.portfolio.analytics import RATING_SCORE, compute_bond_analytics, normalize_rating


_SOVEREIGN_NAME_HINTS = (
    "GOVERNMENT OF INDIA",
    "GOI",
    "STATE DEVELOPMENT LOAN",
    "STATE DEV LOAN",
    "SDL",
    "G-SEC",
    "GSEC",
    "T-BILL",
    "TREASURY BILL",
)

_MUNICIPAL_NAME_HINTS = (
    "MUNICIPAL",
    "URBAN LOCAL BODY",
    "ULB",
    "CITY CORPORATION",
    "MUNICIPAL CORPORATION",
)


def _upper_text(value: Any) -> str:
    return str(value or "").strip().upper()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        numeric = float(value)
        if numeric != numeric:
            return default
        return numeric
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except Exception:
        try:
            return date.fromisoformat(text[:10])
        except Exception:
            return None


def _rating_score(label: str) -> float:
    normalized = normalize_rating(label or "NR")
    raw_score = float(RATING_SCORE.get(normalized, RATING_SCORE.get("NR", 7.0)))
    return max(20.0, min(100.0, 108.0 - (raw_score * 8.0)))


def _overall_from_factors(factors: list[dict[str, Any]]) -> float:
    total_weight = sum(float(f.get("weight", 0.0)) for f in factors) or 1.0
    weighted = sum(float(f.get("score", 0.0)) * float(f.get("weight", 0.0)) for f in factors)
    return round(weighted / total_weight, 1)


def _risk_level(overall_score: float) -> str:
    if overall_score >= 80:
        return "LOW"
    if overall_score >= 60:
        return "MODERATE"
    return "HIGH"


def _recommendation(overall_score: float) -> str:
    if overall_score >= 85:
        return "APPROVE"
    if overall_score >= 65:
        return "REVIEW"
    return "REJECT"


def _bond_class(record: Mapping[str, Any], classification: Mapping[str, Any] | None = None) -> tuple[str, str]:
    classification = dict(classification or classify_instrument(record))
    issuer_type = _upper_text(record.get("issuer_type") or classification.get("category") or classification.get("portfolio_category"))
    issuer_name = _upper_text(record.get("issuer_name") or record.get("bond_name") or record.get("name"))
    portfolio_category = _upper_text(classification.get("portfolio_category") or classification.get("legacy_category"))

    if issuer_type == "G-SEC" or any(hint in issuer_name for hint in ("GOVERNMENT OF INDIA", "GOI", "G-SEC", "GSEC", "TREASURY BILL")):
        return "SOVEREIGN", "Government of India direct obligation"
    if issuer_type == "SDL" or any(hint in issuer_name for hint in ("STATE DEVELOPMENT LOAN", "SDL")):
        return "STATE_GUARANTEED", "State development loan / state-backed obligation"
    if issuer_type in {"MUNICIPAL", "ULB"} or any(hint in issuer_name for hint in _MUNICIPAL_NAME_HINTS):
        return "MUNICIPAL", "Municipal or urban local body exposure"
    if portfolio_category in {"CENTRAL_STATE", "SDS"}:
        if issuer_type == "SDL":
            return "STATE_GUARANTEED", "State-backed issuance"
        return "SOVEREIGN", "Central government linked issuance"
    return "CORPORATE", "Corporate credit exposure"


def _bond_analytics(record: Mapping[str, Any]) -> dict[str, Any]:
    maturity_date = _parse_date(record.get("maturity_date") or record.get("maturity") or record.get("maturity_dt"))
    if not maturity_date:
        return {}

    coupon_rate = _safe_float(record.get("coupon_rate") or record.get("coupon") or record.get("interest_rate"), 0.0)
    face_value = _safe_float(record.get("face_value") or record.get("nominal_value") or 1000.0, 1000.0)
    clean_price = _safe_float(record.get("clean_price") or record.get("market_price") or record.get("current_price") or 100.0, 100.0)
    ytm = _safe_float(record.get("ytm") or record.get("yield") or record.get("ytm_latest") or 0.0, 0.0)
    issuer_type = _upper_text(record.get("issuer_type") or record.get("instrument_type") or "CORP")
    rating = normalize_rating(record.get("credit_rating") or record.get("rating") or record.get("rating_latest") or "NR")

    if ytm <= 0:
        tenor_years = max((maturity_date - date.today()).days / 365.25, 0.25)
        ytm = get_synthetic_yield(issuer_type or "CORP", rating or "NR", tenor_years)

    coupon_frequency = _safe_int(record.get("coupon_frequency") or record.get("frequency") or 2, 2)
    issue_date = _parse_date(record.get("issue_date") or record.get("start_date") or record.get("listing_date"))

    try:
        analytics = compute_bond_analytics(
            coupon_rate=coupon_rate,
            face_value=face_value,
            clean_price=clean_price,
            ytm=ytm,
            maturity_date=maturity_date,
            coupon_frequency=max(coupon_frequency, 1),
            issue_date=issue_date,
        )
    except Exception:
        analytics = {
            "clean_price": clean_price,
            "gsec_benchmark_yield": interpolate_gsec_yield(max((maturity_date - date.today()).days / 365.25, 0.25)),
            "years_to_maturity": round(max((maturity_date - date.today()).days / 365.25, 0.25), 4),
        }

    analytics.update(
        {
            "coupon_rate": coupon_rate,
            "face_value": face_value,
            "ytm": ytm,
            "credit_rating": rating,
        }
    )
    return analytics


class BondClassificationEngine:
    def classify(self, bond_data: Mapping[str, Any]) -> dict[str, Any]:
        record = dict(bond_data or {})
        classified = dict(classify_instrument(record))
        bond_class, reason = _bond_class(record, classified)
        issuer_type = _upper_text(record.get("issuer_type") or record.get("instrument_type") or classified.get("legacy_category"))
        issuer_name = str(record.get("issuer_name") or record.get("bond_name") or record.get("name") or "").strip()

        confidence = 0.97 if bond_class in {"SOVEREIGN", "STATE_GUARANTEED"} else 0.88
        if issuer_type in {"CORPORATE", "BANK", "NBFC", "HFC", "PSU"}:
            confidence = 0.9

        return {
            "bond_class": bond_class,
            "issuer_type": issuer_type or "UNKNOWN",
            "issuer_name": issuer_name,
            "confidence": round(confidence, 2),
            "reason": reason,
            "classification": classified,
            "portfolio_category": classified.get("portfolio_category"),
            "legacy_category": classified.get("legacy_category"),
            "asset_type": classified.get("type"),
        }


class DueDiligenceEngine:
    async def evaluate(self, bond_data: Mapping[str, Any]) -> dict[str, Any]:
        record = dict(bond_data or {})
        classification = BondClassificationEngine().classify(record)
        analytics = _bond_analytics(record)

        issuer_type = classification.get("issuer_type") or _upper_text(record.get("issuer_type"))
        issuer_name = classification.get("issuer_name") or str(record.get("issuer_name") or "").strip()
        rating = normalize_rating(record.get("credit_rating") or record.get("rating") or record.get("rating_latest") or "NR")
        rating_score = _rating_score(rating)
        spread_bps = _safe_float(analytics.get("spread_over_gsec_bps") or analytics.get("spread_bps") or 0.0, 0.0)
        duration = _safe_float(analytics.get("modified_duration") or analytics.get("duration") or 0.0, 0.0)
        secured = bool(record.get("secured") or record.get("security_type") or record.get("security"))
        has_im = bool(record.get("im_data") or record.get("document_url") or record.get("issuer_name"))
        bond_class = classification.get("bond_class") or "CORPORATE"

        factor_specs = [
            ("instrument_nature", 0.10, {"SOVEREIGN": 98.0, "STATE_GUARANTEED": 90.0, "MUNICIPAL": 80.0, "CORPORATE": 70.0}.get(bond_class, 65.0), "Instrument type and issuer form"),
            ("government_support", 0.10, {"SOVEREIGN": 100.0, "STATE_GUARANTEED": 88.0, "MUNICIPAL": 72.0, "CORPORATE": 35.0}.get(bond_class, 40.0), "Explicit or implicit support"),
            ("financial_position", 0.12, rating_score, f"Normalized rating score for {rating}"),
            ("utilisation_of_proceeds", 0.08, 88.0 if record.get("im_data") else 65.0, "Use of proceeds and documentation availability"),
            ("repayment_mechanism", 0.08, 90.0 if secured else 68.0, "Security, guarantee, and repayment clarity"),
            ("business_model", 0.08, 86.0 if issuer_name else 60.0, "Issuer disclosure quality"),
            ("financial_trend", 0.14, max(30.0, 100.0 - min(spread_bps / 2.0, 60.0) - duration * 1.5), "Spread and duration trend"),
            ("credit_rating", 0.10, rating_score, f"Credit rating {rating}"),
            ("default_history", 0.10, 100.0 if not record.get("default_history") else 20.0, "No known default flags in local record"),
            ("legal_compliance", 0.10, 92.0 if has_im else 60.0, "IM and disclosure completeness"),
            ("risk_assessment", 0.10, max(20.0, 100.0 - min(spread_bps, 200.0) * 0.6 - duration * 1.2), "Spread, duration, and liquidity risk"),
        ]

        factors = [
            {
                "name": name,
                "weight": weight,
                "score": round(score, 1),
                "comment": comment,
            }
            for name, weight, score, comment in factor_specs
        ]

        overall_score = _overall_from_factors(factors)
        risk_level = _risk_level(overall_score)
        recommendation = _recommendation(overall_score)

        return {
            "bond_class": bond_class,
            "issuer_type": issuer_type,
            "issuer_name": issuer_name,
            "classification": classification,
            "analytics": analytics,
            "factors": factors,
            "overall_score": overall_score,
            "risk_level": risk_level,
            "recommendation": recommendation,
            "summary": {
                "overall_score": overall_score,
                "risk_level": risk_level,
                "recommendation": recommendation,
                "bond_class": bond_class,
                "issuer_name": issuer_name,
                "issuer_type": issuer_type,
            },
            "generated_at": utcnow_iso(),
        }


class SovereignAnalysisEngine:
    def _analysis(self, bond_data: Mapping[str, Any], label: str) -> dict[str, Any]:
        record = dict(bond_data or {})
        classification = BondClassificationEngine().classify(record)
        analytics = _bond_analytics(record)
        issuer_name = classification.get("issuer_name") or str(record.get("issuer_name") or "").strip()
        issuer_type = _upper_text(record.get("issuer_type") or classification.get("issuer_type") or label)
        benchmark_yield = _safe_float(analytics.get("gsec_benchmark_yield") or 0.0, 0.0)
        bond_yield = _safe_float(analytics.get("ytm") or record.get("ytm") or 0.0, 0.0)
        spread_bps = round((bond_yield - benchmark_yield) * 100, 2) if bond_yield and benchmark_yield else _safe_float(analytics.get("spread_over_gsec_bps") or 0.0, 0.0)
        duration = _safe_float(analytics.get("modified_duration") or 0.0, 0.0)
        score = max(30.0, 100.0 - min(abs(spread_bps) / 2.0, 60.0) - duration * 1.2)
        risk_level = _risk_level(score)

        return {
            "bond_class": classification.get("bond_class"),
            "issuer_type": issuer_type,
            "issuer_name": issuer_name,
            "benchmark_yield": round(benchmark_yield, 4),
            "bond_yield": round(bond_yield, 4),
            "spread_bps": round(spread_bps, 2),
            "duration": round(duration, 4),
            "risk_level": risk_level,
            "recommendation": "TRACK" if risk_level == "LOW" else ("REVIEW" if risk_level == "MODERATE" else "CAUTION"),
            "analysis": {
                "issuer_support": "High" if label == "G-SEC" else "State backed",
                "fiscal_anchor": "Central government" if label == "G-SEC" else "State finance and treasury support",
                "benchmark_curve": interpolate_gsec_yield(max(_safe_float(analytics.get("years_to_maturity") or 0.0, 0.0), 0.25)),
                "market_summary": analytics,
            },
            "summary": {
                "risk_level": risk_level,
                "spread_bps": round(spread_bps, 2),
                "duration": round(duration, 4),
                "recommendation": "TRACK" if risk_level == "LOW" else ("REVIEW" if risk_level == "MODERATE" else "CAUTION"),
            },
            "generated_at": utcnow_iso(),
        }

    def analyze_gsec(self, bond_data: Mapping[str, Any]) -> dict[str, Any]:
        return self._analysis(bond_data, "G-SEC")

    def analyze_sdl(self, bond_data: Mapping[str, Any]) -> dict[str, Any]:
        return self._analysis(bond_data, "SDL")


class MunicipalBondEvaluationEngine:
    def evaluate(self, bond_data: Mapping[str, Any]) -> dict[str, Any]:
        record = dict(bond_data or {})
        classification = BondClassificationEngine().classify(record)
        analytics = _bond_analytics(record)
        issuer_name = classification.get("issuer_name") or str(record.get("issuer_name") or "").strip()
        revenue_visibility = 86.0 if record.get("im_data") else 60.0
        escrow_strength = 82.0 if record.get("secured") or record.get("escrow") else 55.0
        dscr = _safe_float(record.get("dscr") or record.get("debt_service_coverage") or 1.25, 1.25)
        tax_efficiency = 80.0 if "MUNICIPAL" in _upper_text(issuer_name) or "URBAN" in _upper_text(issuer_name) else 68.0
        governance = 78.0 if record.get("im_data") else 62.0
        market_signal = max(30.0, 100.0 - min(_safe_float(analytics.get("spread_over_gsec_bps") or 0.0, 0.0) / 3.0, 55.0))

        factors = [
            {"name": "revenue_visibility", "weight": 0.24, "score": revenue_visibility, "comment": "Visibility into municipal revenues and disclosures"},
            {"name": "escrow_strength", "weight": 0.20, "score": escrow_strength, "comment": "Escrow or other structural support"},
            {"name": "dscr", "weight": 0.16, "score": min(100.0, max(20.0, dscr * 50.0)), "comment": "Debt service coverage"},
            {"name": "tax_collection", "weight": 0.12, "score": tax_efficiency, "comment": "Local tax collection and cash flow"},
            {"name": "governance", "weight": 0.12, "score": governance, "comment": "Disclosure and governance quality"},
            {"name": "market_signal", "weight": 0.16, "score": market_signal, "comment": "Market spread and liquidity signal"},
        ]
        overall_score = _overall_from_factors(factors)
        risk_level = _risk_level(overall_score)
        recommendation = _recommendation(overall_score)

        return {
            "bond_class": classification.get("bond_class"),
            "issuer_name": issuer_name,
            "classification": classification,
            "analytics": analytics,
            "factors": factors,
            "overall_score": overall_score,
            "risk_level": risk_level,
            "recommendation": recommendation,
            "summary": {
                "overall_score": overall_score,
                "risk_level": risk_level,
                "recommendation": recommendation,
                "bond_class": "MUNICIPAL",
                "issuer_name": issuer_name,
            },
            "generated_at": utcnow_iso(),
        }


class CorporateBondEnhancementEngine:
    async def evaluate(self, bond_data: Mapping[str, Any]) -> dict[str, Any]:
        record = dict(bond_data or {})
        classification = BondClassificationEngine().classify(record)
        analytics = _bond_analytics(record)
        issuer_name = classification.get("issuer_name") or str(record.get("issuer_name") or "").strip()
        issuer_type = classification.get("issuer_type") or _upper_text(record.get("issuer_type"))
        rating = normalize_rating(record.get("credit_rating") or record.get("rating") or record.get("rating_latest") or "NR")
        rating_score = _rating_score(rating)
        secured = bool(record.get("secured") or record.get("security_type") or record.get("security"))
        spread_bps = _safe_float(analytics.get("spread_over_gsec_bps") or 0.0, 0.0)
        duration = _safe_float(analytics.get("modified_duration") or 0.0, 0.0)
        liquidity = 82.0 if _safe_float(record.get("liquidity_score") or 0.0, 0.0) >= 50 else 60.0

        factors = [
            {"name": "management_quality", "weight": 0.18, "score": 78.0 if issuer_name else 60.0, "comment": "Issuer disclosure and management visibility"},
            {"name": "board_governance", "weight": 0.16, "score": 80.0 if record.get("im_data") else 62.0, "comment": "Governance and filing quality"},
            {"name": "group_support", "weight": 0.12, "score": 84.0 if any(hint in issuer_name.upper() for hint in ("TATA", "HDFC", "ICICI", "RELIANCE", "L&T")) else 66.0, "comment": "Group or sponsor support"},
            {"name": "security_structure", "weight": 0.16, "score": 92.0 if secured else 68.0, "comment": "Security or guarantee structure"},
            {"name": "credit_rating", "weight": 0.20, "score": rating_score, "comment": f"Normalized rating {rating}"},
            {"name": "liquidity", "weight": 0.10, "score": liquidity, "comment": "Secondary market liquidity"},
            {"name": "market_risk", "weight": 0.08, "score": max(30.0, 100.0 - min(spread_bps / 2.0, 60.0) - duration * 1.2), "comment": "Spread and duration risk"},
        ]
        overall_score = _overall_from_factors(factors)
        risk_level = _risk_level(overall_score)
        recommendation = _recommendation(overall_score)

        return {
            "bond_class": classification.get("bond_class") or "CORPORATE",
            "issuer_type": issuer_type,
            "issuer_name": issuer_name,
            "classification": classification,
            "analytics": analytics,
            "factors": factors,
            "overall_score": overall_score,
            "risk_level": risk_level,
            "recommendation": recommendation,
            "summary": {
                "overall_score": overall_score,
                "risk_level": risk_level,
                "recommendation": recommendation,
                "bond_class": "CORPORATE",
                "issuer_name": issuer_name,
                "issuer_type": issuer_type,
            },
            "generated_at": utcnow_iso(),
        }


class ICReportGenerator:
    async def generate_ic_note(
        self,
        *,
        bond_data: Mapping[str, Any],
        analytics: Mapping[str, Any] | None = None,
        portfolio_context: Mapping[str, Any] | None = None,
        compliance_regime: str = "epfo",
    ) -> dict[str, Any]:
        record = dict(bond_data or {})
        analytics = dict(analytics or {})
        classification = dict(analytics.get("classification") or BondClassificationEngine().classify(record))
        due_diligence = dict(analytics.get("due_diligence") or await DueDiligenceEngine().evaluate(record))
        issuer_type = classification.get("issuer_type") or _upper_text(record.get("issuer_type"))
        issuer_name = classification.get("issuer_name") or str(record.get("issuer_name") or "").strip()
        bond_class = classification.get("bond_class") or due_diligence.get("bond_class") or "CORPORATE"

        if issuer_type in {"G-SEC", "SDL"}:
            sovereign = dict(analytics.get("sovereign") or (SovereignAnalysisEngine().analyze_gsec(record) if issuer_type == "G-SEC" else SovereignAnalysisEngine().analyze_sdl(record)))
            analytics["sovereign"] = sovereign
        else:
            sovereign = {}

        if bond_class == "MUNICIPAL":
            municipal = dict(analytics.get("municipal") or MunicipalBondEvaluationEngine().evaluate(record))
            analytics["municipal"] = municipal
        else:
            municipal = {}

        if bond_class == "CORPORATE":
            corporate = dict(analytics.get("corporate") or await CorporateBondEnhancementEngine().evaluate(record))
            analytics["corporate"] = corporate
        else:
            corporate = {}

        market = dict(record.get("market_data") or analytics.get("market_data") or {})
        overall_score = _safe_float(due_diligence.get("overall_score") or corporate.get("overall_score") or municipal.get("overall_score") or sovereign.get("summary", {}).get("overall_score") or 0.0, 0.0)
        recommendation = due_diligence.get("recommendation") or corporate.get("recommendation") or municipal.get("recommendation") or sovereign.get("recommendation") or _recommendation(overall_score)

        sections = [
            {
                "section_name": "issuer_overview",
                "title": "Issuer Overview",
                "body": [
                    f"Issuer: {issuer_name or 'Unknown'}",
                    f"Issuer type: {issuer_type or 'UNKNOWN'}",
                    f"Bond class: {bond_class}",
                    f"Compliance regime: {compliance_regime}",
                ],
            },
            {
                "section_name": "bond_terms",
                "title": "Bond Terms",
                "body": [
                    f"Coupon rate: {record.get('coupon_rate', 'n/a')}",
                    f"Face value: {record.get('face_value', 'n/a')}",
                    f"Maturity date: {record.get('maturity_date', 'n/a')}",
                    f"YTM: {analytics.get('ytm', record.get('ytm', 'n/a'))}",
                ],
            },
            {
                "section_name": "risk_summary",
                "title": "Risk Summary",
                "body": [
                    f"Overall score: {overall_score}",
                    f"Risk level: {due_diligence.get('risk_level') or corporate.get('risk_level') or municipal.get('risk_level') or sovereign.get('risk_level') or 'UNKNOWN'}",
                    f"Recommendation: {recommendation}",
                ],
            },
        ]

        if sovereign:
            sections.append(
                {
                    "section_name": "sovereign_analysis",
                    "title": "Sovereign Analysis",
                    "body": [
                        f"Benchmark yield: {sovereign.get('benchmark_yield', 'n/a')}",
                        f"Bond yield: {sovereign.get('bond_yield', 'n/a')}",
                        f"Spread: {sovereign.get('spread_bps', 'n/a')} bps",
                        f"Risk level: {sovereign.get('risk_level', 'UNKNOWN')}",
                    ],
                }
            )

        if municipal:
            sections.append(
                {
                    "section_name": "municipal_analysis",
                    "title": "Municipal Analysis",
                    "body": [
                        f"Overall score: {municipal.get('overall_score', 'n/a')}",
                        f"Risk level: {municipal.get('risk_level', 'UNKNOWN')}",
                        f"Recommendation: {municipal.get('recommendation', 'REVIEW')}",
                    ],
                }
            )

        if corporate:
            sections.append(
                {
                    "section_name": "corporate_analysis",
                    "title": "Corporate Analysis",
                    "body": [
                        f"Overall score: {corporate.get('overall_score', 'n/a')}",
                        f"Risk level: {corporate.get('risk_level', 'UNKNOWN')}",
                        f"Recommendation: {corporate.get('recommendation', 'REVIEW')}",
                    ],
                }
            )

        if market:
            sections.append(
                {
                    "section_name": "market_data",
                    "title": "Market Data",
                    "body": [f"{key}: {value}" for key, value in list(market.items())[:8]],
                }
            )

        if portfolio_context:
            sections.append(
                {
                    "section_name": "portfolio_context",
                    "title": "Portfolio Context",
                    "body": [f"{key}: {value}" for key, value in list(dict(portfolio_context).items())[:8]],
                }
            )

        markdown_lines = [
            f"# IC Note - {issuer_name or record.get('isin', 'Unknown ISIN')}",
            "",
            f"- ISIN: {record.get('isin', 'n/a')}",
            f"- Bond class: {bond_class}",
            f"- Recommendation: {recommendation}",
            f"- Overall score: {overall_score}",
            "",
        ]
        for section in sections:
            markdown_lines.append(f"## {section['title']}")
            markdown_lines.extend([f"- {line}" for line in section.get("body", [])])
            markdown_lines.append("")

        report = "\n".join(markdown_lines).strip() + "\n"
        summary = {
            "recommendation": recommendation,
            "overall_score": overall_score,
            "risk_level": due_diligence.get("risk_level") or corporate.get("risk_level") or municipal.get("risk_level") or sovereign.get("risk_level") or _risk_level(overall_score),
            "bond_class": bond_class,
            "issuer_name": issuer_name,
            "issuer_type": issuer_type,
            "compliance_regime": compliance_regime,
        }

        return {
            "isin": record.get("isin"),
            "issuer_name": issuer_name,
            "issuer_type": issuer_type,
            "bond_class": bond_class,
            "summary": summary,
            "sections": sections,
            "analytics": analytics,
            "due_diligence": due_diligence,
            "report": report,
            "markdown": report,
            "generated_at": utcnow_iso(),
        }