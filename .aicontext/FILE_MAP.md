# File Intent Index

## backend/app/api (currently `unified_gateway/routers/`)
* `unified_gateway/main.py` → Primary FastAPI application, scheduler, and central entry point.
* `unified_gateway/routers/auth.py` → Authentication, login, and token issuance endpoints.
* `unified_gateway/routers/risk.py` → Risk analysis and portfolio simulation endpoints.
* `unified_gateway/routers/portfolio.py` → Portfolio state management and basic retrieval.
* `unified_gateway/routers/portfolio_v2.py` → Enhanced portfolio features, sync, and overrides.
* `unified_gateway/routers/market_data.py` → Endpoints to access unified bond market pricing.
* `unified_gateway/routers/ips_compliance.py` → Investment Policy Statement verification endpoints.
* `unified_gateway/routers/governance.py` → Platform governance, approval, and oversight routes.
* `unified_gateway/routers/due_diligence.py` → Security documentation and due diligence checkpoints.
* `unified_gateway/routers/decision_registry.py` → Trust decisions and intent processing routes.
* `unified_gateway/routers/intelligence.py` → Advanced bond insights and scoring retrieval.

## backend/app/services (currently `unified_gateway/services/` & `unified_gateway/enhanced/`)
* `unified_gateway/shared/auth.py` → Core JWT token parsing, role validation, and security policies.
* `unified_gateway/shared/database.py` → Core DB connection handling and schema logic.
* `unified_gateway/services/ips_engine.py` → Executes rules for institutional portfolio constraints.
* `unified_gateway/services/risk_engine.py` → Core math for VaR, duration, and yield curve simulation.
* `unified_gateway/services/decision_orchestrator.py` → Manages the lifecycle of intent-based decisions.
* `unified_gateway/services/audit_service.py` → Creates immutable ledger entries and trust anchors.
* `unified_gateway/services/four_eyes_approval.py` → Enforces Maker-Checker rules for sensitive actions.
* `unified_gateway/enhanced/market_data_fetcher.py` → Orchestrates ingestion from external bond markets.
* `unified_gateway/enhanced/alert_engine.py` → Evaluates market thresholds for automated notifications.
* `unified_gateway/services/bond_scoring_engine.py` → Generates proprietary ratings for bond instruments.
* `unified_gateway/services/portfolio_analytics.py` → Calculates aggregate portfolio metrics and allocations.

## jobs/ (currently mixed in `jobs/`, `scripts/`, `unified_gateway/scripts/`)
* `jobs/agents/bond_im_agent.py` → Standalone agent for bond reporting and PDF generation.
* `unified_gateway/scripts/backup_trust.py` → Cron script to snapshot trust ledgers.
* `scripts/dr_drill.py` → Simulates disaster recovery and failover workflows.
* `scripts/chaos_testing.py` → Validates system resilience by injecting faults.
* `unified_gateway/seeds/seed_indian_bonds.py` → Populates initial bond master dataset.

## infrastructure/
* `Dockerfile` → Container blueprint for the entire unified backend.
* `docker-compose.yml` → Local multi-container development environment definition.
* `docker-compose.staging.yml` → Staging environment composition and replicas.
* `nginx.conf` → Primary reverse proxy and routing configuration.
* `nginx_waf.conf` → Web Application Firewall constraints and rate limiting logic.
* `migrate.ps1` → Windows PowerShell script for automating migrations.
* `DEPLOYMENT_CHECKLIST.md` → Pre-flight checks for production releases.
