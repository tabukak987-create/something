"""
Alerts Router — Exposes active alerts via REST API.
"""

from datetime import date, datetime
import re
from typing import Optional, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..services.alert_engine import AlertEngine
from ..services.explainability import build_counterfactual, build_explanation
from ..services.instrument_classification import (
    INSTRUMENT_CATEGORY_PRIVATE,
    category_metadata,
    classify_instrument,
)
from ..shared.database import execute_query, fetch_all, fetch_one
from ..shared.auth import require_role, Roles

router = APIRouter(prefix="/alerts", tags=["Alerts"])

_engine = AlertEngine()
_SEVERITY_RANK = {"INFO": 1, "WARNING": 2, "CRITICAL": 3}


def _state_payload(status: str, data: dict[str, Any] | None = None, message: str | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "data": data if isinstance(data, dict) else {},
        "message": message,
    }


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _normalize_alert_text(value: object) -> str:
    """Normalize alert text to improve semantic dedupe across metric jitter."""
    text = str(value or "").strip().lower()
    text = re.sub(r"\d+(?:\.\d+)?", "#", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _dedupe_alert_rows(alerts: list[dict]) -> list[dict]:
    """Remove semantic duplicate alerts and keep highest-severity/latest instance."""
    out = {}
    for a in alerts or []:
        title_key = _normalize_alert_text(a.get("title") or a.get("detail") or "")
        key = (
            str(a.get("alert_type") or "").upper(),
            str(a.get("isin") or "").upper(),
            str(a.get("alert_date") or ""),
            title_key,
        )
        prev = out.get(key)
        if not prev:
            out[key] = a
            continue
        prev_rank = _SEVERITY_RANK.get(str(prev.get("severity") or "").upper(), 0)
        cur_rank = _SEVERITY_RANK.get(str(a.get("severity") or "").upper(), 0)
        if cur_rank > prev_rank:
            out[key] = a
            continue
        prev_id = int(prev.get("id") or 0)
        cur_id = int(a.get("id") or 0)
        if cur_id >= prev_id:
            out[key] = a

    deduped = list(out.values())
    deduped.sort(
        key=lambda x: (
            _SEVERITY_RANK.get(str(x.get("severity") or "").upper(), 0),
            str(x.get("created_at") or ""),
            int(x.get("id") or 0),
        ),
        reverse=True,
    )
    return deduped


def _alert_explanation(alert: dict[str, Any]) -> dict[str, Any]:
    metric = _safe_float(alert.get("metric_value"), 0.0)
    threshold = _safe_float(alert.get("threshold_value"), 0.0)
    delta = metric - threshold
    severity = str(alert.get("severity") or "INFO").upper()
    status = "FAIL" if severity == "CRITICAL" else ("WARN" if severity == "WARNING" else "PASS")
    summary = str(alert.get("title") or alert.get("detail") or "Portfolio alert generated").strip()

    confidence = 0.9 if severity == "CRITICAL" else (0.82 if severity == "WARNING" else 0.7)

    return build_explanation(
        module="alerts_router",
        decision_type="alert_evaluation",
        decision_outcome=severity,
        summary=summary,
        key_factors=[
            {
                "name": "Severity",
                "value": severity,
                "impact": "negative" if severity in {"CRITICAL", "WARNING"} else "neutral",
                "evidence": "Alert engine severity ranking",
            },
            {
                "name": "Metric Value",
                "value": round(metric, 6),
                "impact": "negative" if delta > 0 else "neutral",
                "evidence": "Computed signal value from alert checks",
            },
            {
                "name": "Threshold Value",
                "value": round(threshold, 6),
                "impact": "neutral",
                "evidence": "Configured control threshold",
            },
            {
                "name": "Resolved",
                "value": bool(alert.get("is_resolved")),
                "impact": "positive" if alert.get("is_resolved") else "negative",
                "evidence": "Alert lifecycle status",
            },
        ],
        rule_checks=[
            {
                "rule_code": str(alert.get("alert_type") or "ALERT").upper(),
                "status": status,
                "message": str(alert.get("detail") or ""),
                "threshold": round(threshold, 6),
                "actual": round(metric, 6),
            }
        ],
        calculation_trace={
            "metric_value": round(metric, 6),
            "threshold_value": round(threshold, 6),
            "delta": round(delta, 6),
            "alert_date": alert.get("alert_date"),
            "created_at": alert.get("created_at"),
        },
        data_lineage=[
            {"source": "enhanced.alert_engine", "field": "alert_rule_eval", "trust_level": "high"},
            {"source": "unified_alerts", "field": "persisted_alert", "trust_level": "high"},
        ],
        regulatory_basis=[
            "PF Trust monitoring controls for concentration, liquidity, valuation and credit alerts",
        ],
        confidence_score=confidence,
        confidence_drivers=[
            f"alert_type={alert.get('alert_type')}",
            f"severity={severity}",
            f"threshold_gap={round(abs(delta), 6)}",
        ],
        counterfactuals=[
            build_counterfactual(
                scenario="metric_within_threshold",
                expected_outcome="NO_ALERT",
                impact_summary="If metric remained within threshold, this alert would not be emitted.",
                delta_metrics={"metric_value": round(metric, 6), "threshold_value": round(threshold, 6)},
                rationale="Threshold-crossing drives alert emission.",
            )
        ],
        severity=("CRITICAL" if severity == "CRITICAL" else ("WARNING" if severity == "WARNING" else "INFO")),
        tags=["alerts", str(alert.get("alert_type") or "unknown").lower()],
        metadata={
            "id": alert.get("id"),
            "isin": alert.get("isin"),
            "alert_type": alert.get("alert_type"),
        },
    )


def _enrich_alerts(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for alert in alerts:
        item = dict(alert)
        derived = classify_instrument(item)
        category = str(item.get("instrument_category") or derived.get("instrument_category") or INSTRUMENT_CATEGORY_PRIVATE).upper()
        subtype = str(item.get("instrument_subtype") or derived.get("instrument_subtype") or "CORPORATE").upper()
        meta = category_metadata(category)
        item["instrument_category"] = category
        item["instrument_subtype"] = subtype
        item["category_display"] = meta.get("category_display")
        item["category_tag"] = meta.get("category_tag")
        item["risk_label"] = meta.get("risk_label")
        item["explanation"] = _alert_explanation(item)
        enriched.append(item)
    return enriched


def _category_breakdown(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in alerts or []:
        derived = classify_instrument(row)
        category = str(row.get("instrument_category") or derived.get("instrument_category") or INSTRUMENT_CATEGORY_PRIVATE).upper()
        counts[category] = counts.get(category, 0) + 1

    out: list[dict[str, Any]] = []
    for category in sorted(counts.keys()):
        meta = category_metadata(category)
        out.append(
            {
                "instrument_category": category,
                "category_display": meta.get("category_display"),
                "category_tag": meta.get("category_tag"),
                "risk_label": meta.get("risk_label"),
                "count": counts.get(category, 0),
            }
        )
    return out


def _collection_explanation(
    *,
    decision_type: str,
    summary: str,
    total: int,
    critical_count: int,
    warning_count: int,
    info_count: int,
    open_count: int,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    outcome = "CRITICAL" if critical_count > 0 else ("WARN" if warning_count > 0 else "PASS")
    return build_explanation(
        module="alerts_router",
        decision_type=decision_type,
        decision_outcome=outcome,
        summary=summary,
        key_factors=[
            {
                "name": "Total Alerts",
                "value": total,
                "impact": "negative" if total else "neutral",
            },
            {
                "name": "Critical Alerts",
                "value": critical_count,
                "impact": "negative" if critical_count else "neutral",
            },
            {
                "name": "Open Alerts",
                "value": open_count,
                "impact": "negative" if open_count else "positive",
            },
        ],
        rule_checks=[
            {
                "rule_code": "ALERT_CRITICAL_PRESENCE",
                "status": "FAIL" if critical_count > 0 else "PASS",
                "message": "Critical alert count should ideally be zero",
                "actual": critical_count,
            },
            {
                "rule_code": "ALERT_WARNING_PRESENCE",
                "status": "WARN" if warning_count > 0 else "PASS",
                "message": "Warning alerts need remediation tracking",
                "actual": warning_count,
            },
            {
                "rule_code": "ALERT_OPEN_COUNT",
                "status": "WARN" if open_count > 0 else "PASS",
                "message": "Open alerts indicate pending actions",
                "actual": open_count,
            },
        ],
        calculation_trace={
            "total": total,
            "critical": critical_count,
            "warning": warning_count,
            "info": info_count,
            "open": open_count,
        },
        data_lineage=[
            {"source": "enhanced.alert_engine", "field": decision_type, "trust_level": "high"},
            {"source": "unified_alerts", "field": "alerts", "trust_level": "high"},
        ],
        regulatory_basis=[
            "PF Trust daily surveillance and exception-management controls",
        ],
        confidence_score=0.88,
        confidence_drivers=["daily alert snapshot", "deduplicated alert set"],
        counterfactuals=[
            build_counterfactual(
                scenario="all_alerts_resolved",
                expected_outcome="PASS",
                impact_summary="Resolving open alerts reduces monitoring risk posture.",
                delta_metrics={"open_count": open_count},
            )
        ],
        severity=("CRITICAL" if critical_count > 0 else ("WARNING" if warning_count > 0 else "INFO")),
        tags=["alerts", "summary"],
        metadata=metadata or {},
    )


@router.get("/")
async def list_alerts(
    alert_date: Optional[str] = Query(None, description="ISO date (YYYY-MM-DD). Defaults to today."),
    trust_id: str = Query("default", description="Trust identifier"),
    severity: Optional[str] = Query(None, description="Filter by severity: CRITICAL, WARNING, INFO"),
    is_resolved: Optional[int] = Query(None, description="0 = open only, 1 = resolved only, omit = all"),
    limit: int = Query(100, ge=1, le=500),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Return alerts for a given date with optional filters."""
    dt = date.fromisoformat(alert_date) if alert_date else date.today()
    alerts = await _engine.get_alerts_for_date(dt.isoformat(), trust_id=trust_id)
    alerts = _dedupe_alert_rows(alerts)

    if severity:
        alerts = [a for a in alerts if a.get("severity", "").upper() == severity.upper()]
    if is_resolved is not None:
        alerts = [a for a in alerts if int(a.get("is_resolved", 0)) == is_resolved]

    alerts = _enrich_alerts(alerts)
    critical_count = sum(1 for a in alerts if str(a.get("severity") or "").upper() == "CRITICAL")
    warning_count = sum(1 for a in alerts if str(a.get("severity") or "").upper() == "WARNING")
    info_count = sum(1 for a in alerts if str(a.get("severity") or "").upper() == "INFO")
    open_count = sum(1 for a in alerts if not bool(a.get("is_resolved")))
    category_breakdown = _category_breakdown(alerts)

    return {
        "date": dt.isoformat(),
        "total": len(alerts),
        "alerts": alerts[:limit],
        "category_breakdown": category_breakdown,
        "explanation": _collection_explanation(
            decision_type="alerts_list",
            summary=f"Loaded {len(alerts)} alerts for {dt.isoformat()}.",
            total=len(alerts),
            critical_count=critical_count,
            warning_count=warning_count,
            info_count=info_count,
            open_count=open_count,
            metadata={
                "trust_id": trust_id,
                "filter_severity": severity,
                "is_resolved": is_resolved,
                "category_breakdown": category_breakdown,
            },
        ),
    }


@router.get("/bond/{isin}")
async def alerts_for_bond(
    isin: str,
    limit: int = Query(20, ge=1, le=100),
    trust_id: str = Query("default", description="Trust identifier"),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Return alerts for a specific bond ISIN."""
    alerts = await _engine.get_alerts_for_bond(isin, trust_id=trust_id)
    alerts = _dedupe_alert_rows(alerts)
    alerts = _enrich_alerts(alerts)
    critical_count = sum(1 for a in alerts if str(a.get("severity") or "").upper() == "CRITICAL")
    warning_count = sum(1 for a in alerts if str(a.get("severity") or "").upper() == "WARNING")
    info_count = sum(1 for a in alerts if str(a.get("severity") or "").upper() == "INFO")
    open_count = sum(1 for a in alerts if not bool(a.get("is_resolved")))
    category_breakdown = _category_breakdown(alerts)
    return {
        "isin": isin,
        "total": len(alerts),
        "alerts": alerts[:limit],
        "category_breakdown": category_breakdown,
        "explanation": _collection_explanation(
            decision_type="alerts_for_bond",
            summary=f"Loaded {len(alerts)} alert(s) for {isin}.",
            total=len(alerts),
            critical_count=critical_count,
            warning_count=warning_count,
            info_count=info_count,
            open_count=open_count,
            metadata={"isin": isin, "trust_id": trust_id, "category_breakdown": category_breakdown},
        ),
    }


@router.post("/run")
async def run_alert_engine(user=Depends(require_role(Roles.ADMIN))):
    """Manually trigger the alert detection engine."""
    result = await _engine.run_all_checks()
    generated = result.get("alerts_generated", 0) if isinstance(result, dict) else int(result or 0)
    payload = {"status": "ok", "alerts_generated": generated, "date": date.today().isoformat()}
    payload["explanation"] = build_explanation(
        module="alerts_router",
        decision_type="run_alert_engine",
        decision_outcome="PASS",
        summary=f"Alert engine executed and generated {generated} alert(s).",
        key_factors=[
            {"name": "Alerts Generated", "value": generated, "impact": "negative" if generated else "neutral"},
            {"name": "Run Date", "value": payload["date"], "impact": "neutral"},
        ],
        calculation_trace={"run_result": result},
        data_lineage=[{"source": "enhanced.alert_engine", "field": "run_all_checks", "trust_level": "high"}],
        regulatory_basis=["PF Trust automated surveillance runbook"],
        confidence_score=0.95,
        confidence_drivers=["direct engine execution", "synchronous completion"],
        tags=["alerts", "engine-run"],
    )
    return payload


@router.patch("/{alert_id}/resolve")
async def resolve_alert(
    alert_id: int,
    resolved_by: str = Query("system"),
    trust_id: str = Query("default", description="Trust identifier"),
    user=Depends(require_role(Roles.COMPLIANCE)),
):
    """Mark a specific alert as resolved."""
    existing = await fetch_one(
        "SELECT id, isin, is_resolved FROM unified_alerts WHERE id = :aid",
        {"aid": alert_id},
    )
    if not existing:
        raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")
    allowed_isins = await _engine._allowed_isins(trust_id)
    existing_isin = str(existing.get("isin") or "").upper()
    if existing_isin and existing_isin not in allowed_isins:
        raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")
    if existing.get("is_resolved"):
        payload = {"status": "already_resolved", "alert_id": alert_id}
        payload["explanation"] = build_explanation(
            module="alerts_router",
            decision_type="resolve_alert",
            decision_outcome="PASS",
            summary=f"Alert {alert_id} was already resolved.",
            key_factors=[
                {"name": "Alert ID", "value": alert_id, "impact": "neutral"},
                {"name": "Resolved", "value": True, "impact": "positive"},
            ],
            calculation_trace={"existing": existing},
            confidence_score=0.99,
            tags=["alerts", "resolution"],
        )
        return payload

    await execute_query(
        """UPDATE unified_alerts
           SET is_resolved = 1,
               resolved_at = datetime('now'),
               resolved_by = :rb
           WHERE id = :aid""",
        {"aid": alert_id, "rb": resolved_by},
    )
    payload = {"status": "resolved", "alert_id": alert_id}
    payload["explanation"] = build_explanation(
        module="alerts_router",
        decision_type="resolve_alert",
        decision_outcome="PASS",
        summary=f"Alert {alert_id} marked as resolved by {resolved_by}.",
        key_factors=[
            {"name": "Alert ID", "value": alert_id, "impact": "neutral"},
            {"name": "Resolved By", "value": resolved_by, "impact": "positive"},
        ],
        calculation_trace={"alert_id": alert_id, "resolved_by": resolved_by, "trust_id": trust_id},
        confidence_score=0.99,
        tags=["alerts", "resolution"],
    )
    return payload


@router.get("/summary")
async def alert_summary(
    trust_id: str = Query("default", description="Trust identifier"),
    user=Depends(require_role(Roles.VIEWER)),
):
    """Get alert counts grouped by type and severity for today."""
    alerts = await _engine.get_alerts_for_date(date.today().isoformat(), trust_id=trust_id)
    alerts = _dedupe_alert_rows(alerts)
    rows = {}
    open_count = 0
    for alert in alerts:
        key = (str(alert.get("alert_type") or ""), str(alert.get("severity") or ""))
        rows[key] = rows.get(key, 0) + 1
        if not alert.get("is_resolved"):
            open_count += 1
    critical_count = sum(1 for a in alerts if str(a.get("severity") or "").upper() == "CRITICAL")
    warning_count = sum(1 for a in alerts if str(a.get("severity") or "").upper() == "WARNING")
    info_count = sum(1 for a in alerts if str(a.get("severity") or "").upper() == "INFO")
    alerts = _enrich_alerts(alerts)
    category_breakdown = _category_breakdown(alerts)
    payload = {
        "date": date.today().isoformat(),
        "summary": [
            {"alert_type": alert_type, "severity": severity, "cnt": cnt}
            for (alert_type, severity), cnt in sorted(rows.items(), key=lambda item: (item[0][1], item[0][0]))
        ],
        "open_count": open_count,
        "category_breakdown": category_breakdown,
        "explanation": _collection_explanation(
            decision_type="alerts_summary",
            summary=f"Computed alert summary for {date.today().isoformat()}.",
            total=len(alerts),
            critical_count=critical_count,
            warning_count=warning_count,
            info_count=info_count,
            open_count=open_count,
            metadata={"trust_id": trust_id, "category_breakdown": category_breakdown},
        ),
    }
    if not alerts:
        return _state_payload("no_data", payload, "No active alerts yet. Run Full Scan to generate alert metrics.")
    return _state_payload("ok", payload)
