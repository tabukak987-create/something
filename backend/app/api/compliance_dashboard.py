from __future__ import annotations

import csv
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..shared.auth import Roles, require_role
from ..shared.database import fetch_all, fetch_one
from ..shared.logger import get_logger
from ..shared.time_utils import utcnow_iso
from ..services.alert_engine import (
    GOVERNMENT_SPREAD_ALERT_TYPE,
    _is_government_backed_spread,
    _is_market_date_fresh,
)
from ..domain.core_trust.audit import AuditAction, AuditService, AuditSeverity, EntityType

log = get_logger("router.compliance_dashboard")
router = APIRouter(prefix="/compliance-dashboard", tags=["Compliance Dashboard"])


def _json_loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            value = bytes(value).decode("utf-8")
        except Exception:
            return default
    if not isinstance(value, str):
        return default
    raw = value.strip()
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _status_score(status: str) -> float:
    label = str(status or "").strip().upper()
    if label == "PASS":
        return 100.0
    if label == "WARN":
        return 70.0
    if label == "FAIL":
        return 35.0
    return 0.0


def _status_severity(status: str) -> str:
    label = str(status or "").strip().upper()
    if label == "FAIL":
        return "CRITICAL"
    if label == "WARN":
        return "WARNING"
    if label == "PASS":
        return "INFO"
    return "INFO"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _alert_market_date(row: dict[str, Any]) -> str | None:
    value = row.get("source_price_date") or row.get("alert_date")
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:10]


def _is_visible_open_alert(row: dict[str, Any], *, as_of: datetime | None = None) -> bool:
    alert_type = str(row.get("alert_type") or "").upper()
    if alert_type == GOVERNMENT_SPREAD_ALERT_TYPE:
        return False

    if alert_type != "YIELD_SPREAD":
        return True

    if _is_government_backed_spread(row.get("issuer_name"), row.get("issuer_type"), row.get("credit_rating")):
        return False

    market_date = _alert_market_date(row)
    if not market_date:
        return False

    reference_dt = _parse_iso_datetime(row.get("alert_date")) or as_of
    return _is_market_date_fresh(market_date, as_of=(reference_dt.date() if reference_dt else None))


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text[:19], fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


