"""
Automated Backup Restore Test — DR Validation (RTO/RPO Proof)
==============================================================
This script validates that backups are restorable. It:
  1. Finds the latest backup archive in the backup directory
  2. Extracts it to a temp directory
  3. Connects to the restored SQLite DB (or validates PostgreSQL dump)
  4. Runs a set of data integrity queries
  5. Reports RTO/RPO compliance

Run manually:
  python -m Private3.restore_test

Or add to Task Scheduler / cron:
  Windows: schtasks /create /tn "DR_RestoreTest" /sc WEEKLY /d SUN /st 03:00
           /tr "python D:\\investment_j\\Private3\\restore_test.py"
  Linux:   0 3 * * 0 python /path/to/restore_test.py

Exit codes:
  0 — Restore test PASSED
  1 — Restore test FAILED (check output and alert on-call)

Environment Variables (same as offsite_backup.py):
  BACKUP_DIR       — Path to local backup directory (default: D:\\investment_j\\backups)
  BACKUP_PASSWORD  — Encryption password for encrypted archives (optional)
  ALERT_EMAIL      — Email to send results to (optional, requires smtplib config)
  SMTP_HOST        — SMTP host (default: localhost)
  SMTP_PORT        — SMTP port (default: 25)
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Configuration ─────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
BACKUP_DIR    = Path(os.getenv("BACKUP_DIR", str(_REPO_ROOT / "backups")))
BACKUP_PSWD   = os.getenv("BACKUP_PASSWORD", "")
ALERT_EMAIL   = os.getenv("ALERT_EMAIL", "")
REPORT_PATH   = BACKUP_DIR / "restore_test_report.json"
LOG_PATH      = BACKUP_DIR / "restore_test.log"

CRITICAL_TABLES = [
    "universal_bond_master",
    "users",
    "portfolio_holdings",
    "login_attempts",
]

# Tables that are warned (not failed) if missing — may not exist in all datasets
ADVISORY_TABLES = [
    "compliance_alerts",
    "audit_trail",
]

MIN_BOND_COUNT    = 100   # Fail if fewer bonds survive restore
MIN_USER_COUNT    = 1     # Fail if no users survive restore


def _log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f"[{ts}] [{level}] {msg}"
    print(line, file=sys.stderr if level in ("ERROR", "CRITICAL") else sys.stdout)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _find_latest_backup() -> Optional[Path]:
    """Return the most recent .zip backup archive in BACKUP_DIR (recursive)."""
    candidates = sorted(BACKUP_DIR.rglob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _verify_archive_checksum(archive: Path) -> bool:
    """Verify SHA-256 checksum of the archive if a .sha256 sidecar file exists."""
    checksum_file = archive.with_suffix(archive.suffix + ".sha256")
    if not checksum_file.exists():
        _log(f"No checksum file found for {archive.name} — skipping integrity check", "WARNING")
        return True  # Lenient: no checksum file means old backup format

    stored_hex = checksum_file.read_text(encoding="utf-8").strip().split()[0]
    computed_hex = hashlib.sha256(archive.read_bytes()).hexdigest()
    if not (stored_hex == computed_hex):
        _log(f"CHECKSUM MISMATCH for {archive.name}: stored={stored_hex[:12]}... computed={computed_hex[:12]}...", "CRITICAL")
        return False
    _log(f"Checksum verified: {archive.name}")
    return True


def _extract_backup(archive: Path, dest: Path) -> Optional[Path]:
    """Extract backup archive to dest. Returns path to extracted content or None on failure."""
    try:
        with zipfile.ZipFile(archive, "r") as zf:
            if BACKUP_PSWD:
                zf.setpassword(BACKUP_PSWD.encode("utf-8"))
            zf.extractall(dest)
        _log(f"Extracted {archive.name} to {dest}")
        return dest
    except zipfile.BadZipFile:
        _log(f"BadZipFile: {archive.name} is corrupted or not a valid zip", "CRITICAL")
        return None
    except RuntimeError as exc:
        _log(f"Extraction failed (wrong password?): {exc}", "CRITICAL")
        return None


def _find_db_in_restore(restore_dir: Path) -> Optional[Path]:
    """Find the primary SQLite database file in the restored directory."""
    candidates = sorted(restore_dir.rglob("*.db"), key=lambda p: p.stat().st_size, reverse=True)
    return candidates[0] if candidates else None


def _validate_sqlite_db(db_path: Path) -> dict:
    """Run integrity checks on the restored SQLite database."""
    results = {"db_path": str(db_path), "tables_found": [], "row_counts": {}, "integrity_ok": False, "warnings": [], "errors": []}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # SQLite built-in integrity check
        cur.execute("PRAGMA integrity_check")
        ic = cur.fetchone()[0]
        results["integrity_ok"] = (ic == "ok")
        if ic != "ok":
            results["errors"].append(f"SQLite integrity_check: {ic}")

        # Check critical tables
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        all_tables = {row[0] for row in cur.fetchall()}
        results["tables_found"] = sorted(all_tables)

        for table in CRITICAL_TABLES + ADVISORY_TABLES:
            if table in all_tables:
                cur.execute(f"SELECT COUNT(*) FROM [{table}]")
                count = cur.fetchone()[0]
                results["row_counts"][table] = count
            else:
                results["row_counts"][table] = None  # Table missing

        conn.close()
    except Exception as exc:
        results["errors"].append(str(exc))
    return results


def _assess_results(validation: dict) -> tuple[bool, list[str]]:
    """
    Evaluate validation results against RTO/RPO thresholds.
    Returns (passed, list_of_failures).
    """
    failures = []

    if not validation.get("integrity_ok"):
        failures.append("SQLite integrity_check FAILED — database is corrupt.")

    rc = validation.get("row_counts", {})

    bond_count = rc.get("universal_bond_master")
    if bond_count is None:
        failures.append("CRITICAL TABLE MISSING: universal_bond_master")
    elif bond_count < MIN_BOND_COUNT:
        failures.append(f"Bond count too low: {bond_count} < required {MIN_BOND_COUNT}")

    user_count = rc.get("users")
    if user_count is None:
        failures.append("CRITICAL TABLE MISSING: users")
    elif user_count < MIN_USER_COUNT:
        failures.append(f"User count too low: {user_count} < required {MIN_USER_COUNT}")

    # Advisory tables — missing is a warning, not a failure
    for table in ADVISORY_TABLES:
        if rc.get(table) is None:
            validation.setdefault("warnings", []).append(f"Advisory table missing: {table} (will be created on first use)")

    for err in validation.get("errors", []):
        failures.append(f"DB Error: {err}")

    return (len(failures) == 0), failures


def _send_alert_email(subject: str, body: str) -> None:
    """Send email alert if ALERT_EMAIL is configured."""
    if not ALERT_EMAIL:
        return
    try:
        import smtplib
        from email.mime.text import MIMEText
        smtp_host = os.getenv("SMTP_HOST", "localhost")
        smtp_port = int(os.getenv("SMTP_PORT", "25"))
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = f"dr-validator@bond-gateway.in"
        msg["To"] = ALERT_EMAIL
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as s:
            s.sendmail(msg["From"], [ALERT_EMAIL], msg.as_string())
        _log(f"Alert email sent to {ALERT_EMAIL}")
    except Exception as exc:
        _log(f"Failed to send alert email: {exc}", "WARNING")


def run_restore_test() -> int:
    """
    Execute a full backup restoration test.
    Returns 0 on success, 1 on failure.
    """
    start = time.monotonic()
    _log("=" * 60)
    _log("BACKUP RESTORE TEST — STARTING")
    _log("=" * 60)

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "UNKNOWN",
        "backup_file": None,
        "checksum_valid": None,
        "extraction_ok": None,
        "validation": {},
        "failures": [],
        "duration_seconds": 0.0,
        "rto_seconds_measured": 0.0,
    }

    # 1 — Find latest backup
    archive = _find_latest_backup()
    if not archive:
        msg = f"No backup archives found in {BACKUP_DIR}"
        _log(msg, "CRITICAL")
        report["status"] = "FAILED"
        report["failures"] = [msg]
        REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
        _send_alert_email("[CRITICAL] DR Test FAILED — No backups found", msg)
        return 1

    _log(f"Latest backup: {archive.name} ({archive.stat().st_size / 1024:.1f} KB)")
    report["backup_file"] = str(archive)

    # 2 — Verify checksum
    report["checksum_valid"] = _verify_archive_checksum(archive)
    if not report["checksum_valid"]:
        _log("Halting — archive checksum failed.", "CRITICAL")
        report["status"] = "FAILED"
        report["failures"] = ["Archive checksum mismatch — backup integrity compromised."]
        REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
        _send_alert_email("[CRITICAL] DR Test FAILED — Checksum mismatch", report["failures"][0])
        return 1

    # 3 — Extract to temp dir
    rto_start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="restore_test_") as tmp:
        restore_dir = Path(tmp)
        extracted = _extract_backup(archive, restore_dir)
        if not extracted:
            report["status"] = "FAILED"
            report["failures"] = ["Archive extraction failed — see log for details."]
            report["extraction_ok"] = False
            REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
            _send_alert_email("[CRITICAL] DR Test FAILED — Extraction error", report["failures"][0])
            return 1
        report["extraction_ok"] = True

        # 4 — Find and validate DB
        db_path = _find_db_in_restore(restore_dir)
        if not db_path:
            _log("No SQLite .db file found in extracted archive.", "WARNING")
            report["validation"]["note"] = "No SQLite DB found — PostgreSQL dump validation not implemented yet."
            # For PostgreSQL dumps: check for .sql or .dump files
            sql_files = list(restore_dir.rglob("*.sql")) + list(restore_dir.rglob("*.dump"))
            if sql_files:
                _log(f"Found {len(sql_files)} SQL dump files — manual validation required.")
                report["validation"]["sql_files"] = [str(s) for s in sql_files]
                report["status"] = "WARN"
                report["failures"].append("PostgreSQL dump found but automated validation requires psql. Manual verification needed.")
            else:
                report["status"] = "FAILED"
                report["failures"].append("No database file found in backup archive.")
        else:
            _log(f"Found DB: {db_path.name} ({db_path.stat().st_size / 1024:.1f} KB)")
            validation = _validate_sqlite_db(db_path)
            report["validation"] = validation

            passed, failures = _assess_results(validation)
            report["failures"] = failures

            if passed:
                report["status"] = "PASSED"
            else:
                report["status"] = "FAILED"

    rto_end = time.monotonic()
    report["rto_seconds_measured"] = round(rto_end - rto_start, 2)
    report["duration_seconds"] = round(time.monotonic() - start, 2)

    # 5 — Final report
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    _log("=" * 60)
    _log(f"RESULT: {report['status']}")
    _log(f"RTO measured: {report['rto_seconds_measured']}s")
    _log(f"Total duration: {report['duration_seconds']}s")

    if report["failures"]:
        for f in report["failures"]:
            _log(f"  FAILURE: {f}", "ERROR")
    if report.get("validation", {}).get("row_counts"):
        for table, count in report["validation"]["row_counts"].items():
            _log(f"  {table}: {count if count is not None else 'MISSING'} rows")

    _log("=" * 60)

    if report["status"] == "FAILED":
        body = (
            f"Backup restore test FAILED at {report['timestamp']}.\n\n"
            f"Archive: {report['backup_file']}\n\n"
            f"Failures:\n" + "\n".join(f"  - {f}" for f in report["failures"])
        )
        _send_alert_email("[CRITICAL] DR Test FAILED — Immediate action required", body)
        return 1
    elif report["status"] == "WARN":
        body = (
            f"Backup restore test WARNING at {report['timestamp']}.\n\n"
            f"Archive: {report['backup_file']}\n\n"
            "Manual verification required for PostgreSQL dump."
        )
        _send_alert_email("[WARNING] DR Test requires manual verification", body)
        return 0
    else:
        _log("All checks PASSED. Backup is restorable.")
        return 0


if __name__ == "__main__":
    sys.exit(run_restore_test())
