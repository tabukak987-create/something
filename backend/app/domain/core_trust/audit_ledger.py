"""
Immutable Hash-Chained Audit Ledger
====================================
Every audit event is cryptographically chained to the previous one.
Tampering with any entry breaks the chain — detected by verify_chain().

Chain structure
---------------
  genesis_hash = "0" * 64

  entry_hash = SHA256(
      previous_entry_hash
      + ISO8601 timestamp
      + event_type
      + actor (user / service)
      + SHA256(json(payload))   ← payload hash, not raw payload
  )

Daily Merkle anchoring
----------------------
  Each day at midnight the Merkle root of that day's entries is
  written to  reports/audit_anchors/YYYY-MM-DD.json  for offline
  verification. (Production: submit to RFC 3161 TSA / IPFS.)

Usage
-----
    from app.domain.core_trust.audit_ledger import (
        AuditLedger, AuditEvent
    )

    ledger = AuditLedger()          # singleton via get_ledger()
    ledger.append(AuditEvent(
        event_type="USER_LOGIN",
        actor="admin@pftrust.in",
        payload={"ip": "10.0.0.1", "mfa": True},
    ))
    ok, report = ledger.verify_chain()

FastAPI integration
-------------------
    from app.domain.core_trust.audit_ledger import (
        AuditLedgerMiddleware, get_ledger
    )
    app.add_middleware(AuditLedgerMiddleware)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.shared.time_utils import utcnow_iso

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[3]
_REPORTS_DIR = _REPO_ROOT / "reports"
_DB_PATH = Path(
    os.getenv("AUDIT_LEDGER_DB", str(_REPORTS_DIR / "audit_ledger.db"))
)
_ANCHOR_DIR = Path(
    os.getenv("AUDIT_ANCHOR_DIR", str(_REPORTS_DIR / "audit_anchors"))
)

GENESIS_HASH = "0" * 64   # All-zeros genesis block hash

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class AuditEvent:
    """A single immutable audit event (all fields are set strings after append)."""
    event_type: str                       # e.g. "USER_LOGIN", "RULE_CHANGE"
    actor: str                            # user / service that triggered the event
    payload: dict[str, Any] = field(default_factory=dict)

    # Filled in by AuditLedger.append() — do not set manually
    seq_id: int = 0
    timestamp: str = ""
    payload_hash: str = ""
    previous_hash: str = ""
    entry_hash: str = ""


@dataclass
class LedgerEntry:
    """Fully materialised, stored ledger entry."""
    seq_id: int
    timestamp: str
    event_type: str
    actor: str
    payload_hash: str        # SHA-256 of JSON-serialised payload
    previous_hash: str
    entry_hash: str          # SHA-256 chain link hash


@dataclass
class VerificationReport:
    ok: bool
    total_entries: int
    broken_at_seq: Optional[int]
    errors: list[str]
    verified_at: str
    merkle_root: str


# ---------------------------------------------------------------------------
# Helper: canonical hash
# ---------------------------------------------------------------------------
def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _payload_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)
    return _sha256(canonical)


def _compute_entry_hash(
    previous_hash: str,
    timestamp: str,
    event_type: str,
    actor: str,
    payload_hash: str,
) -> str:
    raw = f"{previous_hash}|{timestamp}|{event_type}|{actor}|{payload_hash}"
    return _sha256(raw)


# ---------------------------------------------------------------------------
# Merkle tree
# ---------------------------------------------------------------------------
def _merkle_root(hashes: list[str]) -> str:
    """Compute Merkle root for a list of leaf hashes."""
    if not hashes:
        return GENESIS_HASH
    layer = list(hashes)
    while len(layer) > 1:
        if len(layer) % 2 == 1:
            layer.append(layer[-1])     # duplicate last leaf
        layer = [_sha256(layer[i] + layer[i + 1]) for i in range(0, len(layer), 2)]
    return layer[0]


# ---------------------------------------------------------------------------
# SQLite persistence
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_ledger (
    seq_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT    NOT NULL,
    event_type    TEXT    NOT NULL,
    actor         TEXT    NOT NULL,
    payload_hash  TEXT    NOT NULL,
    previous_hash TEXT    NOT NULL,
    entry_hash    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_ledger(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_ledger(event_type);
"""


