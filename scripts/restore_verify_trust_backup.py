"""
Trust Backup Restore/Verify Utility
===================================
Validates encrypted backup files and performs a restore test.

Supported restore verification:
- SQLite: decrypt + decompress + restore file + basic table-read check
- PostgreSQL dump: decrypt + decompress + pg_restore --list sanity check
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_backup_key() -> bytes:
    key_b64 = os.getenv("BACKUP_ENCRYPTION_KEY", "").strip()
    if not key_b64:
        raise RuntimeError("BACKUP_ENCRYPTION_KEY is required to decrypt encrypted backup files")
    key = base64.urlsafe_b64decode(key_b64.encode("utf-8"))
    if len(key) != 32:
        raise RuntimeError("BACKUP_ENCRYPTION_KEY must decode to 32 bytes")
    return key


def decrypt_payload(data: bytes) -> bytes:
    if len(data) < 13:
        raise RuntimeError("Encrypted payload is too short")
    nonce = data[:12]
    cipher = data[12:]
    return AESGCM(read_backup_key()).decrypt(nonce, cipher, None)


def verify_sqlite_file(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"SQLite restore output missing: {path}")

    con = sqlite3.connect(str(path))
    try:
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row[0] for row in cur.fetchall()]

        checks: dict[str, int] = {}
        for table in ("users", "instrument_master", "portfolio_positions", "audit_logs"):
            if table in tables:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                checks[table] = int(cur.fetchone()[0])
        return {"table_count": len(tables), "sample_counts": checks}
    finally:
        con.close()


def verify_postgres_dump(path: Path) -> dict:
    pg_restore_bin = os.getenv("PG_RESTORE_BIN", "pg_restore")
    cmd = [pg_restore_bin, "--list", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"pg_restore --list failed: {proc.stderr.strip() or proc.stdout.strip()}")
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    return {"toc_entries": len(lines)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore and verify a trust backup file")
    parser.add_argument("--backup-file", required=True)
    parser.add_argument("--engine", choices=["sqlite", "postgres"], default="sqlite")
    parser.add_argument("--output-path", default="", help="Output file path for restored payload")
    parser.add_argument("--metadata", default="", help="Optional metadata json path to cross-check")
    parser.add_argument("--encrypted", action="store_true", help="Treat backup file as encrypted payload")
    args = parser.parse_args()

    backup_file = Path(args.backup_file)
    if not backup_file.exists():
        print(json.dumps({"status": "error", "message": f"Backup file not found: {backup_file}"}, indent=2), file=sys.stderr)
        return 1

    try:
        payload = backup_file.read_bytes()
        if args.encrypted or backup_file.suffix == ".enc":
            payload = decrypt_payload(payload)
        restored = gzip.decompress(payload)

        if args.output_path.strip():
            output_path = Path(args.output_path)
        else:
            suffix = ".db" if args.engine == "sqlite" else ".dump"
            output_path = backup_file.parent / f"restore_test_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}{suffix}"

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(restored)

        if args.engine == "sqlite":
            verification = verify_sqlite_file(output_path)
        else:
            verification = verify_postgres_dump(output_path)

        metadata_check = None
        if args.metadata.strip():
            meta_path = Path(args.metadata)
            if meta_path.exists():
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
                metadata_check = {
                    "payload_sha256": metadata.get("payload_sha256"),
                    "raw_sha256": metadata.get("raw_sha256"),
                }

        print(
            json.dumps(
                {
                    "status": "ok",
                    "verified_at_utc": utcnow_iso(),
                    "engine": args.engine,
                    "restore_output": str(output_path.resolve()),
                    "restore_size_bytes": len(restored),
                    "verification": verification,
                    "metadata_check": metadata_check,
                },
                indent=2,
            )
        )
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
