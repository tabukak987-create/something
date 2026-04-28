"""
Unified Bond Investment Gateway — Main Application
===================================================
Single FastAPI app that consolidates:
  • Bond Intelligence System   → /api/v1/intelligence
  • Compliance Engine          → /api/v1/compliance
  • Bond Quote System          → /api/v1/quotes
  • Bond Agent (Daily Recs)    → /api/v1/agent
  • WebSocket live prices      → /ws/{topic}
  • System health & health log → /api/v1/health

Run:
    uvicorn backend.main:app --host 0.0.0.0 --port 9000 --reload

Or via start_gateway.bat
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Ensure `app.*` imports resolve when launched as `uvicorn backend.main:app`.
_BACKEND_DIR = Path(__file__).resolve().parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ── Load .env before any other module reads os.environ ────────────────────────
_bootstrap_info = None
try:
    from app.shared.env_bootstrap import bootstrap_local_environment

    _bootstrap_info = bootstrap_local_environment(Path(__file__).resolve().parents[1])
except Exception as exc:
    print(f"ENV bootstrap failed: {exc}", file=sys.stderr)
    raise

try:
    from dotenv import load_dotenv as _load_dotenv
    _env_candidates = [
        Path(__file__).resolve().parents[1] / '.env',
        Path(__file__).resolve().parent / '.env',
        Path.cwd() / '.env',
    ]
    for _env_path in _env_candidates:
        if _env_path.exists():
            _load_dotenv(dotenv_path=_env_path, override=False)
except Exception:
    pass  # dotenv optional — env vars may be set by OS/shell

if isinstance(globals().get("_bootstrap_info"), dict) and _bootstrap_info.get("generated_admin_password"):
    print("ADMIN LOGIN CREATED")
    print(f"username: {_bootstrap_info['admin_username']}")
    print(f"password: {_bootstrap_info['admin_password']}")

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ── Shared layer ─────────────────────────────────────────────────────────────
from app.shared.logger import configure_logging
from app.shared.database import init_database, fetch_one
from app.shared.health import (
    check_database,
    check_legacy_portfolio_views,
    check_redis,
    HealthStatus,
    run_timed_task,
    get_health_summary,
    run_comprehensive_health,
)
from app.shared.deployment import deployment_mode, external_data_egress_enabled
from app.shared.websocket_manager import ws_manager
from app.shared.isin_registry import ISINRegistry
from app.shared.auth import (
    router as auth_router,
    hash_password,
    require_role,
    Roles,
    TokenBlacklist,
    validate_token_session,
)
from app.shared.metrics import metrics_router, collector as metrics_collector
from fastapi import Request
from app.domain.core_trust.rate_limiter import check_rate_limit
from app.shared.router_registry import register_gateway_routes
from app.api.api_versioning import VersionNegotiationMiddleware

# ── Enhanced engines ─────────────────────────────────────────────────────────
from app.domain.market_data.base_fetcher import EnhancedMarketDataFetcher
from app.domain.risk.alert_engine import AlertEngine
from app.domain.portfolio.investment_recommendation_engine import generate_investment_recommendation
from app.domain.core_trust.explainability import build_explanation
from app.domain.core_trust.orchestrator import DecisionOrchestrator
from app.domain.core_trust.pipeline import TrustDecisionPipeline

# ── Logging ──────────────────────────────────────────────────────────────────
log = configure_logging(
    service_name="unified_gateway",
    log_level=os.getenv("LOG_LEVEL", "INFO"),
)

# Open-access mode is only for controlled external testing.
# Production default is locked down unless explicitly overridden.
TESTING_OPEN_ACCESS = os.getenv("TESTING_OPEN_ACCESS", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

GATEWAY_ENV = os.getenv("GATEWAY_ENV", "development").lower()
EXPOSE_API_DOCS = os.getenv("EXPOSE_API_DOCS", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
if GATEWAY_ENV in {"prod", "production"} and TESTING_OPEN_ACCESS:
    raise RuntimeError(
        "FATAL: TESTING_OPEN_ACCESS cannot be enabled in production. "
        "Set TESTING_OPEN_ACCESS=false."
    )

# ── Centralized Version ──────────────────────────────────────────────────────
def _read_version() -> str:
    """Read version from VERSION file (single source of truth)."""
    version_file = Path(__file__).resolve().parent.parent / "VERSION"
    try:
        return version_file.read_text().strip()
    except FileNotFoundError:
        return "3.0.0"

APP_VERSION = _read_version()
MIN_RECOMMENDATION_INVESTMENT = 100000.0
STRICT_STARTUP_VALIDATION = os.getenv(
    "STRICT_STARTUP_VALIDATION",
    "true" if GATEWAY_ENV in {"prod", "production"} else "false",
).lower() in {
    "1",
    "true",
    "yes",
    "on",
}

DECISION_TRACE_REQUIRED_FIELDS = {
    "decision_id",
    "policy_version_id",
    "data_snapshot_ids",
    "calculation_version_id",
    "input_hash",
    "output_hash",
    "trace_timestamp",
    "intent_id",
}

DECISION_TRACE_ENFORCED_PATHS = {
    "/api/v1/portfolio/investment-recommendation",
    "/api/portfolio/investment-recommendation",
    "/api/v1/compliance/pre-trade",
    "/api/v1/risk",
    "/api/v1/risk/",
    "/api/v1/risk/rate-sensitivity",
    "/api/v1/risk/spread-simulation",
    "/api/v1/risk/scenarios",
    "/api/v1/risk/var",
    "/api/v1/risk/yield-curve",
    "/api/v1/ips/scan",
    "/api/v1/ips/pretrade/simulate",
}


# ════════════════════════════════════════════════════════════════════════════
# Scheduler
# ════════════════════════════════════════════════════════════════════════════

def _start_scheduler(fetcher: EnhancedMarketDataFetcher):
    """APScheduler: daily market refresh at 07:30 IST + health ping every 5 min."""
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
        egress_enabled = external_data_egress_enabled()

        async def _daily_refresh():
            await run_timed_task(
                "market_data_fetcher",
                "SCHEDULED_REFRESH",
                fetcher.refresh_all(max_bonds=1000),
            )
            # Run alert detection after market data refresh
            try:
                alert_engine = AlertEngine()
                count = await alert_engine.run_all_checks()
                log.info("daily_alerts_generated", count=count)
            except Exception as exc:
                log.warning("daily_alert_engine_failed", error=str(exc))

        async def _exchange_trade_fetch():
            """Pull live NSE + BSE bond trade prices every weekday at market close."""
            try:
                from app.domain.market_data.exchange_fetcher import ExchangeTradeFetcher
                ef = ExchangeTradeFetcher()
                result = await ef.fetch_today()
                log.info("scheduled_exchange_fetch", **result)
            except Exception as exc:
                log.warning("exchange_fetch_failed", error=str(exc))

        async def _weekly_im_fetch():
            """Refresh IM registry from NSE/BSE/SEBI every Sunday morning."""
            try:
                from app.domain.market_data.im_ingestion import IMFetcher
                imf = IMFetcher()
                result = await imf.fetch_all_sources(days_back=7)
                log.info("scheduled_im_fetch", result=result)
            except Exception as exc:
                log.warning("im_fetch_failed", error=str(exc))

        async def _news_refresh():
            """Fetch bond-market news from all RSS feeds every 4 hours."""
            try:
                from app.domain.market_data.news_fetcher import NewsFetcher
                nf = NewsFetcher()
                result = await nf.fetch_all_sources(max_age_days=3)
                log.info("scheduled_news_refresh", stored=result.get("stored", 0))
            except Exception as exc:
                log.warning("news_refresh_failed", error=str(exc))

        async def _health_ping():
            result = await check_database()
            log.info("scheduled_health_ping", status=result.status.value)

        async def _scheduled_trust_anchor():
            try:
                from app.domain.core_trust.audit import AuditService

                result = await AuditService.create_trust_anchor(
                    anchor_provider=os.getenv("TRUST_CHAIN_ANCHOR_PROVIDER", "internal_notary"),
                    provider_reference=f"scheduler:{utcnow_iso()}",
                    metadata={"reason": "scheduled_anchor"},
                )
                log.info("scheduled_trust_chain_anchor", **result)
            except Exception as exc:
                log.warning("scheduled_trust_chain_anchor_failed", error=str(exc))

        configured_jobs = ["health_ping@5min", "four_eyes_expiry_sweep@60min", "trust_chain_anchor@60min"]
        if egress_enabled:
            scheduler.add_job(_daily_refresh, "cron", hour=7, minute=30, id="daily_market_refresh")
            scheduler.add_job(
                _exchange_trade_fetch,
                "cron",
                hour=16,
                minute=30,
                day_of_week="mon-fri",
                id="exchange_trade_fetch",
            )
            scheduler.add_job(_weekly_im_fetch, "cron", day_of_week="sun", hour=6, minute=0, id="weekly_im_fetch")
            scheduler.add_job(_news_refresh, "interval", hours=4, id="news_refresh")
            configured_jobs.extend(
                [
                    "daily_market_refresh@07:30IST",
                    "exchange_trade_fetch@16:30IST Mon-Fri",
                    "weekly_im_fetch@06:00 Sunday",
                    "news_refresh@every 4h",
                    "sebi_api_sync@every 4h",
                ]
            )
        else:
            log.warning(
                "external_scheduler_jobs_disabled",
                mode=deployment_mode(),
                reason="ALLOW_EXTERNAL_DATA_EGRESS is false",
            )

        scheduler.add_job(_health_ping,          "interval", minutes=5, id="health_ping")

        # SEBI API Sync every 4 hours (replaces file-based scraping)
        async def _sebi_sync():
            try:
                from app.domain.compliance.sebi_api_sync import SEBIApiSync
                await SEBIApiSync.initialize()
                result = await SEBIApiSync.run_full_sync()
                log.info("scheduled_sebi_sync", **{k: v for k, v in result.items() if k != 'tasks'})
            except Exception as exc:
                log.warning("sebi_sync_failed", error=str(exc))

        # Four-eyes expiry sweep every hour
        async def _four_eyes_sweep():
            try:
                from app.domain.core_trust.approvals import FourEyesApprovalService
                await FourEyesApprovalService.initialize()
                expired = await FourEyesApprovalService.expire_stale_requests()
                if expired:
                    log.info("four_eyes_expired", count=expired)
            except Exception as exc:
                log.warning("four_eyes_sweep_failed", error=str(exc))

        if egress_enabled:
            scheduler.add_job(_sebi_sync, "interval", hours=4, id="sebi_api_sync")
        scheduler.add_job(_four_eyes_sweep,  "interval", minutes=60, id="four_eyes_expiry_sweep")
        scheduler.add_job(_scheduled_trust_anchor, "interval", minutes=60, id="trust_chain_anchor")
        scheduler.start()
        log.info("scheduler_started", jobs=configured_jobs)
        return scheduler
    except ImportError:
        log.warning("apscheduler_missing", hint="pip install apscheduler")
        return None


# ════════════════════════════════════════════════════════════════════════════
# Lifespan
# ════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("gateway_starting", version=APP_VERSION)
    production_mode = GATEWAY_ENV in {"prod", "production"}
    app.state.startup_phase = "BOOTSTRAP"
    app.state.decision_pipeline_ready = False
    app.state.startup_critical_failures = []

    async def _run_step(step_name: str, coro, timeout_seconds: float = 30.0):
        log.info("startup_step_begin", step=step_name)
        try:
            result = await asyncio.wait_for(coro, timeout=timeout_seconds)
            log.info("startup_step_complete", step=step_name)
            return result
        except asyncio.TimeoutError:
            log.warning("startup_step_timeout", step=step_name, timeout_seconds=timeout_seconds)
        except Exception as exc:
            log.warning("startup_step_failed", step=step_name, error=str(exc))
        return None

    # 1 — Shared database schema
    await _run_step("init_database", init_database(), timeout_seconds=45.0)
    try:
        from app.domain.portfolio.engine import ensure_portfolio_core_schema

        await _run_step("portfolio_core_schema", ensure_portfolio_core_schema(), timeout_seconds=45.0)
        log.info("portfolio_core_schema_initialized")
    except Exception as exc:
        log.warning("portfolio_core_schema_init_skipped", error=str(exc))
    db_health = await _run_step("check_database", check_database(), timeout_seconds=15.0)
    if db_health is not None:
        log.info("database_init", status=db_health.status.value)
    else:
        log.warning("database_init_skipped", reason="timeout_or_failure")

    legacy_views_health = await _run_step("legacy_portfolio_views", check_legacy_portfolio_views(), timeout_seconds=15.0)
    if legacy_views_health is not None:
        log.info("legacy_portfolio_views_check", status=legacy_views_health.status.value)
        if legacy_views_health.status != HealthStatus.OK:
            log.warning("legacy_portfolio_views_non_ok", message=legacy_views_health.message)
    else:
        log.warning("legacy_portfolio_views_check", status="UNKNOWN")

    # 1a — Initialize token blacklist (C2 remediation)
    try:
        from app.shared.auth import TokenBlacklist
        await _run_step("token_blacklist_initialize", TokenBlacklist.initialize(), timeout_seconds=10.0)
        log.info("token_blacklist_initialized")
    except Exception as exc:
        log.warning("token_blacklist_init_skipped", error=str(exc))

    redis_health = await _run_step("check_redis", check_redis(), timeout_seconds=15.0)
    if redis_health is not None:
        log.info("redis_health_check", status=redis_health.status.value, message=redis_health.message)
        if redis_health.status == HealthStatus.ERROR:
            log.warning("redis_health_non_ok", message=redis_health.message)
    else:
        log.warning("redis_health_check", status="UNKNOWN", message="timeout_or_failure")

    startup_critical_failures: list[str] = []
    if db_health is None or db_health.status != HealthStatus.OK:
        startup_critical_failures.append("database_unavailable")
    if redis_health is None or redis_health.status == HealthStatus.ERROR:
        startup_critical_failures.append("redis_unavailable")
    app.state.startup_critical_failures = startup_critical_failures

    if startup_critical_failures and STRICT_STARTUP_VALIDATION:
        raise RuntimeError(
            "Strict startup validation failed: " + ", ".join(startup_critical_failures)
        )

    async def _deferred_bootstrap() -> None:
        try:
            async def _background_step(step_name: str, coro, timeout_seconds: float = 30.0):
                return await _run_step(step_name, coro, timeout_seconds=timeout_seconds)

            # 1b — Initialize institutional audit trail
            try:
                from app.domain.core_trust.audit import AuditService
                await _background_step("audit_trail_initialize", AuditService.initialize(), timeout_seconds=20.0)
                log.info("audit_trail_initialized")
            except Exception as exc:
                log.warning("audit_trail_init_skipped", error=str(exc))

            # 1c — Initialize compliance hardening
            try:
                from app.domain.compliance.rules_hardened import ComplianceHardeningService
                await _background_step("compliance_hardening_initialize", ComplianceHardeningService.initialize(), timeout_seconds=20.0)
                log.info("compliance_hardening_initialized")
            except Exception as exc:
                log.warning("compliance_hardening_init_skipped", error=str(exc))

            # 1c.1 — Initialize decision orchestrator registry tables
            try:
                from app.domain.core_trust.orchestrator import DecisionOrchestrator
                await _background_step("decision_orchestrator_initialize", DecisionOrchestrator.initialize(), timeout_seconds=20.0)
                await _background_step("decision_pipeline_initialize", TrustDecisionPipeline.initialize(), timeout_seconds=20.0)
                app.state.decision_pipeline_ready = True
                log.info("decision_orchestrator_initialized")
            except Exception as exc:
                app.state.decision_pipeline_ready = False
                log.warning("decision_orchestrator_init_skipped", error=str(exc))

            # 1d — Initialize data integrity service
            try:
                from app.domain.core_trust.data_integrity import DataIntegrityService
                await _background_step("data_integrity_initialize", DataIntegrityService.initialize(), timeout_seconds=20.0)
                log.info("data_integrity_initialized")
            except Exception as exc:
                log.warning("data_integrity_init_skipped", error=str(exc))

            try:
                from app.domain.core_trust.data_governance import ensure_governance_schema
                await _background_step("data_governance_initialize", ensure_governance_schema(), timeout_seconds=20.0)
                log.info("data_governance_initialized")
            except Exception as exc:
                log.warning("data_governance_init_skipped", error=str(exc))

            # 1e — Initialize institutional reports service
            try:
                from app.domain.portfolio.institutional_reports import InstitutionalReportService
                await _background_step("institutional_reports_initialize", InstitutionalReportService.initialize(), timeout_seconds=20.0)
                log.info("institutional_reports_initialized")
            except Exception as exc:
                log.warning("institutional_reports_init_skipped", error=str(exc))

            # 1f — Initialize IPS compliance engine (rules, pre-trade, post-trade, tax, audit)
            try:
                from app.domain.compliance.engine import IPSEngine
                await _background_step("ips_engine_initialize", IPSEngine.initialize(), timeout_seconds=20.0)
                log.info("ips_engine_initialized")
            except Exception as exc:
                log.warning("ips_engine_init_skipped", error=str(exc))

            try:
                from app.domain.compliance.validators_pretrade import PreTradeValidator
                await _background_step("pretrade_validator_initialize", PreTradeValidator.initialize(), timeout_seconds=20.0)
                log.info("pretrade_validator_initialized")
            except Exception as exc:
                log.warning("pretrade_validator_init_skipped", error=str(exc))

            try:
                from app.domain.compliance.validators_posttrade import PostTradeMonitor
                await _background_step("posttrade_monitor_initialize", PostTradeMonitor.initialize(), timeout_seconds=20.0)
                log.info("posttrade_monitor_initialized")
            except Exception as exc:
                log.warning("posttrade_monitor_init_skipped", error=str(exc))

            try:
                from app.domain.compliance.tax_benchmark import TaxAccountingService, BenchmarkService
                await _background_step("tax_accounting_initialize", TaxAccountingService.initialize(), timeout_seconds=20.0)
                await _background_step("benchmark_initialize", BenchmarkService.initialize(), timeout_seconds=20.0)
                log.info("tax_benchmark_initialized")
            except Exception as exc:
                log.warning("tax_benchmark_init_skipped", error=str(exc))

            try:
                from app.domain.compliance.self_audit import SystemSelfAudit
                await _background_step("self_audit_initialize", SystemSelfAudit.initialize(), timeout_seconds=20.0)
                log.info("self_audit_initialized")
            except Exception as exc:
                log.warning("self_audit_init_skipped", error=str(exc))

            # 2 — Seed universal bond master if empty (full 20k bonds)
            registry = ISINRegistry()
            try:
                count = await _background_step("bond_master_count", registry.count(), timeout_seconds=20.0)
            except Exception as exc:
                count = None
                log.warning("bond_master_count_failed", error=str(exc))
            if count == 0:
                log.info("seeding_bond_master_full")
                try:
                    from scripts.seeds.seed_indian_bonds import run_seed
                    result = await _background_step("bond_master_seed", run_seed(batch_size=500), timeout_seconds=120.0)
                    if result is not None:
                        log.info("seed_complete", **result)
                except Exception as exc:
                    log.warning("seed_failed", error=str(exc))

            # 2b — Ensure a bootstrap admin account exists for local login.
            try:
                from app.shared.database import fetch_one as _f1, execute_query as _eq
                from app.shared.time_utils import utcnow_iso

                admin_username = (os.environ.get("ADMIN_USERNAME")
                                  or os.environ.get("GATEWAY_ADMIN_USERNAME")
                                  or os.environ.get("GATEWAY_ADMIN_USER")
                                  or "admin").strip()
                admin_email = (os.environ.get("ADMIN_EMAIL")
                               or f"{admin_username}@bond-gateway.in")
                admin_pwd = os.environ.get("ADMIN_PASSWORD") or os.environ.get("GATEWAY_ADMIN_PASSWORD")
                if not admin_pwd:
                    raise RuntimeError("ADMIN_PASSWORD is unavailable after environment bootstrap")

                async def _ensure_admin_user() -> str:
                    existing = await _f1("SELECT id FROM users WHERE username = :u", {"u": admin_username})
                    if existing:
                        return "exists"
                    now = utcnow_iso()
                    await _eq(
                        "INSERT INTO users (username,email,hashed_password,role,is_active,created_at) "
                        "VALUES (:u,:e,:hp,:r,1,:now)",
                        {
                            "u": admin_username,
                            "e": admin_email,
                            "hp": hash_password(admin_pwd),
                            "r": "ADMIN",
                            "now": now,
                        },
                    )
                    return "created"

                admin_action = await _background_step(
                    "bootstrap_admin_user",
                    _ensure_admin_user(),
                    timeout_seconds=20.0,
                )

                log.info(
                    "bootstrap_admin_ensured",
                    admin_username=admin_username,
                    admin_action=admin_action,
                )
            except Exception as exc:
                log.warning("default_user_creation_skipped", error=str(exc))

            # 3 — Initial market data if missing
            fetcher = EnhancedMarketDataFetcher()
            from app.shared.database import fetch_one
            try:
                md_count = await _background_step("market_data_count", fetch_one("SELECT COUNT(*) AS cnt FROM unified_market_data"), timeout_seconds=20.0)
            except Exception as exc:
                md_count = None
                log.warning("market_data_count_failed", error=str(exc))
            if not md_count or md_count["cnt"] == 0:
                log.info("seeding_market_data")
                await _background_step(
                    "market_data_refresh",
                    run_timed_task(
                        "market_data_fetcher",
                        "STARTUP",
                        fetcher.refresh_all(max_bonds=500),
                    ),
                    timeout_seconds=120.0,
                )

            # 4 — Start scheduler
            scheduler = _start_scheduler(fetcher)
            app.state.scheduler = scheduler

            # 5 — Initialize news table + initial fetch
            try:
                from app.domain.market_data.news_fetcher import NewsFetcher
                news = NewsFetcher()
                await _background_step("news_ensure_table", news.ensure_table(), timeout_seconds=20.0)
                news_count = await _background_step("news_count", fetch_one("SELECT COUNT(*) AS cnt FROM bond_news"), timeout_seconds=20.0)
                if not news_count or news_count["cnt"] == 0:
                    log.info("seeding_initial_news")
                    await _background_step("news_seed", news.fetch_all_sources(max_age_days=7), timeout_seconds=120.0)
            except Exception as exc:
                log.warning("news_init_skipped", error=str(exc)[:100])

            log.info("startup_background_complete")
            app.state.startup_phase = "READY"
        except asyncio.CancelledError:
            log.info("startup_background_cancelled")
            app.state.startup_phase = "CANCELLED"
            raise
        except Exception as exc:
            app.state.startup_phase = "DEGRADED"
            log.warning("startup_background_failed", error=str(exc))

    async def _intent_reconciliation_loop() -> None:
        while True:
            try:
                summary = await TrustDecisionPipeline.reconcile_incomplete_intents(limit=50)
                if summary.get("failed") or summary.get("reconciled"):
                    log.info("decision_intent_reconcile", **summary)
            except Exception as exc:
                log.warning("decision_intent_reconcile_failed", error=str(exc))
            await asyncio.sleep(30)

    app.state.bootstrap_task = asyncio.create_task(_deferred_bootstrap())
    app.state.intent_reconciliation_task = asyncio.create_task(_intent_reconciliation_loop())

    log.info("gateway_ready", url=f"http://localhost:{os.getenv('PORT', '9005')}", docs=f"http://localhost:{os.getenv('PORT', '9005')}/docs")
    yield

    bootstrap_task = getattr(app.state, "bootstrap_task", None)
    if bootstrap_task and not bootstrap_task.done():
        bootstrap_task.cancel()

    intent_task = getattr(app.state, "intent_reconciliation_task", None)
    if intent_task and not intent_task.done():
        intent_task.cancel()

    if getattr(app.state, "scheduler", None):
        app.state.scheduler.shutdown()
    log.info("gateway_shutdown")


# ════════════════════════════════════════════════════════════════════════════
# FastAPI Application
# ════════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="Unified Bond Investment Gateway",
    version=APP_VERSION,
    description=(
        "Consolidated API gateway for PF Trust fixed-income operations. "
        "Combines Bond Intelligence, Compliance Engine, Quote System, and "
        "Daily Bond Agent into a single, observable service with WebSocket "
        "real-time pricing, structured logging, and a shared PostgreSQL/SQLite registry."
    ),
    docs_url="/docs" if EXPOSE_API_DOCS else None,
    redoc_url="/redoc" if EXPOSE_API_DOCS else None,
    lifespan=lifespan,
)


@app.middleware("http")
async def enforce_decision_trace_contract(request: Request, call_next):
    response = await call_next(request)

    if request.url.path not in DECISION_TRACE_ENFORCED_PATHS:
        return response
    if response.status_code >= 400:
        return response

    content_type = str(response.headers.get("content-type") or "").lower()
    if "application/json" not in content_type:
        return response

    body = b""
    async for chunk in response.body_iterator:
        body += chunk

    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        return JSONResponse(
            status_code=500,
            content={"detail": "Fail-closed trace contract violated: response is not valid JSON"},
        )

    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=500,
            content={"detail": "Fail-closed trace contract violated: response payload must be a JSON object"},
        )

    missing = [field for field in DECISION_TRACE_REQUIRED_FIELDS if field not in payload]
    if missing:
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Fail-closed trace contract violated",
                "missing_fields": missing,
                "path": request.url.path,
            },
        )

    passthrough_headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in {"content-length", "content-type"}
    }
    return JSONResponse(status_code=response.status_code, content=payload, headers=passthrough_headers)

# CORS: Configurable origins via env var; defaults to localhost on common ports
_cors_origins = [
    o.strip() for o in os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:9000,http://127.0.0.1:9000,http://localhost:9005,http://127.0.0.1:9005"
    ).split(",")
]
# Allow all origins during external testing; otherwise keep the regular allowlist.
_cors_origin_regex = r".*" if TESTING_OPEN_ACCESS else os.getenv("CORS_ALLOWED_ORIGIN_REGEX", "").strip() or r"https://.*\.trycloudflare\.com"
_cors_allow_origins = ["*"] if TESTING_OPEN_ACCESS else _cors_origins
_cors_allow_methods = ["*"] if TESTING_OPEN_ACCESS else ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
_cors_allow_headers = ["*"] if TESTING_OPEN_ACCESS else ["Authorization", "Content-Type", "Accept"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins,
    allow_origin_regex=_cors_origin_regex,
    allow_credentials=True,
    allow_methods=_cors_allow_methods,
    allow_headers=_cors_allow_headers,
)

# Gzip compression — speeds up IM document transmissions and large API responses
app.add_middleware(GZipMiddleware, minimum_size=1000)


# ── HTTPS Enforcement Middleware ─────────────────────────────────────────────
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class EnforceHTTPSMiddleware(BaseHTTPMiddleware):
    """Require HTTPS for all non-health routes when enabled."""

    _EXEMPT_PATHS = {
        "/api/v1/health",
        "/api/v1/healthz",
        "/api/v1/health/deep",
    }

    async def dispatch(self, request, call_next):
        if TESTING_OPEN_ACCESS:
            return await call_next(request)

        path = request.url.path
        if path in self._EXEMPT_PATHS:
            return await call_next(request)

        forwarded_proto = request.headers.get("x-forwarded-proto", "")
        scheme = (forwarded_proto or request.url.scheme or "").split(",")[0].strip().lower()
        if scheme != "https":
            return JSONResponse(
                status_code=426,
                content={
                    "detail": "HTTPS is required for this deployment.",
                    "hint": "Route traffic through TLS termination (Nginx/Cloudflare/Load Balancer).",
                },
            )

        return await call_next(request)


_enforce_https_default = "true" if GATEWAY_ENV in {"prod", "production"} else "false"
_enforce_https = os.getenv("ENFORCE_HTTPS", _enforce_https_default).lower() in {"1", "true", "yes", "on"}
if _enforce_https:
    app.add_middleware(EnforceHTTPSMiddleware)
    log.info("https_enforcement_enabled")


# ── Rate Limiting Middleware ──────────────────────────────────────────────────
import time
from collections import defaultdict

class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Token-bucket rate limiter per IP.
    Configurable via RATE_LIMIT_RPM env var (default: 120 requests/min).
    """
    def __init__(self, app, rpm: int = 120):
        super().__init__(app)
        self.rpm = rpm
        self._buckets: dict = defaultdict(lambda: {"tokens": rpm, "last": time.time()})

    async def dispatch(self, request, call_next):
        if TESTING_OPEN_ACCESS:
            return await call_next(request)

        # Skip rate limiting for static files, docs, and WebSocket upgrades
        path = request.url.path
        if (path.startswith("/static") or path.startswith("/ws")
                or path in ("/docs", "/redoc", "/openapi.json")):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        bucket = self._buckets[client_ip]
        now = time.time()
        elapsed = now - bucket["last"]
        bucket["tokens"] = min(self.rpm, bucket["tokens"] + elapsed * (self.rpm / 60))
        bucket["last"] = now

        if bucket["tokens"] < 1:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Try again shortly."},
            )
        bucket["tokens"] -= 1
        return await call_next(request)

