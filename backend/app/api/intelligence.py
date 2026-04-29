"""
Gateway Router — Bond Intelligence
Proxies/re-exposes the bond_intelligence service under /api/v1/intelligence
and injects shared dependencies (ISIN registry, structlog).
"""

from __future__ import annotations

import asyncio
import sys
import os
import json
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Query, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Optional, Any

from ..shared.isin_registry import ISINRegistry
from ..shared.logger import get_logger
from ..shared.database import fetch_all, fetch_one, execute_query
from ..shared.deployment import assert_external_data_egress
from ..shared.auth import require_role, get_current_user, Roles
from ..shared.metrics import alert_manager, AlertSeverity
from ..domain.core_trust.audit import AuditAction, AuditService, AuditSeverity, EntityType
from ..domain.market_data.base_fetcher import EnhancedMarketDataFetcher
from ..domain.risk.scoring_engine import UnifiedScorer
from ..utils.report_generator import UnifiedReportGenerator
from ..utils.pdf_generator import UnifiedPDFGenerator
from .compliance import run_compliance_check

log = get_logger("router.intelligence")
router = APIRouter(prefix="/intelligence", tags=["Bond Intelligence"])

registry = ISINRegistry()
fetcher = EnhancedMarketDataFetcher()
scorer = UnifiedScorer()
report_gen = UnifiedReportGenerator()
pdf_gen = UnifiedPDFGenerator()


RATING_RANK: dict[str, int] = {
    "AAA": 1,
    "AA+": 2,
    "AA": 3,
    "AA-": 4,
    "A+": 5,
    "A": 6,
    "A-": 7,
    "BBB+": 8,
    "BBB": 9,
    "BBB-": 10,
    "BB+": 11,
    "BB": 12,
    "BB-": 13,
    "B+": 14,
    "B": 15,
    "B-": 16,
    "C": 17,
    "D": 18,
}

MAX_REASONABLE_PORTFOLIO_MV_INR = 1_000_000_000_000_000.0

CRITICAL_FIELD_STATUS_KEYS: tuple[str, ...] = (
    "ytm",
    "coupon_rate",
    "maturity_date",
    "credit_rating",
    "market_price",
    "spread_over_gsec_bps",
    "liquidity_score",
    "entry_ytm",
    "duration",
    "portfolio_market_value",
    "scenario_analysis",
)

GAP_ALIASES: dict[str, tuple[str, ...]] = {
    "ytm": ("current ytm",),
    "coupon_rate": ("coupon", "coupon rate"),
    "maturity_date": ("maturity", "maturity date"),
    "credit_rating": ("latest credit rating", "credit rating"),
    "market_price": ("current market price",),
    "spread_over_gsec_bps": ("yield vs benchmark spread (g-sec)",),
    "liquidity_score": ("liquidity score",),
    "entry_ytm": ("entry ytm",),
    "duration": ("duration",),
    "portfolio_market_value": ("portfolio aum (sanity-checked)",),
    "scenario_analysis": ("scenario analysis base inputs",),
}

FIELD_VALUE_PATHS: dict[str, tuple[tuple[str, ...], ...]] = {
    "ytm": (("bond_snapshot", "yield", "ytm"),),
    "coupon_rate": (("bond_snapshot", "coupon_rate"),),
    "maturity_date": (("bond_snapshot", "maturity_date"),),
    "credit_rating": (("bond_snapshot", "credit_rating", "latest"),),
    "market_price": (("market_intelligence", "current_price"),),
    "spread_over_gsec_bps": (("market_intelligence", "spread_over_gsec_bps"),),
    "liquidity_score": (("market_intelligence", "liquidity_score"),),
    "entry_ytm": (("market_intelligence", "entry_ytm"),),
    "duration": (("bond_snapshot", "duration"),),
    "portfolio_market_value": (
        ("portfolio_impact", "before", "portfolio_market_value"),
        ("portfolio_impact", "after", "portfolio_market_value"),
    ),
    "scenario_analysis": (
        ("risk_engine", "scenario_analysis", "rate_up_pnl"),
        ("risk_engine", "scenario_analysis", "rate_down_pnl"),
    ),
}

CONTROL_STATE_PATH = Path(
    os.environ.get(
        "INTELLIGENCE_CONTROL_STATE_PATH",
        str(Path(__file__).resolve().parents[2] / "data" / "intelligence_control_state.json"),
    )
)

INTELLIGENCE_WEEKLY_REPORT_PATH = Path(
    os.environ.get(
        "INTELLIGENCE_WEEKLY_REPORT_PATH",
        str(Path(__file__).resolve().parents[2] / "data" / "intelligence_weekly_report_latest.json"),
    )
)

_ops_tables_initialized = False
_ops_tables_lock = asyncio.Lock()


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(value if value is not None else {}, ensure_ascii=True)
    except Exception:
        return "{}"


def _json_loads(value: Any, default: Any):
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return default


