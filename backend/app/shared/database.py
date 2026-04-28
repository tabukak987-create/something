"""
Shared Database Layer — Universal PostgreSQL / SQLite Backend
All gateway services share a single connection pool through this module.

Environment variables (first match wins):
  GATEWAY_DB_URL        — Full SQLAlchemy async URL (preferred)
  DATABASE_URL          — Heroku / Railway compatible postgres:// URL
  BOND_INTELLIGENCE_DB_URL  — Legacy compatibility

SQLite fallback is used automatically in development when none are set.
Production environments must set an explicit database URL.
"""

import asyncio
import os
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

import aiosqlite
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import text

from .logger import get_logger

log = get_logger("shared.database")

_BASE_DIR = Path(__file__).parent.parent
_SQLITE_PATH = _BASE_DIR / "data" / "bond_gateway.db"


def _is_production_environment() -> bool:
    return os.getenv("GATEWAY_ENV", "development").lower() in {"prod", "production"}


def _resolve_url() -> str:
    for var in ("GATEWAY_DB_URL", "DATABASE_URL", "BOND_INTELLIGENCE_DB_URL"):
        raw = os.getenv(var, "")
        if raw:
            # Normalise plain postgres:// → postgresql+asyncpg://
            raw = raw.replace("postgres://", "postgresql+asyncpg://", 1)
            raw = raw.replace("postgresql://", "postgresql+asyncpg://", 1)
            return raw
    if _is_production_environment():
        import sys

        print(
            "FATAL: No database URL configured in production. "
            "Set GATEWAY_DB_URL (or DATABASE_URL) to a PostgreSQL/SQLAlchemy URL.",
            file=sys.stderr,
        )
        sys.exit(1)
    # H5 Remediation: Warn loudly if falling back to SQLite
    import sys
    _force_pg = os.getenv("FORCE_POSTGRESQL", "false").lower() == "true"
    if _force_pg:
        print("FATAL: FORCE_POSTGRESQL=true but no GATEWAY_DB_URL set. Exiting.", file=sys.stderr)
        sys.exit(1)
    fallback_message = (
        "No database URL configured — using SQLite fallback. "
        "Set GATEWAY_DB_URL=postgresql+asyncpg://... for production."
    )
    if _is_production_environment():
        print(f"WARNING: {fallback_message}", file=sys.stderr)
    else:
        print(f"INFO: {fallback_message}", file=sys.stderr)
    _SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{_SQLITE_PATH}"


DATABASE_URL: str = _resolve_url()
IS_SQLITE: bool = "sqlite" in DATABASE_URL

_engine_kwargs: dict = {"echo": False, "pool_pre_ping": False}
if IS_SQLITE:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    # M5 Remediation: Explicit connection pool configuration for PostgreSQL
    _engine_kwargs["pool_size"] = int(os.getenv("DB_POOL_SIZE", "15"))
    _engine_kwargs["max_overflow"] = int(os.getenv("DB_MAX_OVERFLOW", "30"))
    _engine_kwargs["pool_recycle"] = int(os.getenv("DB_POOL_RECYCLE", "1800"))  # 30 min
    _engine_kwargs["pool_timeout"] = int(os.getenv("DB_POOL_TIMEOUT", "30"))    # Wait max 30s
    _engine_kwargs["pool_pre_ping"] = os.getenv("DB_POOL_PRE_PING", "false").lower() == "true"

    # Log pool configuration for observability
    print(
        f"DB Pool: size={_engine_kwargs['pool_size']}, "
        f"overflow={_engine_kwargs['max_overflow']}, "
        f"recycle={_engine_kwargs['pool_recycle']}s, "
        f"timeout={_engine_kwargs['pool_timeout']}s"
    )

engine: AsyncEngine = create_async_engine(DATABASE_URL, **_engine_kwargs)
async_session: async_sessionmaker = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)
_DB_IO_LOCK = asyncio.Lock()
_CURRENT_SESSION: ContextVar[AsyncSession | None] = ContextVar("shared_database_current_session", default=None)