_rate_limit_rpm = int(os.getenv("RATE_LIMIT_RPM", "600"))  # 600 RPM = 10 req/s; fine for single-user local app
app.add_middleware(RateLimitMiddleware, rpm=_rate_limit_rpm)


# ── Auth Endpoint Rate Limiter (Stricter) ─────────────────────────────────────
class AuthRateLimitMiddleware(BaseHTTPMiddleware):
    """
    Strict per-IP rate limiter for authentication endpoints.
    Default: 10 attempts per 15 minutes per IP (protects login, token refresh, password reset).
    Distributed brute-force protection: all auth failures count against the IP budget.
    """
    AUTH_PATHS = {
        "/api/v1/auth/token",
        "/api/v1/auth/refresh",
        "/api/v1/auth/password-reset-request",
        "/api/v1/auth/password-reset-confirm",
    }

    def __init__(self, app, max_attempts: int = 10, window_seconds: int = 900):
        super().__init__(app)
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        # {ip: [(timestamp, path), ...]}
        self._windows: dict[str, list] = defaultdict(list)

    def _clean_window(self, ip: str):
        cutoff = time.time() - self.window_seconds
        self._windows[ip] = [t for t in self._windows[ip] if t[0] > cutoff]

    async def dispatch(self, request, call_next):
        if TESTING_OPEN_ACCESS:
            return await call_next(request)

        path = request.url.path
        if path not in self.AUTH_PATHS:
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        self._clean_window(client_ip)
        count = len(self._windows[client_ip])

        if count >= self.max_attempts:
            retry_after = self.window_seconds - int(time.time() - self._windows[client_ip][0][0])
            log.warning("auth_rate_limit_exceeded", ip=client_ip, path=path, attempts=count)
            return JSONResponse(
                status_code=429,
                content={
                    "detail": f"Too many authentication attempts. Try again in {retry_after}s.",
                    "retry_after_seconds": max(0, retry_after),
                },
                headers={"Retry-After": str(max(0, retry_after))},
            )
        self._windows[client_ip].append((time.time(), path))
        return await call_next(request)

