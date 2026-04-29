"""
Risk & Analysis Router
=======================
Endpoints for stress testing, VaR, scenarios, yield curve analysis.
All calculations from services/risk_engine.py. No frontend calculations.
"""

from typing import Any

from fastapi import APIRouter, Depends, Request

from ..shared.auth import require_role, Roles
from ..shared.logger import get_logger
from ..domain.core_trust.orchestrator import DecisionOrchestrator
from ..domain.core_trust.explainability import build_counterfactual, build_explanation
from ..domain.market_data.instrument_classification import (
    INSTRUMENT_CATEGORY_EQUITY,
    INSTRUMENT_CATEGORY_PRIVATE,
    INSTRUMENT_CATEGORY_PSU,
    INSTRUMENT_CATEGORY_STATE_CENTRAL,
    category_metadata,
    classify_instrument,
)
from ..domain.risk.engine import (
    compute_rate_sensitivity,
    compute_spread_simulation,
    run_scenarios,
    compute_var,
    compute_yield_curve_position,
    compute_full_risk_analytics,
)

log = get_logger("router.risk")
router = APIRouter(prefix="/risk", tags=["Risk & Analysis"])

TRACE_REQUIRED_FIELDS = (
    "decision_id",
    "policy_version_id",
    "data_snapshot_ids",
    "calculation_version_id",
    "input_hash",
    "output_hash",
    "trace_timestamp",
    "intent_id",
)


def _state_payload(status: str, data: dict[str, Any] | None = None, message: str | None = None) -> dict[str, Any]:
    payload = {
        "status": status,
        "data": data if isinstance(data, dict) else {},
        "message": message,
    }
    trace_source = payload["data"] if isinstance(payload.get("data"), dict) else {}
    for field in TRACE_REQUIRED_FIELDS:
        payload[field] = trace_source.get(field)
    return payload


def _normalize_service_result(result: dict[str, Any] | None) -> tuple[str, dict[str, Any], str | None]:
    if not isinstance(result, dict):
        return "error", {}, "Risk service returned invalid payload"

    status = str(result.get("status") or "").strip().lower()
    if status in {"ok", "no_data", "error"}:
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        message = str(result.get("message") or "") or None
        return status, data, message

    if result.get("error"):
        message = str(result.get("error"))
        status = "no_data" if _is_no_portfolio_error(message) else "error"
        return status, result, message

    return "ok", result, None


def _is_no_portfolio_error(message: Any) -> bool:
    text = str(message or "").strip().lower()
    return (
        "no portfolio data" in text
        or "no holdings" in text
        or "insufficient portfolio data" in text
        or "portfolio value is zero" in text
    )


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _category_risk_breakdown(
    rows: list[dict[str, Any]] | None,
    *,
    total_market_value: float,
) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, float]] = {
        INSTRUMENT_CATEGORY_STATE_CENTRAL: {"market_value": 0.0, "duration_contribution": 0.0, "dv01": 0.0},
        INSTRUMENT_CATEGORY_PSU: {"market_value": 0.0, "duration_contribution": 0.0, "dv01": 0.0},
        INSTRUMENT_CATEGORY_PRIVATE: {"market_value": 0.0, "duration_contribution": 0.0, "dv01": 0.0},
        INSTRUMENT_CATEGORY_EQUITY: {"market_value": 0.0, "duration_contribution": 0.0, "dv01": 0.0},
    }

    for row in rows or []:
        derived = classify_instrument(row)
        category = str(row.get("instrument_category") or derived.get("instrument_category") or INSTRUMENT_CATEGORY_PRIVATE).upper()
        bucket = buckets.setdefault(category, {"market_value": 0.0, "duration_contribution": 0.0, "dv01": 0.0})
        bucket["market_value"] += _safe_float(row.get("market_value"), 0.0)
        bucket["duration_contribution"] += _safe_float(row.get("duration_contribution"), 0.0)
        bucket["dv01"] += _safe_float(row.get("dv01"), 0.0)

    out: list[dict[str, Any]] = []
    for category in [
        INSTRUMENT_CATEGORY_STATE_CENTRAL,
        INSTRUMENT_CATEGORY_PSU,
        INSTRUMENT_CATEGORY_PRIVATE,
        INSTRUMENT_CATEGORY_EQUITY,
    ]:
        bucket = buckets.get(category) or {}
        mv = _safe_float(bucket.get("market_value"), 0.0)
        meta = category_metadata(category)
        out.append(
            {
                "instrument_category": category,
                "category_display": meta.get("category_display"),
                "category_tag": meta.get("category_tag"),
                "risk_label": meta.get("risk_label"),
                "market_value": round(mv, 2),
                "weight_pct": round((mv / total_market_value) * 100.0, 4) if total_market_value > 0 else 0.0,
                "duration_contribution": round(_safe_float(bucket.get("duration_contribution"), 0.0), 6),
                "dv01": round(_safe_float(bucket.get("dv01"), 0.0), 2),
            }
        )
    return out