async def get_session() -> AsyncGenerator[AsyncSession, None]:           # FastAPI Depends()
    async with async_session() as session:
        yield session


def transactional_session(func):
    """Run an async function inside a shared database session and transaction."""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        async with async_session() as session:
            token = _CURRENT_SESSION.set(session)
            try:
                async with session.begin():
                    return await func(*args, **kwargs)
            finally:
                _CURRENT_SESSION.reset(token)

    return wrapper


# ──────────────────────────────────────────────────────────────────────────────
# Lightweight helpers (no ORM models needed for raw queries)
# ──────────────────────────────────────────────────────────────────────────────

def _adapt_query(query: str) -> str:
    """Adapt SQLite-flavoured SQL to PostgreSQL at runtime."""
    if not IS_SQLITE:
        if "AUTOINCREMENT" in query.upper() or "CREATE TABLE" in query.upper():
            query = _adapt_ddl_for_postgres(query)
        import re
        # All date/time columns are stored as TEXT (SQLite schema), so we must
        # compare against TEXT representations, not native DATE/TIMESTAMP types.
        # CURRENT_DATE → TO_CHAR(CURRENT_DATE,'YYYY-MM-DD') (TEXT-compatible)
        # CURRENT_TIMESTAMP → TO_CHAR(NOW(),'YYYY-MM-DD HH24:MI:SS') (TEXT-compatible)

        # datetime('now', '-N days') → (TO_CHAR(NOW() - INTERVAL 'N days','YYYY-MM-DD HH24:MI:SS'))
        query = re.sub(
            r"datetime\('now',\s*'-(\d+)\s*days?'\)",
            lambda m: f"(TO_CHAR(NOW() - INTERVAL '{m.group(1)} days','YYYY-MM-DD HH24:MI:SS'))",
            query,
        )
        # datetime('now') → TO_CHAR(NOW(),'YYYY-MM-DD HH24:MI:SS')
        query = query.replace("datetime('now')", "TO_CHAR(NOW(),'YYYY-MM-DD HH24:MI:SS')")

        # date('now', '-N days') → TO_CHAR(CURRENT_DATE - INTERVAL 'N days','YYYY-MM-DD')  [literal N]
        query = re.sub(
            r"date\('now',\s*'-(\d+)\s*days?'\)",
            lambda m: f"TO_CHAR(CURRENT_DATE - INTERVAL '{m.group(1)} days','YYYY-MM-DD')",
            query,
        )
        # date('now', :param) → TO_CHAR(CURRENT_DATE + :param::interval,'YYYY-MM-DD')  [dynamic param]
        query = re.sub(
            r"date\('now',\s*(:[\w]+)\)",
            lambda m: f"TO_CHAR(CURRENT_DATE + {m.group(1)}::interval,'YYYY-MM-DD')",
            query,
        )
        # Plain date('now') → TO_CHAR(CURRENT_DATE,'YYYY-MM-DD')
        query = re.sub(r"date\('now'\)", "TO_CHAR(CURRENT_DATE,'YYYY-MM-DD')", query)
    return query


async def fetch_all(query: str, params: Optional[dict] = None) -> list[dict]:
    query = _adapt_query(query)
    current_session = _CURRENT_SESSION.get()
    if current_session is not None:
        result = await current_session.execute(text(query), params if params is not None else {})
        rows = result.fetchall()
        keys = result.keys()
        return [dict(zip(keys, row)) for row in rows]
    async with _DB_IO_LOCK:
        async with async_session() as session:
            result = await session.execute(text(query), params if params is not None else {})
            rows = result.fetchall()
            keys = result.keys()
            return [dict(zip(keys, row)) for row in rows]


async def fetch_one(query: str, params: Optional[dict] = None) -> Optional[dict]:
    rows = await fetch_all(query, params)
    return rows[0] if rows else None