_auth_max_attempts = int(os.getenv("AUTH_RATE_LIMIT_ATTEMPTS", "10"))
_auth_window_sec   = int(os.getenv("AUTH_RATE_LIMIT_WINDOW_SECONDS", "900"))
app.add_middleware(AuthRateLimitMiddleware, max_attempts=_auth_max_attempts, window_seconds=_auth_window_sec)


# ── Security Headers Middleware ───────────────────────────────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add institutional-grade security headers to all responses."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if TESTING_OPEN_ACCESS:
            return response

        path = request.url.path
        is_im_pdf_preview = path.startswith("/api/v1/im-checker/uploads/") and path.endswith("/file")

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN" if is_im_pdf_preview else "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        # HSTS with preload (max-age=2 years per HSTS Preload List requirements)
        response.headers["Strict-Transport-Security"] = (
            "max-age=63072000; includeSubDomains; preload"
        )
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        frame_ancestors = "'self'" if is_im_pdf_preview else "'none'"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "font-src 'self' data:; "
            "img-src 'self' data: blob:; "
            "connect-src 'self' ws: wss:; "
            f"frame-ancestors {frame_ancestors}; "
            "base-uri 'self'; "
            "form-action 'self';"
        )
        return response

app.add_middleware(SecurityHeadersMiddleware)