class _DB:
    """Thread-safe SQLite wrapper for the audit ledger."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(path)
        self._local = threading.local()
        self._lock = threading.Lock()
        # Initialise schema
        con = self._conn()
        con.executescript(_SCHEMA)
        con.commit()

    def _conn(self) -> sqlite3.Connection:
        if not getattr(self._local, "conn", None):
            self._local.conn = sqlite3.connect(
                self._path,
                check_same_thread=False,
                isolation_level=None,   # autocommit
            )
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=FULL")
        return self._local.conn

    def insert(self, entry: LedgerEntry) -> int:
        with self._lock:
            cur = self._conn().execute(
                """INSERT INTO audit_ledger
                   (timestamp, event_type, actor, payload_hash,
                    previous_hash, entry_hash)
                   VALUES (?,?,?,?,?,?)""",
                (
                    entry.timestamp, entry.event_type, entry.actor,
                    entry.payload_hash, entry.previous_hash, entry.entry_hash,
                ),
            )
            return cur.lastrowid

    def last_entry(self) -> Optional[LedgerEntry]:
        row = self._conn().execute(
            "SELECT * FROM audit_ledger ORDER BY seq_id DESC LIMIT 1"
        ).fetchone()
        return LedgerEntry(**dict(row)) if row else None

    def all_entries(self) -> list[LedgerEntry]:
        rows = self._conn().execute(
            "SELECT * FROM audit_ledger ORDER BY seq_id ASC"
        ).fetchall()
        return [LedgerEntry(**dict(r)) for r in rows]

    def entries_for_date(self, d: date) -> list[LedgerEntry]:
        prefix = d.strftime("%Y-%m-%d")
        rows = self._conn().execute(
            "SELECT * FROM audit_ledger WHERE timestamp LIKE ? ORDER BY seq_id",
            (f"{prefix}%",),
        ).fetchall()
        return [LedgerEntry(**dict(r)) for r in rows]

    def count(self) -> int:
        return self._conn().execute(
            "SELECT COUNT(*) FROM audit_ledger"
        ).fetchone()[0]

    def recent(self, n: int = 50) -> list[LedgerEntry]:
        rows = self._conn().execute(
            "SELECT * FROM audit_ledger ORDER BY seq_id DESC LIMIT ?", (n,)
        ).fetchall()
        return [LedgerEntry(**dict(r)) for r in reversed(rows)]


# ---------------------------------------------------------------------------
# AuditLedger — main class
# ---------------------------------------------------------------------------
class AuditLedger:
    """
    Immutable hash-chained audit ledger.

    Each `append()` call atomically:
     1. Computes entry_hash = SHA256(prev_hash | timestamp | event | actor | payload_hash)
     2. Persists to SQLite with WAL + FULL synchronous mode
     3. Returns the fully materialised LedgerEntry

    `verify_chain()` re-hashes every entry from genesis and reports
    the first discrepancy.
    """

    def __init__(self, db_path: Path = _DB_PATH) -> None:
        self._db = _DB(db_path)
        self._anchor_dir = _ANCHOR_DIR
        self._anchor_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def append(self, event: AuditEvent) -> LedgerEntry:
        """Append a new auditable event to the chain. Thread-safe."""
        with self._lock:
            last = self._db.last_entry()
            previous_hash = last.entry_hash if last else GENESIS_HASH
            timestamp = utcnow_iso()
            ph = _payload_hash(event.payload)
            eh = _compute_entry_hash(previous_hash, timestamp, event.event_type,
                                     event.actor, ph)
            entry = LedgerEntry(
                seq_id=0,   # set by DB
                timestamp=timestamp,
                event_type=event.event_type,
                actor=event.actor,
                payload_hash=ph,
                previous_hash=previous_hash,
                entry_hash=eh,
            )
            seq_id = self._db.insert(entry)
            entry.seq_id = seq_id
            logger.debug("Audit entry #%d: %s by %s", seq_id, event.event_type, event.actor)
            return entry

    def verify_chain(self) -> tuple[bool, VerificationReport]:
        """
        Verify the integrity of the entire chain from genesis.

        Returns (ok, VerificationReport).  ok=True means no tampering detected.
        """
        entries = self._db.all_entries()
        errors: list[str] = []
        broken_at: Optional[int] = None
        expected_previous = GENESIS_HASH
        hashes: list[str] = []

        for entry in entries:
            # Verify previous_hash linkage
            if entry.previous_hash != expected_previous:
                msg = (
                    f"Chain broken at seq_id={entry.seq_id}: "
                    f"expected previous_hash={expected_previous[:16]}… "
                    f"got {entry.previous_hash[:16]}…"
                )
                errors.append(msg)
                if broken_at is None:
                    broken_at = entry.seq_id

            # Verify entry_hash correctness
            expected_eh = _compute_entry_hash(
                entry.previous_hash, entry.timestamp,
                entry.event_type, entry.actor, entry.payload_hash,
            )
            if entry.entry_hash != expected_eh:
                msg = (
                    f"Entry hash mismatch at seq_id={entry.seq_id}: "
                    f"stored={entry.entry_hash[:16]}… "
                    f"computed={expected_eh[:16]}…"
                )
                errors.append(msg)
                if broken_at is None:
                    broken_at = entry.seq_id

            hashes.append(entry.entry_hash)
            expected_previous = entry.entry_hash

        merkle = _merkle_root(hashes)
        ok = len(errors) == 0
        report = VerificationReport(
            ok=ok,
            total_entries=len(entries),
            broken_at_seq=broken_at,
            errors=errors,
            verified_at=utcnow_iso(),
            merkle_root=merkle,
        )
        return ok, report

    def anchor_today(self) -> dict:
        """
        Write the Merkle root of today's entries to
        reports/audit_anchors/YYYY-MM-DD.json for offline verification.

        In production this would also be submitted to an RFC 3161
        timestamp authority or IPFS.
        """
        today = date.today()
        entries = self._db.entries_for_date(today)
        merkle = _merkle_root([e.entry_hash for e in entries])
        anchor = {
            "date": today.isoformat(),
            "entry_count": len(entries),
            "merkle_root": merkle,
            "anchored_at": utcnow_iso(),
            "first_seq": entries[0].seq_id if entries else None,
            "last_seq": entries[-1].seq_id if entries else None,
        }
        anchor_path = self._anchor_dir / f"{today.isoformat()}.json"
        anchor_path.write_text(json.dumps(anchor, indent=2), encoding="utf-8")
        logger.info("Merkle anchor written: %s  root=%s", anchor_path.name, merkle[:16])
        return anchor

    def recent_entries(self, n: int = 50) -> list[LedgerEntry]:
        return self._db.recent(n)

    def entry_count(self) -> int:
        return self._db.count()

    def export_json(self, path: Path, last_n: Optional[int] = None) -> int:
        """Export entries to JSON for external auditor consumption."""
        entries = self._db.all_entries() if not last_n else self._db.recent(last_n)
        ok, report = self.verify_chain()
        export = {
            "exported_at": utcnow_iso(),
            "chain_integrity": asdict(report),
            "entries": [asdict(e) for e in entries],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(export, indent=2), encoding="utf-8")
        return len(entries)


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------
_ledger_instance: Optional[AuditLedger] = None
_ledger_lock = threading.Lock()


def get_ledger() -> AuditLedger:
    global _ledger_instance
    if _ledger_instance is None:
        with _ledger_lock:
            if _ledger_instance is None:
                _ledger_instance = AuditLedger()
    return _ledger_instance


# ---------------------------------------------------------------------------
# FastAPI middleware
# ---------------------------------------------------------------------------
try:
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request as StarletteRequest
    import time as _time

    class AuditLedgerMiddleware(BaseHTTPMiddleware):
        """
        Auto-audit every API request.

        Records: method, path, status_code, actor (from JWT sub claim),
        client IP, duration_ms.

        Excluded: /healthz, /metrics, static files.
        """

        EXCLUDED_PREFIXES = ("/healthz", "/metrics", "/static", "/favicon")

        def __init__(self, app, ledger: Optional[AuditLedger] = None) -> None:
            super().__init__(app)
            self._ledger = ledger or get_ledger()

        async def dispatch(self, request: StarletteRequest, call_next):
            path = request.url.path
            if any(path.startswith(p) for p in self.EXCLUDED_PREFIXES):
                return await call_next(request)

            start = _time.perf_counter()
            response = await call_next(request)
            duration_ms = round((_time.perf_counter() - start) * 1000, 1)

            # Extract actor from request state (set by auth middleware)
            actor = getattr(request.state, "user_sub", None)
            if not actor:
                authorization = request.headers.get("Authorization", "")
                actor = "unauthenticated" if not authorization else "authenticated"

            try:
                self._ledger.append(AuditEvent(
                    event_type="HTTP_REQUEST",
                    actor=str(actor),
                    payload={
                        "method": request.method,
                        "path": path,
                        "status_code": response.status_code,
                        "client_ip": request.client.host if request.client else "unknown",
                        "duration_ms": duration_ms,
                    },
                ))
            except Exception as exc:   # pragma: no cover
                logger.warning("AuditLedgerMiddleware append failed: %s", exc)

            return response

except ImportError:
    # Starlette not available (e.g. running tests solo)
    class AuditLedgerMiddleware:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            pass


# ---------------------------------------------------------------------------
# CLI: verify / export / anchor
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    import argparse

    parser = argparse.ArgumentParser(description="Audit Ledger CLI")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("verify", help="Verify chain integrity")
    sub.add_parser("anchor", help="Write today's Merkle anchor")
    ex = sub.add_parser("export", help="Export ledger to JSON")
    ex.add_argument("path", help="Output JSON path")
    ex.add_argument("--last", type=int, default=None, help="Export last N entries")

    seed = sub.add_parser("seed", help="Seed sample events for testing")
    seed.add_argument("--count", type=int, default=20)

    args = parser.parse_args()
    ledger = get_ledger()

    if args.cmd == "verify":
        ok, report = ledger.verify_chain()
        status = "✅ CHAIN INTACT" if ok else "❌ CHAIN COMPROMISED"
        print(f"\n{status}")
        print(f"  Total entries : {report.total_entries}")
        print(f"  Merkle root   : {report.merkle_root[:32]}…")
        print(f"  Verified at   : {report.verified_at}")
        if report.errors:
            print("\n  Errors:")
            for e in report.errors:
                print(f"    • {e}")
        sys.exit(0 if ok else 1)

    elif args.cmd == "anchor":
        a = ledger.anchor_today()
        print(f"✅ Anchor written — {a['entry_count']} entries  Merkle: {a['merkle_root'][:32]}…")

    elif args.cmd == "export":
        n = ledger.export_json(Path(args.path), last_n=args.last)
        print(f"✅ Exported {n} entries to {args.path}")

    elif args.cmd == "seed":
        import random
        event_types = [
            "USER_LOGIN", "USER_LOGOUT", "RULE_CHANGE", "BOND_QUOTE_FETCH",
            "PORTFOLIO_UPDATE", "COMPLIANCE_ALERT", "MFA_CONFIRM", "RATE_LIMIT_HIT",
            "DR_DRILL_START", "DR_DRILL_COMPLETE",
        ]
        actors = [
            "admin@pftrust.in", "trustee@pftrust.in",
            "analyst@partner.in", "system::compliance_engine",
        ]
        for i in range(args.count):
            ledger.append(AuditEvent(
                event_type=random.choice(event_types),
                actor=random.choice(actors),
                payload={"seed_index": i, "value": random.randint(1000, 99999)},
            ))
        print(f"✅ Seeded {args.count} events  — total: {ledger.entry_count()}")
        ok, report = ledger.verify_chain()
        print(f"   Chain: {'INTACT' if ok else 'BROKEN'}  Merkle: {report.merkle_root[:32]}…")

    else:
        parser.print_help()