async def execute_query(query: str, params: Optional[dict] = None) -> Any:
    query = _adapt_query(query)
    current_session = _CURRENT_SESSION.get()
    if current_session is not None:
        return await current_session.execute(text(query), params if params is not None else {})
    async with _DB_IO_LOCK:
        async with async_session() as session:
            result = await session.execute(text(query), params if params is not None else {})
            await session.commit()
            return result


async def execute_ddl(query: str, params: Optional[dict] = None) -> Any:
    """Execute DDL through a direct engine connection."""
    query = _adapt_query(query)
    current_session = _CURRENT_SESSION.get()
    if current_session is not None:
        return await current_session.execute(text(query), params if params is not None else {})
    async with _DB_IO_LOCK:
        async with engine.begin() as conn:
            return await conn.execute(text(query), params if params is not None else {})


async def _drop_relation_if_exists(name: str) -> None:
    """Drop a legacy table or view regardless of which object type exists."""
    if IS_SQLITE:
        await execute_ddl(f"DROP VIEW IF EXISTS {name}")
        await execute_ddl(f"DROP TABLE IF EXISTS {name}")
        return

    for statement in (
        f"DROP VIEW IF EXISTS {name} CASCADE",
        f"DROP TABLE IF EXISTS {name} CASCADE",
    ):
        try:
            await execute_ddl(statement)
            return
        except Exception as exc:
            message = str(exc).lower()
            if "is not a view" in message or "is not a table" in message:
                continue
            raise


async def _ensure_legacy_portfolio_views() -> None:
    """Create compatibility views used by legacy portfolio, risk, and report queries."""

    async def _table_exists(table_name: str) -> bool:
        if IS_SQLITE:
            rows = await fetch_all(
                """
                SELECT name AS table_name
                FROM sqlite_master
                WHERE type = 'table' AND name = :table_name
                """,
                {"table_name": table_name},
            )
        else:
            rows = await fetch_all(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                  AND table_name = :table_name
                """,
                {"table_name": table_name},
            )
        return bool(rows)

    for table_name in (
        "portfolio_holdings",
        "instrument_master",
        "universal_bond_master",
        "market_prices",
    ):
        if not await _table_exists(table_name):
            log.warning("legacy_portfolio_views_skipped", missing_table=table_name)
            return

    await _drop_relation_if_exists("portfolio_current_positions")
    await _drop_relation_if_exists("unified_portfolio")

    unified_portfolio_sql = """
        CREATE VIEW unified_portfolio AS
        SELECT
            im.isin AS isin,
            im.issuer AS issuer_name,
            COALESCE(ubm.face_value, 100.0) AS face_value,
            SUM(
                COALESCE(ph.quantity, 0.0)
                * COALESCE(ubm.face_value, 100.0)
                * COALESCE(ph.purchase_price, 0.0)
                / 100.0
            ) AS book_value,
            SUM(COALESCE(ph.quantity, 0.0)) AS quantity,
            SUM(
                COALESCE(ph.quantity, 0.0)
                * COALESCE(ubm.face_value, 100.0)
                * COALESCE(lp.price, ph.purchase_price, 0.0)
                / 100.0
            ) AS market_value,
            MIN(ph.purchase_date) AS purchase_date,
            im.maturity_date AS maturity_date,
            im.coupon_rate AS coupon_rate,
            NULL AS ytm_at_purchase,
            im.category AS category,
            im.sector AS sector,
            im.rating_latest AS rating,
            CURRENT_TIMESTAMP AS last_updated
        FROM portfolio_holdings ph
        INNER JOIN instrument_master im ON im.isin = ph.isin
        LEFT JOIN universal_bond_master ubm ON ubm.isin = ph.isin
        LEFT JOIN (
            SELECT
                m.isin,
                (
                    SELECT m2.price
                    FROM market_prices m2
                    WHERE m2.isin = m.isin
                    ORDER BY m2.date DESC, COALESCE(m2.source, '') DESC
                    LIMIT 1
                ) AS price
            FROM market_prices m
            GROUP BY m.isin
        ) lp ON lp.isin = ph.isin
        GROUP BY
            im.isin,
            im.issuer,
            ubm.face_value,
            im.maturity_date,
            im.coupon_rate,
            im.category,
            im.sector,
            im.rating_latest
    """
    await execute_ddl(unified_portfolio_sql)
    await execute_ddl("CREATE VIEW portfolio_current_positions AS SELECT * FROM unified_portfolio")


def _adapt_ddl_for_postgres(ddl: str) -> str:
    """Convert SQLite-flavoured DDL to PostgreSQL-compatible DDL."""
    import re
    # Replace AUTOINCREMENT with SERIAL PRIMARY KEY
    ddl = re.sub(
        r'INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT',
        'SERIAL PRIMARY KEY',
        ddl,
        flags=re.IGNORECASE,
    )
    # Replace datetime('now') with CURRENT_TIMESTAMP
    ddl = ddl.replace("datetime('now')", "CURRENT_TIMESTAMP")
    # Remove REFERENCES ... ON DELETE CASCADE for CREATE TABLE (PG needs tables to exist first)
    # Actually PG supports it fine if tables are created in order, keep it
    return ddl


async def _table_has_column(table_name: str, column_name: str) -> bool:
    if IS_SQLITE:
        rows = await fetch_all(f"PRAGMA table_info({table_name})")
        return any(str(row.get("name") or "").lower() == column_name.lower() for row in rows)

    rows = await fetch_all(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = :table_name
          AND column_name = :column_name
        LIMIT 1
        """,
        {"table_name": table_name, "column_name": column_name},
    )
    return bool(rows)