# ── Prometheus Metrics Tracking Middleware ─────────────────────────────────────
class MetricsTrackingMiddleware(BaseHTTPMiddleware):
    """Track request count, latency, and error rate for Prometheus scraping."""

    async def dispatch(self, request, call_next):
        start = time.time()
        response = await call_next(request)
        duration = time.time() - start
        # Normalize path to avoid cardinality explosion
        path = request.url.path
        # Collapse path parameters (e.g., /api/v1/bonds/INE123 → /api/v1/bonds/:id)
        if "/api/" in path:
            parts = path.split("/")
            normalized = "/".join(
                ":id" if (i > 3 and len(p) > 8 and not p.startswith("v")) else p
                for i, p in enumerate(parts)
            )
        else:
            normalized = path
        metrics_collector.track_request(request.method, normalized, response.status_code, duration)
        return response


if os.getenv("PROMETHEUS_ENABLED", "true").lower() in ("true", "1", "yes"):
    app.add_middleware(MetricsTrackingMiddleware)


# ── CSRF Protection Middleware (Defense-in-Depth) ─────────────────────────────
class CSRFProtectionMiddleware(BaseHTTPMiddleware):
    """
    Defense-in-depth CSRF protection via Origin/Referer header validation.

    This API uses JWT Bearer tokens (not cookies), making it inherently
    immune to traditional CSRF attacks. This middleware adds an extra layer
    by rejecting state-changing requests from unexpected Origins.

    Behavior:
    - Safe methods (GET, HEAD, OPTIONS) are always allowed.
    - State-changing methods (POST, PUT, DELETE, PATCH) must have a
      valid Origin or Referer header matching CORS_ALLOWED_ORIGINS.
    - Requests with no Origin/Referer are allowed (non-browser clients).
    - Requests with a mismatched Origin are rejected with 403.
    """
    STATE_CHANGING = {"POST", "PUT", "DELETE", "PATCH"}

    def __init__(self, app):
        super().__init__(app)
        origins_str = os.getenv(
            "CORS_ALLOWED_ORIGINS",
            "http://localhost:9000,http://127.0.0.1:9000,http://localhost:9005,http://127.0.0.1:9005",
        )
        self._allowed_origins = set(o.strip().rstrip("/") for o in origins_str.split(",") if o.strip())
        # Ensure common local origins are always present
        self._allowed_origins.update({
            "http://localhost:9000", "http://127.0.0.1:9000",
            "http://localhost:9005", "http://127.0.0.1:9005"
        })

    async def _safe_next(self, request, call_next):
        try:
            return await call_next(request)
        except RuntimeError as exc:
            # Client disconnects during high-concurrency runs can bubble up from BaseHTTPMiddleware.
            if "No response returned." in str(exc):
                log.debug("csrf_client_disconnected", path=request.url.path)
                return JSONResponse(status_code=204, content=None)
            raise

    async def dispatch(self, request, call_next):
        if TESTING_OPEN_ACCESS:
            return await self._safe_next(request, call_next)

        if request.method not in self.STATE_CHANGING:
            return await self._safe_next(request, call_next)

        origin = request.headers.get("origin", "").rstrip("/")
        referer = request.headers.get("referer", "")

        # Non-browser clients (curl, httpie, SDKs) typically don't send Origin
        if not origin and not referer:
            return await self._safe_next(request, call_next)

        # Extract origin from referer if origin header is absent
        check_origin = origin
        if not check_origin and referer:
            from urllib.parse import urlparse
            parsed = urlparse(referer)
            check_origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")

        if check_origin and check_origin not in self._allowed_origins:
            # Normalize check_origin by removing port if present
            from urllib.parse import urlparse
            p = urlparse(check_origin)
            hostname = p.hostname or ""
            
            if not (hostname == "trycloudflare.com" or hostname.endswith(".trycloudflare.com")):
                return JSONResponse(
                    status_code=403,
                    content={"detail": f"CSRF validation failed: origin {check_origin} not allowed."},
                )

        return await self._safe_next(request, call_next)