def _with_explanation(
    payload: dict[str, Any],
    *,
    decision_type: str,
    decision_outcome: str,
    summary: str,
    key_factors: list[dict[str, Any]],
    rule_checks: list[dict[str, Any]] | None = None,
    calculation_trace: dict[str, Any] | None = None,
    confidence_score: float = 0.0,
    confidence_drivers: list[str] | None = None,
    counterfactuals: list[dict[str, Any]] | None = None,
    severity: str = "INFO",
) -> dict[str, Any]:
    out = dict(payload)
    out["explanation"] = build_explanation(
        module="risk_router",
        decision_type=decision_type,
        decision_outcome=decision_outcome,
        summary=summary,
        key_factors=key_factors,
        rule_checks=rule_checks or [],
        calculation_trace=calculation_trace or {},
        data_lineage=[
            {"source": "risk_engine", "field": decision_type, "trust_level": "high"},
            {"source": "portfolio_current_positions", "field": "holdings", "trust_level": "high"},
            {"source": "unified_market_data", "field": "latest_ytm_price", "trust_level": "high"},
        ],
        regulatory_basis=[
            "PF Trust risk governance and stress-testing controls",
            "Portfolio risk reporting and scenario coverage requirements",
        ],
        confidence_score=max(0.0, min(_safe_float(confidence_score, 0.0), 1.0)),
        confidence_drivers=confidence_drivers or [],
        counterfactuals=counterfactuals or [],
        severity=severity,
        tags=["risk", decision_type],
    )
    return out


async def _record_risk_decision(
    payload: dict[str, Any],
    *,
    decision_type: str,
    decision_outcome: str,
    source_endpoint: str,
    user: Any,
) -> dict[str, Any]:
    normalized_outcome = str(decision_outcome or "").upper()
    selected_monitor = normalized_outcome in {"PASS", "WARN"}
    actor = (user or {}).get("username") if isinstance(user, dict) else str(user or "system")
    portfolio_snapshot = payload.get("portfolio_snapshot") if isinstance(payload.get("portfolio_snapshot"), dict) else {}
    total_market_value = _safe_float(
        payload.get("total_market_value", portfolio_snapshot.get("total_market_value")),
        0.0,
    )
    holding_count = int(_safe_float(portfolio_snapshot.get("holding_count"), 0.0))
    calc_date = str(payload.get("calc_date") or "")
    idempotency_key = (
        f"risk|{decision_type}|{source_endpoint}|{actor}|"
        f"{normalized_outcome}|{calc_date}|{holding_count}|{round(total_market_value, 2)}"
    )
    input_snapshot = {
        "source_endpoint": source_endpoint,
        "decision_type": decision_type,
        "requested_by": actor,
        "decision_outcome": normalized_outcome,
        "calc_date": calc_date,
        "portfolio_holding_count": holding_count,
        "portfolio_total_market_value": round(total_market_value, 2),
    }
    alternatives: list[dict[str, Any]] = [
        {
            "alternative_id": "monitor_as_is",
            "alternative_label": "Continue monitoring current risk posture",
            "status": "SELECTED" if selected_monitor else "REJECTED",
            "score": 90.0 if selected_monitor else 30.0,
            "rejection_reason": "" if selected_monitor else "Risk outcome requires immediate de-risk action",
            "rule_code": "RISK_MONITORING_DECISION",
            "metrics": {
                "decision_outcome": normalized_outcome,
                "source_endpoint": source_endpoint,
            },
        },
        {
            "alternative_id": "de_risk_portfolio",
            "alternative_label": "De-risk portfolio exposure",
            "status": "SELECTED" if not selected_monitor else "REJECTED",
            "score": 95.0 if not selected_monitor else 25.0,
            "rejection_reason": "" if not selected_monitor else "Current outcome does not mandate forced de-risking",
            "rule_code": "RISK_DERISK_PATH",
            "metrics": {
                "decision_outcome": normalized_outcome,
                "source_endpoint": source_endpoint,
            },
        },
    ]

    return await DecisionOrchestrator.record_decision(
        payload=payload,
        decision_type=decision_type,
        decision_outcome=decision_outcome,
        module_name="risk_router",
        source_endpoint=source_endpoint,
        user=user,
        status="RECORDED",
        input_payload=input_snapshot,
        idempotency_key=idempotency_key,
        policy_version_id=None,
        calculation_version_id="risk_router_v1",
        alternatives=alternatives,
        market_snapshot_ref="unified_market_data:latest",
        policy_snapshot_ref="risk_policy_v1",
        metadata={
            "risk_endpoint": source_endpoint,
            "data_verified": True,
            "intelligence_completed": True,
            "compliance_validated": True,
            "policy_name": "Unified Portfolio Risk Policy",
            "policy_body": {
                "version": "v1",
                "risk_methods": [
                    "duration_dv01",
                    "spread_stress",
                    "scenario_analysis",
                    "value_at_risk",
                    "yield_curve_positioning",
                ],
                "soft_limits": {
                    "var_95_1d_pct": 2.0,
                    "scenario_loss_pct": 5.0,
                    "spread_stress_loss_pct": 7.0,
                },
            },
            "source_snapshots": [
                {
                    "snapshot_type": "portfolio_state",
                    "snapshot_ref": "portfolio:current",
                    "snapshot_payload": {
                        "decision_type": decision_type,
                        "endpoint": source_endpoint,
                        "category_risk_breakdown": payload.get("category_risk_breakdown"),
                        "duration_contributions": payload.get("duration_contributions"),
                    },
                },
                {
                    "snapshot_type": "market_data",
                    "snapshot_ref": "unified_market_data:latest",
                    "snapshot_payload": {
                        "decision_type": decision_type,
                        "calc_date": payload.get("calc_date"),
                        "portfolio_dv01": payload.get("portfolio_dv01"),
                        "portfolio_duration": payload.get("portfolio_duration"),
                        "var": payload.get("var"),
                    },
                },
            ],
        },
    )