async def _ensure_unified_alerts_spread_metadata() -> None:
    if not await _table_has_column("unified_alerts", "source_price_date"):
        try:
            await execute_ddl("ALTER TABLE unified_alerts ADD COLUMN source_price_date TEXT")
        except Exception:
            pass

    for column_name in ("issuer_name", "issuer_type", "credit_rating", "instrument_category"):
        if not await _table_has_column("unified_alerts", column_name):
            try:
                await execute_ddl(f"ALTER TABLE unified_alerts ADD COLUMN {column_name} TEXT")
            except Exception:
                pass

    for table_name in ("universal_bond_master", "broker_daily_quotes"):
        if not await _table_has_column(table_name, "instrument_category"):
            try:
                await execute_ddl(f"ALTER TABLE {table_name} ADD COLUMN instrument_category TEXT")
            except Exception:
                pass

    try:
        await execute_ddl("CREATE INDEX IF NOT EXISTS idx_ua_source_price_date ON unified_alerts(source_price_date)")
    except Exception:
        pass


async def _ensure_user_auth_metadata() -> None:
    """Keep auth-related columns available even when an older SQLite DB is reused."""
    if not await _table_has_column("users", "password_changed_at"):
        try:
            await execute_ddl("ALTER TABLE users ADD COLUMN password_changed_at TEXT")
        except Exception:
            pass


async def init_database() -> None:
    """Create the shared schema (universal_bond_master + related tables)."""
    schema_path = Path(__file__).parent / "schema.sql"
    if not schema_path.exists():
        return
    ddl = schema_path.read_text(encoding="utf-8")

    if IS_SQLITE:
        db_path = _SQLITE_PATH
        async with _DB_IO_LOCK:
            async with aiosqlite.connect(str(db_path)) as db:
                for stmt in [s.strip() for s in ddl.split(";") if s.strip()]:
                    try:
                        await db.execute(stmt + ";")
                    except Exception:
                        pass
                await db.commit()
    else:
        ddl = _adapt_ddl_for_postgres(ddl)
        for stmt in [s.strip() for s in ddl.split(";") if s.strip()]:
            try:
                await execute_ddl(stmt)
            except Exception:
                pass

    await _ensure_unified_alerts_spread_metadata()
    await _ensure_user_auth_metadata()

    await _ensure_legacy_portfolio_views()
