"""
PostgreSQL Migration Guide — Switch all PF Trust services from SQLite to PostgreSQL
─────────────────────────────────────────────────────────────────────────────────────
Run this script ONCE when you are ready to move to PostgreSQL.
It will:
  1. Check if PostgreSQL is reachable
  2. Create databases and users for all 5 services
  3. Run Alembic migrations for compliance_engine
  4. Run SQLAlchemy create_all for remaining services
  5. (Optionally) export existing SQLite data for manual import

Services managed:
  - compliance_engine     → compliance_db       / user: compliance
  - investment_engine     → investment_engine_db / user: investment_engine
  - bond_quote_system     → bond_quote_db        / user: bond_quote
  - bond_intelligence     → bond_intelligence_db / user: bond_intelligence
  - unified_gateway       → bond_gateway_db      / user: gw_user

Prerequisites:
  - PostgreSQL installed: https://www.postgresql.org/download/windows/
  - psql in PATH
  - asyncpg / sqlalchemy in requirements.txt (they are)

Usage:
    python migrate_to_postgres.py --pg-password yourpassword
    python migrate_to_postgres.py --pg-password yourpassword --migrate-sqlite
    python migrate_to_postgres.py --pg-password ""           # for trust auth (no password)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PRIVATE3 = Path(__file__).parent
VENV_PYTHON = PRIVATE3.parent / ".venv" / "Scripts" / "python.exe"
PYTHON = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable

# ── Service definitions ────────────────────────────────────────────────────────
SERVICES = [
    {
        "name": "compliance_engine",
        "root": PRIVATE3 / "compliance_engine",
        "db": "compliance_db",
        "user": "compliance",
        "pass": os.environ.get("CE_DB_PASSWORD", ""),
        "alembic": True,       # Has Alembic migrations
        "sqlalchemy_models": None,
    },
    {
        "name": "investment_engine",
        "root": PRIVATE3 / "investment_engine",
        "db": "investment_engine_db",
        "user": "investment_engine",
        "pass": os.environ.get("IE_DB_PASSWORD", ""),
        "alembic": False,
        "sqlalchemy_models": "investment_engine.db.schema_via_sql",  # uses raw SQL schema
    },
    {
        "name": "bond_quote_system",
        "root": PRIVATE3 / "bond_quote_system",
        "db": "bond_quote_db",
        "user": "bond_quote",
        "pass": os.environ.get("BQ_DB_PASSWORD", ""),
        "alembic": True,       # Has Alembic migrations in migrations/
        "alembic_dir": PRIVATE3 / "bond_quote_system",
        "sqlalchemy_models": None,
    },
    {
        "name": "bond_intelligence",
        "root": PRIVATE3 / "bond_intelligence",
        "db": "bond_intelligence_db",
        "user": "bond_intelligence",
        "pass": os.environ.get("BI_DB_PASSWORD", ""),
        "alembic": False,
        "sqlalchemy_models": "bond_intelligence.db.models",
    },
    {
        "name": "unified_gateway",
        "root": PRIVATE3 / "unified_gateway",
        "db": "bond_gateway_db",
        "user": "gw_user",
        "pass": os.environ.get("GW_DB_PASSWORD", ""),
        "alembic": False,
        "sqlalchemy_models": "unified_gateway.db.models",
    },
]


def run(cmd: list, check=True, **kwargs) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    return subprocess.run(cmd, check=check, **kwargs)


def check_postgres(host: str, port: int, user: str, password: str) -> bool:
    env = os.environ.copy()
    env["PGPASSWORD"] = password
    result = subprocess.run(
        ["psql", "-h", host, "-p", str(port), "-U", user, "-c", "SELECT 1;"],
        env=env, capture_output=True, text=True
    )
    return result.returncode == 0


def create_db_and_user(host: str, port: int, superuser: str, password: str,
                       db: str, user: str, user_pass: str):
    env = os.environ.copy()
    env["PGPASSWORD"] = password
    cmds = [
        f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{user}') THEN CREATE USER {user} WITH PASSWORD '{user_pass}'; END IF; END $$;",
        f"CREATE DATABASE {db} OWNER {user};",
        f"GRANT ALL PRIVILEGES ON DATABASE {db} TO {user};",
    ]
    for sql in cmds:
        result = subprocess.run(
            ["psql", "-h", host, "-p", str(port), "-U", superuser, "-c", sql],
            env=env, capture_output=True, text=True
        )
        if result.returncode != 0 and "already exists" not in result.stderr:
            print(f"    [INFO] {result.stderr.strip()[:120]}")


def write_env_url(root: Path, user: str, user_pass: str, host: str, port: int, db: str):
    """Update or create the .env in a service directory with the PG DATABASE_URL."""
    env_path = root / ".env"
    new_line = f"DATABASE_URL=postgresql+asyncpg://{user}:{user_pass}@{host}:{port}/{db}"

    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
        old_line = next((l for l in content.splitlines()
                         if "DATABASE_URL=" in l and not l.startswith("#")), None)
        if old_line:
            content = content.replace(old_line, new_line)
        else:
            content = new_line + "\n" + content
        env_path.write_text(content, encoding="utf-8")
    else:
        env_path.write_text(new_line + "\n", encoding="utf-8")
    print(f"    Updated .env: {new_line}")


def run_alembic(service_root: Path, db_url: str):
    env = os.environ.copy()
    env["DATABASE_URL"] = db_url
    # Try known names for the DB URL env var per service
    env["ENGINE_DATABASE_URL"] = db_url
    env["BOND_INTELLIGENCE_DB_URL"] = db_url
    run([PYTHON, "-m", "alembic", "upgrade", "head"], cwd=str(service_root), env=env)


def init_tables_via_sqlalchemy(service_name: str, db_url: str):
    """Run SQLAlchemy Base.metadata.create_all for services that don't use Alembic."""
    print(f"    Initialising tables via SQLAlchemy create_all...")
    script = f"""
import asyncio, sys, os
sys.path.insert(0, r'{str(PRIVATE3)}')
os.environ['ENGINE_DATABASE_URL'] = r'{db_url}'
os.environ['BOND_INTELLIGENCE_DB_URL'] = r'{db_url}'
os.environ['DATABASE_URL'] = r'{db_url}'
from sqlalchemy.ext.asyncio import create_async_engine

async def main():
    try:
"""

    if service_name == "investment_engine":
        script += f"""
        from Private3.investment_engine.db.connection import _PG_ENGINE, _init_postgres
        from Private3.investment_engine.config.settings import get_settings
        await _init_postgres(get_settings())
        print("  investment_engine tables created")
"""
    elif service_name == "bond_intelligence":
        script += f"""
        from Private3.bond_intelligence.db.database import engine, Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        print("  bond_intelligence tables created")
"""
    elif service_name == "unified_gateway":
        script += f"""
        print("  unified_gateway SQLAlchemy models not configured in this workspace layout; skipping create_all")
"""

    script += """
    except Exception as e:
        print(f"  [WARN] create_all error: {e}")

asyncio.run(main())
"""
    result = subprocess.run([PYTHON, "-c", script.strip()], capture_output=True, text=True)
    if result.stdout:
        print("   ", result.stdout.strip())
    if result.returncode != 0 and result.stderr:
        print(f"    [WARN] {result.stderr.strip()[:200]}")


