# Folder Overview

## Purpose
The `backend` folder contains the core Python FastAPI application, housing all business logic, data models, API endpoints, and integrations for the Unified Bond Investment Gateway.

## Key Files
- `main.py` → FastAPI entry point, application lifespan, and unified server configuration.
- `requirements.txt` → Python dependencies.

## Structure
- `app/api/` → API controllers and route handlers organized by domain (v1).
- `app/services/` → Core business logic (Compliance, Intelligence, Trading, Alerts).
- `app/shared/` → Shared components (Auth, Database, Logging, Metrics).
- `app/utils/` → Helper functions and utilities.
- `app/data/` → Local database files or static seed data.

## Rules
- All incoming requests must be processed asynchronously.
- Database access should be handled through `app/shared/database.py`.
- Complex business decisions must be routed through the `DecisionOrchestrator` to maintain an audit trail.

## Dependencies
- Depends on PostgreSQL/SQLite (via `sqlite3` or `asyncpg`).
- Depends on external APIs (SEBI, NSE, BSE) for market data.