def _parse_datetime(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = _to_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _count_anomalies(control_layer: dict | None) -> dict:
    control_layer = control_layer or {}
    anomalies = control_layer.get("anomalies") or {}
    data_count = len(anomalies.get("data_anomalies") or [])
    logic_count = len(anomalies.get("logic_anomalies") or [])
    ux_count = len(anomalies.get("ux_anomalies") or [])
    high_count = 0
    medium_count = 0
    for bucket in ("data_anomalies", "logic_anomalies", "ux_anomalies"):
        for item in anomalies.get(bucket) or []:
            sev = _safe_upper((item or {}).get("severity"))
            if sev == "HIGH":
                high_count += 1
            elif sev == "MEDIUM":
                medium_count += 1
    return {
        "data": data_count,
        "logic": logic_count,
        "ux": ux_count,
        "high": high_count,
        "medium": medium_count,
        "total": data_count + logic_count + ux_count,
    }


def _build_missing_summary(payload: dict) -> dict:
    gaps = payload.get("gaps") or {}
    critical = [x for x in (gaps.get("critical") or []) if _to_text(x)]
    important = [x for x in (gaps.get("important") or []) if _to_text(x)]
    advanced = [x for x in (gaps.get("advanced") or []) if _to_text(x)]
    return {
        "critical": critical,
        "important": important,
        "advanced": advanced,
        "critical_count": len(critical),
        "important_count": len(important),
        "advanced_count": len(advanced),
        "total": len(critical) + len(important) + len(advanced),
    }


def _linear_slope(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    return (values[-1] - values[0]) / float(len(values) - 1)


def _compute_predictive_signal(observations: list[dict]) -> dict:
    if not observations:
        return {
            "status": "INSUFFICIENT_HISTORY",
            "early_warning_score": 0.0,
            "summary": "No prior analysis history is available for trend detection",
            "signals": {},
            "observations": 0,
        }

    health_values = [
        _to_float(item.get("health_score"))
        for item in observations
        if _to_float(item.get("health_score")) is not None
    ]
    confidence_values = [
        _to_float(item.get("confidence_score"))
        for item in observations
        if _to_float(item.get("confidence_score")) is not None
    ]
    completeness_values = [
        _to_float(item.get("completeness_pct"))
        for item in observations
        if _to_float(item.get("completeness_pct")) is not None
    ]

    health_slope = _linear_slope(health_values)
    confidence_slope = _linear_slope(confidence_values)
    completeness_slope = _linear_slope(completeness_values)

    blocked_count = sum(1 for item in observations if bool(item.get("is_blocked")))
    high_severity_count = sum(1 for item in observations if _safe_upper(item.get("control_severity")) == "HIGH")
    total = max(1, len(observations))
    blocked_ratio = blocked_count / total
    high_ratio = high_severity_count / total

    score = 0.0
    indicators: list[str] = []

    if health_slope <= -2.0:
        score += 38.0
        indicators.append("Health score has a steep negative slope")
    elif health_slope <= -1.0:
        score += 24.0
        indicators.append("Health score is drifting down")

    if confidence_slope <= -2.0:
        score += 28.0
        indicators.append("Decision confidence is eroding rapidly")
    elif confidence_slope <= -1.0:
        score += 16.0
        indicators.append("Decision confidence is trending lower")

    if completeness_slope <= -2.0:
        score += 22.0
        indicators.append("Data completeness is deteriorating")
    elif completeness_slope <= -1.0:
        score += 12.0
        indicators.append("Data completeness is declining")

    if blocked_ratio >= 0.20:
        score += 35.0
        indicators.append("Blocked-analysis ratio is elevated")
    elif blocked_ratio >= 0.10:
        score += 20.0

    if high_ratio >= 0.25:
        score += 20.0
        indicators.append("High-severity control events are recurring")
    elif high_ratio >= 0.10:
        score += 12.0

    latest = observations[-1]
    latest_health = _to_float(latest.get("health_score"))
    if latest_health is not None and latest_health < 70.0:
        score += 12.0

    warning_score = min(100.0, round(score, 2))
    if warning_score >= 70.0:
        status = "CRITICAL"
        summary = "Early warning indicates likely reliability degradation without intervention"
    elif warning_score >= 45.0:
        status = "WATCH"
        summary = "Early warning detected; intervention should be planned"
    else:
        status = "STABLE"
        summary = "Trend profile is stable"

    return {
        "status": status,
        "early_warning_score": warning_score,
        "summary": summary,
        "signals": {
            "health_slope": round(health_slope, 3),
            "confidence_slope": round(confidence_slope, 3),
            "completeness_slope": round(completeness_slope, 3),
            "blocked_ratio": round(blocked_ratio * 100.0, 2),
            "high_severity_ratio": round(high_ratio * 100.0, 2),
        },
        "leading_indicators": indicators,
        "observations": len(observations),
    }


def _build_decision_explainability(payload: dict) -> dict:
    decision = payload.get("decision_signal") or {}
    risk = payload.get("risk_engine") or {}
    dq = payload.get("data_quality") or {}
    compliance = payload.get("compliance") or {}
    control = payload.get("control_layer") or {}
    gaps = payload.get("gaps") or {}

    confidence = _to_float(decision.get("confidence_score"), 0.0) or 0.0
    risk_score = _to_float(risk.get("risk_score"), 0.0) or 0.0
    completeness = _to_float(dq.get("completeness_pct"), 0.0) or 0.0
    control_health = _to_float(control.get("health_score"), 0.0) or 0.0

    why_decision = [
        f"Decision outcome is {_to_text(decision.get('label') or decision.get('signal') or 'REVIEW')} based on policy and quality checks",
        f"Model confidence is {confidence:.1f}/100 with data completeness at {completeness:.1f}%",
        f"Composite risk score is {risk_score:.1f} ({_to_text(risk.get('risk_band') or 'UNKNOWN')})",
    ]

    risk_components = risk.get("components") or {}
    ranked_components = sorted(
        [
            ("Credit", _to_float(risk_components.get("credit_risk"))),
            ("Rate Sensitivity", _to_float(risk_components.get("interest_rate_risk"))),
            ("Liquidity", _to_float(risk_components.get("liquidity_risk"))),
            ("Concentration", _to_float(risk_components.get("concentration_risk"))),
        ],
        key=lambda item: item[1] if item[1] is not None else -1,
        reverse=True,
    )
    key_risks = [
        f"{name} contribution is {score:.1f}/100"
        for name, score in ranked_components[:2]
        if score is not None
    ]

    failed_rules = int(compliance.get("failures") or 0)
    warned_rules = int(compliance.get("warnings") or 0)
    data_notes = [
        f"Critical gaps: {len(gaps.get('critical') or [])}, important gaps: {len(gaps.get('important') or [])}",
        f"Control-layer severity is {_to_text(control.get('severity') or 'NONE')} with health {control_health:.1f}/100",
    ]
    if failed_rules > 0:
        data_notes.append(f"Compliance has {failed_rules} failure(s) requiring remediation")
    elif warned_rules > 0:
        data_notes.append(f"Compliance has {warned_rules} warning(s) under monitoring")

    committee_actions: list[str] = []
    signal = _safe_upper(decision.get("signal"))
    severity = _safe_upper(control.get("severity"))
    if signal == "AVOID" or severity == "HIGH":
        committee_actions.append("Hold allocation until quality and risk exceptions are resolved")
        committee_actions.append("Escalate to risk committee with full evidence pack")
    elif signal == "REVIEW" or severity == "MEDIUM":
        committee_actions.append("Require manual IC review and supporting memo before execution")
        committee_actions.append("Apply tighter position cap until confidence improves")
    else:
        committee_actions.append("Eligible for execution under normal mandate limits")
        committee_actions.append("Continue monitoring for trend degradation")

    return {
        "headline": _to_text(decision.get("reason")) or "Decision generated from integrated quality, risk, and compliance signals",
        "why_this_decision": why_decision,
        "key_risk_drivers": key_risks,
        "data_reliability_notes": data_notes,
        "committee_actions": committee_actions,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _normalize_audit_row(row: dict) -> dict:
    created_at = _to_text(row.get("created_at"))
    anomalies = _json_loads(row.get("anomaly_counts_json"), {})
    missing_summary = _json_loads(row.get("missing_summary_json"), {})
    payload_snapshot = _json_loads(row.get("payload_json"), {})
    return {
        "id": row.get("id"),
        "isin": _to_text(row.get("isin")),
        "decision_signal": _to_text(row.get("decision_signal")),
        "decision_label": _to_text(row.get("decision_label")),
        "decision_reason": _to_text(row.get("decision_reason")),
        "confidence_score": _to_float(row.get("confidence_score")),
        "confidence_level": _to_text(row.get("confidence_level")),
        "risk_score": _to_float(row.get("risk_score")),
        "risk_band": _to_text(row.get("risk_band")),
        "completeness_pct": _to_float(row.get("completeness_pct")),
        "control_severity": _to_text(row.get("control_severity")),
        "health_score": _to_float(row.get("health_score")),
        "is_blocked": bool(row.get("is_blocked")),
        "external_alert_triggered": bool(row.get("external_alert_triggered")),
        "predictive_status": _to_text(row.get("predictive_status")),
        "predictive_score": _to_float(row.get("predictive_score")),
        "anomaly_counts": anomalies,
        "missing_summary": missing_summary,
        "payload_snapshot": payload_snapshot,
        "created_at": created_at,
    }


async def _ensure_intelligence_ops_tables() -> None:
    global _ops_tables_initialized
    if _ops_tables_initialized:
        return
    async with _ops_tables_lock:
        if _ops_tables_initialized:
            return
        await execute_query(
            """
            CREATE TABLE IF NOT EXISTS intelligence_analysis_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                isin TEXT NOT NULL,
                decision_signal TEXT,
                decision_label TEXT,
                decision_reason TEXT,
                confidence_score REAL,
                confidence_level TEXT,
                risk_score REAL,
                risk_band TEXT,
                completeness_pct REAL,
                control_severity TEXT,
                health_score REAL,
                is_blocked INTEGER DEFAULT 0,
                external_alert_triggered INTEGER DEFAULT 0,
                predictive_status TEXT,
                predictive_score REAL,
                anomaly_counts_json TEXT,
                missing_summary_json TEXT,
                payload_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        await execute_query(
            "CREATE INDEX IF NOT EXISTS idx_intelligence_audit_isin_created_at ON intelligence_analysis_audit (isin, created_at)"
        )
        await execute_query(
            "CREATE INDEX IF NOT EXISTS idx_intelligence_audit_created_at ON intelligence_analysis_audit (created_at)"
        )
        await execute_query(
            """
            CREATE TABLE IF NOT EXISTS intelligence_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                isin TEXT,
                category TEXT,
                sentiment TEXT,
                feedback_text TEXT NOT NULL,
                user_id TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        await execute_query(
            "CREATE INDEX IF NOT EXISTS idx_intelligence_feedback_created_at ON intelligence_feedback (created_at)"
        )
        _ops_tables_initialized = True


async def _fetch_recent_audit_rows(isin: str | None = None, limit: int = 200) -> list[dict]:
    await _ensure_intelligence_ops_tables()
    if isin:
        rows = await fetch_all(
            """
            SELECT * FROM intelligence_analysis_audit
            WHERE isin = :isin
            ORDER BY created_at DESC, id DESC
            LIMIT :limit
            """,
            {"isin": isin.strip().upper(), "limit": limit},
        )
    else:
        rows = await fetch_all(
            """
            SELECT * FROM intelligence_analysis_audit
            ORDER BY created_at DESC, id DESC
            LIMIT :limit
            """,
            {"limit": limit},
        )
    return [_normalize_audit_row(row) for row in rows]


async def _dispatch_external_alert_if_needed(payload: dict) -> bool:
    control = payload.get("control_layer") or {}
    severity = _safe_upper(control.get("severity"))
    if severity != "HIGH":
        return False

    isin = _to_text(payload.get("isin")) or "UNKNOWN"
    confidence = _to_float(((payload.get("decision_signal") or {}).get("confidence_score")), 0.0) or 0.0
    health = _to_float(control.get("health_score"), 0.0) or 0.0
    summary = (
        f"High-severity intelligence control triggered for {isin}. "
        f"Health score={health:.1f}, confidence={confidence:.1f}. "
        "Decision output has been blocked pending remediation."
    )
    metadata = {
        "isin": isin,
        "control_severity": severity,
        "enforcement_action": _to_text(control.get("enforcement_action")),
        "health_score": health,
        "decision_signal": _to_text((payload.get("decision_signal") or {}).get("signal")),
        "analysis_state": payload.get("analysis_state") or {},
    }
    try:
        await alert_manager.fire(
            AlertSeverity.CRITICAL,
            f"Intelligence Control Blocked: {isin}",
            summary,
            metadata,
        )
        return True
    except Exception as exc:
        log.error("external_alert_dispatch_failed", isin=isin, error=str(exc))
        return False


async def _record_analysis_audit(payload: dict, predictive_signal: dict, external_alert_triggered: bool) -> None:
    await _ensure_intelligence_ops_tables()
    decision = payload.get("decision_signal") or {}
    risk = payload.get("risk_engine") or {}
    dq = payload.get("data_quality") or {}
    control = payload.get("control_layer") or {}
    anomaly_counts = _count_anomalies(control)
    missing_summary = _build_missing_summary(payload)
    await execute_query(
        """
        INSERT INTO intelligence_analysis_audit (
            isin,
            decision_signal,
            decision_label,
            decision_reason,
            confidence_score,
            confidence_level,
            risk_score,
            risk_band,
            completeness_pct,
            control_severity,
            health_score,
            is_blocked,
            external_alert_triggered,
            predictive_status,
            predictive_score,
            anomaly_counts_json,
            missing_summary_json,
            payload_json,
            created_at
        ) VALUES (
            :isin,
            :decision_signal,
            :decision_label,
            :decision_reason,
            :confidence_score,
            :confidence_level,
            :risk_score,
            :risk_band,
            :completeness_pct,
            :control_severity,
            :health_score,
            :is_blocked,
            :external_alert_triggered,
            :predictive_status,
            :predictive_score,
            :anomaly_counts_json,
            :missing_summary_json,
            :payload_json,
            :created_at
        )
        """,
        {
            "isin": _to_text(payload.get("isin")).upper(),
            "decision_signal": _to_text(decision.get("signal")),
            "decision_label": _to_text(decision.get("label")),
            "decision_reason": _to_text(decision.get("reason")),
            "confidence_score": _to_float(decision.get("confidence_score")),
            "confidence_level": _to_text(decision.get("confidence_level")),
            "risk_score": _to_float(risk.get("risk_score")),
            "risk_band": _to_text(risk.get("risk_band")),
            "completeness_pct": _to_float(dq.get("completeness_pct")),
            "control_severity": _to_text(control.get("severity") or "NONE"),
            "health_score": _to_float(control.get("health_score")),
            "is_blocked": 1 if _safe_upper(((payload.get("analysis_state") or {}).get("status")) or "") == "BLOCKED" else 0,
            "external_alert_triggered": 1 if external_alert_triggered else 0,
            "predictive_status": _to_text(predictive_signal.get("status")),
            "predictive_score": _to_float(predictive_signal.get("early_warning_score")),
            "anomaly_counts_json": _json_dumps(anomaly_counts),
            "missing_summary_json": _json_dumps(missing_summary),
            "payload_json": _json_dumps(payload),
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )


class IntelligenceFeedbackCreate(BaseModel):
    feedback: str = Field(min_length=5, max_length=4000)
    isin: Optional[str] = Field(default=None, max_length=32)
    category: str = Field(default="SYSTEM_QUALITY", max_length=64)
    sentiment: str = Field(default="NEUTRAL", max_length=32)


def _to_float(value, default: float | None = None) -> float | None:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _to_text(value) -> str:
    return str(value or "").strip()


def _safe_upper(value) -> str:
    return _to_text(value).upper()


def _parse_date(value) -> date | None:
    if value in (None, ""):
        return None
    s = _to_text(value)
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:19], fmt).date()
        except Exception:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        return None


def _category_bucket(*values) -> str:
    for raw in values:
        token = _safe_upper(raw)
        if not token:
            continue
        if token in {"CENTRAL", "GOVT", "GOVERNMENT", "SOVEREIGN", "GOI", "G-SEC", "GSEC", "T-BILL", "TBILL"}:
            return "CENTRAL"
        if token in {"STATE", "SDL", "STATE GOVT", "STATE GOVERNMENT"}:
            return "STATE"
        if token in {"PSU", "PUBLIC SECTOR", "PSE"}:
            return "PSU"
        if token in {"PRIVATE", "CORPORATE"}:
            return "PRIVATE"
        if token in {"EQUITY"}:
            return "EQUITY"
    return "UNKNOWN"


def _rating_outlook_from_rank(rank: int | None) -> str:
    if rank is None:
        return "UNKNOWN"
    if rank <= 4:
        return "STABLE"
    if rank <= 7:
        return "WATCH"
    return "NEGATIVE"


def _risk_band(score: float) -> str:
    if score >= 75:
        return "HIGH"
    if score >= 45:
        return "MEDIUM"
    return "LOW"


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = sum(values) / len(values)
    var = sum((x - m) ** 2 for x in values) / (len(values) - 1)
    return var ** 0.5


def _round_or_none(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _get_path(data: dict, path: tuple[str, ...]):
    cursor = data
    for key in path:
        if not isinstance(cursor, dict) or key not in cursor:
            return None
        cursor = cursor[key]
    return cursor


def _set_path(data: dict, path: tuple[str, ...], value) -> None:
    cursor = data
    for key in path[:-1]:
        nxt = cursor.get(key)
        if not isinstance(nxt, dict):
            return
        cursor = nxt
    if isinstance(cursor, dict) and path[-1] in cursor:
        cursor[path[-1]] = value


def _enforce_missing_field_masking(payload: dict) -> None:
    field_status = (((payload.get("data_quality") or {}).get("field_status")) or {})
    for field_key, paths in FIELD_VALUE_PATHS.items():
        status = _safe_upper((field_status.get(field_key) or {}).get("status"))
        if status != "MISSING":
            continue
        for path in paths:
            if _is_number(_get_path(payload, path)):
                _set_path(payload, path, None)


def _build_validation_engine_report(payload: dict) -> dict:
    violations: list[str] = []
    checks: dict[str, bool] = {}

    schema_version = _to_text(payload.get("schema_version"))
    checks["schema_version_present"] = bool(schema_version)
    checks["schema_version_supported"] = schema_version == "2.1.0"

    dq = payload.get("data_quality") or {}
    field_status = dq.get("field_status") or {}
    gaps = payload.get("gaps") or {}
    portfolio_validation = ((payload.get("portfolio_impact") or {}).get("validation")) or {}
    scenario = ((payload.get("risk_engine") or {}).get("scenario_analysis")) or {}

    checks["field_status_present"] = isinstance(field_status, dict) and len(field_status) > 0
    missing_keys = [k for k in CRITICAL_FIELD_STATUS_KEYS if k not in field_status]
    checks["field_status_complete"] = len(missing_keys) == 0

    checks["portfolio_validation_present"] = isinstance(portfolio_validation, dict) and len(portfolio_validation) > 0
    checks["scenario_validation_present"] = isinstance(scenario, dict) and len(scenario) > 0

    if not checks["schema_version_present"]:
        violations.append("schema_version is missing from response")
    if not checks["schema_version_supported"]:
        violations.append("schema_version is not 2.1.0")
    if not checks["field_status_present"]:
        violations.append("data_quality.field_status is missing")
    if missing_keys:
        violations.append(f"data_quality.field_status missing critical keys: {', '.join(missing_keys)}")
    if not checks["portfolio_validation_present"]:
        violations.append("portfolio_impact.validation block is missing")
    if not checks["scenario_validation_present"]:
        violations.append("risk_engine.scenario_analysis block is missing")

    # Rule: missing status or gap presence must not co-exist with numeric renderable values.
    gap_labels = {
        _to_text(label).lower()
        for label in [
            *((gaps.get("critical") or [])),
            *((gaps.get("important") or [])),
            *((gaps.get("advanced") or [])),
        ]
        if _to_text(label)
    }
    for field_key, paths in FIELD_VALUE_PATHS.items():
        status = _safe_upper((field_status.get(field_key) or {}).get("status"))
        in_gap_register = any(alias in gap_labels for alias in GAP_ALIASES.get(field_key, ()))
        should_be_masked = status == "MISSING" or in_gap_register
        if not should_be_masked:
            continue
        for path in paths:
            value = _get_path(payload, path)
            if _is_number(value):
                violations.append(
                    f"Field '{field_key}' is missing/gapped but numeric value is present at {'.'.join(path)}"
                )

    # Rule: scenario requires clean price and duration inputs.
    scenario_available = bool(scenario.get("is_available") is True)
    clean_price = _get_path(payload, ("market_intelligence", "current_price"))
    duration = _get_path(payload, ("bond_snapshot", "duration"))
    up_pnl = _get_path(payload, ("risk_engine", "scenario_analysis", "rate_up_pnl"))
    down_pnl = _get_path(payload, ("risk_engine", "scenario_analysis", "rate_down_pnl"))
    if scenario_available and (clean_price is None or duration is None):
        violations.append("Scenario analysis marked available without required inputs (current_price/duration)")
    if (not scenario_available) and (_is_number(up_pnl) or _is_number(down_pnl)):
        violations.append("Scenario analysis has numeric outputs despite unavailable inputs")

    # Rule: AUM sanity must be explicit and enforce masking on failed sanity.
    aum_reasonable = bool(portfolio_validation.get("is_reasonable"))
    before_aum = _get_path(payload, ("portfolio_impact", "before", "portfolio_market_value"))
    after_aum = _get_path(payload, ("portfolio_impact", "after", "portfolio_market_value"))
    max_reasonable = _to_float(portfolio_validation.get("max_reasonable_inr"))
    if not aum_reasonable and (_is_number(before_aum) or _is_number(after_aum)):
        violations.append("AUM sanity failed but numeric AUM is still present")
    if aum_reasonable and max_reasonable is not None:
        if (_is_number(before_aum) and before_aum > max_reasonable) or (_is_number(after_aum) and after_aum > max_reasonable):
            violations.append("AUM exceeds realistic bound while marked as reasonable")

    status = "PASS" if not violations else "FAIL"
    return {
        "status": status,
        "violations": violations,
        "enforcement": "flagged" if violations else "clean",
        "checked_rule_count": len(checks) + 4,
        "checks": checks,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def _humanize_im_item(item) -> str:
    if item is None:
        return ""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, (int, float, bool)):
        return str(item)
    if isinstance(item, dict):
        preferred = ("message", "description", "label", "name", "rule", "trigger", "value")
        for key in preferred:
            text = _to_text(item.get(key))
            if text:
                return text
        parts: list[str] = []
        for key, value in item.items():
            text = _to_text(value)
            if text:
                parts.append(f"{key.replace('_', ' ').title()}: {text}")
            if len(parts) >= 3:
                break
        return "; ".join(parts)
    if isinstance(item, list):
        parts = [_to_text(x) for x in item if _to_text(x)]
        return ", ".join(parts[:4])
    return _to_text(item)


def _read_external_control_state() -> dict:
    try:
        if not CONTROL_STATE_PATH.exists():
            return {}
        return json.loads(CONTROL_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _severity_rank(level: str) -> int:
    return {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}.get(_safe_upper(level), 0)


def _degrade_confidence(decision_signal: dict) -> None:
    if not isinstance(decision_signal, dict):
        return
    current_score = _to_float(decision_signal.get("confidence_score"), 0.0) or 0.0
    next_score = max(0.0, round(current_score - 20.0, 1))
    level = _safe_upper(decision_signal.get("confidence_level"))
    if level == "HIGH":
        next_level = "MEDIUM"
    elif level in {"MEDIUM", ""}:
        next_level = "LOW"
    else:
        next_level = "LOW"
    decision_signal["confidence_score"] = next_score
    decision_signal["confidence_level"] = next_level


def _build_intelligent_control_report(payload: dict) -> dict:
    anomalies: dict[str, list[dict]] = {
        "data_anomalies": [],
        "logic_anomalies": [],
        "ux_anomalies": [],
    }

    def _record(category: str, code: str, severity: str, message: str) -> None:
        anomalies[category].append(
            {
                "code": code,
                "severity": _safe_upper(severity),
                "message": message,
            }
        )

    field_status = (((payload.get("data_quality") or {}).get("field_status")) or {})
    critical_total = len(CRITICAL_FIELD_STATUS_KEYS)
    missing_count = 0
    for key in CRITICAL_FIELD_STATUS_KEYS:
        status = _safe_upper((field_status.get(key) or {}).get("status"))
        if status == "MISSING":
            missing_count += 1
    missing_ratio = (missing_count / critical_total) if critical_total else 0.0
    if missing_ratio >= 0.70:
        _record(
            "data_anomalies",
            "DATA_MISSING_SPIKE",
            "HIGH",
            f"Critical missing ratio is {missing_ratio:.2f}; indicates severe data availability degradation",
        )
    elif missing_ratio >= 0.45:
        _record(
            "data_anomalies",
            "DATA_MISSING_ELEVATED",
            "MEDIUM",
            f"Critical missing ratio is {missing_ratio:.2f}; monitor for drift",
        )

    ytm = _to_float(_get_path(payload, ("bond_snapshot", "yield", "ytm")))
    spread_bps = _to_float(_get_path(payload, ("market_intelligence", "spread_over_gsec_bps")))
    before_aum = _to_float(_get_path(payload, ("portfolio_impact", "before", "portfolio_market_value")))
    if ytm is not None and (ytm < 0.0 or ytm > 40.0):
        _record("data_anomalies", "YTM_SPIKE", "HIGH", f"YTM outlier detected: {ytm}")
    elif ytm is not None and ytm > 25.0:
        _record("data_anomalies", "YTM_ELEVATED", "MEDIUM", f"YTM elevated beyond expected bound: {ytm}")
    if spread_bps is not None and abs(spread_bps) > 2000:
        _record("data_anomalies", "SPREAD_SPIKE", "HIGH", f"Spread over benchmark outlier detected: {spread_bps} bps")
    elif spread_bps is not None and abs(spread_bps) > 1200:
        _record("data_anomalies", "SPREAD_ELEVATED", "MEDIUM", f"Spread over benchmark unusually high: {spread_bps} bps")
    if before_aum is not None and before_aum > MAX_REASONABLE_PORTFOLIO_MV_INR:
        _record("data_anomalies", "AUM_SPIKE", "HIGH", "Portfolio AUM exceeds realistic hard bound")

    scenario = ((payload.get("risk_engine") or {}).get("scenario_analysis")) or {}
    scenario_available = bool(scenario.get("is_available") is True)
    has_up = _is_number(scenario.get("rate_up_pnl"))
    has_down = _is_number(scenario.get("rate_down_pnl"))
    if scenario_available and (ytm is None or _to_float(_get_path(payload, ("bond_snapshot", "duration"))) is None):
        _record(
            "logic_anomalies",
            "SCENARIO_INPUT_CONTRADICTION",
            "HIGH",
            "Scenario outputs are marked available while required base inputs are missing",
        )
    if (not scenario_available) and (has_up or has_down):
        _record(
            "logic_anomalies",
            "SCENARIO_OUTPUT_CONTRADICTION",
            "HIGH",
            "Scenario outputs are populated while scenario availability is false",
        )

    completeness = _to_float(((payload.get("data_quality") or {}).get("completeness_pct")), 0.0) or 0.0
    confidence = _to_float(((payload.get("decision_signal") or {}).get("confidence_score")), 0.0) or 0.0
    if completeness < 40.0 and confidence >= 70.0:
        _record(
            "logic_anomalies",
            "CONFIDENCE_MISMATCH_SEVERE",
            "HIGH",
            "Confidence score is too high for severely incomplete data",
        )
    elif completeness < 60.0 and confidence >= 80.0:
        _record(
            "logic_anomalies",
            "CONFIDENCE_MISMATCH",
            "MEDIUM",
            "Confidence score is inconsistent with data completeness",
        )

    gap_labels = {
        _to_text(label).lower()
        for label in [
            *(((payload.get("gaps") or {}).get("critical")) or []),
            *(((payload.get("gaps") or {}).get("important")) or []),
            *(((payload.get("gaps") or {}).get("advanced")) or []),
        ]
        if _to_text(label)
    }
    for field_key, paths in FIELD_VALUE_PATHS.items():
        status = _safe_upper((field_status.get(field_key) or {}).get("status"))
        in_gaps = any(alias in gap_labels for alias in GAP_ALIASES.get(field_key, ()))
        if status != "MISSING" and not in_gaps:
            continue
        for path in paths:
            value = _get_path(payload, path)
            if _is_number(value):
                _record(
                    "ux_anomalies",
                    "NUMERIC_WHILE_MISSING",
                    "HIGH",
                    f"Numeric value is visible for missing field '{field_key}' at {'.'.join(path)}",
                )

    im_blocks = [
        _get_path(payload, ("im_intelligence", "key_covenants")),
        _get_path(payload, ("im_intelligence", "red_flags")),
        _get_path(payload, ("im_intelligence", "structural_risks")),
        _get_path(payload, ("im_intelligence", "default_downgrade_triggers")),
    ]
    for block in im_blocks:
        if not isinstance(block, list):
            continue
        if any(isinstance(item, (dict, list)) for item in block):
            _record(
                "ux_anomalies",
                "RAW_OBJECT_RENDER_RISK",
                "MEDIUM",
                "Raw object detected in IM sections; formatting risk exists",
            )
            break

    validation_status = _safe_upper(((payload.get("validation") or {}).get("status")))
    validation_engine_status = _safe_upper((((payload.get("data_quality") or {}).get("validation_engine") or {}).get("status"))
    )
    api_contract_component = 100.0 if validation_status == "PASS" and validation_engine_status == "PASS" else 40.0
    calculation_component = max(0.0, 100.0 - (25.0 * len(anomalies["logic_anomalies"])))
    ui_component = max(0.0, 100.0 - (20.0 * len(anomalies["ux_anomalies"])))
    data_component = max(0.0, min(100.0, completeness))
    health_score = round(
        (0.35 * data_component)
        + (0.30 * calculation_component)
        + (0.20 * api_contract_component)
        + (0.15 * ui_component),
        2,
    )
    if health_score >= 90.0:
        health_status = "HEALTHY"
    elif health_score >= 70.0:
        health_status = "WATCH"
    else:
        health_status = "CRITICAL"

    severity = "NONE"
    for category in anomalies.values():
        for item in category:
            if _severity_rank(item["severity"]) > _severity_rank(severity):
                severity = item["severity"]
    if health_score < 70.0 and _severity_rank(severity) < _severity_rank("HIGH"):
        severity = "HIGH"
    elif health_score < 90.0 and severity == "NONE":
        severity = "LOW"

    external_state = _read_external_control_state()
    external_action = ((external_state.get("bond_actions") or {}).get(_to_text(payload.get("isin")))) or {}
    external_severity = _safe_upper(external_action.get("severity"))
    if _severity_rank(external_severity) > _severity_rank(severity):
        severity = external_severity
        _record(
            "data_anomalies",
            "EXTERNAL_RUNTIME_OVERRIDE",
            external_severity,
            _to_text(external_action.get("reason")) or "Runtime monitor external override applied",
        )

    if severity == "HIGH":
        action = "BLOCK_UI_ANALYSIS"
    elif severity == "MEDIUM":
        action = "FLAG_RELIABILITY_AND_REDUCE_CONFIDENCE"
    elif severity == "LOW":
        action = "LOG_WARNING_AND_TREND"
    else:
        action = "NONE"

    return {
        "status": "ALERT" if severity in {"HIGH", "MEDIUM"} else "PASS",
        "severity": severity,
        "enforcement_action": action,
        "health_score": health_score,
        "health_status": health_status,
        "components": {
            "data_completeness_pct": round(data_component, 2),
            "calculation_validity_pct": round(calculation_component, 2),
            "api_response_success_pct": round(api_contract_component, 2),
            "ui_consistency_pct": round(ui_component, 2),
        },
        "anomalies": anomalies,
        "control_checked_at": datetime.now(timezone.utc).isoformat(),
        "blocked_message": "Analysis temporarily unavailable due to data inconsistency",
    }


def _apply_intelligent_control(payload: dict) -> None:
    control = _build_intelligent_control_report(payload)
    payload["control_layer"] = control
    severity = _safe_upper(control.get("severity"))

    if severity == "MEDIUM":
        _degrade_confidence(payload.get("decision_signal") or {})
        data_quality = payload.get("data_quality") or {}
        data_quality["reliability_flag"] = "Data Reliability Issue"
        payload["data_quality"] = data_quality

    if severity == "HIGH":
        payload["analysis_state"] = {
            "status": "BLOCKED",
            "severity": "HIGH",
            "message": control.get("blocked_message"),
        }


async def _resolve_bond_with_market(isin: str) -> tuple[dict, dict]:
    isin_u = isin.strip().upper()
    bond = await registry.get(isin_u)
    if not bond:
        quote = await fetch_one(
            """SELECT bdq.isin, bdq.bond_name, bdq.rating AS credit_rating,
                      bdq.rating_agency, bdq.coupon_rate, bdq.maturity_date,
                      bdq.category AS issuer_type, bdq.broker_name,
                      bdq.quote_date, bdq.clean_price, bdq.ytm
               FROM broker_daily_quotes bdq
               WHERE bdq.isin = :isin
               ORDER BY bdq.quote_date DESC, bdq.extracted_at DESC
               LIMIT 1""",
            {"isin": isin_u},
        )
        if not quote:
            raise HTTPException(status_code=404, detail=f"ISIN {isin_u} not found")
        coupon_pct = quote.get("coupon_rate")
        if coupon_pct is not None and 0 < coupon_pct < 1.0:
            coupon_pct = round(float(coupon_pct) * 100, 4)
        ytm_pct = quote.get("ytm")
        if ytm_pct is not None and 0 < ytm_pct < 1.0:
            ytm_pct = round(float(ytm_pct) * 100, 6)
        bond = {
            "isin": quote.get("isin"),
            "bond_name": quote.get("bond_name"),
            "issuer_name": quote.get("bond_name") or quote.get("isin"),
            "issuer_type": quote.get("issuer_type") or "CORPORATE",
            "credit_rating": quote.get("credit_rating"),
            "rating_agency": quote.get("rating_agency"),
            "coupon_rate": coupon_pct,
            "maturity_date": quote.get("maturity_date"),
            "face_value": 1000,
            "coupon_type": "FIXED",
            "coupon_frequency": 2,
            "sector": quote.get("issuer_type"),
            "instrument_category": quote.get("issuer_type"),
            "is_pf_eligible": False,
            "_partial": True,
            "_source": f"Broker: {quote.get('broker_name')} ({quote.get('quote_date')})",
        }
        return bond, {
            "isin": isin_u,
            "clean_price": quote.get("clean_price"),
            "ytm": ytm_pct,
            "price_date": quote.get("quote_date"),
            "data_source": "broker_daily_quotes",
        }

    market = await fetch_one(
        """SELECT * FROM unified_market_data
           WHERE isin = :isin
           ORDER BY price_date DESC, id DESC
           LIMIT 1""",
        {"isin": isin_u},
    ) or {}
    return bond, market


def _extract_im_insights(im_row: dict | None) -> dict:
    if not im_row:
        return {
            "coverage": "MISSING",
            "key_covenants": [],
            "red_flags": ["IM record unavailable"],
            "structural_risks": [],
            "default_downgrade_triggers": [],
        }

    def _as_json_or_text(value):
        if value in (None, ""):
            return None
        if isinstance(value, (dict, list)):
            return value
        text = _to_text(value)
        try:
            return json.loads(text)
        except Exception:
            return text

    cov_fin = _as_json_or_text(im_row.get("covenants_financial"))
    cov_non = _as_json_or_text(im_row.get("covenants_non_financial"))
    risk_flags = _as_json_or_text(im_row.get("risk_flags"))

    key_covenants: list[str] = []
    for src in (cov_fin, cov_non):
        if isinstance(src, dict):
            for k, v in src.items():
                if len(key_covenants) >= 6:
                    break
                key_covenants.append(f"{k}: {v}")
        elif isinstance(src, list):
            for item in src:
                if len(key_covenants) >= 6:
                    break
                normalized = _humanize_im_item(item)
                if normalized:
                    key_covenants.append(normalized)
        elif isinstance(src, str) and src:
            key_covenants.append(src)

    red_flags: list[str] = []
    if isinstance(risk_flags, list):
        red_flags.extend([_humanize_im_item(x) for x in risk_flags if _humanize_im_item(x)])
    elif isinstance(risk_flags, dict):
        red_flags.extend([_humanize_im_item({k: v}) for k, v in risk_flags.items() if _to_text(v)])
    elif isinstance(risk_flags, str) and risk_flags:
        red_flags.append(risk_flags)

    structural_risks: list[str] = []
    for field in ("call_option_details", "put_option_details", "step_up_clauses", "security_structure", "charge_type"):
        val = _to_text(im_row.get(field))
        if val:
            structural_risks.append(f"{field.replace('_', ' ').title()}: {val}")

    triggers: list[str] = []
    for field in ("default_clauses", "cross_default_triggers", "interest_coverage_req", "debt_ebitda_limit", "dscr_requirement"):
        val = _to_text(im_row.get(field))
        if val:
            triggers.append(f"{field.replace('_', ' ').title()}: {val}")

    return {
        "coverage": "AVAILABLE",
        "key_covenants": key_covenants[:6],
        "red_flags": red_flags[:8],
        "red_flag_summary": {
            "count": len(red_flags),
            "has_default_trigger": any("default" in _safe_upper(x) for x in red_flags),
            "has_downgrade_trigger": any("DOWNGRADE" in _safe_upper(x) or "RATING" in _safe_upper(x) for x in red_flags),
        },
        "structural_risks": structural_risks[:6],
        "default_downgrade_triggers": triggers[:8],
    }


@router.get("/analysis/{isin}")
async def get_institutional_analysis(
    isin: str,
    investment_amount_cr: float = Query(10.0, ge=0.1, le=1000.0),
    rate_shock_bps: int = Query(100, ge=25, le=300),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Institutional-grade decision payload for Investment Analysis tab.
    Returns standardized sections for all bonds with explicit completeness and gaps.
    """
    isin_u = isin.strip().upper()
    bond, market = await _resolve_bond_with_market(isin_u)

    im_row = await fetch_one("SELECT * FROM bond_im_data WHERE isin = :isin", {"isin": isin_u})

    rating = _safe_upper(bond.get("credit_rating"))
    rating_rank = RATING_RANK.get(rating)
    rating_outlook = _rating_outlook_from_rank(rating_rank)

    rating_hist = []
    rating_table_exists = await fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name='rating_history'")
    if rating_table_exists:
        rating_cols = await fetch_all("PRAGMA table_info(rating_history)")
        rating_col_names = {str(c.get("name") or "").strip() for c in rating_cols}
        if "isin" in rating_col_names:
            order_candidates = [
                "rating_date",
                "effective_date",
                "updated_at",
                "created_at",
                "date",
                "id",
            ]
            order_col = next((c for c in order_candidates if c in rating_col_names), "rowid")
            rating_hist = await fetch_all(
                f"SELECT * FROM rating_history WHERE isin = :isin ORDER BY {order_col} DESC LIMIT 20",
                {"isin": isin_u},
            )

    last_rating_change = None
    if rating_hist:
        def _hist_rating(row: dict):
            for key in ("rating", "new_rating", "credit_rating", "current_rating"):
                value = row.get(key)
                if _to_text(value):
                    return value
            return None

        def _hist_date(row: dict):
            for key in ("rating_date", "effective_date", "updated_at", "created_at", "date"):
                value = row.get(key)
                if _to_text(value):
                    return value
            return None

        newest = rating_hist[0]
        prev = rating_hist[1] if len(rating_hist) > 1 else None
        newest_rating = _hist_rating(newest)
        prev_rating = _hist_rating(prev or {})
        if prev and _safe_upper(newest_rating) != _safe_upper(prev_rating):
            last_rating_change = {
                "from": prev_rating,
                "to": newest_rating,
                "date": _hist_date(newest),
            }

    clean_price = _to_float(market.get("clean_price"))
    ytm = _to_float(market.get("ytm") if market.get("ytm") is not None else market.get("ask_yield"))
    ytc = _to_float(market.get("bid_yield"))
    spread_bps = _to_float(market.get("spread_over_gsec"))
    liquidity_score = _to_float(market.get("liquidity_score"))
    spread_source = "reported"
    liquidity_source = "reported"
    if spread_bps is None:
        gsec_yield = _to_float(market.get("gsec_yield") if market.get("gsec_yield") is not None else market.get("benchmark_yield"))
        if ytm is not None and gsec_yield is not None:
            spread_bps = (ytm - gsec_yield) * 100.0
            spread_source = "derived"
    if spread_bps is None:
        spread_source = "unavailable"
    if liquidity_score is None:
        bid_ask_spread = _to_float(market.get("bid_ask_spread"))
        quoted_volume = _to_float(market.get("quoted_volume") if market.get("quoted_volume") is not None else market.get("trade_volume"))
        proxy = None
        if bid_ask_spread is not None and quoted_volume is not None:
            proxy = max(5.0, min(95.0, 100.0 - (bid_ask_spread * 12.0) + min(30.0, quoted_volume / 1_000_000.0)))
        elif bid_ask_spread is not None:
            proxy = max(5.0, min(95.0, 100.0 - (bid_ask_spread * 15.0)))
        elif quoted_volume is not None:
            proxy = max(5.0, min(95.0, 30.0 + min(65.0, quoted_volume / 1_500_000.0)))
        if proxy is not None:
            liquidity_score = round(proxy, 2)
            liquidity_source = "proxy"
    if liquidity_score is None:
        liquidity_source = "unavailable"
    duration = _to_float(market.get("modified_duration"))
    face_value = _to_float(bond.get("face_value"), 1000.0)
    coupon_rate = _to_float(bond.get("coupon_rate"))

    maturity = _parse_date(bond.get("maturity_date"))
    today = date.today()
    years_to_maturity = max(0.0, (maturity - today).days / 365.25) if maturity else None

    # market freshness + trend/volatility
    history_rows = await fetch_all(
        """SELECT clean_price, ytm, price_date
           FROM unified_market_data
           WHERE isin = :isin
           ORDER BY price_date DESC, id DESC
           LIMIT 30""",
        {"isin": isin_u},
    )
    price_date = _parse_date(market.get("price_date") or market.get("last_updated"))
    freshness_days = (today - price_date).days if price_date else None

    prices = [
        _to_float(r.get("clean_price"))
        for r in reversed(history_rows)
        if _to_float(r.get("clean_price")) is not None
    ]
    trend_30d_pct = None
    volatility_indicator = "UNKNOWN"
    if len(prices) >= 2 and prices[0] and prices[-1]:
        trend_30d_pct = ((prices[-1] - prices[0]) / prices[0]) * 100.0
        rets: list[float] = []
        for i in range(1, len(prices)):
            if prices[i - 1] and prices[i]:
                rets.append((prices[i] - prices[i - 1]) / prices[i - 1])
        vol = _stddev(rets) * 100.0 if rets else 0.0
        if vol < 0.5:
            volatility_indicator = "LOW"
        elif vol < 1.5:
            volatility_indicator = "MEDIUM"
        else:
            volatility_indicator = "HIGH"

    # portfolio exposures and impact
    portfolio_rows = await fetch_all(
        """
        WITH latest_market AS (
            SELECT umd.isin, umd.ytm, umd.modified_duration,
                   ROW_NUMBER() OVER (PARTITION BY umd.isin ORDER BY umd.price_date DESC, umd.id DESC) AS rn
            FROM unified_market_data umd
        )
        SELECT p.isin,
             p.market_value AS market_value,
             lm.ytm AS ytm,
             lm.modified_duration AS modified_duration,
               COALESCE(p.issuer_name, ubm.issuer_name) AS issuer_name,
               COALESCE(p.category, ubm.instrument_category, ubm.issuer_type) AS category
        FROM portfolio_current_positions p
        LEFT JOIN latest_market lm ON lm.isin = p.isin AND lm.rn = 1
        LEFT JOIN universal_bond_master ubm ON ubm.isin = p.isin
        """
    )

    normalized_positions: list[dict] = []
    invalid_market_value_rows = 0
    for row in portfolio_rows:
        mv = _to_float(row.get("market_value"))
        if mv is None or mv < 0:
            invalid_market_value_rows += 1
            continue
        normalized_positions.append(
            {
                "market_value": mv,
                "ytm": _to_float(row.get("ytm")),
                "modified_duration": _to_float(row.get("modified_duration")),
                "issuer_name": _to_text(row.get("issuer_name")) or "UNKNOWN",
                "category": row.get("category"),
            }
        )

    total_mv_raw = sum(r["market_value"] for r in normalized_positions)
    aum_is_reasonable = total_mv_raw <= MAX_REASONABLE_PORTFOLIO_MV_INR
    total_mv = total_mv_raw if aum_is_reasonable else None

    before_w_ytm_num = sum((r["ytm"] * r["market_value"]) for r in normalized_positions if r["ytm"] is not None)
    before_w_dur_num = sum((r["modified_duration"] * r["market_value"]) for r in normalized_positions if r["modified_duration"] is not None)
    ytm_covered_mv = sum(r["market_value"] for r in normalized_positions if r["ytm"] is not None)
    dur_covered_mv = sum(r["market_value"] for r in normalized_positions if r["modified_duration"] is not None)

    before_weighted_ytm = (before_w_ytm_num / ytm_covered_mv) if ytm_covered_mv > 0 else None
    before_weighted_duration = (before_w_dur_num / dur_covered_mv) if dur_covered_mv > 0 else None

    issuer_mv = {}
    sector_mv = {}
    for row in normalized_positions:
        mv = row["market_value"]
        issuer = row["issuer_name"]
        sector = _category_bucket(row.get("category"))
        issuer_mv[issuer] = issuer_mv.get(issuer, 0.0) + mv
        sector_mv[sector] = sector_mv.get(sector, 0.0) + mv

    trade_mv = investment_amount_cr * 10_000_000.0
    after_total_mv = (total_mv + trade_mv) if total_mv is not None else None

    after_weighted_ytm = None
    if ytm is not None and ytm_covered_mv > 0:
        after_weighted_ytm = (before_w_ytm_num + (ytm * trade_mv)) / (ytm_covered_mv + trade_mv)

    after_weighted_duration = None
    if duration is not None and dur_covered_mv > 0:
        after_weighted_duration = (before_w_dur_num + (duration * trade_mv)) / (dur_covered_mv + trade_mv)

    issuer_name = _to_text(bond.get("issuer_name") or bond.get("bond_name") or isin_u)
    sector_name = _category_bucket(bond.get("instrument_category"), bond.get("issuer_type"), bond.get("sector"))

    current_weight_pct = ((issuer_mv.get(issuer_name, 0.0) / total_mv) * 100.0) if total_mv and total_mv > 0 else None
    after_weight_pct = (((issuer_mv.get(issuer_name, 0.0) + trade_mv) / after_total_mv) * 100.0) if after_total_mv and after_total_mv > 0 else None
    current_sector_weight_pct = ((sector_mv.get(sector_name, 0.0) / total_mv) * 100.0) if total_mv and total_mv > 0 else None
    after_sector_weight_pct = (((sector_mv.get(sector_name, 0.0) + trade_mv) / after_total_mv) * 100.0) if after_total_mv and after_total_mv > 0 else None

    position_row = await fetch_one("SELECT * FROM portfolio_current_positions WHERE isin = :isin LIMIT 1", {"isin": isin_u})
    entry_ytm = None
    if position_row:
        for key in ("entry_ytm", "purchase_ytm", "acquisition_ytm", "book_ytm", "ytm_at_buy", "ytm"):
            entry_ytm = _to_float(position_row.get(key))
            if entry_ytm is not None:
                break

    # compliance (rules engine)
    compliance_payload = await run_compliance_check(bond, market)

    # relative value peers
    peers = await fetch_all(
        """
        WITH latest_market AS (
            SELECT umd.isin, umd.ytm, umd.modified_duration, umd.spread_over_gsec, umd.clean_price,
                   ROW_NUMBER() OVER (PARTITION BY umd.isin ORDER BY umd.price_date DESC, umd.id DESC) AS rn
            FROM unified_market_data umd
        )
        SELECT ubm.isin, ubm.issuer_name, ubm.credit_rating, ubm.maturity_date,
               lm.ytm, lm.modified_duration, lm.spread_over_gsec, lm.clean_price
        FROM universal_bond_master ubm
        LEFT JOIN latest_market lm ON lm.isin = ubm.isin AND lm.rn = 1
        WHERE ubm.isin != :isin
          AND COALESCE(ubm.credit_rating, '') = COALESCE(:rating, ubm.credit_rating)
        LIMIT 300
        """,
        {"isin": isin_u, "rating": bond.get("credit_rating")},
    )

    if maturity:
        def _maturity_diff_days(row):
            pm = _parse_date(row.get("maturity_date"))
            if not pm:
                return 999999
            return abs((pm - maturity).days)

        peers = sorted(peers, key=_maturity_diff_days)
        peers = [p for p in peers if _maturity_diff_days(p) <= 730][:8]
    else:
        peers = peers[:8]

    peer_ytms = [_to_float(p.get("ytm")) for p in peers if _to_float(p.get("ytm")) is not None]
    peer_avg_ytm = _avg(peer_ytms)
    yield_adv_bps = ((ytm - peer_avg_ytm) * 100.0) if (ytm is not None and peer_avg_ytm is not None) else None

    # risk decomposition
    credit_risk = 80.0 if rating_rank is None else min(100.0, max(5.0, (rating_rank - 1) * 6.0))
    ir_risk = 60.0 if duration is None else min(100.0, max(5.0, duration * 12.5))
    liq_risk = 70.0 if liquidity_score is None else max(0.0, min(100.0, 100.0 - liquidity_score))
    conc_inputs = [x for x in (after_weight_pct, after_sector_weight_pct) if x is not None]
    conc_risk = min(100.0, max(conc_inputs) * 2.0) if conc_inputs else 50.0
    risk_score = round((0.35 * credit_risk) + (0.30 * ir_risk) + (0.20 * liq_risk) + (0.15 * conc_risk), 2)
    risk_band = _risk_band(risk_score)

    # scenario analysis (single-bond shock approximation)
    scenario_assumptions: list[str] = []
    if clean_price is None or clean_price <= 0:
        scenario_assumptions.append("Current clean price unavailable")
    if duration is None:
        scenario_assumptions.append("Modified duration unavailable")
    if trade_mv <= 0:
        scenario_assumptions.append("Investment amount is non-positive")

    scenario_available = not scenario_assumptions
    rate_up_pnl = None
    rate_down_pnl = None
    if scenario_available:
        dv01_per_unit = (clean_price * duration) / 10000.0
        quantity_est = trade_mv / clean_price
        rate_up_pnl = -dv01_per_unit * rate_shock_bps * quantity_est
        rate_down_pnl = abs(rate_up_pnl)

    # IM intelligence
    im_insight = _extract_im_insights(im_row)

    # completeness and gaps
    critical_missing: list[str] = []
    important_missing: list[str] = []
    advanced_missing: list[str] = []

    if not rating:
        critical_missing.append("Latest credit rating")
    if coupon_rate is None:
        critical_missing.append("Coupon")
    if maturity is None:
        critical_missing.append("Maturity")
    if clean_price is None:
        critical_missing.append("Current market price")
    if ytm is None:
        critical_missing.append("Current YTM")
    if spread_bps is None:
        important_missing.append("Yield vs benchmark spread (G-Sec)")
    if liquidity_score is None:
        important_missing.append("Liquidity score")
    if entry_ytm is None:
        important_missing.append("Entry YTM")
    if duration is None:
        important_missing.append("Duration")
    if total_mv is None:
        critical_missing.append("Portfolio AUM (sanity-checked)")
    if before_weighted_ytm is None:
        important_missing.append("Portfolio weighted YTM")
    if before_weighted_duration is None:
        important_missing.append("Portfolio weighted duration")
    if not scenario_available:
        advanced_missing.append("Scenario analysis base inputs")
    if not im_row:
        critical_missing.append("IM intelligence record")
    if not rating_hist:
        advanced_missing.append("Rating transition history")
    if not peers:
        advanced_missing.append("Peer comparison set")

    required_for_completeness = 10
    missing_total = len(critical_missing) + len(important_missing)
    completeness_pct = round(max(0.0, (required_for_completeness - missing_total) / required_for_completeness * 100.0), 2)

    # decision signal
    fail_count = int(compliance_payload.get("failures") or 0)
    warn_count = int(compliance_payload.get("warnings") or 0)
    if len(critical_missing) >= 2 or risk_score >= 75 or fail_count >= 2:
        decision = {"signal": "AVOID", "label": "Avoid", "reason": "Critical data/risk/compliance concerns detected"}
    elif fail_count >= 1 or warn_count >= 3 or risk_score >= 45:
        decision = {"signal": "REVIEW", "label": "Review", "reason": "Requires manual committee review before investment"}
    else:
        decision = {"signal": "INVEST", "label": "Invest", "reason": "Data quality, compliance, and risk profile are acceptable"}

    confidence_raw = (completeness_pct * 0.50) + ((100.0 - risk_score) * 0.30) + (max(0.0, 100.0 - (fail_count * 20.0) - (warn_count * 5.0)) * 0.20)
    confidence_score = max(0.0, min(100.0, round(confidence_raw, 1)))
    if confidence_score >= 75:
        confidence_level = "HIGH"
    elif confidence_score >= 50:
        confidence_level = "MEDIUM"
    else:
        confidence_level = "LOW"
    decision["confidence_level"] = confidence_level
    decision["confidence_score"] = confidence_score

    risk_adjusted_return = None
    if ytm is not None:
        risk_adjusted_return = round(ytm * (100.0 - risk_score) / 100.0, 4)

    field_status_map = {
        "ytm": {
            "status": "available" if ytm is not None else "missing",
            "source": "reported" if ytm is not None else "unavailable",
        },
        "coupon_rate": {
            "status": "available" if coupon_rate is not None else "missing",
            "source": "bond_master" if coupon_rate is not None else "unavailable",
        },
        "maturity_date": {
            "status": "available" if maturity is not None else "missing",
            "source": "bond_master" if maturity is not None else "unavailable",
        },
        "credit_rating": {
            "status": "available" if rating else "missing",
            "source": "bond_master" if rating else "unavailable",
        },
        "market_price": {
            "status": "available" if clean_price is not None else "missing",
            "source": "reported" if clean_price is not None else "unavailable",
        },
        "spread_over_gsec_bps": {
            "status": "available" if spread_bps is not None else "missing",
            "source": spread_source,
        },
        "liquidity_score": {
            "status": "available" if liquidity_score is not None else "missing",
            "source": liquidity_source,
        },
        "entry_ytm": {
            "status": "available" if entry_ytm is not None else "missing",
            "source": "portfolio_position" if entry_ytm is not None else "unavailable",
        },
        "duration": {
            "status": "available" if duration is not None else "missing",
            "source": "reported" if duration is not None else "unavailable",
        },
        "portfolio_market_value": {
            "status": "available" if total_mv is not None else "missing",
            "source": "portfolio_positions" if total_mv is not None else "sanity_check_failed",
        },
        "scenario_analysis": {
            "status": "available" if scenario_available else "missing",
            "source": "computed" if scenario_available else "insufficient_inputs",
        },
    }

    payload = {
        "isin": isin_u,
        "schema_version": "2.1.0",
        "decision_signal": decision,
        "bond_snapshot": {
            "issuer": issuer_name,
            "category": sector_name,
            "coupon_rate": coupon_rate,
            "credit_rating": {
                "latest": rating or None,
                "outlook": rating_outlook,
                "last_change": last_rating_change,
            },
            "yield": {"ytm": ytm, "ytc": ytc},
            "maturity_date": maturity.isoformat() if maturity else None,
            "years_to_maturity": round(years_to_maturity, 4) if years_to_maturity is not None else None,
            "duration": duration,
            "allocation_impact": {
                "current_weight_pct": _round_or_none(current_weight_pct, 4),
                "post_trade_weight_pct": _round_or_none(after_weight_pct, 4),
                "delta_weight_pct": _round_or_none((after_weight_pct - current_weight_pct), 4) if (after_weight_pct is not None and current_weight_pct is not None) else None,
            },
        },
        "im_intelligence": im_insight,
        "market_intelligence": {
            "current_price": clean_price,
            "face_value": face_value,
            "entry_ytm": entry_ytm,
            "spread_over_gsec_bps": spread_bps,
            "spread_source": spread_source,
            "liquidity_score": liquidity_score,
            "liquidity_source": liquidity_source,
            "price_trend_30d_pct": trend_30d_pct,
            "volatility_indicator": volatility_indicator,
            "data_freshness_days": freshness_days,
            "data_source": _to_text(market.get("data_source") or market.get("source_name") or bond.get("_source")),
        },
        "risk_engine": {
            "risk_score": risk_score,
            "risk_band": risk_band,
            "components": {
                "credit_risk": round(credit_risk, 2),
                "interest_rate_risk": round(ir_risk, 2),
                "liquidity_risk": round(liq_risk, 2),
                "concentration_risk": round(conc_risk, 2),
            },
            "explanation": "Composite of rating quality, duration sensitivity, liquidity depth, and concentration after trade.",
            "scenario_analysis": {
                "rate_shock_bps": rate_shock_bps,
                "is_available": scenario_available,
                "assumptions": scenario_assumptions,
                "rate_up_pnl": _round_or_none(rate_up_pnl, 2),
                "rate_down_pnl": _round_or_none(rate_down_pnl, 2),
            },
        },
        "compliance": {
            "overall": compliance_payload.get("overall"),
            "score": compliance_payload.get("score"),
            "rules_checked": compliance_payload.get("rules_checked"),
            "passed": compliance_payload.get("passed"),
            "warnings": compliance_payload.get("warnings"),
            "failures": compliance_payload.get("failures"),
            "violations": compliance_payload.get("checklist") or [],
        },
        "portfolio_impact": {
            "aum_unit": "INR",
            "display_unit": "Cr",
            "validation": {
                "is_reasonable": aum_is_reasonable,
                "max_reasonable_inr": MAX_REASONABLE_PORTFOLIO_MV_INR,
                "invalid_position_count": invalid_market_value_rows,
            },
            "before": {
                "portfolio_market_value": _round_or_none(total_mv, 2),
                "weighted_ytm": _round_or_none(before_weighted_ytm, 4),
                "weighted_duration": _round_or_none(before_weighted_duration, 4),
                "issuer_exposure_pct": _round_or_none(current_weight_pct, 4),
                "sector_exposure_pct": _round_or_none(current_sector_weight_pct, 4),
            },
            "after": {
                "portfolio_market_value": _round_or_none(after_total_mv, 2),
                "weighted_ytm": _round_or_none(after_weighted_ytm, 4),
                "weighted_duration": _round_or_none(after_weighted_duration, 4),
                "issuer_exposure_pct": _round_or_none(after_weight_pct, 4),
                "sector_exposure_pct": _round_or_none(after_sector_weight_pct, 4),
            },
            "delta": {
                "weighted_ytm_bps": _round_or_none((after_weighted_ytm - before_weighted_ytm) * 100.0, 2) if (after_weighted_ytm is not None and before_weighted_ytm is not None) else None,
                "weighted_duration": _round_or_none(after_weighted_duration - before_weighted_duration, 4) if (after_weighted_duration is not None and before_weighted_duration is not None) else None,
                "issuer_exposure_pct": _round_or_none(after_weight_pct - current_weight_pct, 4) if (after_weight_pct is not None and current_weight_pct is not None) else None,
                "sector_exposure_pct": _round_or_none(after_sector_weight_pct - current_sector_weight_pct, 4) if (after_sector_weight_pct is not None and current_sector_weight_pct is not None) else None,
            },
        },
        "relative_value": {
            "peer_count": len(peers),
            "peer_avg_ytm": round(peer_avg_ytm, 4) if peer_avg_ytm is not None else None,
            "yield_advantage_bps": round(yield_adv_bps, 2) if yield_adv_bps is not None else None,
            "risk_adjusted_return": risk_adjusted_return,
            "peers": peers,
        },
        "data_quality": {
            "completeness_pct": completeness_pct,
            "critical_missing_count": len(critical_missing),
            "important_missing_count": len(important_missing),
            "advanced_missing_count": len(advanced_missing),
            "market_data_fresh": freshness_days is not None and freshness_days <= 3,
            "field_status": field_status_map,
        },
        "gaps": {
            "critical": critical_missing,
            "important": important_missing,
            "advanced": advanced_missing,
        },
        "standardization": {
            "sections": [
                "bond_snapshot",
                "im_intelligence",
                "market_intelligence",
                "risk_engine",
                "compliance",
                "portfolio_impact",
                "relative_value",
                "data_quality",
            ],
            "sections_count": 8,
        },
    }

    _enforce_missing_field_masking(payload)
    validation_report = _build_validation_engine_report(payload)
    payload["validation"] = validation_report
    payload["data_quality"]["validation_engine"] = validation_report
    _apply_intelligent_control(payload)

    history_rows = await _fetch_recent_audit_rows(isin=isin_u, limit=20)
    observations = list(reversed(history_rows))
    observations.append(
        {
            "health_score": _to_float(((payload.get("control_layer") or {}).get("health_score")), 0.0) or 0.0,
            "confidence_score": _to_float(((payload.get("decision_signal") or {}).get("confidence_score")), 0.0) or 0.0,
            "completeness_pct": _to_float(((payload.get("data_quality") or {}).get("completeness_pct")), 0.0) or 0.0,
            "is_blocked": _safe_upper(((payload.get("analysis_state") or {}).get("status")) or "") == "BLOCKED",
            "control_severity": _to_text(((payload.get("control_layer") or {}).get("severity")) or "NONE"),
        }
    )
    predictive_signal = _compute_predictive_signal(observations)
    payload["control_layer"]["predictive_degradation"] = predictive_signal
    payload["explainability"] = _build_decision_explainability(payload)

    external_alert_triggered = await _dispatch_external_alert_if_needed(payload)
    try:
        await _record_analysis_audit(payload, predictive_signal, external_alert_triggered)
    except Exception as exc:
        log.error("analysis_audit_record_failed", isin=isin_u, error=str(exc))

    return payload


@router.get("/analysis/{isin}/audit")
async def get_institutional_analysis_audit(
    isin: str,
    limit: int = Query(10, ge=1, le=100),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Return the last N analysis snapshots for an ISIN with change deltas."""
    isin_u = isin.strip().upper()
    rows = await _fetch_recent_audit_rows(isin=isin_u, limit=limit)

    entries: list[dict] = []
    for idx, row in enumerate(rows):
        previous = rows[idx + 1] if idx + 1 < len(rows) else None
        change_vs_previous = None
        if previous:
            change_vs_previous = {
                "decision_changed": row.get("decision_signal") != previous.get("decision_signal"),
                "confidence_delta": _round_or_none(
                    (_to_float(row.get("confidence_score"), 0.0) or 0.0)
                    - (_to_float(previous.get("confidence_score"), 0.0) or 0.0),
                    2,
                ),
                "risk_score_delta": _round_or_none(
                    (_to_float(row.get("risk_score"), 0.0) or 0.0)
                    - (_to_float(previous.get("risk_score"), 0.0) or 0.0),
                    2,
                ),
                "health_score_delta": _round_or_none(
                    (_to_float(row.get("health_score"), 0.0) or 0.0)
                    - (_to_float(previous.get("health_score"), 0.0) or 0.0),
                    2,
                ),
                "completeness_delta": _round_or_none(
                    (_to_float(row.get("completeness_pct"), 0.0) or 0.0)
                    - (_to_float(previous.get("completeness_pct"), 0.0) or 0.0),
                    2,
                ),
                "severity_changed": row.get("control_severity") != previous.get("control_severity"),
                "previous_decision": previous.get("decision_signal"),
                "previous_severity": previous.get("control_severity"),
            }

        entries.append(
            {
                "analysis_id": row.get("id"),
                "created_at": row.get("created_at"),
                "decision_signal": row.get("decision_signal"),
                "decision_label": row.get("decision_label"),
                "decision_reason": row.get("decision_reason"),
                "confidence_score": row.get("confidence_score"),
                "confidence_level": row.get("confidence_level"),
                "risk_score": row.get("risk_score"),
                "risk_band": row.get("risk_band"),
                "completeness_pct": row.get("completeness_pct"),
                "control_severity": row.get("control_severity"),
                "health_score": row.get("health_score"),
                "is_blocked": row.get("is_blocked"),
                "predictive_status": row.get("predictive_status"),
                "predictive_score": row.get("predictive_score"),
                "anomaly_counts": row.get("anomaly_counts") or {},
                "missing_summary": row.get("missing_summary") or {},
                "change_vs_previous": change_vs_previous,
            }
        )

    transitions = []
    chronological = list(reversed(rows))
    for idx in range(1, len(chronological)):
        prev = chronological[idx - 1]
        curr = chronological[idx]
        if prev.get("decision_signal") == curr.get("decision_signal"):
            continue
        transitions.append(
            {
                "from": prev.get("decision_signal"),
                "to": curr.get("decision_signal"),
                "changed_at": curr.get("created_at"),
                "confidence_after": curr.get("confidence_score"),
                "severity_after": curr.get("control_severity"),
            }
        )

    return {
        "isin": isin_u,
        "count": len(entries),
        "entries": entries,
        "decision_transitions": transitions,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/ops/observability")
async def get_intelligence_observability(
    live_limit: int = Query(25, ge=5, le=200),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Global intelligence-ops observability metrics with live analysis feed."""
    rows = await _fetch_recent_audit_rows(limit=800)
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    rows_24h = [
        row
        for row in rows
        if (_parse_datetime(row.get("created_at")) or now) >= cutoff_24h
    ]

    total_24h = len(rows_24h)
    blocked_24h = sum(1 for row in rows_24h if row.get("is_blocked"))
    high_24h = sum(1 for row in rows_24h if _safe_upper(row.get("control_severity")) == "HIGH")
    watch_24h = sum(
        1
        for row in rows_24h
        if _safe_upper(row.get("predictive_status")) in {"WATCH", "CRITICAL"}
    )

    health_values = [
        _to_float(row.get("health_score"))
        for row in rows_24h
        if _to_float(row.get("health_score")) is not None
    ]
    confidence_values = [
        _to_float(row.get("confidence_score"))
        for row in rows_24h
        if _to_float(row.get("confidence_score")) is not None
    ]
    avg_health = _round_or_none(_avg(health_values), 2)
    avg_confidence = _round_or_none(_avg(confidence_values), 2)

    risk_by_isin: dict[str, dict] = {}
    for row in rows_24h:
        isin = _to_text(row.get("isin"))
        if not isin:
            continue
        bucket = risk_by_isin.setdefault(isin, {"isin": isin, "risk_points": 0, "high_events": 0, "blocked_events": 0})
        if row.get("is_blocked"):
            bucket["risk_points"] += 4
            bucket["blocked_events"] += 1
        if _safe_upper(row.get("control_severity")) == "HIGH":
            bucket["risk_points"] += 3
            bucket["high_events"] += 1
        if _safe_upper(row.get("predictive_status")) == "CRITICAL":
            bucket["risk_points"] += 2
        elif _safe_upper(row.get("predictive_status")) == "WATCH":
            bucket["risk_points"] += 1

    top_at_risk_isins = sorted(
        risk_by_isin.values(),
        key=lambda item: (item.get("risk_points", 0), item.get("blocked_events", 0), item.get("high_events", 0)),
        reverse=True,
    )[:8]

    live_feed = []
    for row in rows[:live_limit]:
        severity = _safe_upper(row.get("control_severity") or "NONE")
        predictive_status = _safe_upper(row.get("predictive_status") or "")
        if row.get("is_blocked"):
            event_type = "BLOCKED"
            message = "Analysis blocked by intelligent control"
        elif severity == "HIGH":
            event_type = "HIGH_SEVERITY"
            message = "High-severity anomaly detected"
        elif predictive_status in {"WATCH", "CRITICAL"}:
            event_type = "PREDICTIVE_WARNING"
            message = "Predictive degradation warning raised"
        else:
            event_type = "ANALYSIS"
            message = "Analysis completed"

        live_feed.append(
            {
                "analysis_id": row.get("id"),
                "event_type": event_type,
                "message": message,
                "isin": row.get("isin"),
                "decision_signal": row.get("decision_signal"),
                "control_severity": row.get("control_severity"),
                "predictive_status": row.get("predictive_status"),
                "confidence_score": row.get("confidence_score"),
                "health_score": row.get("health_score"),
                "created_at": row.get("created_at"),
            }
        )

    return {
        "generated_at": now.isoformat(),
        "kpis": {
            "analyses_last_24h": total_24h,
            "blocked_last_24h": blocked_24h,
            "high_severity_last_24h": high_24h,
            "predictive_warnings_last_24h": watch_24h,
            "avg_health_score_last_24h": avg_health,
            "avg_confidence_last_24h": avg_confidence,
            "blocked_rate_pct": _round_or_none((blocked_24h / total_24h * 100.0) if total_24h else 0.0, 2),
        },
        "top_at_risk_isins": top_at_risk_isins,
        "live_feed": live_feed,
    }


@router.get("/ops/observability/{isin}")
async def get_intelligence_observability_drilldown(
    isin: str,
    limit: int = Query(20, ge=5, le=100),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Per-ISIN observability drill-down with trend and predictive diagnostics."""
    isin_u = isin.strip().upper()
    rows = await _fetch_recent_audit_rows(isin=isin_u, limit=limit)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No analysis audit history found for {isin_u}")

    chronological = list(reversed(rows))
    predictive = _compute_predictive_signal(chronological)

    timeline = [
        {
            "analysis_id": row.get("id"),
            "created_at": row.get("created_at"),
            "decision_signal": row.get("decision_signal"),
            "confidence_score": row.get("confidence_score"),
            "risk_score": row.get("risk_score"),
            "completeness_pct": row.get("completeness_pct"),
            "health_score": row.get("health_score"),
            "control_severity": row.get("control_severity"),
            "is_blocked": row.get("is_blocked"),
            "predictive_status": row.get("predictive_status"),
        }
        for row in chronological
    ]

    latest = rows[0]
    return {
        "isin": isin_u,
        "latest": {
            "analysis_id": latest.get("id"),
            "created_at": latest.get("created_at"),
            "decision_signal": latest.get("decision_signal"),
            "decision_reason": latest.get("decision_reason"),
            "control_severity": latest.get("control_severity"),
            "health_score": latest.get("health_score"),
            "confidence_score": latest.get("confidence_score"),
            "risk_score": latest.get("risk_score"),
            "completeness_pct": latest.get("completeness_pct"),
            "predictive_status": latest.get("predictive_status"),
            "predictive_score": latest.get("predictive_score"),
            "anomaly_counts": latest.get("anomaly_counts") or {},
            "missing_summary": latest.get("missing_summary") or {},
        },
        "predictive_degradation": predictive,
        "timeline": timeline,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/ops/predictive")
async def get_intelligence_predictive_watchlist(
    top: int = Query(10, ge=1, le=50),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Return ISINs ranked by predictive degradation risk."""
    rows = await _fetch_recent_audit_rows(limit=1500)
    grouped: dict[str, list[dict]] = {}
    for row in reversed(rows):
        isin = _to_text(row.get("isin"))
        if not isin:
            continue
        grouped.setdefault(isin, []).append(row)

    watchlist = []
    for isin, history in grouped.items():
        signal = _compute_predictive_signal(history)
        latest = history[-1]
        watchlist.append(
            {
                "isin": isin,
                "predictive_status": signal.get("status"),
                "early_warning_score": signal.get("early_warning_score"),
                "summary": signal.get("summary"),
                "latest_decision": latest.get("decision_signal"),
                "latest_control_severity": latest.get("control_severity"),
                "latest_health_score": latest.get("health_score"),
                "latest_confidence_score": latest.get("confidence_score"),
                "observation_count": len(history),
                "latest_at": latest.get("created_at"),
            }
        )

    ranked = sorted(
        watchlist,
        key=lambda item: (
            _to_float(item.get("early_warning_score"), 0.0) or 0.0,
            1 if _safe_upper(item.get("predictive_status")) == "CRITICAL" else 0,
        ),
        reverse=True,
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": min(top, len(ranked)),
        "items": ranked[:top],
    }


@router.get("/ops/weekly-improvement-report")
async def get_intelligence_weekly_improvement_report(
    days: int = Query(7, ge=3, le=30),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Generate weekly intelligence feedback-loop report with recommended actions."""
    rows = await _fetch_recent_audit_rows(limit=5000)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    in_window = [
        row
        for row in rows
        if (_parse_datetime(row.get("created_at")) or now) >= cutoff
    ]

    total = len(in_window)
    blocked = sum(1 for row in in_window if row.get("is_blocked"))
    high = sum(1 for row in in_window if _safe_upper(row.get("control_severity")) == "HIGH")
    watch = sum(
        1
        for row in in_window
        if _safe_upper(row.get("predictive_status")) in {"WATCH", "CRITICAL"}
    )

    health_values = [
        _to_float(row.get("health_score"))
        for row in in_window
        if _to_float(row.get("health_score")) is not None
    ]
    confidence_values = [
        _to_float(row.get("confidence_score"))
        for row in in_window
        if _to_float(row.get("confidence_score")) is not None
    ]
    completeness_values = [
        _to_float(row.get("completeness_pct"))
        for row in in_window
        if _to_float(row.get("completeness_pct")) is not None
    ]

    missing_counts: dict[str, int] = {}
    risk_by_isin: dict[str, int] = {}
    for row in in_window:
        isin = _to_text(row.get("isin"))
        if isin:
            points = 0
            if row.get("is_blocked"):
                points += 4
            if _safe_upper(row.get("control_severity")) == "HIGH":
                points += 3
            if _safe_upper(row.get("predictive_status")) in {"WATCH", "CRITICAL"}:
                points += 1
            risk_by_isin[isin] = risk_by_isin.get(isin, 0) + points

        missing_summary = row.get("missing_summary") or {}
        for bucket in ("critical", "important", "advanced"):
            for item in missing_summary.get(bucket) or []:
                name = _to_text(item)
                if not name:
                    continue
                missing_counts[name] = missing_counts.get(name, 0) + 1

    await _ensure_intelligence_ops_tables()
    feedback_rows = await fetch_all(
        """
        SELECT id, isin, category, sentiment, feedback_text, user_id, created_at
        FROM intelligence_feedback
        WHERE created_at >= :cutoff
        ORDER BY created_at DESC, id DESC
        LIMIT 300
        """,
        {"cutoff": cutoff.isoformat()},
    )

    recommendations: list[str] = []
    blocked_rate = (blocked / total * 100.0) if total else 0.0
    avg_completeness = _to_float(_avg(completeness_values), 0.0) or 0.0
    avg_health = _to_float(_avg(health_values), 0.0) or 0.0
    if blocked_rate > 5.0:
        recommendations.append("Reduce blocked-analysis rate by prioritizing data source remediations for top affected ISINs")
    if avg_completeness < 75.0:
        recommendations.append("Raise data completeness via mandatory field backfill for coupon, maturity, and market spread")
    if avg_health < 80.0:
        recommendations.append("Tighten anomaly thresholds and add monitor depth for low-health cohorts")
    if watch > 0:
        recommendations.append("Run proactive committee review for ISINs in predictive WATCH/CRITICAL state")
    if not recommendations:
        recommendations.append("Maintain current controls and continue weekly trend surveillance")

    report = {
        "generated_at": now.isoformat(),
        "window_days": days,
        "summary": {
            "total_analyses": total,
            "blocked_analyses": blocked,
            "high_severity_analyses": high,
            "predictive_warnings": watch,
            "blocked_rate_pct": _round_or_none(blocked_rate, 2),
            "avg_health_score": _round_or_none(_avg(health_values), 2),
            "avg_confidence_score": _round_or_none(_avg(confidence_values), 2),
            "avg_completeness_pct": _round_or_none(_avg(completeness_values), 2),
        },
        "top_missing_fields": [
            {"field": field, "count": count}
            for field, count in sorted(missing_counts.items(), key=lambda item: item[1], reverse=True)[:10]
        ],
        "top_at_risk_isins": [
            {"isin": isin, "risk_points": score}
            for isin, score in sorted(risk_by_isin.items(), key=lambda item: item[1], reverse=True)[:10]
        ],
        "feedback_loop": {
            "feedback_count": len(feedback_rows),
            "recent_feedback": feedback_rows[:20],
        },
        "recommended_actions": recommendations,
    }

    try:
        INTELLIGENCE_WEEKLY_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        INTELLIGENCE_WEEKLY_REPORT_PATH.write_text(
            json.dumps(report, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )
    except Exception as exc:
        log.warning("weekly_report_persist_failed", error=str(exc))

    return report


@router.post("/ops/feedback")
async def create_intelligence_feedback(
    payload: IntelligenceFeedbackCreate,
    user=Depends(require_role(Roles.VIEWER)),
):
    """Capture operator feedback for weekly intelligence improvement loop."""
    await _ensure_intelligence_ops_tables()
    user_id = _to_text(user.get("id") or user.get("sub") or user.get("username") or "unknown")
    await execute_query(
        """
        INSERT INTO intelligence_feedback (isin, category, sentiment, feedback_text, user_id, created_at)
        VALUES (:isin, :category, :sentiment, :feedback_text, :user_id, :created_at)
        """,
        {
            "isin": (_to_text(payload.isin).upper() or None),
            "category": _to_text(payload.category) or "SYSTEM_QUALITY",
            "sentiment": _to_text(payload.sentiment) or "NEUTRAL",
            "feedback_text": _to_text(payload.feedback),
            "user_id": user_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return {"status": "ok", "message": "Feedback recorded for weekly improvement review"}


@router.get("/ops/feedback")
async def list_intelligence_feedback(
    days: int = Query(30, ge=1, le=90),
    limit: int = Query(100, ge=1, le=500),
    user=Depends(require_role(Roles.VIEWER)),
):
    """List recent operator feedback items captured for the intelligence feedback loop."""
    await _ensure_intelligence_ops_tables()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = await fetch_all(
        """
        SELECT id, isin, category, sentiment, feedback_text, user_id, created_at
        FROM intelligence_feedback
        WHERE created_at >= :cutoff
        ORDER BY created_at DESC, id DESC
        LIMIT :limit
        """,
        {"cutoff": cutoff, "limit": limit},
    )
    return {
        "count": len(rows),
        "items": rows,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/bonds")
async def list_bonds(
    issuer_type: Optional[str] = None,
    credit_rating: Optional[str] = None,
    sector: Optional[str] = None,
    pf_eligible: Optional[bool] = None,
    invested_only: Optional[bool] = None,
    trust_id: str = Query("default"),
    keyword: Optional[str] = Query(None),
    limit: int = Query(100, le=5000),
    offset: int = 0,
    user=Depends(require_role(Roles.VIEWER)),
):
    """Search the universal bond master registry."""
    log.info("list_bonds", issuer_type=issuer_type, credit_rating=credit_rating)
    bonds = await registry.search(
        issuer_type=issuer_type,
        credit_rating=credit_rating,
        sector=sector,
        pf_eligible=pf_eligible,
        invested_only=False,
        keyword=keyword,
        limit=limit,
        offset=offset,
    )
    # Build an identical WHERE for a COUNT query (no LIMIT/OFFSET)
    conds: list[str] = ["is_active = 1"]
    cp: dict = {}
    if issuer_type:
        conds.append("issuer_type = :issuer_type"); cp["issuer_type"] = issuer_type.upper()
    if credit_rating:
        conds.append("credit_rating = :credit_rating"); cp["credit_rating"] = credit_rating.upper()
    if sector:
        conds.append("sector = :sector"); cp["sector"] = sector.upper()
    if pf_eligible is not None:
        conds.append("is_pf_eligible = :pf_eligible"); cp["pf_eligible"] = 1 if pf_eligible else 0
    if invested_only is True:
        conds.append(
            """
            EXISTS (
                WITH active_positions AS (
                    SELECT p.isin, p.quantity
                    FROM portfolio_positions p
                    WHERE p.trust_id = :pid
                    UNION ALL
                    SELECT h.isin, h.quantity
                    FROM portfolio_holdings h
                    WHERE COALESCE(h.portfolio_id, 'default') = :pid
                      AND NOT EXISTS (SELECT 1 FROM portfolio_positions p WHERE p.trust_id = :pid)
                )
                SELECT 1
                FROM active_positions ap
                WHERE ap.isin = universal_bond_master.isin
                  AND COALESCE(ap.quantity, 0) > 0
            )
            """
        )
        cp["pid"] = trust_id
    if keyword:
        conds.append("(issuer_name LIKE :kw OR bond_name LIKE :kw OR isin LIKE :kw)")
        cp["kw"] = f"%{keyword}%"
    count_row = await fetch_one(
        f"SELECT COUNT(*) AS cnt FROM universal_bond_master WHERE {' AND '.join(conds)}",
        cp or None,
    )
    if invested_only is True:
        invested_rows = await fetch_all(
            """
            WITH active_positions AS (
                SELECT p.isin, p.quantity
                FROM portfolio_positions p
                WHERE p.trust_id = :pid
                UNION ALL
                SELECT h.isin, h.quantity
                FROM portfolio_holdings h
                WHERE COALESCE(h.portfolio_id, 'default') = :pid
                  AND NOT EXISTS (SELECT 1 FROM portfolio_positions p WHERE p.trust_id = :pid)
            )
            SELECT DISTINCT isin FROM active_positions WHERE COALESCE(quantity, 0) > 0
            """,
            {"pid": trust_id},
        )
        invested_isins = {str(r.get("isin") or "").strip().upper() for r in invested_rows}
        bonds = [b for b in bonds if str(b.get("isin") or "").strip().upper() in invested_isins]
    total_count = int(count_row["cnt"]) if count_row else len(bonds)
    return {"bonds": bonds, "total": len(bonds), "total_count": total_count}


@router.get("/bonds/export")
async def export_bonds(
    format: str = Query("csv", pattern="^(csv|json)$"),
    issuer_type: Optional[str] = None,
    limit: int = Query(10000, le=25000),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Export bonds as CSV or JSON."""
    from fastapi.responses import StreamingResponse, JSONResponse
    import io, csv
    bonds = await registry.search(issuer_type=issuer_type, limit=limit)
    if format == "json":
        return JSONResponse({"bonds": bonds, "total": len(bonds)})
    # CSV
    output = io.StringIO()
    if bonds:
        writer = csv.DictWriter(output, fieldnames=bonds[0].keys())
        writer.writeheader()
        writer.writerows(bonds)
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=indian_bonds.csv"},
    )


@router.get("/bonds/{isin}")
async def get_bond(isin: str, user=Depends(require_role(Roles.VIEWER))):
    """Get full bond profile including latest market data.
    Falls back to broker_daily_quotes data if not in the master registry.
    """
    bond = await registry.get(isin)
    if not bond:
        # Fallback: build partial profile from broker quotes
        q = await fetch_one(
            """SELECT bdq.isin, bdq.bond_name, bdq.rating AS credit_rating,
                      bdq.rating_agency, bdq.coupon_rate, bdq.maturity_date,
                      bdq.category AS issuer_type, bdq.broker_name,
                      bdq.quote_date, bdq.clean_price, bdq.ytm
               FROM broker_daily_quotes bdq
               WHERE bdq.isin = :isin
               ORDER BY bdq.quote_date DESC, bdq.extracted_at DESC
               LIMIT 1""",
            {"isin": isin.strip().upper()},
        )
        if not q:
            raise HTTPException(status_code=404, detail=f"ISIN {isin} not found")
        coupon_pct = q["coupon_rate"]
        if coupon_pct is not None and 0 < coupon_pct < 1.0:
            coupon_pct = round(coupon_pct * 100, 4)
        ytm_pct = q["ytm"]
        if ytm_pct is not None and 0 < ytm_pct < 1.0:
            ytm_pct = round(ytm_pct * 100, 6)
        bond = {
            "isin": q["isin"],
            "bond_name": q["bond_name"],
            "issuer_name": q["bond_name"] or q["isin"],
            "issuer_type": q["issuer_type"] or "CORPORATE",
            "credit_rating": q["credit_rating"],
            "rating_agency": q["rating_agency"],
            "coupon_rate": coupon_pct,
            "maturity_date": q["maturity_date"],
            "face_value": 1000,
            "coupon_type": "FIXED",
            "coupon_frequency": 2,
            "sector": q["issuer_type"],
            "is_pf_eligible": False,
            "_partial": True,
            "_source": f"Broker: {q['broker_name']} ({q['quote_date']})",
        }
        md = {"isin": isin, "clean_price": q["clean_price"], "ytm": ytm_pct, "quote_date": str(q["quote_date"])}
        return {"bond": bond, "market_data": md}

    md = await fetch_one(
        """SELECT * FROM unified_market_data
           WHERE isin = :isin ORDER BY price_date DESC LIMIT 1""",
        {"isin": isin},
    )
    return {"bond": bond, "market_data": md}


# ── In-memory benchmark config store (survives until restart) ─────────┐
_benchmark_config: dict = {}


@router.post("/config/benchmark")
async def save_benchmark_config(
    payload: dict,
    user=Depends(require_role(Roles.ADMIN)),
):
    """Persist benchmark configuration in-process (survives gateway restart via reload)."""
    _benchmark_config.update(payload)
    log.info("benchmark_config_saved", config=payload)
    return {"status": "ok", "config": _benchmark_config}


@router.get("/config/benchmark")
async def get_benchmark_config(user=Depends(require_role(Roles.VIEWER))):
    """Return the current benchmark configuration."""
    return {"config": _benchmark_config}


@router.post("/market-data/refresh")
async def trigger_market_refresh(
    request: Request,
    max_bonds: int = Query(500, le=2000),
    user=Depends(require_role(Roles.ADMIN)),
):
    """Trigger a full market-data refresh across all sources."""
    try:
        assert_external_data_egress("intelligence market refresh")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    log.info("market_refresh_triggered", max_bonds=max_bonds)
    result = await fetcher.refresh_all(max_bonds=max_bonds)

    await AuditService.record(
        user={
            "id": str(user.get("id", user.get("sub", "unknown"))),
            "username": user.get("username") or user.get("sub") or "unknown",
            "role": user.get("role", Roles.ADMIN),
        },
        action=AuditAction.PRICE_UPDATE,
        entity_type=EntityType.MARKET_DATA,
        entity_id="INTELLIGENCE_REFRESH",
        old_value=None,
        new_value={"max_bonds": max_bonds, "result": result},
        affected_module="intelligence.market_refresh",
        ip_address=request.client.host if request.client else None,
        severity=AuditSeverity.INFO,
        compliance_flag=True,
        description=f"Intelligence market refresh triggered (max_bonds={max_bonds})",
    )

    return result


@router.get("/market-data/{isin}")
async def get_market_data(isin: str, days: int = Query(30, le=365), user=Depends(require_role(Roles.VIEWER))):
    """Return historical market data for an ISIN (up to *days* back)."""
    rows = await fetch_all(
        """SELECT * FROM unified_market_data
           WHERE isin = :isin
           ORDER BY price_date DESC
           LIMIT :days""",
        {"isin": isin, "days": days},
    )
    return {"isin": isin, "history": rows}


@router.get("/registry/stats")
async def registry_stats(user=Depends(require_role(Roles.VIEWER))):
    """Summary stats for the universal ISIN registry."""
    return await registry.stats()


@router.get("/stats")
async def intelligence_stats(user=Depends(require_role(Roles.VIEWER))):
    """Alias for /registry/stats — total bond and market data counts."""
    return await registry.stats()


@router.post("/scoring/run")
async def run_scoring(limit: int = Query(500, le=5000), user=Depends(require_role(Roles.ADMIN))):
    """Trigger a new institutional scoring run on current market data."""
    log.info("trigger_scoring", limit=limit)
    result = await scorer.score_all(limit=limit)
    return result


@router.get("/reports/market-analysis")
async def get_market_analysis_report(user=Depends(require_role(Roles.VIEWER))):
    """Return the latest intelligent market analysis and top buy recommendations."""
    log.info("generate_market_report")
    report_md = await report_gen.generate_market_report()
    return {"report_md": report_md}


@router.get("/reports/market-analysis/pdf")
async def get_market_analysis_pdf(user=Depends(require_role(Roles.VIEWER))):
    """Generate and download the professional PDF version of the market report."""
    from fastapi.responses import Response
    log.info("generate_pdf_report")
    
    # Reuse the same query logic as UnifiedReportGenerator 
    # (In prod, we'd refactor this into a service)
    bonds = await fetch_all(
        """
        SELECT s.isin, s.iss_score, s.recommendation, s.overall_explanation,
               b.issuer_name, b.issuer_type, b.credit_rating, b.maturity_date, b.coupon_rate,
               m.ytm, m.clean_price, m.spread_over_gsec
        FROM   unified_bond_scores s
        JOIN   universal_bond_master b ON s.isin = b.isin
        JOIN   unified_market_data   m ON s.isin = m.isin
        WHERE  s.score_date = (SELECT MAX(score_date) FROM unified_bond_scores)
          AND  m.price_date = (SELECT MAX(price_date) FROM unified_market_data WHERE isin = s.isin)
        ORDER  BY s.iss_score DESC
        """
    )

    if not bonds:
        # Keep report flow usable by bootstrapping scores on-demand when no scoring run exists yet.
        try:
            await scorer.score_all(limit=500)
        except Exception:
            log.warning("pdf_scoring_bootstrap_failed")

        bonds = await fetch_all(
            """
            SELECT s.isin, s.iss_score, s.recommendation, s.overall_explanation,
                   b.issuer_name, b.issuer_type, b.credit_rating, b.maturity_date, b.coupon_rate,
                   m.ytm, m.clean_price, m.spread_over_gsec
            FROM   unified_bond_scores s
            JOIN   universal_bond_master b ON s.isin = b.isin
            JOIN   unified_market_data   m ON s.isin = m.isin
            WHERE  s.score_date = (SELECT MAX(score_date) FROM unified_bond_scores)
              AND  m.price_date = (SELECT MAX(price_date) FROM unified_market_data WHERE isin = s.isin)
            ORDER  BY s.iss_score DESC
            """
        )

    if not bonds:
        bonds = await fetch_all(
            """
            SELECT b.isin,
                   0.0 AS iss_score,
                   'WATCH' AS recommendation,
                   'Score pending. Generated from latest market snapshot.' AS overall_explanation,
                   b.issuer_name,
                   b.issuer_type,
                   b.credit_rating,
                   b.maturity_date,
                   b.coupon_rate,
                   m.ytm,
                   m.clean_price,
                   m.spread_over_gsec
            FROM   universal_bond_master b
            JOIN   unified_market_data m ON b.isin = m.isin
            WHERE  m.price_date = (
                SELECT MAX(price_date) FROM unified_market_data WHERE isin = b.isin
            )
            ORDER BY m.ytm DESC
            LIMIT  100
            """
        )

    if not bonds:
        bonds = [
            {
                "isin": "N/A",
                "iss_score": 0.0,
                "recommendation": "WATCH",
                "overall_explanation": "No scored bonds available. Generated placeholder report.",
                "issuer_name": "No Data",
                "issuer_type": "N/A",
                "credit_rating": "N/A",
                "maturity_date": None,
                "coupon_rate": 0.0,
                "ytm": 0.0,
                "clean_price": 0.0,
                "spread_over_gsec": 0.0,
            }
        ]
        
    pdf_bytes = pdf_gen.generate_report(bonds)
    
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=Indian_Bond_Market_Analysis.pdf"}
    )


@router.get("/top-bonds")
async def top_bonds(limit: int = Query(20, le=100), user=Depends(require_role(Roles.VIEWER))):
    """Top bonds ranked by YTM from latest market data."""
    rows = await fetch_all(
        """
        SELECT b.isin, b.bond_name, b.issuer_name, b.issuer_type,
               b.credit_rating, b.maturity_date,
               m.ytm, m.clean_price, m.modified_duration,
               m.spread_over_gsec, m.data_source, m.price_date
        FROM   universal_bond_master b
        JOIN   unified_market_data   m ON b.isin = m.isin
        WHERE  b.is_active = 1
          AND  m.price_date = (
                SELECT MAX(price_date) FROM unified_market_data
                WHERE isin = b.isin
               )
        ORDER  BY m.ytm DESC
        LIMIT  :limit
        """,
        {"limit": limit},
    )
    return {"bonds": rows, "total": len(rows)}


@router.post("/admin/seed", tags=["Admin"])
async def seed_bonds(batch_size: int = Query(500, le=2000), user=Depends(require_role(Roles.ADMIN))):
    """
    Trigger the full Indian bond seeder (20,000+ bonds).
    Seeds all G-Sec, T-Bills, SDLs, PSU, Bank, NBFC, Corporate, Municipal & Covered bonds.
    """
    log.info("admin_seed_triggered", batch_size=batch_size)
    try:
        from ..seeds.seed_indian_bonds import run_seed
        result = await run_seed(batch_size=batch_size)
        log.info("admin_seed_complete", **result)
        return result
    except Exception as exc:
        log.error("admin_seed_error", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/math-equations", tags=["Reference"])
async def get_math_equations(user=Depends(require_role(Roles.VIEWER))):
    """Return the mathematical equations reference as Markdown."""
    import pathlib
    md_path = pathlib.Path(__file__).parent.parent.parent / "MATH_EQUATIONS.md"
    if md_path.exists():
        content = md_path.read_text(encoding="utf-8")
    else:
        content = "# Math Equations\n\nFile not found. See `Private3/MATH_EQUATIONS.md`."
    return {"content": content, "source": str(md_path)}