app.add_middleware(CSRFProtectionMiddleware)


# ── Data Freeze Middleware (P2B) ──────────────────────────────────────────────
class DataFreezeMiddleware(BaseHTTPMiddleware):
    """
    CRITICAL: Block valuation/reporting endpoints when market data is too stale.
    Prevents decision-making on outdated pricing data.

    Behavior:
    - If >50% of portfolio ISINs have pricing >72h stale or missing → FREEZE
    - Frozen endpoints return HTTP 503 with explicit warning
    - Non-valuation endpoints (auth, healthz, admin) are unaffected
    - Freeze status is cached for 5 minutes to avoid DB hammering
    """
    VALUATION_PREFIXES = (
        "/api/v1/portfolio/valuation",
        "/api/v1/intelligence/bond-analysis",
        "/api/v1/institutional/stress-test",
        "/api/v1/reports/trustee",
        "/api/v1/reports/quarterly",
        "/api/v2/portfolio/valuation",
        "/api/v2/reports/trustee",
        "/api/v2/reports/quarterly",
        "/cio-dashboard-data",
    )

    def __init__(self, app):
        super().__init__(app)
        self._freeze_status: dict = {}
        self._last_check: float = 0
        self._cache_ttl = 300  # 5 minutes

    async def _safe_next(self, request, call_next):
        try:
            return await call_next(request)
        except RuntimeError as exc:
            if "No response returned." in str(exc):
                log.debug("data_freeze_client_disconnected", path=request.url.path)
                return JSONResponse(status_code=204, content=None)
            raise

    async def dispatch(self, request, call_next):
        if TESTING_OPEN_ACCESS:
            return await self._safe_next(request, call_next)

        path = request.url.path
        if not any(path.startswith(p) for p in self.VALUATION_PREFIXES):
            return await self._safe_next(request, call_next)

        # Check freeze status (cached)
        import time as _time
        now = _time.time()
        if now - self._last_check > self._cache_ttl:
            try:
                from app.domain.core_trust.data_integrity import DataIntegrityService
                freshness = await DataIntegrityService.check_data_freshness()
                total = freshness.get("total_holdings", 0)
                stale = freshness.get("stale", 0) + freshness.get("missing", 0)
                self._freeze_status = {
                    "frozen": total > 0 and (stale / total) > 0.5,
                    "stale_pct": round(stale / total * 100, 1) if total > 0 else 0,
                    "stale_count": stale,
                    "total": total,
                }
                self._last_check = now
            except Exception as exc:
                log.warning("data_freeze_check_failed", error=str(exc))
                # If we can't check, allow requests through (fail-open for availability)
                return await self._safe_next(request, call_next)

        if self._freeze_status.get("frozen"):
            return JSONResponse(
                status_code=503,
                content={
                    "error": "DATA_FREEZE_ACTIVE",
                    "message": (
                        f"Valuation endpoints frozen: {self._freeze_status['stale_pct']}% "
                        f"of portfolio pricing is stale/missing "
                        f"({self._freeze_status['stale_count']}/{self._freeze_status['total']} ISINs). "
                        "Do NOT use stale data for investment decisions. "
                        "Contact IT to refresh market data feeds."
                    ),
                    "stale_pct": self._freeze_status["stale_pct"],
                },
                headers={"X-Data-Freeze": "true", "Retry-After": "300"},
            )

        # Add freshness warning header even when not frozen
        response = await self._safe_next(request, call_next)
        if self._freeze_status.get("stale_pct", 0) > 20:
            response.headers["X-Data-Freshness-Warning"] = (
                f"{self._freeze_status['stale_pct']}% stale"
            )
        return response