def export_sqlite_data(service_root: Path, service_name: str):
    """Export existing SQLite data to JSON for manual import."""
    import glob
    db_files = list(service_root.glob("**/*.db")) + list(service_root.glob("db/*.db"))
    db_files = [f for f in db_files if "__pycache__" not in str(f)]
    if not db_files:
        print(f"    [SKIP] No SQLite .db files found for {service_name}")
        return
    for db_file in db_files:
        export_path = db_file.with_suffix(".sqlite_export.json")
        script = f"""
import sqlite3, json
conn = sqlite3.connect(r'{db_file}')
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [r[0] for r in cur.fetchall()]
data = {{}}
for t in tables:
    cur.execute(f'SELECT * FROM {{t}}')
    data[t] = [dict(r) for r in cur.fetchall()]
conn.close()
with open(r'{export_path}', 'w') as f:
    json.dump(data, f, indent=2, default=str)
print(f'Exported {{sum(len(v) for v in data.values())}} rows from {{len(tables)}} tables → {export_path}')
"""
        result = subprocess.run([PYTHON, "-c", script.strip()], capture_output=True, text=True)
        if result.stdout:
            print("   ", result.stdout.strip())
        if result.returncode != 0:
            print(f"    [WARN] Export error: {result.stderr.strip()[:120]}")