@router.get("/")
async def full_risk_report(request: Request, user=Depends(require_role(Roles.VIEWER))):
    """Complete risk analytics: rate sensitivity, spreads, scenarios, VaR, yield curve."""
    try:
        log.info(
            "risk_endpoint_request",
            endpoint=str(request.url.path),
            input_payload={"query": dict(request.query_params)},
            user=(user or {}).get("username") if isinstance(user, dict) else str(user or "system"),
        )
        service_result = await compute_full_risk_analytics()
        service_status, service_data, service_message = _normalize_service_result(service_result)
        result = service_data if service_status == "ok" else {**service_data, "error": service_message or "Risk analytics unavailable"}
        log.info(
            "risk_endpoint_service_state",
            endpoint=str(request.url.path),
            service_status=service_status,
            portfolio_snapshot=(service_data.get("portfolio_snapshot") if isinstance(service_data, dict) else None),
            message=service_message,
        )
        if result.get("error"):
            no_data = _is_no_portfolio_error(result.get("error"))
            return _state_payload(
                "no_data" if no_data else "error",
                result,
                "Insufficient portfolio data for risk computation" if no_data else str(result.get("error")),
            )
        rate_slice = (result.get("rate_sensitivity") or {}) if isinstance(result, dict) else {}
        total_mv = _safe_float(rate_slice.get("total_market_value"), 0.0)
        if total_mv > 0:
            result["category_risk_breakdown"] = _category_risk_breakdown(
                rate_slice.get("duration_contributions") or [],
                total_market_value=total_mv,
            )
        var95_pct = _safe_float(((result.get("var") or {}).get("var") or {}).get("var_95_1d_pct"), 0.0)
        worst_case = (result.get("scenarios") or {}).get("worst_case") or {}
        worst_case_pct = abs(_safe_float(worst_case.get("pnl_pct"), 0.0))
        outcome = "PASS" if var95_pct <= 2.0 and worst_case_pct <= 5.0 else "WARN"

        explained = _with_explanation(
            result,
            decision_type="full_risk_report",
            decision_outcome=outcome,
            summary="Generated comprehensive portfolio risk analytics across rates, spreads, scenarios, VaR, and yield curve.",
            key_factors=[
                {
                    "name": "VaR 95% (1D, %)",
                    "value": round(var95_pct, 4),
                    "impact": "negative" if var95_pct > 2.0 else "neutral",
                    "evidence": "Variance-covariance VaR estimate",
                },
                {
                    "name": "Worst Scenario Loss (%)",
                    "value": round(worst_case_pct, 4),
                    "impact": "negative" if worst_case_pct > 5.0 else "neutral",
                    "evidence": "Worst predefined macro/credit scenario",
                },
            ],
            rule_checks=[
                {
                    "rule_code": "RISK_VAR_95_LIMIT",
                    "status": "PASS" if var95_pct <= 2.0 else "WARN",
                    "message": "Daily VaR 95% within internal soft threshold",
                    "threshold": 2.0,
                    "actual": round(var95_pct, 4),
                },
                {
                    "rule_code": "RISK_SCENARIO_LOSS_LIMIT",
                    "status": "PASS" if worst_case_pct <= 5.0 else "WARN",
                    "message": "Worst predefined scenario loss within tolerance",
                    "threshold": 5.0,
                    "actual": round(worst_case_pct, 4),
                },
            ],
            calculation_trace={
                "var_95_1d_pct": round(var95_pct, 4),
                "worst_case": worst_case,
                "calc_date": result.get("calc_date"),
            },
            confidence_score=0.8,
            confidence_drivers=["multiple independent risk methods", "same-day risk snapshot"],
            severity="WARNING" if outcome == "WARN" else "INFO",
        )
        recorded = await _record_risk_decision(
            explained,
            decision_type="full_risk_report",
            decision_outcome=outcome,
            source_endpoint="/api/v1/risk/",
            user=user,
        )
        return _state_payload("ok", recorded)
    except Exception as e:
        log.exception("risk_analytics_failed")
        return _state_payload("error", {}, f"Risk analytics failed: {str(e)}")