app.add_middleware(DataFreezeMiddleware)


# ── IP Allowlist Middleware (H4 Remediation) ──────────────────────────────────
class AdminIPAllowlistMiddleware(BaseHTTPMiddleware):
    """
    Restrict /governance/* and /institutional/* admin endpoints to allowlisted IPs.
    Configurable via ADMIN_IP_ALLOWLIST env var (comma-separated).
    If not set, all IPs are allowed (backwards-compatible).
    """
    PROTECTED_PREFIXES = ("/api/v1/governance/", "/api/v1/institutional/stress-test")

    def __init__(self, app, allowlist: list[str] = None):
        super().__init__(app)
        self.allowlist = set(allowlist or [])

    async def dispatch(self, request, call_next):
        if TESTING_OPEN_ACCESS:
            return await call_next(request)

        if self.allowlist:
            path = request.url.path
            if any(path.startswith(prefix) for prefix in self.PROTECTED_PREFIXES):
                client_ip = request.client.host if request.client else "unknown"
                if client_ip not in self.allowlist:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": f"Access denied. IP {client_ip} not in admin allowlist."},
                    )
        return await call_next(request)

_admin_ips = os.getenv("ADMIN_IP_ALLOWLIST", "")
if _admin_ips:
    _allowlist = [ip.strip() for ip in _admin_ips.split(",") if ip.strip()]
    app.add_middleware(AdminIPAllowlistMiddleware, allowlist=_allowlist)
    log.info("admin_ip_allowlist_enabled", ips=_allowlist)


# ── Mount routers ─────────────────────────────────────────────────────────────
app.include_router(auth_router, prefix="/api/v1")
register_gateway_routes(app)

# ── Monitoring / Prometheus metrics ──────────────────────────────────────────
if os.getenv("PROMETHEUS_ENABLED", "true").lower() in ("true", "1", "yes"):
    app.include_router(metrics_router)

# ── Version negotiation middleware ────────────────────────────────────────────
app.add_middleware(VersionNegotiationMiddleware)

# ── Static files (unified UI + all dashboards) ───────────────────────────────
_STATIC = Path(__file__).parent.parent / "frontend"
if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


# ── Dashboard convenience routes (served from unified_gateway/static/dashboards/)
# These replace the old per-service HTML endpoints that lived on ports 8000-8900.
# All URLs are now: http://localhost:9000/dashboards/<service>/<file>

