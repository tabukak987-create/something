"""
Health Check Framework
Structured health checks for all background tasks, DB connectivity,
Redis, disk space, memory, and downstream service health.
Writes results to service_health_log table.
"""

from __future__ import annotations

import os
import shutil
import time
from typing import Callable, Coroutine, Any
from enum import Enum

from .database import execute_query, fetch_all, engine, _DB_IO_LOCK, IS_SQLITE
from .logger import get_logger
from .time_utils import utcnow_iso

log = get_logger("health")


class HealthStatus(str, Enum):
    OK = "OK"
    WARN = "WARN"
    ERROR = "ERROR"


class HealthResult:
    def __init__(
        self,
        service: str,
        check_type: str,
        status: HealthStatus,
        message: str = "",
        duration_ms: int = 0,
    ):
        self.service = service
        self.check_type = check_type
        self.status = status
        self.message = message
        self.duration_ms = duration_ms
        self.recorded_at = utcnow_iso()

    def to_dict(self) -> dict:
        return {
            "service": self.service,
            "check_type": self.check_type,
            "status": self.status.value,
            "message": self.message,
            "duration_ms": self.duration_ms,
            "recorded_at": self.recorded_at,
        }


async def _persist_result(result: HealthResult) -> None:
    try:
        await execute_query(
            """
            INSERT INTO service_health_log
                (service_name, check_type, status, message, duration_ms)
            VALUES (:service, :check_type, :status, :message, :duration_ms)
            """,
            {
                "service": result.service,
                "check_type": result.check_type,
                "status": result.status.value,
                "message": result.message,
                "duration_ms": result.duration_ms,
            },
        )
    except Exception as exc:
        log.warning("health_persist_failed", error=str(exc))


# ―― Concrete checks ――――――――――――――――――――――――――――――――――――――――――――――――――――――

async def check_database() -> HealthResult:
    """Verify the shared database is alive and reachable."""
    start = time.monotonic()
    try:
        async with _DB_IO_LOCK:
            async with engine.connect() as conn:
                from sqlalchemy import text
                await conn.execute(text("SELECT 1"))
        ms = int((time.monotonic() - start) * 1000)
        result = HealthResult("shared_db", "HEALTHCHECK", HealthStatus.OK, "Database reachable", ms)
    except Exception as exc:
        ms = int((time.monotonic() - start) * 1000)
        result = HealthResult("shared_db", "HEALTHCHECK", HealthStatus.ERROR, str(exc), ms)
        log.error("db_health_failed", error=str(exc))

    await _persist_result(result)
    return result


async def run_timed_task(
    service: str,
    check_type: str,
    coro: Coroutine[Any, Any, Any],
) -> HealthResult:
    """
    Execute *coro*, record timing, and persist pass/fail to health log.

    Use this to wrap scheduled background tasks (e.g. daily market refresh).
    """
    log.info("task_started", service=service, check_type=check_type)
    start = time.monotonic()
    try:
        await coro
        ms = int((time.monotonic() - start) * 1000)
        result = HealthResult(service, check_type, HealthStatus.OK, "Completed successfully", ms)
        log.info("task_completed", service=service, duration_ms=ms)
    except Exception as exc:
        ms = int((time.monotonic() - start) * 1000)
        result = HealthResult(service, check_type, HealthStatus.ERROR, str(exc), ms)
        log.error("task_failed", service=service, error=str(exc), duration_ms=ms)

    await _persist_result(result)
    return result


# ―― Additional Comprehensive Checks ―――――――――――――――――――――――――――――――――――――――

async def check_redis() -> HealthResult:
    """Verify Redis connectivity and response time."""
    start = time.monotonic()
    redis_url = os.getenv("REDIS_URL", "")
    production_mode = os.getenv("GATEWAY_ENV", "development").lower() in {"prod", "production"}
    if not redis_url:
        ms = int((time.monotonic() - start) * 1000)
        severity = HealthStatus.ERROR if production_mode else HealthStatus.WARN
        message = (
            "REDIS_URL not configured in production; Redis-backed token revocation is unavailable"
            if production_mode
            else "REDIS_URL not configured; token revocation is running in memory-only mode"
        )
        result = HealthResult("redis", "HEALTHCHECK", severity, message, ms)
        await _persist_result(result)
        return result
    try:
        import redis.asyncio as aioredis
        client = aioredis.from_url(redis_url, decode_responses=True)
        pong = await client.ping()
        info = await client.info("memory")
        used_mb = info.get("used_memory_human", "?")
        ms = int((time.monotonic() - start) * 1000)
        result = HealthResult("redis", "HEALTHCHECK", HealthStatus.OK,
                              f"PONG received, memory={used_mb}", ms)
        await client.aclose()
    except Exception as exc:
        ms = int((time.monotonic() - start) * 1000)
        result = HealthResult("redis", "HEALTHCHECK", HealthStatus.ERROR, str(exc), ms)
        log.error("redis_health_failed", error=str(exc))
    await _persist_result(result)
    return result


