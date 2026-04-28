from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import uuid4

SCHEMA_VERSION = "1.0.0"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _as_text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def _sanitize(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(v) for v in value]
    return str(value)


def _confidence_level(score: float) -> str:
    if score >= 0.8:
        return "high"
    if score >= 0.5:
        return "medium"
    return "low"


def _default_severity(decision_outcome: str) -> str:
    up = str(decision_outcome or "").upper()
    if any(token in up for token in ("FAIL", "BLOCK", "CRITICAL", "REJECT")):
        return "CRITICAL"
    if any(token in up for token in ("WARN", "REMEDIATION")):
        return "WARNING"
    return "INFO"


def _normalize_factor(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": _as_text(item.get("name") or item.get("label") or item.get("factor"), "Factor"),
        "value": _sanitize(item.get("value")),
        "impact": _as_text(item.get("impact"), "neutral").lower(),
        "evidence": _as_text(item.get("evidence") or item.get("source"), ""),
    }


def _normalize_rule(item: Mapping[str, Any]) -> dict[str, Any]:
    status = _as_text(item.get("status"), "INFO").upper()
    if status not in {"PASS", "FAIL", "WARN", "INFO", "N/A"}:
        status = "INFO"
    return {
        "rule_code": _as_text(item.get("rule_code") or item.get("rule") or item.get("code"), "RULE"),
        "status": status,
        "message": _as_text(item.get("message"), ""),
        "threshold": _sanitize(item.get("threshold")),
        "actual": _sanitize(item.get("actual") or item.get("value")),
    }


def build_change_tracking(
    previous_state: Mapping[str, Any] | None,
    current_state: Mapping[str, Any] | None,
    tracked_fields: Sequence[str],
    previous_decision_id: str | None = None,
) -> dict[str, Any]:
    before = previous_state or {}
    after = current_state or {}
    changes: list[dict[str, Any]] = []

    for field in tracked_fields:
        left = before.get(field)
        right = after.get(field)
        if left != right:
            changes.append(
                {
                    "field": field,
                    "before": _sanitize(left),
                    "after": _sanitize(right),
                }
            )

    return {
        "changed": bool(changes),
        "changes": changes,
        "previous_decision_id": previous_decision_id,
    }


def build_counterfactual(
    scenario: str,
    expected_outcome: str,
    impact_summary: str,
    delta_metrics: Mapping[str, Any] | None = None,
    rationale: str | None = None,
) -> dict[str, Any]:
    return {
        "scenario": _as_text(scenario, "counterfactual"),
        "expected_outcome": _as_text(expected_outcome, "UNKNOWN"),
        "impact_summary": _as_text(impact_summary, ""),
        "delta_metrics": _sanitize(delta_metrics or {}),
        "rationale": _as_text(rationale, ""),
    }


def build_explanation(
    module: str,
    decision_type: str,
    decision_outcome: str,
    summary: str,
    *,
    key_factors: Sequence[Mapping[str, Any]] | None = None,
    rule_checks: Sequence[Mapping[str, Any]] | None = None,
    calculation_trace: Mapping[str, Any] | None = None,
    data_lineage: Sequence[Mapping[str, Any]] | None = None,
    regulatory_basis: Sequence[str] | None = None,
    confidence_score: float | None = None,
    confidence_drivers: Sequence[str] | None = None,
    counterfactuals: Sequence[Mapping[str, Any]] | None = None,
    change_tracking: Mapping[str, Any] | None = None,
    override: Mapping[str, Any] | None = None,
    decision_id: str | None = None,
    severity: str | None = None,
    tags: Sequence[str] | None = None,
    audit_metadata: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    factors = [_normalize_factor(item) for item in (key_factors or [])]
    rules = [_normalize_rule(item) for item in (rule_checks or [])]

    score = _as_float(confidence_score, 0.0)
    score = max(0.0, min(score, 1.0))

    override_payload = {
        "applied": bool((override or {}).get("applied")),
        "reason": _as_text((override or {}).get("reason"), ""),
        "by": _as_text((override or {}).get("by"), ""),
        "at": _as_text((override or {}).get("at"), ""),
    }

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "decision_id": _as_text(decision_id, str(uuid4())),
        "generated_at": _utcnow_iso(),
        "module": _as_text(module, "unknown"),
        "decision_type": _as_text(decision_type, "decision"),
        "decision_outcome": _as_text(decision_outcome, "UNKNOWN"),
        "summary": _as_text(summary, "No explanation was provided."),
        "explanation_levels": {
            "level_1": {
                "headline": _as_text(summary, "No explanation was provided."),
            },
            "level_2": {
                "key_factors": factors,
                "rule_checks": rules,
            },
            "level_3": {
                "calculation_trace": _sanitize(calculation_trace or {}),
                "data_lineage": _sanitize(list(data_lineage or [])),
                "regulatory_basis": [str(x) for x in (regulatory_basis or [])],
                "audit_metadata": _sanitize(audit_metadata or {}),
            },
        },
        "confidence": {
            "score": round(score, 4),
            "level": _confidence_level(score),
            "drivers": [str(x) for x in (confidence_drivers or [])],
        },
        "counterfactuals": _sanitize(list(counterfactuals or [])),
        "change_tracking": _sanitize(change_tracking or {"changed": False, "changes": []}),
        "override": override_payload,
        "ui_hints": {
            "severity": _as_text(severity, _default_severity(decision_outcome)),
            "tags": [str(x) for x in (tags or [])],
        },
    }

    if metadata:
        payload["metadata"] = _sanitize(metadata)

    return payload


