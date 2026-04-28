from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

DEFAULT_ISINS = ["INE084A01016", "INE540P07483", "INE020B08DL4"]
DEFAULT_HIGH_RISK_ISINS: list[str] = []
DEFAULT_EDGE_CASE_ISINS: list[str] = []


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _severity_rank(severity: str) -> int:
    return {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}.get(str(severity or "").upper(), 0)


def _max_severity(left: str, right: str) -> str:
    return left if _severity_rank(left) >= _severity_rank(right) else right


def _read_csv_env(name: str, fallback: list[str] | None = None) -> list[str]:
    raw = os.environ.get(name, "")
    if not raw.strip():
        return list(fallback or [])
    dedup: list[str] = []
    seen: set[str] = set()
    for item in raw.split(","):
        normalized = item.strip().upper()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        dedup.append(normalized)
    return dedup


def _get_isins() -> list[str]:
    return _read_csv_env("RUNTIME_MONITOR_ISINS", DEFAULT_ISINS)


def _rotate_pick(items: list[str], count: int, seed: int) -> list[str]:
    if not items or count <= 0:
        return []
    if count >= len(items):
        return list(items)
    start = seed % len(items)
    picked: list[str] = []
    for i in range(count):
        picked.append(items[(start + i) % len(items)])
    return picked