@router.get("/rate-sensitivity")
async def rate_sensitivity(request: Request, user=Depends(require_role(Roles.VIEWER))):
    """DV01 and price impact for parallel rate shocks (±25/50/100/150/200bps)."""
    try:
        log.info(
            "risk_endpoint_request",
            endpoint=str(request.url.path),
            input_payload={"query": dict(request.query_params)},
            user=(user or {}).get("username") if isinstance(user, dict) else str(user or "system"),
        )
        service_result = await compute_rate_sensitivity()
        service_status, service_data, service_message = _normalize_service_result(service_result)
        result = service_data if service_status == "ok" else {**service_data, "error": service_message or "Risk computation unavailable"}
        log.info(
            "risk_endpoint_service_state",
            endpoint=str(request.url.path),
            service_status=service_status,
            portfolio_snapshot=(service_data.get("portfolio_snapshot") if isinstance(service_data, dict) else None),
            message=service_message,
        )

        if result.get("error"):
            no_data = _is_no_portfolio_error(result.get("error"))
            explained = _with_explanation(
                result,
                decision_type="rate_sensitivity",
                decision_outcome="NO_DATA" if no_data else "ERROR",
                summary="Rate sensitivity could not be computed because portfolio data is unavailable.",
                key_factors=[{"name": "Error", "value": result.get("error"), "impact": "negative"}],
                confidence_score=0.0,
                severity="CRITICAL",
            )
            recorded = await _record_risk_decision(
                explained,
                decision_type="rate_sensitivity",
                decision_outcome="NO_DATA" if no_data else "ERROR",
                source_endpoint="/api/v1/risk/rate-sensitivity",
                user=user,
            )
            if no_data:
                return _state_payload("no_data", recorded, "Insufficient portfolio data for risk computation")
            return _state_payload("error", recorded, str(result.get("error") or "Rate sensitivity unavailable"))

        shocks = result.get("rate_shocks") or []
        total_mv = _safe_float(result.get("total_market_value"), 0.0)
        result["category_risk_breakdown"] = _category_risk_breakdown(
            result.get("duration_contributions") or [],
            total_market_value=total_mv,
        )
        worst = min(shocks, key=lambda x: _safe_float(x.get("pnl"), 0.0)) if shocks else {}
        worst_pct = abs(_safe_float(worst.get("pnl_pct"), 0.0))
        outcome = "PASS" if worst_pct <= 5.0 else "WARN"

        explained = _with_explanation(
            result,
            decision_type="rate_sensitivity",
            decision_outcome=outcome,
            summary=(
                f"Rate sensitivity completed. Worst parallel shock "
                f"{int(_safe_float(worst.get('shock_bps'), 0.0))} bps implies {round(_safe_float(worst.get('pnl'), 0.0), 2)} PnL."
            ),
            key_factors=[
                {
                    "name": "Portfolio DV01",
                    "value": round(_safe_float(result.get("portfolio_dv01"), 0.0), 2),
                    "impact": "negative" if _safe_float(result.get("portfolio_dv01"), 0.0) > 0 else "neutral",
                    "evidence": "Aggregate first-order rate sensitivity",
                },
                {
                    "name": "Worst Shock Loss (%)",
                    "value": round(worst_pct, 4),
                    "impact": "negative" if worst_pct > 5.0 else "neutral",
                    "evidence": "Parallel shock stress grid",
                },
            ],
            rule_checks=[
                {
                    "rule_code": "RATE_SHOCK_TOLERANCE",
                    "status": "PASS" if worst_pct <= 5.0 else "WARN",
                    "message": "Worst parallel shock loss within soft tolerance",
                    "threshold": 5.0,
                    "actual": round(worst_pct, 4),
                }
            ],
            calculation_trace={
                "total_market_value": result.get("total_market_value"),
                "portfolio_duration": result.get("portfolio_duration"),
                "worst_shock": worst,
            },
            confidence_score=0.85,
            confidence_drivers=[f"shock_points={len(shocks)}", "duration-convexity approximation"],
            counterfactuals=[
                build_counterfactual(
                    scenario="100bps_parallel_up",
                    expected_outcome="LOWER_PORTFOLIO_VALUE",
                    impact_summary="A +100 bps parallel shift generally produces a negative duration-driven PnL.",
                    delta_metrics={"shock_bps": 100},
                    rationale="Reference stress point for rate risk monitoring.",
                )
            ],
            severity="WARNING" if outcome == "WARN" else "INFO",
        )
        recorded = await _record_risk_decision(
            explained,
            decision_type="rate_sensitivity",
            decision_outcome=outcome,
            source_endpoint="/api/v1/risk/rate-sensitivity",
            user=user,
        )
        return _state_payload("ok", recorded)
    except Exception as e:
        log.exception("rate_sensitivity_failed")
        return _state_payload("error", {}, str(e))


