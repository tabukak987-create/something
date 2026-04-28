"""
Trust Backup Utility
====================
Creates encrypted daily/weekly backups for SQLite or PostgreSQL deployments.

Design goals:
- Simple and deterministic (no hidden behavior)
- Works in both cloud demo and on-prem modes
- Produces metadata for audit/recovery evidence
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def to_sync_postgres_url(db_url: str) -> str:
    return db_url.replace("postgresql+asyncpg://", "postgresql://", 1).replace("postgres://", "postgresql://", 1)


def sqlite_path_from_url(db_url: str) -> Path:
    prefix = "sqlite+aiosqlite:///"
    if not db_url.startswith(prefix):
        raise ValueError("Expected sqlite+aiosqlite URL")
    raw = db_url[len(prefix) :]
    return Path(raw)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_backup_key() -> bytes:
    key_b64 = os.getenv("BACKUP_ENCRYPTION_KEY", "").strip()
    if not key_b64:
        raise RuntimeError(
            "BACKUP_ENCRYPTION_KEY is required for encrypted backups. "
            "Generate with: python -c \"import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())\""
        )

    key = base64.urlsafe_b64decode(key_b64.encode("utf-8"))
    if len(key) != 32:
        raise RuntimeError("BACKUP_ENCRYPTION_KEY must decode to exactly 32 bytes")
    return key


def encrypt_payload(payload: bytes) -> tuple[bytes, str]:
    key = read_backup_key()
    nonce = os.urandom(12)
    cipher = AESGCM(key).encrypt(nonce, payload, None)
    encrypted = nonce + cipher
    return encrypted, sha256_bytes(encrypted)


def create_sqlite_snapshot(db_path: Path, work_dir: Path) -> Path:
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")
    snapshot = work_dir / "sqlite_snapshot.db"
    shutil.copy2(db_path, snapshot)
    return snapshot


def create_postgres_snapshot(db_url: str, work_dir: Path) -> Path:
    dsn = to_sync_postgres_url(db_url)
    dump_path = work_dir / "postgres_snapshot.dump"
    pg_dump_bin = os.getenv("PG_DUMP_BIN", "pg_dump")

    cmd = [
        pg_dump_bin,
        "--format=custom",
        "--file",
        str(dump_path),
        dsn,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return dump_path


def load_backup_payload(source_file: Path) -> bytes:
    return source_file.read_bytes()


def cleanup_old_backups(backup_dir: Path, retention_days: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = 0
    for path in backup_dir.glob("trust_backup_*.*"):
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    for path in backup_dir.glob("trust_backup_*.json"):
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="Create encrypted trust backup (daily/weekly mode)")
    parser.add_argument("--backup-dir", default="backups", help="Backup output directory")
    parser.add_argument("--secondary-dir", default="", help="Optional second storage directory (for 3-2-1)")
    parser.add_argument("--mode", choices=["daily", "weekly-full"], default="daily")
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument("--db-url", default=os.getenv("GATEWAY_DB_URL") or os.getenv("DATABASE_URL") or "")
    parser.add_argument("--sqlite-path", default="", help="Explicit SQLite path override")
    parser.add_argument("--no-encrypt", action="store_true", help="Write compressed but unencrypted backup")
    args = parser.parse_args()

    backup_dir = Path(args.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    work_dir = backup_dir / f".tmp_{stamp}"
    work_dir.mkdir(parents=True, exist_ok=True)

    db_url = args.db_url.strip()
    sqlite_override = args.sqlite_path.strip()

    try:
        if sqlite_override:
            engine = "sqlite"
            source_snapshot = create_sqlite_snapshot(Path(sqlite_override), work_dir)
            source_ref = str(Path(sqlite_override).resolve())
        elif db_url.startswith("sqlite"):
            engine = "sqlite"
            sqlite_path = sqlite_path_from_url(db_url)
            source_snapshot = create_sqlite_snapshot(sqlite_path, work_dir)
            source_ref = str(sqlite_path.resolve())
        elif db_url.startswith("postgres") or db_url.startswith("postgresql"):
            engine = "postgres"
            source_snapshot = create_postgres_snapshot(db_url, work_dir)
            parsed = urlparse(to_sync_postgres_url(db_url))
            source_ref = f"{parsed.hostname}:{parsed.port or 5432}/{parsed.path.lstrip('/')}"
        else:
            raise RuntimeError("Could not detect database engine. Provide --db-url or --sqlite-path.")

        raw_bytes = load_backup_payload(source_snapshot)
        raw_sha256 = sha256_bytes(raw_bytes)
        compressed = gzip.compress(raw_bytes, compresslevel=9)

        base_name = f"trust_backup_{args.mode}_{stamp}"
        if args.no_encrypt:
            payload_bytes = compressed
            payload_sha = sha256_bytes(payload_bytes)
            payload_path = backup_dir / f"{base_name}.gz"
        else:
            payload_bytes, payload_sha = encrypt_payload(compressed)
            payload_path = backup_dir / f"{base_name}.gz.enc"

        payload_path.write_bytes(payload_bytes)

        metadata = {
            "created_at_utc": utcnow_iso(),
            "mode": args.mode,
            "engine": engine,
            "source": source_ref,
            "retention_days": args.retention_days,
            "encrypted": not args.no_encrypt,
            "backup_file": str(payload_path.resolve()),
            "raw_size_bytes": len(raw_bytes),
            "payload_size_bytes": len(payload_bytes),
            "raw_sha256": raw_sha256,
            "payload_sha256": payload_sha,
            "secondary_copy": None,
        }

        if args.secondary_dir.strip():
            secondary_dir = Path(args.secondary_dir)
            secondary_dir.mkdir(parents=True, exist_ok=True)
            secondary_payload = secondary_dir / payload_path.name
            secondary_meta = secondary_dir / f"{base_name}.json"
            shutil.copy2(payload_path, secondary_payload)
            metadata["secondary_copy"] = str(secondary_payload.resolve())
            secondary_meta.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        metadata_path = backup_dir / f"{base_name}.json"
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        removed = cleanup_old_backups(backup_dir, args.retention_days)

        print(json.dumps({"status": "ok", "backup": metadata, "old_files_removed": removed}, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, indent=2), file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