async def check_legacy_portfolio_views() -> HealthResult:
    """Verify compatibility views are present after schema boot."""
    start = time.monotonic()
    expected_views = {"unified_portfolio", "portfolio_current_positions"}
    try:
        if IS_SQLITE:
            rows = await fetch_all(
                """
                SELECT name AS view_name
                FROM sqlite_master
                WHERE type = 'view'
                  AND name IN ('unified_portfolio', 'portfolio_current_positions')
                """
            )
        else:
            rows = await fetch_all(
                """
                SELECT table_name AS view_name
                FROM information_schema.views
                WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                  AND table_name IN ('unified_portfolio', 'portfolio_current_positions')
                """
            )

        found_views = {str(row.get('view_name') or '').strip() for row in rows}
        missing_views = sorted(expected_views - found_views)
        ms = int((time.monotonic() - start) * 1000)
        if missing_views:
            result = HealthResult(
                "shared_db",
                "HEALTHCHECK",
                HealthStatus.ERROR,
                f"Missing compatibility views after boot: {', '.join(missing_views)}",
                ms,
            )
        else:
            result = HealthResult(
                "shared_db",
                "HEALTHCHECK",
                HealthStatus.OK,
                "Compatibility views present: unified_portfolio, portfolio_current_positions",
                ms,
            )
    except Exception as exc:
        ms = int((time.monotonic() - start) * 1000)
        result = HealthResult("shared_db", "HEALTHCHECK", HealthStatus.ERROR, str(exc), ms)
        log.error("legacy_portfolio_views_health_failed", error=str(exc))

    await _persist_result(result)
    return result


async def check_disk_space() -> HealthResult:
    """Check available disk space on the data volume."""
    start = time.monotonic()
    try:
        # Check the drive where the application data resides
        data_path = os.getenv("DATA_PATH", os.path.dirname(os.path.abspath(__file__)))
        usage = shutil.disk_usage(data_path)
        free_gb = usage.free / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)
        pct_free = (usage.free / usage.total) * 100
        ms = int((time.monotonic() - start) * 1000)

        if pct_free < 5:
            status = HealthStatus.ERROR
            msg = f"CRITICAL: {free_gb:.1f}GB free ({pct_free:.1f}%) of {total_gb:.1f}GB"
        elif pct_free < 15:
            status = HealthStatus.WARN
            msg = f"LOW: {free_gb:.1f}GB free ({pct_free:.1f}%) of {total_gb:.1f}GB"
        else:
            status = HealthStatus.OK
            msg = f"{free_gb:.1f}GB free ({pct_free:.1f}%) of {total_gb:.1f}GB"

        result = HealthResult("disk", "HEALTHCHECK", status, msg, ms)
    except Exception as exc:
        ms = int((time.monotonic() - start) * 1000)
        result = HealthResult("disk", "HEALTHCHECK", HealthStatus.ERROR, str(exc), ms)
    await _persist_result(result)
    return result


async def check_memory() -> HealthResult:
    """Check process memory usage."""
    start = time.monotonic()
    try:
        import psutil  # type: ignore[import-not-found]
        proc = psutil.Process()
        mem = proc.memory_info()
        rss_mb = mem.rss / (1024 ** 2)
        sys_mem = psutil.virtual_memory()
        sys_pct = sys_mem.percent
        ms = int((time.monotonic() - start) * 1000)

        if sys_pct > 90:
            status = HealthStatus.ERROR
            msg = f"CRITICAL: System {sys_pct}% used, process RSS={rss_mb:.0f}MB"
        elif sys_pct > 75:
            status = HealthStatus.WARN
            msg = f"HIGH: System {sys_pct}% used, process RSS={rss_mb:.0f}MB"
        else:
            status = HealthStatus.OK
            msg = f"System {sys_pct}% used, process RSS={rss_mb:.0f}MB"

        result = HealthResult("memory", "HEALTHCHECK", status, msg, ms)
    except ImportError:
        ms = int((time.monotonic() - start) * 1000)
        result = HealthResult("memory", "HEALTHCHECK", HealthStatus.WARN,
                              "psutil not installed; memory check skipped", ms)
    except Exception as exc:
        ms = int((time.monotonic() - start) * 1000)
        result = HealthResult("memory", "HEALTHCHECK", HealthStatus.ERROR, str(exc), ms)
    await _persist_result(result)
    return result