async def _fetch_all_safe(query: str, params: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    try:
        rows = await fetch_all(query, params or {})
        return [dict(row) for row in rows]
    except Exception as exc:
        log.warning("compliance_dashboard_query_failed", error=str(exc))
        return []


async def _fetch_one_safe(query: str, params: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    try:
        row = await fetch_one(query, params or {})
        return dict(row) if row else None
    except Exception as exc:
        log.warning("compliance_dashboard_query_failed", error=str(exc))
        return None


async def _compliance_trend(days: int) -> list[dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).strftime("%Y-%m-%d")
    rows = await _fetch_all_safe(
        """
        SELECT checked_at, overall_status
        FROM unified_compliance_audit
        WHERE substr(checked_at, 1, 10) >= :cutoff
        ORDER BY checked_at ASC
        """,
        {"cutoff": cutoff},
    )

    by_day: dict[str, dict[str, Any]] = {}
    for row in rows:
        day = str(row.get("checked_at") or "")[:10]
        if not day:
            continue
        bucket = by_day.setdefault(day, {"scores": [], "pass": 0, "warn": 0, "fail": 0})
        status = str(row.get("overall_status") or "").upper()
        bucket["scores"].append(_status_score(status))
        if status == "PASS":
            bucket["pass"] += 1
        elif status == "WARN":
            bucket["warn"] += 1
        elif status == "FAIL":
            bucket["fail"] += 1

    trend = []
    for day in sorted(by_day.keys()):
        bucket = by_day[day]
        scores = bucket.get("scores") or [0.0]
        avg_score = round(sum(scores) / max(len(scores), 1), 2)
        trend.append(
            {
                "date": day,
                "compliance_score": avg_score,
                "pass": bucket.get("pass", 0),
                "warn": bucket.get("warn", 0),
                "fail": bucket.get("fail", 0),
                "sample_size": len(scores),
            }
        )
    return trend


def _summarize_rule_violations(rows: list[dict[str, Any]], rule_filter: str = "") -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    needle = rule_filter.strip().lower()

    for row in rows:
        checked_at = row.get("checked_at")
        violations = _json_loads(row.get("violations_json"), [])
        if not isinstance(violations, list):
            continue
        for violation in violations:
            if not isinstance(violation, dict):
                continue
            code = str(
                violation.get("rule_code")
                or violation.get("code")
                or violation.get("rule")
                or "UNSPECIFIED"
            ).strip()
            if not code:
                code = "UNSPECIFIED"
            if needle and needle not in code.lower():
                continue

            status = str(violation.get("status") or row.get("overall_status") or "WARN").upper()
            entry = grouped.setdefault(
                code,
                {
                    "rule_code": code,
                    "severity": _status_severity(status),
                    "count": 0,
                    "latest_seen": checked_at,
                    "message": str(violation.get("message") or "").strip(),
                },
            )
            entry["count"] += 1
            entry["latest_seen"] = max(str(entry.get("latest_seen") or ""), str(checked_at or ""))

            current_severity = str(entry.get("severity") or "INFO")
            new_severity = _status_severity(status)
            rank = {"CRITICAL": 3, "WARNING": 2, "INFO": 1}
            if rank.get(new_severity, 0) > rank.get(current_severity, 0):
                entry["severity"] = new_severity

            if not entry.get("message"):
                entry["message"] = str(violation.get("message") or "").strip()

    return sorted(grouped.values(), key=lambda item: (-_safe_int(item.get("count")), str(item.get("rule_code") or "")))


def _version_row_payload(row: dict[str, Any]) -> dict[str, Any]:
    summary = _json_loads(row.get("summary_json"), {})
    if not isinstance(summary, dict):
        summary = {}
    return {
        "id": row.get("id"),
        "batch_id": row.get("batch_id"),
        "trust_id": row.get("trust_id"),
        "portfolio_id": row.get("portfolio_id"),
        "version_number": row.get("version_number"),
        "approved_by": row.get("approved_by"),
        "approved_at": row.get("approved_at"),
        "version_status": row.get("version_status"),
        "summary": summary,
    }


def _canonical_key(snapshot: dict[str, Any], fallback_row_number: Any) -> str:
    isin = str(snapshot.get("isin") or "").strip().upper()
    row_no = snapshot.get("source_row_number")
    if row_no in (None, ""):
        row_no = fallback_row_number
    if row_no in (None, ""):
        row_no = "na"
    return f"{isin}::{row_no}"


def _tracked_field_changes(left: dict[str, Any], right: dict[str, Any]) -> list[dict[str, Any]]:
    tracked = [
        "quantity",
        "face_value",
        "purchase_price",
        "credit_rating",
        "credit_rating_2",
        "coupon_rate",
        "maturity_date",
        "issuer_name",
    ]
    changes: list[dict[str, Any]] = []
    for field in tracked:
        if left.get(field) != right.get(field):
            changes.append(
                {
                    "field": field,
                    "left": left.get(field),
                    "right": right.get(field),
                }
            )
    return changes


async def _latest_backup_metadata() -> tuple[Optional[dict[str, Any]], Optional[Path]]:
    candidates: list[Path] = []
    env_dirs = [os.getenv("TRUST_BACKUP_DIR", ""), os.getenv("BACKUP_DIR", "")]
    for raw in env_dirs:
        if raw and raw.strip():
            candidates.append(Path(raw.strip()))

    base_dir = Path(__file__).resolve().parents[1]
    candidates.extend(
        [
            Path.cwd() / "backups",
            base_dir / "backups",
            base_dir.parent / "backups",
            base_dir.parent.parent / "backups",
        ]
    )

    seen: set[str] = set()
    metadata_files: list[Path] = []
    for directory in candidates:
        key = str(directory.resolve()) if directory.exists() else str(directory)
        if key in seen:
            continue
        seen.add(key)
        if not directory.exists() or not directory.is_dir():
            continue
        metadata_files.extend(directory.glob("trust_backup_*.json"))

    best_meta: Optional[dict[str, Any]] = None
    best_path: Optional[Path] = None
    best_time: Optional[datetime] = None

    for path in metadata_files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            created = _parse_iso_datetime(payload.get("created_at_utc"))
            if created is None:
                created = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if best_time is None or created > best_time:
                best_time = created
                best_meta = payload
                best_path = path
        except Exception:
            continue

    return best_meta, best_path


def _backup_health_payload(metadata: Optional[dict[str, Any]], metadata_path: Optional[Path]) -> dict[str, Any]:
    if not metadata:
        return {
            "status": "RED",
            "message": "No backup metadata was found.",
            "hours_since_last_backup": None,
            "checks": {
                "recent_backup": False,
                "encrypted": False,
                "checksum_present": False,
                "secondary_copy": False,
            },
        }

    created = _parse_iso_datetime(metadata.get("created_at_utc"))
    now = datetime.now(timezone.utc)
    age_hours = None
    if created:
        age_hours = round((now - created).total_seconds() / 3600.0, 2)

    encrypted = bool(metadata.get("encrypted"))
    checksum_present = bool(metadata.get("payload_sha256") and metadata.get("raw_sha256"))
    secondary_copy_path = metadata.get("secondary_copy")
    secondary_copy_ok = False
    if secondary_copy_path:
        try:
            secondary_copy_ok = Path(str(secondary_copy_path)).exists()
        except Exception:
            secondary_copy_ok = False

    recent_backup = age_hours is not None and age_hours <= 26.0
    stale_backup = age_hours is not None and age_hours > 72.0

    if recent_backup and encrypted and checksum_present:
        status = "GREEN"
        message = "Backup posture healthy."
    elif stale_backup:
        status = "RED"
        message = "Latest backup is older than 72 hours."
    else:
        status = "AMBER"
        message = "Backup exists but one or more readiness checks are degraded."

    return {
        "status": status,
        "message": message,
        "hours_since_last_backup": age_hours,
        "metadata_file": str(metadata_path) if metadata_path else None,
        "checks": {
            "recent_backup": recent_backup,
            "encrypted": encrypted,
            "checksum_present": checksum_present,
            "secondary_copy": secondary_copy_ok,
        },
    }


async def _summary_payload(trust_id: str, days: int) -> dict[str, Any]:
    trend = await _compliance_trend(days)
    as_of = datetime.now(timezone.utc)

    total_bonds_row = await _fetch_one_safe(
        "SELECT COUNT(DISTINCT isin) AS cnt FROM portfolio_holdings WHERE portfolio_id = :pid",
        {"pid": trust_id},
    )
    active_alert_rows = await _fetch_all_safe(
        """
        SELECT
            ua.id,
            ua.alert_date,
            ua.source_price_date,
            ua.isin,
            ua.alert_type,
            ua.severity,
            ua.title,
            ua.detail,
            ua.metric_value,
            ua.threshold_value,
            ua.is_resolved,
            ua.resolved_at,
            ua.resolved_by,
            b.issuer_name,
            b.issuer_type,
            b.credit_rating
        FROM unified_alerts ua
        LEFT JOIN universal_bond_master b ON b.isin = ua.isin
        WHERE ua.is_resolved = 0
        ORDER BY ua.alert_date DESC, ua.created_at DESC
        """,
    )
    active_alerts_row = {"cnt": len([row for row in active_alert_rows if _is_visible_open_alert(row, as_of=as_of)])}
    unresolved_incidents_row = await _fetch_one_safe(
        "SELECT COUNT(*) AS cnt FROM monitoring_events WHERE is_resolved = 0",
    )

    # Fallback when monitoring_events table is unavailable.
    if not unresolved_incidents_row:
        unresolved_incidents_row = active_alerts_row or {"cnt": 0}

    current_score = trend[-1]["compliance_score"] if trend else 0.0
    if current_score >= 90:
        posture = "GREEN"
    elif current_score >= 75:
        posture = "AMBER"
    else:
        posture = "RED"

    return {
        "trust_id": trust_id,
        "days": days,
        "summary_bar": {
            "total_bonds_monitored": _safe_int((total_bonds_row or {}).get("cnt")),
            "active_alerts": _safe_int((active_alerts_row or {}).get("cnt")),
            "unresolved_incidents": _safe_int((unresolved_incidents_row or {}).get("cnt")),
            "compliance_score": round(current_score, 2),
            "posture": posture,
        },
        "compliance_score_trend": trend,
        "generated_at": utcnow_iso(),
    }


@router.get("/summary")
async def compliance_dashboard_summary(
    trust_id: str = Query("default", description="Trust identifier"),
    days: int = Query(30, ge=7, le=365),
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    return await _summary_payload(trust_id=trust_id, days=days)


@router.get("/alerts")
async def compliance_dashboard_alerts(
    severity: Optional[str] = Query(default=None),
    rule: Optional[str] = Query(default=None),
    status: str = Query("OPEN", description="OPEN | RESOLVED | ALL"),
    limit: int = Query(50, ge=1, le=500),
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    filters = []
    params: dict[str, Any] = {"limit": limit}

    normalized_status = str(status or "OPEN").strip().upper()
    if normalized_status not in {"OPEN", "RESOLVED", "ALL"}:
        raise HTTPException(status_code=400, detail="status must be one of: OPEN, RESOLVED, ALL")

    if normalized_status == "OPEN":
        filters.append("is_resolved = 0")
    elif normalized_status == "RESOLVED":
        filters.append("is_resolved = 1")

    if severity:
        filters.append("UPPER(severity) = :severity")
        params["severity"] = str(severity).strip().upper()

    if rule:
        filters.append("UPPER(alert_type) LIKE :rule")
        params["rule"] = f"%{str(rule).strip().upper()}%"

    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    query_limit_sql = "" if normalized_status == "OPEN" else "LIMIT :limit"
    alert_rows = await _fetch_all_safe(
        f"""
        SELECT
            ua.id,
            ua.alert_date,
            ua.source_price_date,
            ua.isin,
            ua.alert_type,
            ua.severity,
            ua.title,
            ua.detail,
            ua.metric_value,
            ua.threshold_value,
            ua.is_resolved,
            ua.resolved_at,
            ua.resolved_by,
            b.issuer_name,
            b.issuer_type,
            b.credit_rating
        FROM unified_alerts ua
        LEFT JOIN universal_bond_master b ON b.isin = ua.isin
        {where_sql}
        ORDER BY
            CASE UPPER(severity)
                WHEN 'CRITICAL' THEN 1
                WHEN 'WARNING' THEN 2
                ELSE 3
            END,
            alert_date DESC,
            id DESC
        {query_limit_sql}
        """,
        params,
    )

    as_of = datetime.now(timezone.utc)
    if normalized_status == "OPEN":
        alert_rows = [row for row in alert_rows if _is_visible_open_alert(row, as_of=as_of)]

    alert_rows = alert_rows[:limit]

    audit_rows = await _fetch_all_safe(
        """
        SELECT checked_at, overall_status, violations_json
        FROM unified_compliance_audit
        ORDER BY checked_at DESC
        LIMIT 500
        """,
    )
    rule_breakdown = _summarize_rule_violations(audit_rows, rule_filter=rule or "")

    severity_counts: dict[str, int] = {}
    for row in alert_rows:
        sev = str(row.get("severity") or "INFO").upper()
        severity_counts[sev] = severity_counts.get(sev, 0) + 1

    return {
        "status_filter": normalized_status,
        "total_active_violations": len(alert_rows),
        "top_active_violations": [
            {
                "id": row.get("id"),
                "timestamp": row.get("alert_date"),
                "source_price_date": row.get("source_price_date"),
                "isin": row.get("isin"),
                "rule_key": row.get("alert_type"),
                "severity": row.get("severity"),
                "title": row.get("title"),
                "detail": (
                    f"{row.get('detail')} (price date {row.get('source_price_date') or row.get('alert_date')})"
                    if str(row.get("alert_type") or "").upper() in {"YIELD_SPREAD", GOVERNMENT_SPREAD_ALERT_TYPE}
                    else row.get("detail")
                ),
                "metric_value": row.get("metric_value"),
                "threshold_value": row.get("threshold_value"),
                "is_resolved": bool(row.get("is_resolved")),
                "resolved_at": row.get("resolved_at"),
                "resolved_by": row.get("resolved_by"),
            }
            for row in alert_rows
        ],
        "drilldown": {
            "by_rule": rule_breakdown[:100],
            "by_severity": severity_counts,
        },
        "generated_at": utcnow_iso(),
    }


@router.get("/audit-trail")
async def compliance_dashboard_audit_trail(
    user_id: Optional[str] = Query(default=None),
    action_type: Optional[str] = Query(default=None),
    entity_type: Optional[str] = Query(default=None),
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    where = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    parsed_start = _parse_iso_datetime(start_date) if start_date else None
    parsed_end = _parse_iso_datetime(end_date) if end_date else None
    if start_date and parsed_start is None:
        raise HTTPException(status_code=400, detail="start_date must be a valid ISO date/datetime")
    if end_date and parsed_end is None:
        raise HTTPException(status_code=400, detail="end_date must be a valid ISO date/datetime")
    if parsed_start and parsed_end and parsed_start > parsed_end:
        raise HTTPException(status_code=400, detail="start_date must be less than or equal to end_date")

    normalized_start = start_date
    normalized_end = end_date
    if parsed_start is not None:
        normalized_start = parsed_start.isoformat()
    if parsed_end is not None:
        if isinstance(end_date, str) and "T" not in end_date and " " not in end_date:
            normalized_end = (parsed_end + timedelta(days=1) - timedelta(microseconds=1)).isoformat()
        else:
            normalized_end = parsed_end.isoformat()

    if user_id:
        where.append("user_id = :user_id")
        params["user_id"] = str(user_id)
    if action_type:
        where.append("UPPER(action_type) = :action_type")
        params["action_type"] = str(action_type).strip().upper()
    if entity_type:
        where.append("UPPER(entity_type) = :entity_type")
        params["entity_type"] = str(entity_type).strip().upper()
    if start_date:
        where.append("timestamp_utc >= :start_date")
        params["start_date"] = str(normalized_start)
    if end_date:
        where.append("timestamp_utc <= :end_date")
        params["end_date"] = str(normalized_end)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    rows = await _fetch_all_safe(
        f"""
        SELECT
            id,
            user_id,
            username,
            user_role,
            timestamp_utc,
            action_type,
            entity_type,
            entity_id,
            old_value,
            new_value,
            affected_module,
            ip_address,
            session_id,
            severity,
            compliance_flag,
            description,
            metadata_json,
            checksum
        FROM institutional_audit_log
        {where_sql}
        ORDER BY timestamp_utc DESC, id DESC
        LIMIT :limit OFFSET :offset
        """,
        params,
    )

    total_row = await _fetch_one_safe(
        f"SELECT COUNT(*) AS cnt FROM institutional_audit_log {where_sql}",
        {k: v for k, v in params.items() if k not in {"limit", "offset"}},
    )

    entries = []
    for row in rows:
        entries.append(
            {
                "id": row.get("id"),
                "timestamp_utc": row.get("timestamp_utc"),
                "user_id": row.get("user_id"),
                "username": row.get("username"),
                "user_role": row.get("user_role"),
                "action_type": row.get("action_type"),
                "entity_type": row.get("entity_type"),
                "entity_id": row.get("entity_id"),
                "before": _json_loads(row.get("old_value"), row.get("old_value")),
                "after": _json_loads(row.get("new_value"), row.get("new_value")),
                "source_system": row.get("affected_module"),
                "ip_address": row.get("ip_address"),
                "session_id": row.get("session_id"),
                "severity": row.get("severity"),
                "compliance_flag": bool(row.get("compliance_flag")),
                "description": row.get("description"),
                "metadata": _json_loads(row.get("metadata_json"), row.get("metadata_json")),
                "checksum": row.get("checksum"),
            }
        )

    return {
        "total": _safe_int((total_row or {}).get("cnt")),
        "limit": limit,
        "offset": offset,
        "entries": entries,
        "generated_at": utcnow_iso(),
    }


@router.get("/audit-trail/{entry_id}")
async def compliance_dashboard_audit_entry(
    entry_id: int,
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    row = await _fetch_one_safe(
        """
        SELECT
            id,
            user_id,
            username,
            user_role,
            timestamp_utc,
            action_type,
            entity_type,
            entity_id,
            old_value,
            new_value,
            affected_module,
            ip_address,
            session_id,
            severity,
            compliance_flag,
            description,
            metadata_json,
            checksum
        FROM institutional_audit_log
        WHERE id = :id
        """,
        {"id": entry_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"Audit entry not found: {entry_id}")

    return {
        "entry": {
            "id": row.get("id"),
            "timestamp_utc": row.get("timestamp_utc"),
            "user_id": row.get("user_id"),
            "username": row.get("username"),
            "user_role": row.get("user_role"),
            "action_type": row.get("action_type"),
            "entity_type": row.get("entity_type"),
            "entity_id": row.get("entity_id"),
            "before": _json_loads(row.get("old_value"), row.get("old_value")),
            "after": _json_loads(row.get("new_value"), row.get("new_value")),
            "source_system": row.get("affected_module"),
            "ip_address": row.get("ip_address"),
            "session_id": row.get("session_id"),
            "severity": row.get("severity"),
            "compliance_flag": bool(row.get("compliance_flag")),
            "description": row.get("description"),
            "metadata": _json_loads(row.get("metadata_json"), row.get("metadata_json")),
            "checksum": row.get("checksum"),
        }
    }


@router.get("/explain-number")
async def compliance_dashboard_explain_number(
    metric: str = Query("weighted_ytm", description="Metric key to explain"),
    trust_id: str = Query("default", description="Trust identifier"),
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    trace_row = await _fetch_one_safe(
        """
        SELECT
            id,
            entity_type,
            entity_id,
            calculation_name,
            formula_version,
            input_values_snapshot,
            output_values_snapshot,
            source_snapshot_json,
            computed_timestamp,
            actor
        FROM calculation_audit_log
        WHERE entity_type = 'PORTFOLIO' AND entity_id = :entity_id
        ORDER BY computed_timestamp DESC, id DESC
        LIMIT 1
        """,
        {"entity_id": trust_id},
    )

    if not trace_row:
        raise HTTPException(status_code=404, detail="No calculation trace available for the selected portfolio")

    metric_key = str(metric or "").strip()
    input_snapshot = _json_loads(trace_row.get("input_values_snapshot"), {})
    output_snapshot = _json_loads(trace_row.get("output_values_snapshot"), {})
    source_snapshot = _json_loads(trace_row.get("source_snapshot_json"), {})

    output_summary = output_snapshot.get("summary") if isinstance(output_snapshot, dict) else {}
    if not isinstance(output_summary, dict):
        output_summary = {}

    metric_aliases = {
        "compliance_score": ["compliance_score", "score", "health_score"],
        "weighted_ytm": ["weighted_ytm"],
        "weighted_modified_duration": ["weighted_modified_duration", "weighted_duration"],
        "portfolio_rating": ["portfolio_rating", "credit_quality_rating"],
    }

    lookup_keys = [metric_key]
    lookup_keys.extend(metric_aliases.get(metric_key, []))

    metric_value = None
    for key in lookup_keys:
        if key in output_summary:
            metric_value = output_summary.get(key)
            break
        if key in output_snapshot:
            metric_value = output_snapshot.get(key)
            break

    formula_reference = {
        "weighted_ytm": "weighted_ytm = sum(position_value * lot_ytm) / sum(position_value)",
        "weighted_modified_duration": "weighted_modified_duration = sum(position_value * lot_modified_duration) / sum(position_value)",
        "compliance_score": "compliance_score = weighted average of PASS/WARN/FAIL status scores over monitoring window",
        "portfolio_rating": "portfolio_rating = rating label derived from weighted credit score",
    }

    lineage_rows = await _fetch_all_safe(
        """
        SELECT
            entity_type,
            entity_id,
            field_name,
            source_name,
            source_type,
            ingestion_method,
            source_reference,
            timestamp_ingested,
            timestamp_effective,
            confidence_level,
            created_at
        FROM data_provenance_ledger
        WHERE entity_type IN ('PORTFOLIO_POSITION', 'PORTFOLIO')
        ORDER BY created_at DESC
        LIMIT 50
        """,
    )

    return {
        "metric": metric_key,
        "metric_value": metric_value,
        "formula": formula_reference.get(metric_key, "See formula_version and input snapshots for exact computation path."),
        "calculation_trace": {
            "trace_id": trace_row.get("id"),
            "calculation_name": trace_row.get("calculation_name"),
            "formula_version": trace_row.get("formula_version"),
            "computed_timestamp": trace_row.get("computed_timestamp"),
            "actor": trace_row.get("actor"),
            "inputs": input_snapshot,
            "outputs": output_snapshot,
            "source_snapshot": source_snapshot,
        },
        "data_lineage": lineage_rows,
        "generated_at": utcnow_iso(),
    }


@router.get("/versions")
async def compliance_dashboard_versions(
    trust_id: str = Query("default", description="Trust identifier"),
    portfolio_id: Optional[str] = Query(default=None),
    limit: int = Query(25, ge=1, le=200),
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    pid = str(portfolio_id or trust_id)

    version_rows = await _fetch_all_safe(
        """
        SELECT
            id,
            batch_id,
            trust_id,
            portfolio_id,
            version_number,
            approved_by,
            approved_at,
            version_status,
            summary_json
        FROM portfolio_upload_versions
        WHERE trust_id = :trust_id AND portfolio_id = :portfolio_id
        ORDER BY version_number DESC
        LIMIT :limit
        """,
        {"trust_id": trust_id, "portfolio_id": pid, "limit": limit},
    )

    model_rows = await _fetch_all_safe(
        """
        SELECT
            id,
            timestamp_utc,
            user_id,
            username,
            action_type,
            description,
            old_value,
            new_value
        FROM institutional_audit_log
        WHERE action_type IN ('COMPLIANCE_RULE_CHANGE', 'COMPLIANCE_THRESHOLD_CHANGE')
        ORDER BY timestamp_utc DESC, id DESC
        LIMIT :limit
        """,
        {"limit": limit},
    )

    config_rows = await _fetch_all_safe(
        """
        SELECT config_key, config_value, updated_by, updated_at
        FROM system_config
        WHERE config_key LIKE 'compliance.%'
        ORDER BY updated_at DESC
        LIMIT :limit
        """,
        {"limit": limit},
    )

    return {
        "trust_id": trust_id,
        "portfolio_id": pid,
        "portfolio_versions": [_version_row_payload(row) for row in version_rows],
        "latest_portfolio_version": (version_rows[0].get("version_number") if version_rows else None),
        "compliance_model_versions": [
            {
                "id": row.get("id"),
                "timestamp_utc": row.get("timestamp_utc"),
                "user_id": row.get("user_id"),
                "username": row.get("username"),
                "action_type": row.get("action_type"),
                "description": row.get("description"),
                "old_value": _json_loads(row.get("old_value"), row.get("old_value")),
                "new_value": _json_loads(row.get("new_value"), row.get("new_value")),
            }
            for row in model_rows
        ],
        "active_compliance_config": [
            {
                "config_key": row.get("config_key"),
                "config_value": _json_loads(row.get("config_value"), row.get("config_value")),
                "updated_by": row.get("updated_by"),
                "updated_at": row.get("updated_at"),
            }
            for row in config_rows
        ],
        "generated_at": utcnow_iso(),
    }


async def _load_version_snapshot(
    trust_id: str,
    portfolio_id: str,
    version_number: int,
) -> dict[str, Any]:
    version_row = await _fetch_one_safe(
        """
        SELECT
            id,
            batch_id,
            trust_id,
            portfolio_id,
            version_number,
            approved_by,
            approved_at,
            version_status,
            summary_json
        FROM portfolio_upload_versions
        WHERE trust_id = :trust_id AND portfolio_id = :portfolio_id AND version_number = :version_number
        """,
        {
            "trust_id": trust_id,
            "portfolio_id": portfolio_id,
            "version_number": version_number,
        },
    )
    if not version_row:
        raise HTTPException(status_code=404, detail=f"Portfolio version not found: {version_number}")

    row_entries = await _fetch_all_safe(
        """
        SELECT row_number, canonical_json, validated_json, normalized_json, raw_json
        FROM portfolio_upload_rows
        WHERE batch_id = :batch_id
        ORDER BY row_number ASC
        """,
        {"batch_id": version_row.get("batch_id")},
    )

    snapshot: dict[str, dict[str, Any]] = {}
    for row in row_entries:
        payload = (
            _json_loads(row.get("canonical_json"), None)
            or _json_loads(row.get("validated_json"), None)
            or _json_loads(row.get("normalized_json"), None)
            or _json_loads(row.get("raw_json"), None)
        )
        if not isinstance(payload, dict):
            continue
        key = _canonical_key(payload, row.get("row_number"))
        snapshot[key] = payload

    return {
        "version": _version_row_payload(version_row),
        "rows": snapshot,
    }


@router.get("/versions/compare")
async def compliance_dashboard_compare_versions(
    left_version: int = Query(..., ge=1),
    right_version: int = Query(..., ge=1),
    trust_id: str = Query("default", description="Trust identifier"),
    portfolio_id: Optional[str] = Query(default=None),
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    if left_version == right_version:
        raise HTTPException(status_code=400, detail="left_version and right_version must differ")

    pid = str(portfolio_id or trust_id)
    left = await _load_version_snapshot(trust_id=trust_id, portfolio_id=pid, version_number=left_version)
    right = await _load_version_snapshot(trust_id=trust_id, portfolio_id=pid, version_number=right_version)

    left_rows: dict[str, dict[str, Any]] = left["rows"]
    right_rows: dict[str, dict[str, Any]] = right["rows"]

    left_keys = set(left_rows.keys())
    right_keys = set(right_rows.keys())

    added_keys = sorted(right_keys - left_keys)
    removed_keys = sorted(left_keys - right_keys)
    common_keys = sorted(left_keys & right_keys)

    modified_rows: list[dict[str, Any]] = []
    for key in common_keys:
        left_payload = left_rows[key]
        right_payload = right_rows[key]
        if json.dumps(left_payload, sort_keys=True, default=str) == json.dumps(right_payload, sort_keys=True, default=str):
            continue

        changes = _tracked_field_changes(left_payload, right_payload)
        if not changes:
            changes = [{"field": "_raw", "left": left_payload, "right": right_payload}]

        modified_rows.append(
            {
                "row_key": key,
                "isin": right_payload.get("isin") or left_payload.get("isin"),
                "changes": changes,
            }
        )

    def _basic_row_payload(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "isin": payload.get("isin"),
            "issuer_name": payload.get("issuer_name"),
            "quantity": payload.get("quantity"),
            "face_value": payload.get("face_value"),
            "purchase_price": payload.get("purchase_price"),
            "credit_rating": payload.get("credit_rating"),
            "maturity_date": payload.get("maturity_date"),
        }

    return {
        "trust_id": trust_id,
        "portfolio_id": pid,
        "left_version": left["version"],
        "right_version": right["version"],
        "diff_summary": {
            "added_rows": len(added_keys),
            "removed_rows": len(removed_keys),
            "modified_rows": len(modified_rows),
            "unchanged_rows": len(common_keys) - len(modified_rows),
        },
        "added": [
            {"row_key": key, "payload": _basic_row_payload(right_rows[key])}
            for key in added_keys[:200]
        ],
        "removed": [
            {"row_key": key, "payload": _basic_row_payload(left_rows[key])}
            for key in removed_keys[:200]
        ],
        "modified": modified_rows[:200],
        "generated_at": utcnow_iso(),
    }


@router.get("/access-control")
async def compliance_dashboard_access_control(
    trust_id: Optional[str] = Query(default=None, description="Optional portfolio/trust filter"),
    limit: int = Query(100, ge=1, le=1000),
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    role_matrix = [
        {
            "role": "SUPER_ADMIN",
            "permissions": [
                "full_system_control",
                "audit_integrity_verification",
                "rbac_admin",
                "export_all_reports",
            ],
        },
        {
            "role": "ADMIN",
            "permissions": [
                "user_management",
                "compliance_configuration",
                "portfolio_operations",
                "audit_view",
            ],
        },
        {
            "role": "PORTFOLIO",
            "permissions": [
                "portfolio_updates",
                "trade_execution",
                "recommendation_runs",
                "view_compliance",
            ],
        },
        {
            "role": "TRUSTEE",
            "permissions": [
                "read_only_governance",
                "view_reports",
                "view_compliance_dashboard",
                "export_trust_reports",
            ],
        },
        {
            "role": "COMPLIANCE",
            "permissions": [
                "view_compliance_dashboard",
                "resolve_alerts",
                "rule_review",
                "audit_view",
            ],
        },
        {
            "role": "AUDITOR",
            "permissions": [
                "read_only_all_modules",
                "view_audit_logs",
                "export_audit_reports",
            ],
        },
        {
            "role": "VIEWER",
            "permissions": [
                "dashboard_read",
                "portfolio_summary_read",
            ],
        },
    ]

    user_counts = await _fetch_all_safe(
        "SELECT role, COUNT(*) AS count FROM users GROUP BY role ORDER BY role"
    )

    where = ["action_type = 'PORTFOLIO_VIEW'", "entity_type = 'PORTFOLIO'"]
    params: dict[str, Any] = {"limit": limit}
    if trust_id:
        where.append("entity_id = :entity_id")
        params["entity_id"] = trust_id

    access_logs = await _fetch_all_safe(
        f"""
        SELECT
            id,
            user_id,
            username,
            user_role,
            timestamp_utc,
            entity_id,
            ip_address,
            session_id,
            description,
            metadata_json
        FROM institutional_audit_log
        WHERE {' AND '.join(where)}
        ORDER BY timestamp_utc DESC, id DESC
        LIMIT :limit
        """,
        params,
    )

    return {
        "role_matrix": role_matrix,
        "role_user_counts": user_counts,
        "portfolio_access_logs": [
            {
                "id": row.get("id"),
                "timestamp_utc": row.get("timestamp_utc"),
                "user_id": row.get("user_id"),
                "username": row.get("username"),
                "user_role": row.get("user_role"),
                "portfolio_id": row.get("entity_id"),
                "ip_address": row.get("ip_address"),
                "session_id": row.get("session_id"),
                "description": row.get("description"),
                "metadata": _json_loads(row.get("metadata_json"), row.get("metadata_json")),
            }
            for row in access_logs
        ],
        "generated_at": utcnow_iso(),
    }


async def _backup_status_payload() -> dict[str, Any]:
    metadata, metadata_path = await _latest_backup_metadata()
    health = _backup_health_payload(metadata, metadata_path)

    latest_self_audit = await _fetch_one_safe(
        """
        SELECT
            id,
            run_type,
            total_tests,
            passed,
            failed,
            warnings,
            overall_status,
            started_at,
            completed_at,
            run_by
        FROM self_audit_runs
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """,
    )

    return {
        "latest_backup": metadata,
        "backup_health": health,
        "latest_self_audit": latest_self_audit,
        "report_download_url": "/api/v1/compliance-dashboard/backup-status/report",
        "generated_at": utcnow_iso(),
    }


@router.get("/backup-status")
async def compliance_dashboard_backup_status(
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    return await _backup_status_payload()


@router.get("/backup-status/report")
async def compliance_dashboard_backup_report(
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    payload = await _backup_status_payload()
    report = {
        "report_type": "backup_disaster_readiness",
        "generated_at": utcnow_iso(),
        **payload,
    }
    filename = f"backup_readiness_report_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    body = json.dumps(report, indent=2, ensure_ascii=False).encode("utf-8")
    return StreamingResponse(
        io.BytesIO(body),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export-center")
async def compliance_dashboard_export_center(
    trust_id: Optional[str] = Query(default=None, description="Optional trust identifier for export links"),
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    trust_suffix = ""
    if trust_id and str(trust_id).strip():
        trust_suffix = f"&trust_id={quote_plus(str(trust_id).strip())}"

    return {
        "exports": [
            {
                "id": "audit_csv",
                "name": "Institutional Audit CSV",
                "format": "csv",
                "url": "/api/v1/reports/audit-export/csv",
                "description": "Full institutional audit timeline with before/after values.",
            },
            {
                "id": "trust_audit_csv",
                "name": "Trust Audit CSV",
                "format": "csv",
                "url": "/api/v1/reports/audit-export/trust/csv",
                "description": "Canonical trustee-facing audit export with hash chain evidence.",
            },
            {
                "id": "trust_audit_pdf",
                "name": "Trust Audit PDF",
                "format": "pdf",
                "url": "/api/v1/reports/audit-export/trust/pdf",
                "description": "Board-ready trust audit trail PDF.",
            },
            {
                "id": "compliance_dashboard_report_json",
                "name": "Compliance Dashboard Report (JSON)",
                "format": "json",
                "url": f"/api/v1/compliance-dashboard/export/audit-report?format=json{trust_suffix}",
                "description": "Timeline, exceptions, and user actions in machine-readable format.",
            },
            {
                "id": "compliance_dashboard_report_csv",
                "name": "Compliance Dashboard Report (CSV)",
                "format": "csv",
                "url": f"/api/v1/compliance-dashboard/export/audit-report?format=csv{trust_suffix}",
                "description": "Flattened timeline + exceptions export for spreadsheet workflows.",
            },
        ],
        "generated_at": utcnow_iso(),
    }


async def _audit_report_payload(days: int, trust_id: str) -> dict[str, Any]:
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
    summary = await _summary_payload(trust_id=trust_id, days=days)

    timeline_rows = await _fetch_all_safe(
        """
        SELECT
            timestamp_utc,
            user_id,
            username,
            user_role,
            action_type,
            entity_type,
            entity_id,
            severity,
            compliance_flag,
            description,
            affected_module
        FROM institutional_audit_log
        WHERE timestamp_utc >= :cutoff
        ORDER BY timestamp_utc DESC, id DESC
        LIMIT 2000
        """,
        {"cutoff": cutoff_iso},
    )

    exception_rows = await _fetch_all_safe(
        """
        SELECT
            id,
            alert_date,
            isin,
            alert_type,
            severity,
            title,
            detail,
            is_resolved,
            resolved_at,
            resolved_by
        FROM unified_alerts
        WHERE alert_date >= :cutoff
          AND (is_resolved = 0 OR UPPER(severity) = 'CRITICAL')
        ORDER BY alert_date DESC, id DESC
        LIMIT 2000
        """,
        {"cutoff": cutoff_iso[:10]},
    )

    action_rows = await _fetch_all_safe(
        """
        SELECT
            COALESCE(username, user_id, 'unknown') AS actor,
            action_type,
            COUNT(*) AS count
        FROM institutional_audit_log
        WHERE timestamp_utc >= :cutoff
        GROUP BY COALESCE(username, user_id, 'unknown'), action_type
        ORDER BY count DESC
        LIMIT 500
        """,
        {"cutoff": cutoff_iso},
    )

    return {
        "report_type": "compliance_dashboard_audit_report",
        "trust_id": trust_id,
        "period_days": days,
        "generated_at": utcnow_iso(),
        "summary": summary,
        "timeline": timeline_rows,
        "exceptions": exception_rows,
        "user_actions": action_rows,
    }


@router.get("/export/audit-report")
async def compliance_dashboard_export_audit_report(
    format: str = Query("json", description="json | csv"),
    days: int = Query(30, ge=1, le=365),
    trust_id: str = Query("default", description="Trust identifier"),
    user: dict = Depends(require_role(Roles.AUDITOR)),
):
    fmt = str(format or "json").strip().lower()
    if fmt not in {"json", "csv"}:
        raise HTTPException(status_code=400, detail="format must be one of: json, csv")

    payload = await _audit_report_payload(days=days, trust_id=trust_id)

    try:
        await AuditService.record(
            user=user,
            action=AuditAction.REPORT_EXPORTED,
            entity_type=EntityType.REPORT,
            entity_id="compliance_dashboard_audit_report",
            old_value=None,
            new_value={"format": fmt, "days": days, "trust_id": trust_id},
            affected_module="compliance_dashboard",
            severity=AuditSeverity.INFO,
            compliance_flag=True,
            description="Compliance dashboard audit report exported",
        )
    except Exception:
        pass

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    if fmt == "json":
        filename = f"compliance_dashboard_audit_report_{stamp}.json"
        body = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        return StreamingResponse(
            io.BytesIO(body),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    timeline = payload.get("timeline") or []
    exceptions = payload.get("exceptions") or []

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["section", "timestamp", "actor", "action_type", "entity_type", "entity_id", "severity", "detail"])

    for row in timeline:
        writer.writerow(
            [
                "timeline",
                row.get("timestamp_utc"),
                row.get("username") or row.get("user_id"),
                row.get("action_type"),
                row.get("entity_type"),
                row.get("entity_id"),
                row.get("severity"),
                row.get("description"),
            ]
        )

    for row in exceptions:
        writer.writerow(
            [
                "exception",
                row.get("alert_date"),
                row.get("resolved_by") or "system",
                row.get("alert_type"),
                "ALERT",
                row.get("isin"),
                row.get("severity"),
                row.get("detail") or row.get("title"),
            ]
        )

    filename = f"compliance_dashboard_audit_report_{stamp}.csv"
    body = output.getvalue().encode("utf-8")
    return StreamingResponse(
        io.BytesIO(body),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