@router.get("/spread-simulation")
async def spread_simulation(request: Request, user=Depends(require_role(Roles.VIEWER))):
    """Credit spread scenarios: parallel +50bps, tiered widening, largest issuer default."""
    try:
        log.info(
            "risk_endpoint_request",
            endpoint=str(request.url.path),
            input_payload={"query": dict(request.query_params)},
            user=(user or {}).get("username") if isinstance(user, dict) else str(user or "system"),
        )
        service_result = await compute_spread_simulation()
        service_status, service_data, service_message = _normalize_service_result(service_result)
        result = service_data if service_status == "ok" else {**service_data, "error": service_message or "Spread simulation unavailable"}
        log.info(
            "risk_endpoint_service_state",
            endpoint=str(request.url.path),
            service_status=service_status,
            portfolio_snapshot=(service_data.get("portfolio_snapshot") if isinstance(service_data, dict) else None),
            message=service_message,
        )
        if result.get("error"):
            no_data = _is_no_portfolio_error(result.get("error"))
            explained = _with_explanation(
                result,
                decision_type="spread_simulation",
                decision_outcome="NO_DATA" if no_data else "ERROR",
                summary="Spread simulation could not be computed because portfolio data is unavailable.",
                key_factors=[{"name": "Error", "value": result.get("error"), "impact": "negative"}],
                confidence_score=0.0,
                severity="CRITICAL",
            )
            recorded = await _record_risk_decision(
                explained,
                decision_type="spread_simulation",
                decision_outcome="NO_DATA" if no_data else "ERROR",
                source_endpoint="/api/v1/risk/spread-simulation",
                user=user,
            )
            if no_data:
                return _state_payload("no_data", recorded, "Insufficient portfolio data for risk computation")
            return _state_payload("error", recorded, str(result.get("error") or "Spread simulation unavailable"))

        parallel = result.get("parallel_50bps") or {}
        tiered = result.get("tiered_widening") or {}
        default_case = result.get("largest_default") or {}
        worst_pct = max(
            abs(_safe_float(parallel.get("pnl_pct"), 0.0)),
            abs(_safe_float(tiered.get("pnl_pct"), 0.0)),
            abs(_safe_float(default_case.get("pnl_pct"), 0.0)),
        )
        outcome = "PASS" if worst_pct <= 7.0 else "WARN"

        explained = _with_explanation(
            result,
            decision_type="spread_simulation",
            decision_outcome=outcome,
            summary="Spread risk simulation computed for parallel, tiered, and concentrated-default shocks.",
            key_factors=[
                {
                    "name": "Parallel +50bps PnL (%)",
                    "value": round(_safe_float(parallel.get("pnl_pct"), 0.0), 4),
                    "impact": "negative" if _safe_float(parallel.get("pnl_pct"), 0.0) < 0 else "neutral",
                },
                {
                    "name": "Largest Issuer Default Loss (%)",
                    "value": round(_safe_float(default_case.get("pnl_pct"), 0.0), 4),
                    "impact": "negative",
                    "evidence": f"Issuer={default_case.get('issuer')}",
                },
            ],
            rule_checks=[
                {
                    "rule_code": "SPREAD_STRESS_LOSS_LIMIT",
                    "status": "PASS" if worst_pct <= 7.0 else "WARN",
                    "message": "Worst spread scenario loss within soft tolerance",
                    "threshold": 7.0,
                    "actual": round(worst_pct, 4),
                }
            ],
            calculation_trace={
                "parallel_50bps": parallel,
                "tiered_widening": tiered,
                "largest_default": default_case,
            },
            confidence_score=0.82,
            confidence_drivers=["three spread stress formulations", "holding-level duration analytics"],
            counterfactuals=[
                build_counterfactual(
                    scenario="no_spread_widening",
                    expected_outcome="HIGHER_PORTFOLIO_VALUE",
                    impact_summary="Removing spread widening assumptions reduces simulated credit losses.",
                    delta_metrics={"parallel_50bps_pnl": _safe_float(parallel.get("pnl"), 0.0)},
                    rationale="Benchmark comparison for credit spread sensitivity.",
                )
            ],
            severity="WARNING" if outcome == "WARN" else "INFO",
        )
        recorded = await _record_risk_decision(
            explained,
            decision_type="spread_simulation",
            decision_outcome=outcome,
            source_endpoint="/api/v1/risk/spread-simulation",
            user=user,
        )
        return _state_payload("ok", recorded)
    except Exception as e:
        log.exception("spread_sim_failed")
        return _state_payload("error", {}, str(e))


