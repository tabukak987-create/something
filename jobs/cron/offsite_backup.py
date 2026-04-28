"""
Offsite Cloud Backup — PF Trust Bond Platform
==============================================
Uploads the most-recent local encrypted backup to remote object storage.

Supported backends (configure via environment variables):
  • AWS S3       — set S3_BUCKET, S3_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
  • Azure Blob   — set AZURE_STORAGE_CONNECTION_STRING, AZURE_CONTAINER

Usage:
    python offsite_backup.py                   # auto-pick latest backup
    python offsite_backup.py --file backup.zip # upload specific file
    python offsite_backup.py --dry-run         # show what would be uploaded, don't upload
    python offsite_backup.py --verify          # verify last upload's integrity

Schedule (Windows Task Scheduler):
    Action: D:\\investment_j\\.venv\\Scripts\\python.exe
    Arguments: D:\\investment_j\\Private3\\offsite_backup.py
    Trigger: Daily at 01:00 (30 min after local backup at 00:30)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
BACKUP_DIR  = BASE_DIR.parent / "backups"
LOG_FILE    = BASE_DIR.parent / "logs" / "offsite_backup.log"
MANIFEST    = BASE_DIR.parent / "backups" / "offsite_manifest.json"

LOG_FILE.parent.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("offsite_backup")


# ── Helpers ────────────────────────────────────────────────────────────────────

def sha256_file(path: Path) -> str:
    """Return hex SHA-256 of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_checksum_sidecar(archive: Path, checksum: str) -> None:
    """Write a .sha256 sidecar file alongside the archive for restore-test verification."""
    sidecar = archive.with_suffix(archive.suffix + ".sha256")
    sidecar.write_text(f"{checksum}  {archive.name}\n", encoding="utf-8")
    log.info("checksum sidecar written: %s", sidecar.name)


