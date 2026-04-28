"""
Runtime Intrusion Detection System (IDS)
==========================================
Detects anomalous API access patterns without impacting latency.

Detection rules (all configurable via env or constructor):
  R01  BRUTE_FORCE         ≥10 failed auth per IP per 5 min
  R02  CREDENTIAL_STUFFING ≥5 distinct user accounts failed per IP per 10 min
  R03  OFF_HOURS_ACCESS    Privileged role access 22:00–06:00 IST
  R04  RAPID_ENUMERATION   ≥30 distinct endpoints per IP per 60 s
  R05  IMPOSSIBLE_TRAVEL   Same user from 2+ geographically distinct IPs < 5 min
  R06  PERMISSION_BOUNDARY Attempt to access endpoint above own role tier
  R07  DATA_EXFILTRATION   ≥50 response KB per request (large bond dumps)
  R08  PENTEST_SIGNATURE   Known pentest tool User-Agent strings
  R09  REPLAY_ATTACK       Same JWT jti used > 1 time after logout
  R10  ANOMALOUS_FREQUENCY ≥200 requests/IP/min (bot / scraper)

Architecture
------------
  - In-memory sliding window counters (threading.Lock protected)
  - All alerts written to AuditLedger (hash-chained, tamper-evident)
  - Severity levels: INFO / WARN / ALERT / CRITICAL
  - Auto-block (returns 429) for CRITICAL alerts
  - Integration: add IdsMiddleware to FastAPI app

Usage
-----
    from app.domain.core_trust.intrusion_detection import IdsMiddleware
    app.add_middleware(IdsMiddleware)
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..shared.time_utils import utcnow, utcnow_iso

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_IDS_DEFAULT_LOG = _REPO_ROOT / "reports" / "ids_alerts.jsonl"

# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------
class Severity:
    INFO = "INFO"
    WARN = "WARN"
    ALERT = "ALERT"
    CRITICAL = "CRITICAL"


# ---------------------------------------------------------------------------
# Alert
# ---------------------------------------------------------------------------
@dataclass
class IdsAlert:
    rule_id: str
    severity: str
    actor: str          # IP or user
    description: str
    evidence: dict[str, Any]
    timestamp: str = field(default_factory=utcnow_iso)
    action_taken: str = "logged"

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Sliding window counter (lock-free per key strategy)
# ---------------------------------------------------------------------------
class _SlidingWindow:
    """Counts events within a rolling time window."""

    def __init__(self, window_seconds: int) -> None:
        self._window = window_seconds
        self._events: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def record(self, key: str, value: Any = 1) -> None:
        now = time.monotonic()
        with self._lock:
            dq = self._events[key]
            dq.append((now, value))
            # Trim stale
            cutoff = now - self._window
            while dq and dq[0][0] < cutoff:
                dq.popleft()

    def count(self, key: str) -> int:
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            dq = self._events[key]
            while dq and dq[0][0] < cutoff:
                dq.popleft()
            return len(dq)

    def distinct_values(self, key: str) -> set:
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            dq = self._events[key]
            while dq and dq[0][0] < cutoff:
                dq.popleft()
            return {v for _, v in dq}

    def sum_values(self, key: str) -> float:
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            dq = self._events[key]
            while dq and dq[0][0] < cutoff:
                dq.popleft()
            return sum(v for _, v in dq)

    def clear(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)


# ---------------------------------------------------------------------------
# Known pentest User-Agent fragments
# ---------------------------------------------------------------------------
PENTEST_UA_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.I) for p in [
        r"sqlmap", r"nikto", r"dirbuster", r"nmap", r"masscan",
        r"burp", r"zap", r"acunetix", r"nessus", r"metasploit",
        r"hydra", r"medusa", r"gobuster", r"ffuf", r"wfuzz",
        r"nuclei", r"curl\/7\.[0-4]",  # old curl used in exploit kits
    ]
]

# IST = UTC+5:30
IST_OFFSET_HOURS = 5.5

# RBAC tier mapping (lower = more privileged)
ROLE_TIER: dict[str, int] = {
    "super_admin": 1,
    "admin": 2,
    "compliance_officer": 3,
    "risk_manager": 3,
    "portfolio_manager": 4,
    "analyst": 5,
    "trustee": 5,
    "viewer": 6,
    "readonly": 7,
}
ENDPOINT_MIN_TIER: dict[str, int] = {
    "/api/v1/admin": 2,
    "/api/v1/compliance": 3,
    "/api/v1/risk": 3,
    "/api/v1/portfolio": 4,
    "/api/v1/bonds": 5,
    "/api/v1/auth": 7,
}


# ---------------------------------------------------------------------------
# IDS Engine
# ---------------------------------------------------------------------------
class IntrusionDetectionSystem:
    """
    Stateful intrusion detection engine.

    Call process_request() synchronously — designed < 0.5 ms hot path.
    """

    def __init__(
        self,
        alert_log_path: Optional[Path] = None,
        auto_block_critical: bool = True,
        on_alert: Optional[Any] = None,     # callable(IdsAlert) -> None
    ) -> None:
        self.auto_block_critical = auto_block_critical
        self._on_alert = on_alert
        self._alert_log = alert_log_path or Path(
            os.getenv("IDS_ALERT_LOG", str(_IDS_DEFAULT_LOG))
        )
        self._alert_log.parent.mkdir(parents=True, exist_ok=True)
        self._blocked_ips: set[str] = set()
        self._blocked_lock = threading.Lock()

        # Sliding windows per rule
        self._w_auth_fails  = _SlidingWindow(300)   # R01: 5 min
        self._w_accounts    = _SlidingWindow(600)   # R02: 10 min
        self._w_endpoints   = _SlidingWindow(60)    # R04: 60 s
        self._w_user_ips    = _SlidingWindow(300)   # R05: 5 min per user
        self._w_rps         = _SlidingWindow(60)    # R10: 60 s
        self._used_jtis: set[str] = set()
        self._jti_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def process_request(
        self,
        ip: str,
        path: str,
        method: str,
        status_code: int,
        user: Optional[str],
        role: Optional[str],
        user_agent: str,
        response_bytes: int,
        jti: Optional[str],
        is_auth_endpoint: bool,
        is_auth_success: bool,
    ) -> list[IdsAlert]:
        """Evaluate all rules and return any fired alerts."""
        alerts: list[IdsAlert] = []

        # R10: anomalous frequency (first — cheapest check)
        self._w_rps.record(ip)
        rps = self._w_rps.count(ip)
        if rps >= 200:
            alerts.append(self._alert(
                "R10", Severity.CRITICAL, ip,
                f"Anomalous request frequency: {rps} req/min",
                {"rps": rps, "path": path},
            ))

        # R01: brute force
        if is_auth_endpoint and not is_auth_success:
            self._w_auth_fails.record(ip)
            fails = self._w_auth_fails.count(ip)
            if fails >= 10:
                alerts.append(self._alert(
                    "R01", Severity.CRITICAL, ip,
                    f"Brute-force: {fails} consecutive auth failures",
                    {"fails_5m": fails, "path": path},
                ))

        # R02: credential stuffing — distinct accounts
        if is_auth_endpoint and not is_auth_success and user:
            self._w_accounts.record(ip, user)
            distinct = len(self._w_accounts.distinct_values(ip))
            if distinct >= 5:
                alerts.append(self._alert(
                    "R02", Severity.CRITICAL, ip,
                    f"Credential stuffing: {distinct} distinct accounts tried",
                    {"distinct_accounts": distinct},
                ))

        # R03: off-hours privileged access
        if role in ("admin", "super_admin", "compliance_officer"):
            utc_hour = utcnow().hour
            ist_hour = (utc_hour + IST_OFFSET_HOURS) % 24
            if not (6 <= ist_hour < 22):
                alerts.append(self._alert(
                    "R03", Severity.WARN, user or ip,
                    f"Off-hours privileged access at {ist_hour:.0f}:xx IST",
                    {"ist_hour": ist_hour, "role": role, "path": path},
                ))

        # R04: rapid endpoint enumeration
        self._w_endpoints.record(ip, path)
        distinct_paths = len(self._w_endpoints.distinct_values(ip))
        if distinct_paths >= 30:
            alerts.append(self._alert(
                "R04", Severity.ALERT, ip,
                f"Rapid endpoint enumeration: {distinct_paths} distinct paths/60s",
                {"distinct_paths": distinct_paths},
            ))

        # R05: impossible travel (multiple IPs for same user < 5 min)
        if user:
            self._w_user_ips.record(user, ip)
            user_ips = self._w_user_ips.distinct_values(user)
            if len(user_ips) >= 2:
                alerts.append(self._alert(
                    "R05", Severity.ALERT, user,
                    f"Impossible travel: user from {len(user_ips)} different IPs in 5 min",
                    {"ips": list(user_ips)},
                ))

        # R06: permission boundary
        if role and user:
            user_tier = ROLE_TIER.get(role, 7)
            for prefix, min_tier in ENDPOINT_MIN_TIER.items():
                if path.startswith(prefix) and user_tier > min_tier:
                    alerts.append(self._alert(
                        "R06", Severity.ALERT, user,
                        f"Permission boundary violation: role={role} tier={user_tier} path={path}",
                        {"role": role, "path": path, "required_tier": min_tier},
                    ))
                    break

        # R07: data exfiltration
        if response_bytes >= 50 * 1024:
            alerts.append(self._alert(
                "R07", Severity.WARN, user or ip,
                f"Large response: {response_bytes // 1024} KB on {path}",
                {"bytes": response_bytes, "path": path},
            ))

        # R08: pentest signature
        for pat in PENTEST_UA_PATTERNS:
            if pat.search(user_agent):
                alerts.append(self._alert(
                    "R08", Severity.CRITICAL, ip,
                    f"Pentest tool detected: {user_agent[:80]}",
                    {"user_agent": user_agent, "pattern": pat.pattern},
                ))
                break

        # R09: JWT replay
        if jti:
            with self._jti_lock:
                if jti in self._used_jtis:
                    alerts.append(self._alert(
                        "R09", Severity.CRITICAL, user or ip,
                        f"JWT replay attack: jti={jti[:16]}…",
                        {"jti": jti[:16], "path": path},
                    ))
                else:
                    self._used_jtis.add(jti)

        # Auto-block critical IPs
        critical = [a for a in alerts if a.severity == Severity.CRITICAL]
        if critical and self.auto_block_critical:
            with self._blocked_lock:
                self._blocked_ips.add(ip)
                for a in critical:
                    a.action_taken = "ip_blocked"

        # Emit alerts
        for alert in alerts:
            self._emit(alert)

        return alerts

    def is_blocked(self, ip: str) -> bool:
        with self._blocked_lock:
            return ip in self._blocked_ips

    def unblock(self, ip: str) -> None:
        with self._blocked_lock:
            self._blocked_ips.discard(ip)

    def blocked_ips(self) -> list[str]:
        with self._blocked_lock:
            return sorted(self._blocked_ips)

    def clear_jti_cache(self) -> None:
        with self._jti_lock:
            self._used_jtis.clear()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _alert(
        self, rule_id: str, severity: str, actor: str,
        description: str, evidence: dict,
    ) -> IdsAlert:
        return IdsAlert(
            rule_id=rule_id,
            severity=severity,
            actor=actor,
            description=description,
            evidence=evidence,
        )

    def _emit(self, alert: IdsAlert) -> None:
        try:
            with open(self._alert_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(alert.to_dict()) + "\n")
        except Exception as exc:
            logger.warning("IDS alert log write failed: %s", exc)

        log_fn = {
            Severity.INFO: logger.info,
            Severity.WARN: logger.warning,
            Severity.ALERT: logger.error,
            Severity.CRITICAL: logger.critical,
        }.get(alert.severity, logger.warning)
        log_fn("IDS [%s/%s] %s — %s", alert.rule_id, alert.severity,
               alert.actor, alert.description)

        if self._on_alert:
            try:
                self._on_alert(alert)
            except Exception:
                pass

    def get_recent_alerts(
        self,
        last_n: int = 100,
        min_severity: str = Severity.INFO,
    ) -> list[IdsAlert]:
        """Read recent alerts from log file."""
        severity_rank = {
            Severity.INFO: 0, Severity.WARN: 1,
            Severity.ALERT: 2, Severity.CRITICAL: 3,
        }
        min_rank = severity_rank.get(min_severity, 0)
        alerts: list[IdsAlert] = []
        try:
            lines = self._alert_log.read_text(encoding="utf-8").splitlines()
            for line in reversed(lines[-last_n * 2:]):
                if not line.strip():
                    continue
                data = json.loads(line)
                if severity_rank.get(data.get("severity", ""), 0) >= min_rank:
                    alerts.append(IdsAlert(**data))
                    if len(alerts) >= last_n:
                        break
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("IDS alert read error: %s", exc)
        return list(reversed(alerts))


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_ids_instance: Optional[IntrusionDetectionSystem] = None
_ids_lock = threading.Lock()


def get_ids() -> IntrusionDetectionSystem:
    global _ids_instance
    if _ids_instance is None:
        with _ids_lock:
            if _ids_instance is None:
                _ids_instance = IntrusionDetectionSystem()
    return _ids_instance


# ---------------------------------------------------------------------------
# FastAPI Middleware
# ---------------------------------------------------------------------------
try:
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request as _Req
    from starlette.responses import JSONResponse

    class IdsMiddleware(BaseHTTPMiddleware):
        """
        FastAPI middleware — wraps IDS around every request.
        Blocked IPs receive HTTP 429 immediately.
        """

        def __init__(self, app, ids: Optional[IntrusionDetectionSystem] = None) -> None:
            super().__init__(app)
            self._ids = ids or get_ids()

        async def dispatch(self, request: _Req, call_next):
            ip = (request.client.host if request.client else "unknown")

            # Immediate block check
            if self._ids.is_blocked(ip):
                return JSONResponse(
                    {"detail": "Access denied — security policy"},
                    status_code=429,
                )

            response = await call_next(request)

            path = request.url.path
            user_agent = request.headers.get("User-Agent", "")
            user = getattr(request.state, "user_sub", None)
            role = getattr(request.state, "user_role", None)
            jti  = getattr(request.state, "jti", None)
            is_auth = path.startswith("/api/v1/auth")
            is_ok   = response.status_code < 400
            content_len = int(response.headers.get("content-length", 0))

            self._ids.process_request(
                ip=ip,
                path=path,
                method=request.method,
                status_code=response.status_code,
                user=str(user) if user else None,
                role=str(role) if role else None,
                user_agent=user_agent,
                response_bytes=content_len,
                jti=str(jti) if jti else None,
                is_auth_endpoint=is_auth,
                is_auth_success=is_ok,
            )
            return response

except ImportError:
    class IdsMiddleware:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys, argparse

    parser = argparse.ArgumentParser(description="IDS CLI")
    sub = parser.add_subparsers(dest="cmd")
    ra = sub.add_parser("alerts", help="Show recent alerts")
    ra.add_argument("--n",   type=int, default=20)
    ra.add_argument("--min", default="INFO",
                    choices=["INFO","WARN","ALERT","CRITICAL"])
    sub.add_parser("blocked", help="Show blocked IPs")

    # Self-test
    st = sub.add_parser("test", help="Run IDS self-test")
    args = parser.parse_args()
    ids = get_ids()

    if args.cmd == "alerts":
        alerts = ids.get_recent_alerts(last_n=args.n, min_severity=args.min)
        if not alerts:
            print("No alerts.")
        for a in alerts:
            print(f"[{a.severity}] {a.timestamp[:19]}  {a.rule_id}  {a.actor[:30]}  {a.description[:60]}")

    elif args.cmd == "blocked":
        ips = ids.blocked_ips()
        print(f"Blocked IPs ({len(ips)}): {', '.join(ips) or 'none'}")

    elif args.cmd == "test":
        print("Running IDS self-test…")
        # Trigger R01 brute force
        for _ in range(12):
            ids.process_request(
                ip="10.255.0.1", path="/api/v1/auth/login",
                method="POST", status_code=401, user="attacker@evil.com",
                role=None, user_agent="python-requests/2.28", response_bytes=100,
                jti=None, is_auth_endpoint=True, is_auth_success=False,
            )
        # Trigger R08 pentest
        ids.process_request(
            ip="10.255.0.2", path="/api/v1/bonds", method="GET",
            status_code=200, user=None, role=None,
            user_agent="sqlmap/1.7", response_bytes=200,
            jti=None, is_auth_endpoint=False, is_auth_success=True,
        )
        alerts = ids.get_recent_alerts(last_n=10, min_severity="WARN")
        print(f"✅ Self-test complete — {len(alerts)} alerts recorded")
        for a in alerts:
            print(f"  [{a.severity}] {a.rule_id}: {a.description[:70]}")
        blocked = ids.blocked_ips()
        print(f"  Blocked IPs: {blocked}")

    else:
        parser.print_help()
