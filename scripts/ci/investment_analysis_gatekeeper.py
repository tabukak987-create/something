from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests


CRITICAL_FIELD_STATUS_KEYS = {
    "ytm",
    "coupon_rate",
    "maturity_date",
    "credit_rating",
    "market_price",
    "spread_over_gsec_bps",
    "liquidity_score",
    "entry_ytm",
    "portfolio_market_value",
    "duration",
    "scenario_analysis",
}

GAP_ALIASES = {
    "ytm": {"current ytm"},
    "coupon_rate": {"coupon", "coupon rate"},
    "maturity_date": {"maturity", "maturity date"},
    "credit_rating": {"latest credit rating", "credit rating"},
    "market_price": {"current market price"},
    "spread_over_gsec_bps": {"yield vs benchmark spread (g-sec)"},
    "liquidity_score": {"liquidity score"},
    "entry_ytm": {"entry ytm"},
    "duration": {"duration"},
    "portfolio_market_value": {"portfolio aum (sanity-checked)"},
    "scenario_analysis": {"scenario analysis base inputs"},
}

FIELD_VALUE_PATHS = {
    "ytm": [
        "bond_snapshot.yield.ytm",
        "portfolio_impact.before.weighted_ytm",
        "portfolio_impact.after.weighted_ytm",
        "portfolio_impact.delta.weighted_ytm_bps",
    ],
    "coupon_rate": ["bond_snapshot.coupon_rate"],
    "maturity_date": ["bond_snapshot.maturity_date"],
    "credit_rating": ["bond_snapshot.credit_rating.latest"],
    "market_price": [
        "market_intelligence.current_price",
    ],
    "spread_over_gsec_bps": [
        "market_intelligence.spread_over_gsec_bps",
    ],
    "liquidity_score": [
        "market_intelligence.liquidity_score",
    ],
    "entry_ytm": [
        "market_intelligence.entry_ytm",
    ],
    "portfolio_market_value": [
        "portfolio_impact.before.portfolio_market_value",
        "portfolio_impact.after.portfolio_market_value",
    ],
    "duration": [
        "bond_snapshot.duration",
        "portfolio_impact.before.weighted_duration",
        "portfolio_impact.after.weighted_duration",
        "portfolio_impact.delta.weighted_duration",
    ],
    "scenario_analysis": [
        "risk_engine.scenario_analysis.rate_up_pnl",
        "risk_engine.scenario_analysis.rate_down_pnl",
    ],
}

DEFAULT_ISINS = ["INE084A01016"]


@dataclass
class Violations:
    entries: list[str] = field(default_factory=list)

    def add(self, stage: str, isin: str, message: str) -> None:
        self.entries.append(f"[{stage}] {isin}: {message}")

    def blocked(self) -> bool:
        return bool(self.entries)


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        candidate = value.strip().replace(",", "")
        if not candidate:
            return False
        try:
            float(candidate)
            return True
        except ValueError:
            return False
    return False


def _get_path(payload: dict[str, Any], path: str) -> Any:
    node: Any = payload
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _to_text_or_placeholder(field_key: str, value: Any, missing: bool) -> str:
    if missing:
        return "Data Missing"
    if value is None or (isinstance(value, str) and not value.strip()):
        return "Not Available"
    if field_key == "ytm" and _is_number(value):
        return f"{float(value):.2f}%"
    if field_key == "portfolio_market_value" and _is_number(value):
        return f"INR {float(value):,.2f}"
    return str(value)


def _get_isin_list() -> list[str]:
    raw = os.environ.get("CI_GATEKEEPER_ISINS", "")
    max_count = int(os.environ.get("CI_GATEKEEPER_MAX_ISINS", "1"))
    if not raw.strip():
        return DEFAULT_ISINS[:max(1, max_count)]
    isins = [x.strip() for x in raw.split(",") if x.strip()]
    return isins[:max(1, max_count)]


def _find_open_port(start: int = 9010, end: int = 9100) -> int:
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("No free local port found for gatekeeper probe server")