def create_local_backup() -> Path:
    """
    Create a timestamped zip backup of all critical data sources:
      1. Unified gateway SQLite DB (bond_gateway.db)
      2. Other DB files found across the project
      3. Audit chain log (tamper-evident)
    Returns path to the created archive.
    """
    import zipfile as _zf

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    archive_path = BACKUP_DIR / f"gateway_backup_{ts}.zip"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    # Collect unique source files, keyed by archive name to prevent duplicates
    sources: dict[str, Path] = {}

    # Priority: unified gateway DB first
    gw_db = BASE_DIR / "unified_gateway" / "data" / "bond_gateway.db"
    if gw_db.exists():
        sources["bond_gateway.db"] = gw_db

    # Audit chain
    audit_log = BASE_DIR / "unified_gateway" / "logs" / "audit_chain.jsonl"
    if audit_log.exists():
        sources["audit_chain.jsonl"] = audit_log

    # Other DB files — use relative path to project root to avoid duplicates
    project_root = BASE_DIR.parent
    seen_stems: set[str] = set(sources.keys())
    for db in sorted(project_root.rglob("*.db"), key=lambda p: p.stat().st_size, reverse=True):
        if "Old" in str(db) or "old" in str(db) or "__pycache__" in str(db):
            continue
        # Use relative path as archive name to prevent collisions
        try:
            arc_name = str(db.relative_to(project_root)).replace("\\", "/")
        except ValueError:
            arc_name = db.name
        if arc_name not in sources and db not in sources.values():
            sources[arc_name] = db

    written = []
    with _zf.ZipFile(archive_path, "w", compression=_zf.ZIP_DEFLATED, compresslevel=9) as zf:
        for arc_name, src in sources.items():
            if src.exists():
                zf.write(src, arcname=arc_name)
                written.append(arc_name)
                log.info("  + %s (%s KB)", arc_name, src.stat().st_size // 1024)

    checksum = sha256_file(archive_path)
    write_checksum_sidecar(archive_path, checksum)
    log.info("local backup created: %s (%s files, sha256=%s...)", archive_path.name, len(written), checksum[:12])
    return archive_path


def latest_backup() -> Path | None:
    """Return the most-recent .zip backup file."""
    if not BACKUP_DIR.exists():
        return None
    zips = sorted(BACKUP_DIR.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    return zips[0] if zips else None


def load_manifest() -> dict:
    if MANIFEST.exists():
        with open(MANIFEST) as f:
            return json.load(f)
    return {"uploads": []}


def save_manifest(data: dict):
    with open(MANIFEST, "w") as f:
        json.dump(data, f, indent=2)


# ── AWS S3 Backend ─────────────────────────────────────────────────────────────

def upload_s3(local_path: Path, dry_run: bool = False) -> dict:
    """Upload to AWS S3. Requires: boto3, S3_BUCKET, S3_REGION env vars."""
    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ImportError:
        raise RuntimeError(
            "boto3 not installed. Run: pip install boto3\n"
            "Configure: S3_BUCKET, S3_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY"
        )

    bucket  = os.environ["S3_BUCKET"]
    region  = os.environ.get("S3_REGION", "ap-south-1")
    prefix  = os.environ.get("S3_PREFIX", "bond-platform-backups/")
    key     = prefix + local_path.name
    checksum = sha256_file(local_path)

    if dry_run:
        log.info("[DRY-RUN] S3 upload: s3://%s/%s  (%s bytes, sha256=%s...)",
                 bucket, key, local_path.stat().st_size, checksum[:12])
        return {"backend": "s3", "bucket": bucket, "key": key, "dry_run": True}

    client = boto3.client("s3", region_name=region)
    log.info("uploading to S3: s3://%s/%s (%s bytes)", bucket, key, local_path.stat().st_size)
    try:
        client.upload_file(
            str(local_path), bucket, key,
            ExtraArgs={
                "Metadata": {"sha256": checksum, "platform": "bond-gateway"},
                "StorageClass": "STANDARD_IA",       # cheaper for infrequent access
                "ServerSideEncryption": "AES256",     # S3-managed encryption at rest
            },
        )
    except (BotoCoreError, ClientError) as e:
        raise RuntimeError(f"S3 upload failed: {e}") from e

    log.info("S3 upload complete: s3://%s/%s", bucket, key)
    return {
        "backend": "s3",
        "bucket": bucket,
        "key": key,
        "file": local_path.name,
        "bytes": local_path.stat().st_size,
        "sha256": checksum,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── Azure Blob Backend ─────────────────────────────────────────────────────────

def upload_azure(local_path: Path, dry_run: bool = False) -> dict:
    """Upload to Azure Blob Storage. Requires: azure-storage-blob, AZURE_STORAGE_CONNECTION_STRING."""
    try:
        from azure.storage.blob import BlobServiceClient  # type: ignore
    except ImportError:
        raise RuntimeError(
            "azure-storage-blob not installed. Run: pip install azure-storage-blob\n"
            "Configure: AZURE_STORAGE_CONNECTION_STRING, AZURE_CONTAINER"
        )

    conn_str   = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
    container  = os.environ.get("AZURE_CONTAINER", "bond-platform-backups")
    blob_name  = f"backups/{local_path.name}"
    checksum   = sha256_file(local_path)

    if dry_run:
        log.info("[DRY-RUN] Azure upload: %s/%s  (%s bytes, sha256=%s...)",
                 container, blob_name, local_path.stat().st_size, checksum[:12])
        return {"backend": "azure", "container": container, "blob": blob_name, "dry_run": True}

    client  = BlobServiceClient.from_connection_string(conn_str)
    blobcl  = client.get_blob_client(container=container, blob=blob_name)
    log.info("uploading to Azure Blob: %s/%s (%s bytes)", container, blob_name, local_path.stat().st_size)

    with open(local_path, "rb") as data:
        blobcl.upload_blob(data, overwrite=True, metadata={"sha256": checksum})

    log.info("Azure upload complete: %s/%s", container, blob_name)
    return {
        "backend": "azure",
        "container": container,
        "blob": blob_name,
        "file": local_path.name,
        "bytes": local_path.stat().st_size,
        "sha256": checksum,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── Backend Selector ───────────────────────────────────────────────────────────

def upload_file(local_path: Path, dry_run: bool = False) -> dict:
    """Upload using whichever backend is configured."""
    if os.environ.get("AZURE_STORAGE_CONNECTION_STRING"):
        return upload_azure(local_path, dry_run)
    elif os.environ.get("S3_BUCKET"):
        return upload_s3(local_path, dry_run)
    else:
        raise RuntimeError(
            "No offsite backup backend configured.\n\n"
            "Option A — AWS S3:\n"
            "  Set env vars: S3_BUCKET, S3_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY\n"
            "  Install:      pip install boto3\n\n"
            "Option B — Azure Blob:\n"
            "  Set env vars: AZURE_STORAGE_CONNECTION_STRING, AZURE_CONTAINER\n"
            "  Install:      pip install azure-storage-blob\n\n"
            "Add vars to: D:\\investment_j\\Private3\\unified_gateway\\.env"
        )


# ── Verify Last Upload ─────────────────────────────────────────────────────────

def verify_last(dry_run: bool = False):
    """Verify integrity of last recorded upload."""
    manifest = load_manifest()
    if not manifest["uploads"]:
        log.error("No uploads recorded in manifest.")
        return False
    last = manifest["uploads"][-1]
    local_name = last.get("file")
    local_path = BACKUP_DIR / local_name if local_name else None
    if local_path and local_path.exists():
        current_hash = sha256_file(local_path)
        recorded_hash = last.get("sha256", "")
        match = current_hash == recorded_hash
        status = "MATCH" if match else "MISMATCH"
        log.info("integrity check: %s  file=%s  expected=%s...  actual=%s...",
                 status, local_name, recorded_hash[:12], current_hash[:12])
        return match
    log.warning("Local file not found: %s — cannot verify checksum", local_path)
    return None


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    # Load .env from root directory
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        env_path = BASE_DIR / "unified_gateway" / ".env"
    if env_path.exists():
        from dotenv import load_dotenv
        load_dotenv(env_path)

    parser = argparse.ArgumentParser(description="PF Trust Bond Platform — Offsite Backup Uploader")
    parser.add_argument("--file",    help="Specific backup file to upload (default: latest)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen, don't upload")
    parser.add_argument("--verify",  action="store_true", help="Verify integrity of last upload")
    parser.add_argument("--create",  action="store_true", help="Create a fresh local backup before uploading")
    args = parser.parse_args()

    if args.verify:
        ok = verify_last()
        sys.exit(0 if ok else 1)

    # Optionally create a backup first
    if args.create:
        log.info("Creating fresh local backup...")
        target = create_local_backup()
    elif args.file:
        target = Path(args.file)
        if not target.is_absolute():
            target = BACKUP_DIR / args.file
    else:
        target = latest_backup()

    if not target or not target.exists():
        log.error("No backup file found in %s — run with --create or run Backup_Encrypted.ps1 first.", BACKUP_DIR)
        sys.exit(1)

    # Ensure sidecar checksum exists for restore-test validation
    sidecar = target.with_suffix(target.suffix + ".sha256")
    if not sidecar.exists():
        log.info("Generating missing sidecar checksum for: %s", target.name)
        write_checksum_sidecar(target, sha256_file(target))

    log.info("=== Offsite Backup Upload ===")
    log.info("source: %s (%s KB)", target.name, target.stat().st_size // 1024)
    log.info("dry_run: %s", args.dry_run)

    try:
        result = upload_file(target, dry_run=args.dry_run)
    except RuntimeError as exc:
        log.error("Upload failed: %s", exc)
        sys.exit(1)

    # Record in manifest
    if not args.dry_run:
        manifest = load_manifest()
        manifest["uploads"].append(result)
        # Keep last 30 upload records
        manifest["uploads"] = manifest["uploads"][-30:]
        manifest["last_success"] = result["timestamp"]
        save_manifest(manifest)
        log.info("manifest updated: %s", MANIFEST)

    log.info("=== Upload complete ===")
    print("\nOffsite backup complete:", json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