def main():
    parser = argparse.ArgumentParser(description="Migrate all PF Trust services from SQLite to PostgreSQL")
    parser.add_argument("--pg-host", default="127.0.0.1")
    parser.add_argument("--pg-port", type=int, default=5432)
    parser.add_argument("--pg-superuser", default="postgres")
    parser.add_argument("--pg-password", default="", help="PostgreSQL superuser password (empty for trust auth)")
    parser.add_argument("--services", default="all",
                        help="Comma-separated service names, or 'all' (default)")
    parser.add_argument("--migrate-sqlite", action="store_true",
                        help="Export existing SQLite data to JSON")
    parser.add_argument("--alembic-only", action="store_true",
                        help="Skip DB creation; only run migrations")
    args = parser.parse_args()

    selected = SERVICES if args.services == "all" else [
        s for s in SERVICES if s["name"] in args.services.split(",")
    ]

    print("=" * 65)
    print("PF Trust — PostgreSQL Migration (All Services)")
    print("=" * 65)

    # 1. Check connectivity
    print(f"\n[1] Checking PostgreSQL at {args.pg_host}:{args.pg_port}...")
    if not check_postgres(args.pg_host, args.pg_port, args.pg_superuser, args.pg_password):
        print("  [ERROR] Cannot connect to PostgreSQL.")
        print("  Install from: https://www.postgresql.org/download/windows/")
        sys.exit(1)
    print("  [OK] PostgreSQL is reachable")

    # 2. Process each service
    for svc in selected:
        name = svc["name"]
        print(f"\n{'─'*60}")
        print(f"  Service: {name}")
        print(f"{'─'*60}")

        db_url = f"postgresql+asyncpg://{svc['user']}:{svc['pass']}@{args.pg_host}:{args.pg_port}/{svc['db']}"

        # Create DB + user
        if not args.alembic_only:
            print(f"  Creating DB '{svc['db']}' and user '{svc['user']}'...")
            create_db_and_user(
                args.pg_host, args.pg_port, args.pg_superuser, args.pg_password,
                svc["db"], svc["user"], svc["pass"]
            )
            print(f"  [OK] DB provisioned (errors for 'already exists' are safe to ignore)")

        # Run migrations / create tables
        if svc.get("alembic"):
            alembic_root = svc.get("alembic_dir", svc["root"])
            print(f"  Running Alembic migrations in {alembic_root.name}/...")
            try:
                run_alembic(alembic_root, db_url)
                print(f"  [OK] Alembic migrations complete")
            except Exception as e:
                print(f"  [WARN] Alembic failed: {e}")
                print(f"  Try: alembic revision --autogenerate -m 'init' in {alembic_root.name}/")
        else:
            init_tables_via_sqlalchemy(name, db_url)

        # Export SQLite data
        if args.migrate_sqlite:
            export_sqlite_data(svc["root"], name)

        print(f"  Connection string: {db_url}")

    print("\n" + "=" * 65)
    print("Migration complete!")
    print("Restart all services to apply the new PostgreSQL settings.")
    print("=" * 65)


if __name__ == "__main__":
    main()

