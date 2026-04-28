# Dependency Graph (Simplified)

## Internal Module Hierarchy

* **`main.py` (Gateway)**
  * Requires: `routers/`, `services/`, `enhanced/`, `shared/`
* **`routers/` (API Layer)**
  * Requires: `services/` (for business logic), `shared/auth.py` (for token validation)
* **`services/` (Business Logic Layer)**
  * Requires: `shared/database.py` (for persistence), `shared/logger.py`
  * Sub-dependencies:
    * `decision_orchestrator` depends on `audit_service`, `ips_engine`, `risk_engine`
    * `ips_engine` depends on `shared.database`
* **`enhanced/` (Market Intelligence Layer)**
  * Requires: `shared/database.py`
  * Sub-dependencies:
    * `alert_engine` depends on `market_data_fetcher`
* **`jobs/` (Background Agents)**
  * Requires: HTTP access to Gateway OR direct access to `shared/database.py`
* **`shared/` (Core Utilities)**
  * Independent. Does not depend on `routers/`, `services/`, or `jobs/`.

## External Services & Infrastructure

* **Databases**
  * PostgreSQL / SQLite (via SQLAlchemy) → Core persistence (`compliance.db`, `unified_market_data`, `users`).
  * Redis (Optional) → Caching and distributed rate limiting.
* **External APIs**
  * RBI Retail Direct (Bond master & yield data)
  * NSE / BSE (Exchange trade pricing)
  * SEBI APIs (Regulatory updates, via `sebi_api_sync.py`)
  * Financial Benchmarks India (FBIL) (Yield curves)
* **Infrastructure Components**
  * Nginx → Reverse proxy, WAF, TLS termination.
  * Docker & Docker Compose → Container orchestration and isolation.
  * Uvicorn → ASGI web server.
  * APScheduler → In-memory async task scheduling.
