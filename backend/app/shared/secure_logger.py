"""
Tamper-Proof Signed Audit Logger
=================================
Every security-critical event (login, logout, trade, alert, config change) is
written to both the structured console logger and a tamper-evident append-only
file. Each entry contains:
  - ISO-8601 timestamp
  - Severity level
  - Event name and payload
  - Sequence number (monotonically increasing per process)
  - HMAC-SHA256 chain signature (signs this entry + previous entry's hash)

This "hash chain" design means that if any historical log entry is altered,
all subsequent entries become invalid — detectable at any point by the
verify_audit_log() utility function.

Environment Variables:
  AUDIT_LOG_PATH     — File path for the tamper-evident audit log
                       (default: <gateway_dir>/logs/audit_chain.jsonl)
  AUDIT_SIGN_KEY     — 64-char hex key used to sign entries (REQUIRED in prod)
                       Auto-derives from ALE_ENCRYPTION_KEY if not set.
  LOG_JSON           — Set to "1" to emit JSON to stdout as well.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from threading import Lock
from typing import Any, Optional

from .time_utils import utcnow_iso

# ── Key Bootstrap ─────────────────────────────────────────────────────────────
def _get_sign_key() -> bytes:
    """
    Lazy-load the audit signing key (after .env is loaded by main.py).
    Falls back to deriving from ALE_ENCRYPTION_KEY if AUDIT_SIGN_KEY is not set.
    """
    raw_sign_key = os.getenv("AUDIT_SIGN_KEY", "")
    if not raw_sign_key:
        ale_key = os.getenv("ALE_ENCRYPTION_KEY", "")
        if ale_key:
            raw_sign_key = hashlib.sha256((ale_key + ":audit_chain").encode()).hexdigest()
        else:
            raise RuntimeError(
                "FATAL: Neither AUDIT_SIGN_KEY nor ALE_ENCRYPTION_KEY is set. "
                "Tamper-proof logging requires a signing key."
            )
    try:
        return bytes.fromhex(raw_sign_key[:64])
    except Exception:
        return raw_sign_key.encode("utf-8")[:32].ljust(32, b"\x00")

# ── Log Path ──────────────────────────────────────────────────────────────────
_DEFAULT_LOG_DIR = Path(__file__).parent.parent / "logs"
_DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
_AUDIT_LOG_PATH = Path(os.getenv("AUDIT_LOG_PATH", str(_DEFAULT_LOG_DIR / "audit_chain.jsonl")))


class TamperEvidentAuditLog:
    """
    Append-only tamper-evident audit log with HMAC hash chain.
    Thread-safe via a lock.
    """

    def __init__(self, path: Path = _AUDIT_LOG_PATH):
        self._path = path
        self._lock = Lock()
        self._seq: int = 0
        self._prev_hash: str = "0" * 64  # Genesis hash
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._bootstrap()

    def _bootstrap(self) -> None:
        """Load last entry's hash and sequence number for chain continuity."""
        if not self._path.exists():
            return
        last_line = ""
        try:
            with open(self._path, "rb") as f:
                # Read last non-empty line efficiently
                f.seek(0, 2)  # EOF
                size = f.tell()
                if size == 0:
                    return
                buf_size = min(size, 4096)
                f.seek(-buf_size, 2)
                tail = f.read(buf_size).decode("utf-8", errors="ignore")
                lines = [l for l in tail.splitlines() if l.strip()]
                if lines:
                    last_line = lines[-1]
        except Exception:
            return

        if last_line:
            try:
                entry = json.loads(last_line)
                self._seq = int(entry.get("seq", 0))
                self._prev_hash = entry.get("entry_hash", "0" * 64)
            except Exception:
                pass

    def _sign_entry(self, entry: dict) -> str:
        """
        Produce an HMAC-SHA256 over the canonical JSON of the entry
        chained with the previous entry's hash.
        """
        chain_input = self._prev_hash + json.dumps(entry, sort_keys=True, default=str)
        return hmac.new(_get_sign_key(), chain_input.encode("utf-8"), hashlib.sha256).hexdigest()

    def write(
        self,
        event: str,
        level: str = "INFO",
        actor: Optional[str] = None,
        **payload: Any,
    ) -> str:
        """
        Write a tamper-evident audit entry.

        Returns the entry's HMAC hash (can be stored in a separate integrity DB).
        """
        with self._lock:
            self._seq += 1
            ts = utcnow_iso()
            entry: dict = {
                "seq": self._seq,
                "ts": ts,
                "level": level.upper(),
                "event": event,
                "actor": actor,
                **payload,
            }
            entry_hash = self._sign_entry(entry)
            entry["entry_hash"] = entry_hash
            self._prev_hash = entry_hash

            line = json.dumps(entry, default=str) + "\n"
            try:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception as exc:
                sys.stderr.write(f"AUDIT LOG WRITE FAILURE: {exc}\n")

            return entry_hash

    def verify(self) -> dict:
        """
        Verify every entry in the log file.
        Returns {"valid": True/False, "entries": N, "first_tampered_seq": N or None}.
        """
        if not self._path.exists():
            return {"valid": True, "entries": 0, "first_tampered_seq": None}

        prev_hash = "0" * 64
        entries = 0
        first_tampered: Optional[int] = None

        with open(self._path, "r", encoding="utf-8") as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                entries += 1
                try:
                    entry = json.loads(raw_line)
                    stored_hash = entry.pop("entry_hash", "")
                    expected = self._sign_entry_static({**entry}, prev_hash)
                    if not hmac.compare_digest(stored_hash, expected):
                        if first_tampered is None:
                            first_tampered = entry.get("seq", entries)
                    prev_hash = stored_hash if stored_hash else prev_hash
                except Exception:
                    if first_tampered is None:
                        first_tampered = entries

        return {
            "valid": first_tampered is None,
            "entries": entries,
            "first_tampered_seq": first_tampered,
        }

    def _sign_entry_static(self, entry: dict, prev_hash: str) -> str:
        """Static variant of _sign_entry for use during verification."""
        chain_input = prev_hash + json.dumps(entry, sort_keys=True, default=str)
        return hmac.new(_get_sign_key(), chain_input.encode("utf-8"), hashlib.sha256).hexdigest()


# ── Global Singleton ──────────────────────────────────────────────────────────
_audit_log: Optional[TamperEvidentAuditLog] = None


def get_audit_log() -> TamperEvidentAuditLog:
    """Return the global singleton audit log instance."""
    global _audit_log
    if _audit_log is None:
        _audit_log = TamperEvidentAuditLog()
    return _audit_log


def audit(event: str, level: str = "INFO", actor: Optional[str] = None, **payload: Any) -> None:
    """
    Convenience function: write a tamper-evident audit entry.

    Usage:
        from .shared.secure_logger import audit
        audit("trade_executed", actor=user["username"], isin="INE020B01018", amount=500000)
        audit("login_failed", level="WARNING", actor="unknown_user", ip="1.2.3.4")
    """
    get_audit_log().write(event, level=level, actor=actor, **payload)


def verify_audit_log() -> dict:
    """Verify the entire audit log chain. Returns integrity report."""
    return get_audit_log().verify()