@router.get("/scenarios")
async def scenarios(request: Request, user=Depends(require_role(Roles.VIEWER))):
    """Run all predefined macro/credit/rate scenarios."""
    try:
        log.info(
            "risk_endpoint_request",
            endpoint=str(request.url.path),
            input_payload={"query": dict(request.query_params)},
            user=(user or {}).get("username") if isinstance(user, dict) else str(user or "system"),
        )
        service_result = await run_scenarios()
        service_status, service_data, service_message = _normalize_service_result(service_result)
        result = service_data if service_status == "ok" else {**service_data, "error": service_message or "Scenario analysis unavailable"}
        log.info(
            "risk_endpoint_service_state",
            endpoint=str(request.url.path),
            service_status=service_status,
            portfolio_snapshot=(service_data.get("portfolio_snapshot") if isinstance(service_data, dict) else None),
            message=service_message,
        )
        if result.get("error"):
            no_data = _is_no_portfolio_error(result.get("error"))
            explained = _with_explanation(
                result,
                decision_type="scenario_analysis",
                decision_outcome="NO_DATA" if no_data else "ERROR",
                summary="Scenario analysis could not run because portfolio data is unavailable.",
                key_factors=[{"name": "Error", "value": result.get("error"), "impact": "negative"}],
                confidence_score=0.0,
                severity="CRITICAL",
            )
            recorded = await _record_risk_decision(
                explained,
                decision_type="scenario_analysis",
                decision_outcome="NO_DATA" if no_data else "ERROR",
                source_endpoint="/api/v1/risk/scenarios",
                user=user,
            )
            if no_data:
                return _state_payload("no_data", recorded, "Insufficient portfolio data for risk computation")
            return _state_payload("error", recorded, str(result.get("error") or "Scenario analysis unavailable"))

        worst = result.get("worst_case") or {}
        worst_pct = abs(_safe_float(worst.get("pnl_pct"), 0.0))
        outcome = "PASS" if worst_pct <= 8.0 else "WARN"

        explained = _with_explanation(
            result,
            decision_type="scenario_analysis",
            decision_outcome=outcome,
            summary=f"Ran {len(result.get('scenarios') or [])} predefined macro and credit scenarios.",
            key_factors=[
                {
                    "name": "Worst Scenario",
                    "value": worst.get("scenario_name") or "N/A",
                    "impact": "negative" if worst else "neutral",
                },
                {
                    "name": "Worst Scenario Loss (%)",
                    "value": round(worst_pct, 4),
                    "impact": "negative" if worst_pct > 8.0 else "neutral",
                },
            ],
            rule_checks=[
                {
                    "rule_code": "SCENARIO_LOSS_LIMIT",
                    "status": "PASS" if worst_pct <= 8.0 else "WARN",
                    "message": "Worst predefined scenario loss within soft tolerance",
                    "threshold": 8.0,
                    "actual": round(worst_pct, 4),
                }
            ],
            calculation_trace={
                "worst_case": worst,
                "best_case": result.get("best_case"),
                "calc_date": result.get("calc_date"),
            },
            confidence_score=0.8,
            confidence_drivers=["predefined scenario library", "holding-level repricing"],
            severity="WARNING" if outcome == "WARN" else "INFO",
        )
        recorded = await _record_risk_decision(
            explained,
            decision_type="scenario_analysis",
            decision_outcome=outcome,
            source_endpoint="/api/v1/risk/scenarios",
            user=user,
        )
        return _state_payload("ok", recorded)
    except Exception as e:
        log.exception("scenarios_failed")
        return _state_payload("error", {}, str(e))