def _merge_ordered(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            if item in seen:
                continue
            seen.add(item)
            merged.append(item)
    return merged


def _resolve_monitor_isins(mode: str) -> list[str]:
    base_pool = _get_isins()
    high_risk = _read_csv_env("RUNTIME_MONITOR_HIGH_RISK_ISINS", DEFAULT_HIGH_RISK_ISINS)
    edge_cases = _read_csv_env("RUNTIME_MONITOR_EDGE_CASE_ISINS", DEFAULT_EDGE_CASE_ISINS)
    updated = _read_csv_env("RUNTIME_MONITOR_RECENTLY_UPDATED_ISINS", [])

    offset = int(os.environ.get("RUNTIME_MONITOR_ROTATION_OFFSET", "0") or "0")
    seed = int(time.time() // 600) + offset
    if mode == "deep":
        max_isins = int(os.environ.get("RUNTIME_MONITOR_MAX_ISINS", "12") or "12")
        deep = _merge_ordered(high_risk, edge_cases, updated, base_pool)
        return deep[: max_isins if max_isins > 0 else len(deep)]

    max_isins = int(os.environ.get("RUNTIME_MONITOR_MAX_ISINS", "6") or "6")
    light = _merge_ordered(
        _rotate_pick(high_risk, 2, seed),
        _rotate_pick(edge_cases, 2, seed + 1),
        _rotate_pick(updated or base_pool, 2, seed + 2),
        _rotate_pick(base_pool, 4, seed + 3),
    )
    return light[: max_isins if max_isins > 0 else len(light)]


def _get_path(payload: dict[str, Any], path: str) -> Any:
    node: Any = payload
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _login(base_url: str, username: str, password: str) -> str:
    response = requests.post(
        f"{base_url}/api/v1/auth/token",
        data={"username": username, "password": password},
        timeout=20,
    )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not token:
        raise RuntimeError("No access token returned by auth endpoint")
    return token


def _probe(base_url: str, token: str, isin: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"}
    timeout_sec = int(os.environ.get("RUNTIME_MONITOR_REQUEST_TIMEOUT_SECONDS", "45") or "45")
    retries = max(1, int(os.environ.get("RUNTIME_MONITOR_PROBE_RETRIES", "2") or "2"))
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        started = time.perf_counter()
        try:
            response = requests.get(
                f"{base_url}/api/v1/intelligence/analysis/{isin}",
                headers=headers,
                timeout=timeout_sec,
            )
            latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise RuntimeError(f"Non-object payload for {isin}")
            data["_monitor_latency_ms"] = latency_ms
            return data
        except Exception as exc:  # noqa: BLE001 - retry loop for transient runtime probe failures
            last_error = exc
            if attempt < retries:
                time.sleep(float(min(2, attempt)))

    raise RuntimeError(f"analysis probe failed for {isin}: {last_error}")


def _health_from_payload(payload: dict[str, Any]) -> float | None:
    control_health = _to_float(_get_path(payload, "control_layer.health_score"))
    if control_health is None:
        return None
    return max(0.0, min(100.0, control_health))


def _severity_from_payload(payload: dict[str, Any], latency_ms: float) -> tuple[str, list[str]]:
    reasons: list[str] = []
    severity = "NONE"

    if payload.get("schema_version") != "2.1.0":
        severity = _max_severity(severity, "HIGH")
        reasons.append("schema_version mismatch")
    if _get_path(payload, "validation.status") != "PASS":
        severity = _max_severity(severity, "HIGH")
        reasons.append("validation.status not PASS")
    if _get_path(payload, "data_quality.validation_engine.status") != "PASS":
        severity = _max_severity(severity, "HIGH")
        reasons.append("validation_engine.status not PASS")
    validation_violations = _get_path(payload, "validation.violations")
    if isinstance(validation_violations, list) and validation_violations:
        severity = _max_severity(severity, "HIGH")
        reasons.append("validation.violations non-empty")

    explainability = payload.get("explainability") if isinstance(payload.get("explainability"), dict) else None
    if explainability is None:
        severity = _max_severity(severity, "HIGH")
        reasons.append("explainability missing")
    else:
        if not str(explainability.get("headline") or "").strip():
            severity = _max_severity(severity, "HIGH")
            reasons.append("explainability headline missing")

    control_severity = str(_get_path(payload, "control_layer.severity") or "NONE").upper()
    if control_severity in {"LOW", "MEDIUM", "HIGH"}:
        severity = _max_severity(severity, control_severity)
        reasons.append(f"control_layer severity={control_severity}")
    else:
        severity = _max_severity(severity, "HIGH")
        reasons.append("control_layer severity missing or invalid")

    predictive_status = str(_get_path(payload, "control_layer.predictive_degradation.status") or "").upper()
    if predictive_status:
        reasons.append(f"predictive status={predictive_status}")
    else:
        severity = _max_severity(severity, "HIGH")
        reasons.append("predictive degradation status missing")

    health = _health_from_payload(payload)
    if health is None:
        severity = _max_severity(severity, "HIGH")
        reasons.append("control_layer health_score missing")
    elif health < 70.0:
        severity = _max_severity(severity, "HIGH")
        reasons.append(f"health_score critical ({health:.1f})")
    elif health < 90.0:
        severity = _max_severity(severity, "MEDIUM")
        reasons.append(f"health_score watch ({health:.1f})")

    if latency_ms >= 7000.0:
        severity = _max_severity(severity, "HIGH")
        reasons.append(f"latency spike ({latency_ms:.0f} ms)")
    elif latency_ms >= 4000.0:
        severity = _max_severity(severity, "MEDIUM")
        reasons.append(f"latency elevated ({latency_ms:.0f} ms)")

    if not reasons:
        reasons.append("no anomaly detected")
    return severity, reasons


def _load_control_state(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_control_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _compact_history(history: list[dict[str, Any]], max_items: int = 30) -> list[dict[str, Any]]:
    if len(history) <= max_items:
        return history
    return history[-max_items:]


def main() -> int:
    base_url = os.environ.get("GATEWAY_BASE_URL", "").strip()
    if not base_url:
        print("MONITOR BLOCKED: GATEWAY_BASE_URL is not configured")
        return 1

    username = os.environ.get("GATEWAY_ADMIN_USER", "")
    password = os.environ.get("GATEWAY_ADMIN_PASSWORD", "")
    if not username or not password:
        print("MONITOR BLOCKED: Gateway credentials are not configured")
        return 1

    mode = str(os.environ.get("RUNTIME_MONITOR_MODE", "light") or "light").strip().lower()
    if mode not in {"light", "deep"}:
        mode = "light"
    isins = _resolve_monitor_isins(mode)
    if not isins:
        print("MONITOR BLOCKED: no ISINs resolved for monitoring")
        return 1

    control_state_path = Path(
        os.environ.get(
            "RUNTIME_MONITOR_CONTROL_STATE_PATH",
            str(Path(__file__).resolve().parents[2] / "data" / "intelligence_control_state.json"),
        )
    )
    control_state = _load_control_state(control_state_path)
    existing_history = control_state.get("history") if isinstance(control_state.get("history"), dict) else {}
    next_history: dict[str, list[dict[str, Any]]] = {}
    bond_actions: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    fail_on_high = str(os.environ.get("RUNTIME_MONITOR_FAIL_ON_HIGH", "true") or "true").strip().lower() == "true"

    try:
        token = _login(base_url, username, password)
        for isin in isins:
            payload = _probe(base_url, token, isin)
            latency_ms = _to_float(payload.get("_monitor_latency_ms")) or 0.0
            severity, reasons = _severity_from_payload(payload, latency_ms)
            health = _health_from_payload(payload)
            result = {
                "isin": isin,
                "severity": severity,
                "health_score": health,
                "latency_ms": latency_ms,
                "reasons": reasons,
                "checked_at": _utc_now_iso(),
            }
            results.append(result)

            prior = existing_history.get(isin) if isinstance(existing_history, dict) else None
            history = prior if isinstance(prior, list) else []
            history.append(
                {
                    "checked_at": result["checked_at"],
                    "severity": severity,
                    "health_score": health,
                    "latency_ms": latency_ms,
                }
            )
            next_history[isin] = _compact_history(history)

            if severity in {"MEDIUM", "HIGH"}:
                ttl_minutes = 45 if severity == "HIGH" else 20
                bond_actions[isin] = {
                    "severity": severity,
                    "reason": "; ".join(reasons[:3]),
                    "source": "runtime-monitor",
                    "mode": mode,
                    "updated_at": result["checked_at"],
                    "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat(),
                }
    except Exception as exc:  # noqa: BLE001
        print(f"MONITOR BLOCKED: probe execution failed: {exc}")
        return 1

    # Keep non-expired legacy actions for bonds not checked in this cycle.
    existing_actions = control_state.get("bond_actions") if isinstance(control_state.get("bond_actions"), dict) else {}
    now = datetime.now(timezone.utc)
    for isin, action in existing_actions.items():
        if isin in bond_actions or not isinstance(action, dict):
            continue
        expires = action.get("expires_at")
        try:
            expiry = datetime.fromisoformat(str(expires).replace("Z", "+00:00")) if expires else None
        except ValueError:
            expiry = None
        if expiry is None or expiry > now:
            bond_actions[isin] = action

    high_count = len([r for r in results if r["severity"] == "HIGH"])
    medium_count = len([r for r in results if r["severity"] == "MEDIUM"])
    low_count = len([r for r in results if r["severity"] == "LOW"])
    health_values = [
        _to_float(r.get("health_score"))
        for r in results
        if _to_float(r.get("health_score")) is not None
    ]
    average_health = round(sum(health_values) / len(health_values), 2) if health_values else None

    control_snapshot = {
        "updated_at": _utc_now_iso(),
        "mode": mode,
        "isins_checked": isins,
        "summary": {
            "total": len(results),
            "high": high_count,
            "medium": medium_count,
            "low": low_count,
            "average_health_score": average_health,
        },
        "bond_actions": bond_actions,
        "results": results,
        "history": next_history,
    }
    _save_control_state(control_state_path, control_snapshot)

    print(f"MONITOR MODE: {mode.upper()}")
    print(f"MONITOR ISINS: {', '.join(isins)}")
    for row in results:
        health_text = "n/a" if row["health_score"] is None else f"{row['health_score']:.1f}"
        print(
            f"- {row['isin']}: severity={row['severity']} health={health_text} latency_ms={row['latency_ms']:.0f} reasons={'; '.join(row['reasons'][:2])}"
        )
    print(f"MONITOR CONTROL STATE: {control_state_path}")

    if high_count and fail_on_high:
        print("MONITOR ALERT: high severity anomalies detected")
        return 1

    if high_count or medium_count:
        print("MONITOR WATCH: anomalies detected and control state updated")
    else:
        print("MONITOR PASS: runtime probes healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