async def check_downstream_service(name: str, url: str, timeout: float = 10.0) -> HealthResult:
    """Check if a downstream microservice is reachable via its health endpoint."""
    start = time.monotonic()
    try:
        import httpx
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            ms = int((time.monotonic() - start) * 1000)
            if resp.status_code < 400:
                result = HealthResult(name, "DOWNSTREAM", HealthStatus.OK,
                                      f"HTTP {resp.status_code}, {ms}ms", ms)
            else:
                result = HealthResult(name, "DOWNSTREAM", HealthStatus.WARN,
                                      f"HTTP {resp.status_code}", ms)
    except ImportError:
        # httpx not available, try requests
        try:
            import aiohttp  # type: ignore[import-not-found]
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    ms = int((time.monotonic() - start) * 1000)
                    if resp.status < 400:
                        result = HealthResult(name, "DOWNSTREAM", HealthStatus.OK,
                                              f"HTTP {resp.status}, {ms}ms", ms)
                    else:
                        result = HealthResult(name, "DOWNSTREAM", HealthStatus.WARN,
                                              f"HTTP {resp.status}", ms)
        except Exception as exc:
            ms = int((time.monotonic() - start) * 1000)
            result = HealthResult(name, "DOWNSTREAM", HealthStatus.ERROR, str(exc), ms)
    except Exception as exc:
        ms = int((time.monotonic() - start) * 1000)
        result = HealthResult(name, "DOWNSTREAM", HealthStatus.ERROR, str(exc), ms)
        log.warning("downstream_check_failed", service=name, error=str(exc))
    await _persist_result(result)
    return result


async def check_all_downstream_services() -> list[HealthResult]:
    """Check all configured downstream microservices."""
    services = {
        "compliance_engine": os.getenv("COMPLIANCE_ENGINE_URL", "http://localhost:8100") + "/health",
        "bond_intelligence": os.getenv("BOND_INTELLIGENCE_URL", "http://localhost:8500") + "/health",
        "bond_quote_system": os.getenv("BOND_QUOTE_URL", "http://localhost:8000") + "/health",
        "institutional_fis": os.getenv("INSTITUTIONAL_FIS_URL", "http://localhost:8600") + "/health",
        "investment_engine": os.getenv("INVESTMENT_ENGINE_URL", "http://localhost:8900") + "/health",
    }
    results = []
    for name, url in services.items():
        result = await check_downstream_service(name, url, timeout=10.0)
        results.append(result)
    return results


async def run_comprehensive_health() -> dict:
    """Run all health checks and return a consolidated report."""
    results = {}

    # Core checks
    db_result = await check_database()
    results["database"] = db_result.to_dict()

    redis_result = await check_redis()
    results["redis"] = redis_result.to_dict()

    disk_result = await check_disk_space()
    results["disk"] = disk_result.to_dict()

    mem_result = await check_memory()
    results["memory"] = mem_result.to_dict()

    # Downstream services
    downstream = await check_all_downstream_services()
    results["downstream"] = {r.service: r.to_dict() for r in downstream}

    # Overall status
    all_results = [db_result, redis_result, disk_result, mem_result] + downstream
    if any(r.status == HealthStatus.ERROR for r in all_results):
        overall = HealthStatus.ERROR
    elif any(r.status == HealthStatus.WARN for r in all_results):
        overall = HealthStatus.WARN
    else:
        overall = HealthStatus.OK

    return {
        "overall": overall.value,
        "timestamp": utcnow_iso(),
        "checks": results,
    }


async def get_health_summary(last_n: int = 50) -> dict:
    """Return the latest *last_n* health events for the system status API."""
    rows = await fetch_all(
        """
        SELECT service_name, check_type, status, message, duration_ms, recorded_at
        FROM   service_health_log
        ORDER  BY recorded_at DESC
        LIMIT  :n
        """,
        {"n": last_n},
    )
    services: dict[str, str] = {}
    for r in rows:
        svc = r["service_name"]
        if svc not in services:
            services[svc] = r["status"]

    overall = (
        HealthStatus.ERROR if HealthStatus.ERROR.value in services.values()
        else HealthStatus.WARN if HealthStatus.WARN.value in services.values()
        else HealthStatus.OK
    )
    return {
        "overall": overall.value,
        "services": services,
        "recent_events": rows,
    }
