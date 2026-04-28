# AI Context: Bond Intelligence System

## System Purpose
A centralized backend gateway for fixed-income institutional portfolio operations. It serves as an integrated Finance, Risk, and Bond Intelligence system combining real-time quote feeds, pre/post-trade compliance checking, algorithmic bond scoring, automated report generation (PDF), and background alert agents. Designed with strict audit trails, rule-based execution, and strict architectural constraints.

## Core Modules
* **Unified Gateway API (`unified_gateway/main.py`)**: The FastAPI monolithic entry point for all HTTP routes and background scheduling.
* **Compliance Engine (`unified_gateway/services/ips_engine.py`, `pretrade_validator.py`)**: Enforces Investment Policy Statements (IPS) and pre/post-trade rules.
* **Decision Orchestrator (`unified_gateway/services/decision_orchestrator.py`)**: Manages the intent-based decision pipelines for portfolios.
* **Market Intelligence (`unified_gateway/enhanced/`)**: Connects to NSE/BSE/RBI to sync bond data, trades, and news.
* **Background Agents (`jobs/`, `unified_gateway/scripts/`)**: Automated jobs for PDF reporting, system dr-drills, and syncs.

## Data Flow
* **Ingestion**: `EnhancedMarketDataFetcher` & `SEBIApiSync` → External APIs → SQLite/PostgreSQL (`unified_market_data`).
* **Processing**: `BondScoringEngine` & `RiskEngine` read data → calculate yields, spread, risk metrics.
* **Execution**: User Request → FastAPI Routers (`unified_gateway/routers/`) → Services (`unified_gateway/services/`) → Responses.
* **Audit**: All actions → `AuditService` (immutable ledger).

## Execution Flow
* **Request Lifecycle**: Nginx WAF → FastAPI `main.py` → Token Auth (`shared.auth`) → Rate Limiter → Router → Service Layer → DB Query → JSON Response.
* **Job Lifecycle**: APScheduler (`main.py`) → triggers async fetchers/analyzers on intervals → updates DB state → fires AlertEngine → records Trust Anchor.

## ⚠️ What NOT to Touch (Critical Safety Areas)
* `unified_gateway/shared/auth.py`: Cryptographic hash checks and role-based access.
* `unified_gateway/services/audit_service.py`: Immutable ledger and trust anchors.
* `unified_gateway/main.py`: APScheduler core loops and strict middleware constraints (unless explicitly requested).
* `unified_gateway/services/four_eyes_approval.py`: Maker-checker logic for institutional compliance.