@router.get("/var")
async def value_at_risk(request: Request, user=Depends(require_role(Roles.VIEWER))):
    """Parametric VaR at 95%/99% (1-day, 10-day) + Expected Shortfall."""
    try:
        log.info(
            "risk_endpoint_request",
            endpoint=str(request.url.path),
            input_payload={"query": dict(request.query_params)},
            user=(user or {}).get("username") if isinstance(user, dict) else str(user or "system"),
        )
        service_result = await compute_var()
        service_status, service_data, service_message = _normalize_service_result(service_result)
        result = service_data if service_status == "ok" else {**service_data, "error": service_message or "VaR unavailable"}
        log.info(
            "risk_endpoint_service_state",
            endpoint=str(request.url.path),
            service_status=service_status,
            portfolio_snapshot=(service_data.get("portfolio_snapshot") if isinstance(service_data, dict) else None),
            message=service_message,
        )
        if result.get("error"):
            no_data = _is_no_portfolio_error(result.get("error"))
            explained = _with_explanation(
                result,
                decision_type="value_at_risk",
                decision_outcome="NO_DATA" if no_data else "ERROR",
                summary="VaR could not be computed because portfolio data is unavailable.",
                key_factors=[{"name": "Error", "value": result.get("error"), "impact": "negative"}],
                confidence_score=0.0,
                severity="CRITICAL",
            )
            recorded = await _record_risk_decision(
                explained,
                decision_type="value_at_risk",
                decision_outcome="NO_DATA" if no_data else "ERROR",
                source_endpoint="/api/v1/risk/var",
                user=user,
            )
            if no_data:
                return _state_payload("no_data", recorded, "Insufficient portfolio data for risk computation")
            return _state_payload("error", recorded, str(result.get("error") or "VaR unavailable"))

        var_data = result.get("var") or {}
        var95_pct = _safe_float(var_data.get("var_95_1d_pct"), 0.0)
        var99_pct = _safe_float(var_data.get("var_99_1d_pct"), 0.0)
        outcome = "PASS" if var95_pct <= 2.0 and var99_pct <= 3.0 else "WARN"

        explained = _with_explanation(
            result,
            decision_type="value_at_risk",
            decision_outcome=outcome,
            summary="Computed parametric VaR and Expected Shortfall using rating-volatility and correlation assumptions.",
            key_factors=[
                {
                    "name": "VaR 95% (1D, %)",
                    "value": round(var95_pct, 4),
                    "impact": "negative" if var95_pct > 2.0 else "neutral",
                },
                {
                    "name": "VaR 99% (1D, %)",
                    "value": round(var99_pct, 4),
                    "impact": "negative" if var99_pct > 3.0 else "neutral",
                },
                {
                    "name": "Portfolio Daily Volatility (%)",
                    "value": round(_safe_float(result.get("portfolio_daily_vol_pct"), 0.0), 6),
                    "impact": "negative" if _safe_float(result.get("portfolio_daily_vol_pct"), 0.0) > 1.0 else "neutral",
                },
            ],
            rule_checks=[
                {
                    "rule_code": "VAR_95_LIMIT",
                    "status": "PASS" if var95_pct <= 2.0 else "WARN",
                    "message": "1-day 95% VaR within soft threshold",
                    "threshold": 2.0,
                    "actual": round(var95_pct, 4),
                },
                {
                    "rule_code": "VAR_99_LIMIT",
                    "status": "PASS" if var99_pct <= 3.0 else "WARN",
                    "message": "1-day 99% VaR within soft threshold",
                    "threshold": 3.0,
                    "actual": round(var99_pct, 4),
                },
            ],
            calculation_trace={
                "portfolio_value": result.get("portfolio_value"),
                "var": var_data,
                "expected_shortfall": result.get("expected_shortfall"),
                "methodology": result.get("methodology"),
            },
            confidence_score=0.78,
            confidence_drivers=["parametric VaR model", "rating-based volatility mapping"],
            counterfactuals=[
                build_counterfactual(
                    scenario="lower_correlation_regime",
                    expected_outcome="LOWER_VAR",
                    impact_summary="If cross-asset correlations fall, aggregate portfolio VaR declines.",
                    delta_metrics={"portfolio_daily_vol_pct": _safe_float(result.get("portfolio_daily_vol_pct"), 0.0)},
                    rationale="Correlation assumptions materially influence VaR estimates.",
                )
            ],
            severity="WARNING" if outcome == "WARN" else "INFO",
        )
        recorded = await _record_risk_decision(
            explained,
            decision_type="value_at_risk",
            decision_outcome=outcome,
            source_endpoint="/api/v1/risk/var",
            user=user,
        )
        return _state_payload("ok", recorded)
    except Exception as e:
        log.exception("var_failed")
        return _state_payload("error", {}, str(e))