def _start_server(repo_root: Path, port: int) -> subprocess.Popen[str]:
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "backend.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    # Keep probe startup deterministic for CI/local checks that enforce strong JWT secrets.
    env.setdefault("JWT_SECRET_KEY", "ci_gatekeeper_probe_secret_key_2026_strong_value")
    env.setdefault("REDIS_REQUIRED", "false")
    return subprocess.Popen(
        cmd,
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _wait_for_server(base_url: str, timeout_sec: int = 45) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            response = requests.get(f"{base_url}/health", timeout=3)
            if response.status_code in {200, 404}:
                return True
        except requests.RequestException:
            pass
        time.sleep(1)
    return False


def _stop_server(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _read_process_tail(proc: subprocess.Popen[str], max_lines: int = 60) -> str:
    if not proc.stdout:
        return ""
    lines = proc.stdout.readlines()
    if len(lines) <= max_lines:
        return "".join(lines)
    return "".join(lines[-max_lines:])


def _get_token(base_url: str, username: str, password: str) -> str:
    response = requests.post(
        f"{base_url}/api/v1/auth/token",
        data={"username": username, "password": password},
        timeout=30,
    )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not token:
        raise RuntimeError("Auth succeeded but no access token was returned")
    return token


def _validate_contract(payload: dict[str, Any], isin: str, violations: Violations) -> None:
    schema_version = payload.get("schema_version")
    if schema_version != "2.1.0":
        violations.add("Stage 2 - API Contract", isin, f"schema_version is {schema_version}, expected 2.1.0")

    validation = payload.get("validation")
    if not isinstance(validation, dict):
        violations.add("Stage 2 - API Contract", isin, "validation object is missing")
    else:
        if validation.get("status") != "PASS":
            violations.add(
                "Stage 2 - API Contract",
                isin,
                f"validation.status is {validation.get('status')}, expected PASS",
            )
        vlist = validation.get("violations")
        if not isinstance(vlist, list):
            violations.add("Stage 2 - API Contract", isin, "validation.violations is not an array")
        elif vlist:
            violations.add("Stage 2 - API Contract", isin, f"validation.violations is not empty: {vlist}")

    validation_engine = _get_path(payload, "data_quality.validation_engine")
    if not isinstance(validation_engine, dict):
        violations.add("Stage 2 - API Contract", isin, "data_quality.validation_engine object is missing")
    elif validation_engine.get("status") != "PASS":
        violations.add(
            "Stage 2 - API Contract",
            isin,
            f"data_quality.validation_engine.status is {validation_engine.get('status')}, expected PASS",
        )

    field_status = _get_path(payload, "data_quality.field_status")
    if not isinstance(field_status, dict):
        violations.add("Stage 2 - API Contract", isin, "data_quality.field_status is missing")
        return

    missing_keys = sorted(CRITICAL_FIELD_STATUS_KEYS - set(field_status.keys()))
    if missing_keys:
        violations.add("Stage 2 - API Contract", isin, f"field_status missing critical keys: {missing_keys}")

    explainability = payload.get("explainability")
    if not isinstance(explainability, dict):
        violations.add("Stage 2 - API Contract", isin, "explainability object is missing")
    else:
        if not str(explainability.get("headline") or "").strip():
            violations.add("Stage 2 - API Contract", isin, "explainability.headline is missing")
        if not str(explainability.get("generated_at") or "").strip():
            violations.add("Stage 2 - API Contract", isin, "explainability.generated_at is missing")

    control_layer = payload.get("control_layer")
    if not isinstance(control_layer, dict):
        violations.add("Stage 2 - API Contract", isin, "control_layer object is missing")
        return

    control_severity = str(control_layer.get("severity") or "").upper()
    if control_severity not in {"NONE", "LOW", "MEDIUM", "HIGH"}:
        violations.add("Stage 2 - API Contract", isin, f"control_layer.severity is invalid: {control_severity or 'EMPTY'}")

    health_score = control_layer.get("health_score")
    if not _is_number(health_score):
        violations.add("Stage 2 - API Contract", isin, "control_layer.health_score is missing or non-numeric")

    predictive = control_layer.get("predictive_degradation")
    if not isinstance(predictive, dict):
        violations.add("Stage 2 - API Contract", isin, "control_layer.predictive_degradation is missing")
    else:
        if not str(predictive.get("status") or "").strip():
            violations.add("Stage 2 - API Contract", isin, "control_layer.predictive_degradation.status is missing")

    if control_severity == "HIGH":
        analysis_state = payload.get("analysis_state") if isinstance(payload.get("analysis_state"), dict) else {}
        if str(analysis_state.get("status") or "").upper() != "BLOCKED":
            violations.add(
                "Stage 2 - API Contract",
                isin,
                "control_layer.severity is HIGH but analysis_state.status is not BLOCKED",
            )


def _validate_data_consistency(payload: dict[str, Any], isin: str, violations: Violations) -> None:
    field_status = _get_path(payload, "data_quality.field_status")
    if not isinstance(field_status, dict):
        violations.add("Stage 3 - Data Consistency", isin, "data_quality.field_status is missing")
        return

    gap_block = payload.get("gaps") or {}
    gap_values = []
    if isinstance(gap_block, dict):
        gap_values.extend(gap_block.get("critical") or [])
        gap_values.extend(gap_block.get("important") or [])
        gap_values.extend(gap_block.get("advanced") or [])
    gap_set = {str(x).strip().lower() for x in gap_values if str(x).strip()}

    for key, aliases in GAP_ALIASES.items():
        status_info = field_status.get(key) if isinstance(field_status.get(key), dict) else {}
        status = str(status_info.get("status", "")).strip().lower()
        status_missing = status == "missing"
        gap_missing = bool(gap_set.intersection(aliases))
        if status_missing and not gap_missing:
            violations.add(
                "Stage 3 - Data Consistency",
                isin,
                f"{key} is marked missing in field_status but absent from gap register",
            )
        if gap_missing and not status_missing:
            violations.add(
                "Stage 3 - Data Consistency",
                isin,
                f"{key} appears in gap register but field_status is not missing",
            )

    for key, paths in FIELD_VALUE_PATHS.items():
        status_info = field_status.get(key) if isinstance(field_status.get(key), dict) else {}
        status = str(status_info.get("status", "")).strip().lower()
        status_missing = status == "missing"
        gap_missing = bool(gap_set.intersection(GAP_ALIASES.get(key, {key})))
        must_be_masked = status_missing or gap_missing

        if not must_be_masked:
            continue

        for path in paths:
            value = _get_path(payload, path)
            if _is_number(value):
                violations.add(
                    "Stage 3 - Data Consistency",
                    isin,
                    f"{key} marked missing but numeric value found at {path}: {value}",
                )


def _validate_calculations(payload: dict[str, Any], isin: str, violations: Violations) -> None:
    scenario = _get_path(payload, "risk_engine.scenario_analysis")
    if isinstance(scenario, dict):
        available = bool(scenario.get("is_available") is True)
        up_pnl = scenario.get("rate_up_pnl")
        down_pnl = scenario.get("rate_down_pnl")
        if available and (not _is_number(up_pnl) or not _is_number(down_pnl)):
            violations.add(
                "Stage 3 - Data Consistency",
                isin,
                "scenario_analysis.is_available is true but numeric outputs are incomplete",
            )
        if not available and (_is_number(up_pnl) or _is_number(down_pnl)):
            violations.add(
                "Stage 3 - Data Consistency",
                isin,
                "scenario_analysis.is_available is false but numeric outputs are populated",
            )

    aum_validation = _get_path(payload, "portfolio_impact.validation") or {}
    before_value = _get_path(payload, "portfolio_impact.before.portfolio_market_value")
    after_value = _get_path(payload, "portfolio_impact.after.portfolio_market_value")
    if isinstance(aum_validation, dict):
        is_reasonable = aum_validation.get("is_reasonable")
        if is_reasonable is False and (_is_number(before_value) or _is_number(after_value)):
            violations.add(
                "Stage 3 - Data Consistency",
                isin,
                "AUM flagged unreasonable but market value outputs remain numeric",
            )


def _validate_ui_simulation(payload: dict[str, Any], isin: str, violations: Violations) -> None:
    field_status = _get_path(payload, "data_quality.field_status")
    if not isinstance(field_status, dict):
        violations.add("Stage 4 - UI Rendering", isin, "data_quality.field_status missing for UI simulation")
        return

    allowed_placeholders = {"Data Missing", "Not Available"}

    ytm_status = _get_path(payload, "data_quality.field_status.ytm.status")
    ytm_missing = str(ytm_status).strip().lower() == "missing"
    ytm_text = _to_text_or_placeholder("ytm", _get_path(payload, "bond_snapshot.yield.ytm"), ytm_missing)
    if ytm_missing and ytm_text == "0.00%":
        violations.add("Stage 4 - UI Rendering", isin, "missing YTM renders as 0.00% instead of placeholder")

    aum_reasonable = _get_path(payload, "portfolio_impact.validation.is_reasonable")
    before_aum_missing = str(_get_path(payload, "data_quality.field_status.portfolio_market_value.status")).lower() == "missing"
    aum_text = _to_text_or_placeholder(
        "portfolio_market_value",
        _get_path(payload, "portfolio_impact.before.portfolio_market_value"),
        before_aum_missing,
    )
    if aum_reasonable is False and aum_text not in allowed_placeholders:
        violations.add(
            "Stage 4 - UI Rendering",
            isin,
            "AUM is unreasonable but UI simulation does not produce a placeholder",
        )

    im_blocks = [
        _get_path(payload, "im_intelligence.issuer_intelligence"),
        _get_path(payload, "im_intelligence.analyst_takeaways"),
        _get_path(payload, "im_intelligence.trade_recommendation"),
    ]
    for block in im_blocks:
        if not isinstance(block, list):
            continue
        for idx, item in enumerate(block):
            if isinstance(item, (dict, list)):
                violations.add(
                    "Stage 4 - UI Rendering",
                    isin,
                    f"raw object found in IM output at index {idx}",
                )


def _probe_payload(base_url: str, token: str, isin: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"}
    timeout = int(os.environ.get("CI_GATEKEEPER_TIMEOUT_SECONDS", "120"))
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = requests.get(
                f"{base_url}/api/v1/intelligence/analysis/{isin}",
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise RuntimeError(f"Endpoint returned a non-object payload for {isin}")
            return data
        except Exception as exc:  # noqa: BLE001 - explicit retry loop
            last_error = exc
            if attempt < 3:
                time.sleep(2 * attempt)
    raise RuntimeError(f"analysis endpoint probe failed for {isin}: {last_error}")


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    username = os.environ.get("GATEWAY_ADMIN_USER", "admin")
    password = os.environ.get("GATEWAY_ADMIN_PASSWORD", "Admin@2026!Gateway")
    isins = _get_isin_list()

    violations = Violations()
    port = _find_open_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = _start_server(repo_root, port)

    try:
        if not _wait_for_server(base_url):
            violations.add("Boot", "N/A", "Local API server did not become healthy in time")
            log_tail = _read_process_tail(proc)
            if log_tail:
                print("---- Uvicorn Log Tail ----")
                print(log_tail)
            return 1

        token = _get_token(base_url, username, password)

        for isin in isins:
            payload = _probe_payload(base_url, token, isin)
            _validate_contract(payload, isin, violations)
            _validate_data_consistency(payload, isin, violations)
            _validate_calculations(payload, isin, violations)
            _validate_ui_simulation(payload, isin, violations)

    except Exception as exc:  # noqa: BLE001 - fail-fast gatekeeper behavior
        print(f"DEPLOYMENT BLOCKED: gatekeeper execution error: {exc}")
        return 1
    finally:
        _stop_server(proc)

    report = {
        "status": "BLOCK" if violations.blocked() else "PASS",
        "checked_isins": isins,
        "violation_count": len(violations.entries),
        "violations": violations.entries,
    }

    report_path = os.environ.get("GATEKEEPER_REPORT_PATH", "")
    if report_path.strip():
        Path(report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")

    if violations.blocked():
        print("DEPLOYMENT BLOCKED")
        print("Validation violations:")
        for item in violations.entries:
            print(f"- {item}")
        return 1

    print("DEPLOYMENT GATEKEEPER PASS")
    print(f"Validated {len(isins)} ISIN payload(s) with zero violations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