@app.get("/dashboards/{service}/{page:path}", include_in_schema=False)
async def serve_dashboard(service: str, page: str):
    """Serve legacy service dashboards now hosted inside the gateway static folder."""
    dash_path = _STATIC / "dashboards" / service / page
    if dash_path.exists() and dash_path.is_file():
        return FileResponse(str(dash_path))
    return HTMLResponse(
        f"<h2>Dashboard not found</h2><p>Path: dashboards/{service}/{page}</p>",
        status_code=404,
    )


@app.get("/dashboards", include_in_schema=False)
async def list_dashboards():
    """Return a JSON index of all available dashboards."""
    dash_root = _STATIC / "dashboards"
    index: list[dict] = []
    if dash_root.exists():
        for html in sorted(dash_root.rglob("*.html")):
            rel = html.relative_to(dash_root)
            parts = rel.parts  # (service, filename)
            index.append({
                "service": parts[0] if len(parts) > 1 else "root",
                "file": parts[-1],
                "url": f"/dashboards/{rel.as_posix()}",
            })
    return JSONResponse({"dashboards": index})


# ════════════════════════════════════════════════════════════════════════════
# WebSocket — Real-Time Price & Event Stream
# ════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws/{topic}")
async def websocket_endpoint(ws: WebSocket, topic: str):
    """
    WebSocket feed for real-time updates.

    Topics:
      prices       — market data refresh events + individual ISIN updates
      compliance   — compliance check results
      system       — scheduler heartbeats, health events
    """
    # Authenticate websocket clients using Authorization: Bearer <token>
    # or a `token` query parameter for clients that cannot set headers.
    token = None
    auth_header = ws.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
    if not token:
        token = ws.query_params.get("token")

    if not token:
        await ws.close(code=4401)
        return

    try:
        _, ws_user = await validate_token_session(token)
    except Exception:
        await ws.close(code=4401)
        return

    topic_min_role = {
        "prices": Roles.VIEWER,
        "alerts": Roles.VIEWER,
        "compliance": Roles.COMPLIANCE,
        "system": Roles.ADMIN,
    }
    if topic not in topic_min_role:
        await ws.close(code=4404)
        return

    role_rank = {
        Roles.SUPER_ADMIN: 7,
        Roles.ADMIN: 6,
        Roles.PORTFOLIO: 5,
        Roles.TRUSTEE: 4,
        Roles.COMPLIANCE: 3,
        Roles.AUDITOR: 2,
        Roles.VIEWER: 1,
    }
    user_role = ws_user.get("role", Roles.VIEWER)
    if role_rank.get(user_role, 0) < role_rank.get(topic_min_role[topic], 0):
        await ws.close(code=4403)
        return

    await ws_manager.connect(topic, ws)
    log.info("ws_client_connected", topic=topic,
             total=ws_manager.connection_count())
    try:
        # Send initial connection confirmation
        import json
        await ws.send_text(json.dumps({
            "event": "connected",
            "topic": topic,
            "message": f"Subscribed to '{topic}' feed",
        }))
        # Keep connection alive; messages arrive from server-side broadcasts
        while True:
            data = await ws.receive_text()
            # Echo back any client pings
            if data.strip().lower() in ("ping", "{}"):
                await ws.send_text('{"event":"pong"}')
    except WebSocketDisconnect:
        ws_manager.disconnect(topic, ws)
        log.info("ws_client_disconnected", topic=topic)


# ════════════════════════════════════════════════════════════════════════════
# System Endpoints
# ════════════════════════════════════════════════════════════════════════════


class _InvestmentRecommendationRequest(BaseModel):
    portfolio_id: str
    investment_amount: float
    investment_date: str | None = None


async def _record_investment_recommendation_decision(
    req: _InvestmentRecommendationRequest,
    user: dict,
    source_endpoint: str,
) -> dict:
    payload = await generate_investment_recommendation(
        portfolio_id=req.portfolio_id,
        investment_amount=req.investment_amount,
        investment_date=req.investment_date,
    )
    if not isinstance(payload, dict):
        payload = {"value": payload}

    if not isinstance(payload.get("explanation"), dict):
        payload["explanation"] = build_explanation(
            module="investment_recommendation_router",
            decision_type="investment_recommendation",
            decision_outcome="PASS",
            summary="Generated investment recommendation payload.",
            key_factors=[
                {"name": "Portfolio", "value": req.portfolio_id, "impact": "neutral"},
                {"name": "Investment Amount", "value": req.investment_amount, "impact": "neutral"},
            ],
            tags=["recommendation", "portfolio"],
        )

    compliant = bool(((payload or {}).get("pf_compliance") or {}).get("pf_compliant", True))
    outcome = "PASS" if compliant else "WARN"
    if isinstance(payload.get("explanation"), dict):
        payload["explanation"]["decision_outcome"] = outcome
    request_payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    calc_version = f"gateway:{APP_VERSION}"
    idempotency_basis = {
        "endpoint": source_endpoint,
        "portfolio_id": req.portfolio_id,
        "investment_amount": req.investment_amount,
        "investment_date": req.investment_date,
    }
    idempotency_key = hashlib.sha256(
        json.dumps(idempotency_basis, sort_keys=True).encode("utf-8")
    ).hexdigest()

    alternatives: list[dict[str, object]] = []
    for idx, bond in enumerate((payload or {}).get("recommended_bonds") or [], start=1):
        alternatives.append(
            {
                "alternative_id": bond.get("isin") or f"candidate-{idx}",
                "alternative_label": bond.get("bond_name") or bond.get("isin") or f"Candidate {idx}",
                "status": "SELECTED" if idx <= 5 else "REJECTED",
                "score": float(bond.get("score") or 0.0),
                "rejection_reason": "" if idx <= 5 else "Lower-ranked than selected bonds",
                "rule_code": "RECOMMENDATION_RANKING",
                "metrics": {
                    "ytm": bond.get("ytm"),
                    "duration": bond.get("modified_duration"),
                    "rating": bond.get("credit_rating"),
                },
            }
        )

    return await DecisionOrchestrator.record_decision(
        payload=payload,
        decision_type="investment_recommendation",
        decision_outcome=outcome,
        module_name="investment_recommendation_router",
        source_endpoint=source_endpoint,
        user=user,
        status="RECORDED",
        input_payload=request_payload,
        idempotency_key=idempotency_key,
        policy_version_id=None,
        calculation_version_id=calc_version,
        alternatives=alternatives,
        portfolio_snapshot_ref=req.portfolio_id,
        market_snapshot_ref=req.investment_date,
        policy_snapshot_ref="pf_compliance_v1",
        metadata={
            "portfolio_id": req.portfolio_id,
            "investment_amount": req.investment_amount,
            "investment_date": req.investment_date,
            "pf_compliant": compliant,
            "data_verified": True,
            "intelligence_completed": True,
            "compliance_validated": True,
            "calculation_version_id": calc_version,
            "policy_name": "PF Core Allocation Policy",
            "policy_body": {
                "version": "v1",
                "constraints": {
                    "minimum_investment": MIN_RECOMMENDATION_INVESTMENT,
                    "pf_compliance_required": True,
                },
                "objective": "Generate compliant fixed-income allocation recommendations",
            },
            "source_snapshots": [
                {
                    "snapshot_type": "portfolio_state",
                    "snapshot_ref": req.portfolio_id,
                    "snapshot_payload": {
                        "portfolio_id": req.portfolio_id,
                        "investment_amount": req.investment_amount,
                    },
                },
                {
                    "snapshot_type": "market_data",
                    "snapshot_ref": req.investment_date or "latest",
                    "snapshot_payload": {
                        "as_of": req.investment_date or "latest",
                        "source": "unified_market_data",
                    },
                },
                {
                    "snapshot_type": "im_document",
                    "snapshot_ref": "im_registry:latest",
                    "snapshot_payload": {
                        "source": "im_registry",
                        "mode": "latest",
                    },
                },
            ],
        },
    )