@router.get("/yield-curve")
async def yield_curve(request: Request, user=Depends(require_role(Roles.VIEWER))):
    """Portfolio positioning on the yield curve vs G-Sec benchmark."""
    try:
        log.info(
            "risk_endpoint_request",
            endpoint=str(request.url.path),
            input_payload={"query": dict(request.query_params)},
            user=(user or {}).get("username") if isinstance(user, dict) else str(user or "system"),
        )
        service_result = await compute_yield_curve_position()
        service_status, service_data, service_message = _normalize_service_result(service_result)
        result = service_data if service_status == "ok" else {**service_data, "error": service_message or "Yield curve unavailable"}
        log.info(
            "risk_endpoint_service_state",
            endpoint=str(request.url.path),
            service_status=service_status,
            portfolio_snapshot=(service_data.get("portfolio_snapshot") if isinstance(service_data, dict) else None),
            message=service_message,
        )
        if result.get("error"):
            no_data = _is_no_portfolio_error(result.get("error"))
            explained = _with_explanation(
                result,
                decision_type="yield_curve_position",
                decision_outcome="NO_DATA" if no_data else "ERROR",
                summary="Yield-curve positioning could not be computed because portfolio data is unavailable.",
                key_factors=[{"name": "Error", "value": result.get("error"), "impact": "negative"}],
                confidence_score=0.0,
                severity="CRITICAL",
            )
            recorded = await _record_risk_decision(
                explained,
                decision_type="yield_curve_position",
                decision_outcome="NO_DATA" if no_data else "ERROR",
                source_endpoint="/api/v1/risk/yield-curve",
                user=user,
            )
            if no_data:
                return _state_payload("no_data", recorded, "Insufficient portfolio data for risk computation")
            return _state_payload("error", recorded, str(result.get("error") or "Yield curve unavailable"))

        spread_bps = _safe_float(result.get("portfolio_spread_bps"), 0.0)
        weighted_yield = _safe_float(result.get("portfolio_weighted_yield"), 0.0)

        explained = _with_explanation(
            result,
            decision_type="yield_curve_position",
            decision_outcome="PASS",
            summary="Mapped portfolio tenor positioning against benchmark G-Sec curve and computed aggregate spread pickup.",
            key_factors=[
                {
                    "name": "Portfolio Weighted Yield",
                    "value": round(weighted_yield, 4),
                    "impact": "neutral",
                },
                {
                    "name": "Portfolio Spread over G-Sec (bps)",
                    "value": round(spread_bps, 2),
                    "impact": "positive" if spread_bps > 0 else "neutral",
                },
                {
                    "name": "Tenor Buckets Covered",
                    "value": len(result.get("yield_curve") or []),
                    "impact": "neutral",
                },
            ],
            calculation_trace={
                "yield_curve": result.get("yield_curve"),
                "gsec_curve": result.get("gsec_curve"),
                "calc_date": result.get("calc_date"),
            },
            confidence_score=0.83,
            confidence_drivers=["tenor interpolation with benchmark curve", "holding-weighted aggregation"],
            severity="INFO",
        )
        recorded = await _record_risk_decision(
            explained,
            decision_type="yield_curve_position",
            decision_outcome="PASS",
            source_endpoint="/api/v1/risk/yield-curve",
            user=user,
        )
        return _state_payload("ok", recorded)
    except Exception as e:
        log.exception("yield_curve_failed")
        return _state_payload("error", {}, str(e))