def ensure_explanation_schema(
    explanation: Mapping[str, Any] | None,
    *,
    module: str,
    decision_type: str,
    decision_outcome: str,
    summary: str,
) -> dict[str, Any]:
    if not explanation:
        return build_explanation(module, decision_type, decision_outcome, summary)

    normalized = build_explanation(
        module=explanation.get("module") or module,
        decision_type=explanation.get("decision_type") or decision_type,
        decision_outcome=explanation.get("decision_outcome") or decision_outcome,
        summary=explanation.get("summary") or summary,
        key_factors=(
            ((explanation.get("explanation_levels") or {}).get("level_2") or {}).get("key_factors")
            if isinstance(explanation.get("explanation_levels"), Mapping)
            else explanation.get("key_factors")
        )
        or [],
        rule_checks=(
            ((explanation.get("explanation_levels") or {}).get("level_2") or {}).get("rule_checks")
            if isinstance(explanation.get("explanation_levels"), Mapping)
            else explanation.get("rule_checks")
        )
        or [],
        calculation_trace=(
            ((explanation.get("explanation_levels") or {}).get("level_3") or {}).get("calculation_trace")
            if isinstance(explanation.get("explanation_levels"), Mapping)
            else explanation.get("calculation_trace")
        )
        or {},
        data_lineage=(
            ((explanation.get("explanation_levels") or {}).get("level_3") or {}).get("data_lineage")
            if isinstance(explanation.get("explanation_levels"), Mapping)
            else explanation.get("data_lineage")
        )
        or [],
        regulatory_basis=(
            ((explanation.get("explanation_levels") or {}).get("level_3") or {}).get("regulatory_basis")
            if isinstance(explanation.get("explanation_levels"), Mapping)
            else explanation.get("regulatory_basis")
        )
        or [],
        confidence_score=((explanation.get("confidence") or {}).get("score") if isinstance(explanation.get("confidence"), Mapping) else None),
        confidence_drivers=((explanation.get("confidence") or {}).get("drivers") if isinstance(explanation.get("confidence"), Mapping) else None),
        counterfactuals=explanation.get("counterfactuals") or [],
        change_tracking=explanation.get("change_tracking") or None,
        override=explanation.get("override") or None,
        decision_id=explanation.get("decision_id"),
        severity=((explanation.get("ui_hints") or {}).get("severity") if isinstance(explanation.get("ui_hints"), Mapping) else None),
        tags=((explanation.get("ui_hints") or {}).get("tags") if isinstance(explanation.get("ui_hints"), Mapping) else None),
        audit_metadata=(
            ((explanation.get("explanation_levels") or {}).get("level_3") or {}).get("audit_metadata")
            if isinstance(explanation.get("explanation_levels"), Mapping)
            else explanation.get("audit_metadata")
        )
        or None,
        metadata=explanation.get("metadata") if isinstance(explanation.get("metadata"), Mapping) else None,
    )

    return normalized