@app.post("/api/v1/portfolio/investment-recommendation", tags=["Portfolio Management"])
async def investment_recommendation_v1(
    req: _InvestmentRecommendationRequest,
    user=Depends(require_role(Roles.PORTFOLIO)),
):
    """Primary v1 endpoint for Bond Allocation recommendation requests."""
    if req.investment_amount <= 0:
        raise HTTPException(400, "investment_amount must be > 0")
    if req.investment_amount < MIN_RECOMMENDATION_INVESTMENT:
        raise HTTPException(400, f"investment_amount must be >= {int(MIN_RECOMMENDATION_INVESTMENT)}")
    return await _record_investment_recommendation_decision(
        req=req,
        user=user,
        source_endpoint="/api/v1/portfolio/investment-recommendation",
    )


@app.post("/api/portfolio/investment-recommendation", tags=["Portfolio Management"])
async def investment_recommendation_alias(
    req: _InvestmentRecommendationRequest,
    user=Depends(require_role(Roles.PORTFOLIO)),
):
    """Compatibility alias for clients expecting /api/portfolio/investment-recommendation."""
    if req.investment_amount <= 0:
        raise HTTPException(400, "investment_amount must be > 0")
    if req.investment_amount < MIN_RECOMMENDATION_INVESTMENT:
        raise HTTPException(400, f"investment_amount must be >= {int(MIN_RECOMMENDATION_INVESTMENT)}")
    return await _record_investment_recommendation_decision(
        req=req,
        user=user,
        source_endpoint="/api/portfolio/investment-recommendation",
    )

@app.get("/", include_in_schema=False)
async def root():
    """Serve the unified portal UI."""
    index = _STATIC / "index.html"
    if index.exists():
        try:
            html = index.read_text(encoding="utf-8")
            html = html.replace("__APP_VERSION__", APP_VERSION)
            return HTMLResponse(
                content=html,
                status_code=200,
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                    "Pragma": "no-cache",
                    "Expires": "0",
                },
            )
        except Exception:
            return FileResponse(str(index))
    return HTMLResponse(content=_PORTAL_FALLBACK, status_code=200)


@app.get("/api/v1/health", tags=["System"])
async def health(request: Request, _: None = Depends(check_rate_limit)):
    """Public liveness probe with rate limiting (60 req/min)."""
    db = await check_database()
    return {
        "status": db.status.value,
        "version": APP_VERSION,
        "database": {
            "status": db.status.value,
        },
    }


@app.get("/api/v1/health/deep", tags=["System"])
async def health_deep(user=Depends(require_role(Roles.ADMIN))):
    """Comprehensive health check — DB, Redis, disk, memory, all downstream services."""
    report = await run_comprehensive_health()
    report["version"] = APP_VERSION
    report["websockets"] = {
        "topics": ws_manager.topics,
        "connections": ws_manager.connection_count(),
    }
    return report


@app.get("/api/v1/health/readiness", tags=["System"])
async def health_readiness(user=Depends(require_role(Roles.ADMIN))):
    bootstrap_task = getattr(app.state, "bootstrap_task", None)
    intent_task = getattr(app.state, "intent_reconciliation_task", None)
    startup_failures = list(getattr(app.state, "startup_critical_failures", []) or [])
    decision_pipeline_ready = bool(getattr(app.state, "decision_pipeline_ready", False))
    startup_phase = str(getattr(app.state, "startup_phase", "UNKNOWN"))

    try:
        catalog_readiness = await TrustDecisionPipeline.readiness_catalog_report()
    except Exception as exc:
        catalog_readiness = {
            "status": "ERROR",
            "error": str(exc),
            "checked_at": utcnow_iso(),
        }

    readiness_ok = (
        not startup_failures
        and decision_pipeline_ready
        and bootstrap_task is not None
        and bootstrap_task.done()
        and startup_phase == "READY"
        and str(catalog_readiness.get("status") or "") == "OK"
    )

    return {
        "status": "READY" if readiness_ok else "NOT_READY",
        "strict_startup_validation": STRICT_STARTUP_VALIDATION,
        "startup_phase": startup_phase,
        "startup_critical_failures": startup_failures,
        "decision_pipeline_ready": decision_pipeline_ready,
        "bootstrap_task_done": bool(bootstrap_task and bootstrap_task.done()),
        "intent_reconciler_alive": bool(intent_task and not intent_task.done()),
        "catalog_readiness": catalog_readiness,
        "version": APP_VERSION,
    }


@app.get("/api/v1/health/log", tags=["System"])
async def health_log(last_n: int = 100, user=Depends(require_role(Roles.ADMIN))):
    """Return last *n* health log entries."""
    return await get_health_summary(last_n)


@app.get("/api/v1/health/ws", tags=["System"])
async def ws_status(user=Depends(require_role(Roles.ADMIN))):
    """WebSocket connection stats."""
    return {
        "topics": ws_manager.topics,
        "total_connections": ws_manager.connection_count(),
        "per_topic": {t: ws_manager.connection_count(t) for t in ws_manager.topics},
    }


# ── Minimal fallback HTML shown when static/index.html is absent ─────────────
_PORTAL_FALLBACK = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Unified Bond Gateway</title>
<style>
  body{font-family:Segoe UI,sans-serif;background:#0f1117;color:#e2e8f0;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
  .box{text-align:center;padding:48px;background:#1a1d27;border-radius:12px}
  h1{color:#4f8cff;margin-bottom:8px}
  a{color:#4f8cff;text-decoration:none;margin:0 12px;font-size:14px}
  a:hover{text-decoration:underline}
  .tag{background:#22263a;padding:6px 16px;border-radius:20px;
       font-size:12px;color:#94a3b8;margin:4px}
</style></head>
<body><div class="box">
  <h1>Unified Bond Investment Gateway</h1>
  <p style="color:#94a3b8;margin-bottom:24px">All services consolidated. Real-time WebSocket pricing enabled.</p>
  <div style="margin-bottom:20px">
    <span class="tag">Bond Intelligence</span>
    <span class="tag">Compliance Engine</span>
    <span class="tag">Quote System</span>
    <span class="tag">Bond Agent</span>
  </div>
  <div>
    <a href="/docs">📖 Swagger Docs</a>
    <a href="/redoc">📄 ReDoc</a>
    <a href="/api/v1/health">❤️ Health</a>
    <a href="/api/v1/agent/recommendations">🎯 Recommendations</a>
    <a href="/api/v1/alerts">⚠️ Alerts</a>
  </div>
</div></body></html>
"""
