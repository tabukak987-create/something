# Migration architecture audit

Date: 2026-08-07

## Current system

The application is a single FastAPI gateway with a vanilla HTML/JavaScript user interface. It is not safe to remove its `backend/` or `frontend/` folders yet: they contain the only implementation of the product's business workflows.

| Area | Current implementation | Migration target |
| --- | --- | --- |
| HTTP API | FastAPI routers beneath `backend/app/api` | Spring Boot controllers and DTOs |
| Authentication | JWT, BCrypt, role-based access, MFA and token revocation | Spring Security, JWT, BCrypt, role authorities; MFA parity |
| Persistence | SQLite locally; PostgreSQL-capable SQL layer | MySQL 8 with versioned Flyway migrations |
| User interface | HTML pages, `frontend/js/app.js`, Chart.js | React, TypeScript, React Router, typed API client |
| Scheduled work | Python jobs and APScheduler | Retain Python jobs initially; invoke through a controlled integration boundary |

## Inventory

- 23 API modules, registered under both `/api/v1` and selected `/api/v2` routes.
- 281 endpoint declarations identified.
- 141 table declarations found across the shared schema and domain modules.
- 139 Python files across the backend, scripts, and scheduled jobs.
- 11 HTML pages and 6 JavaScript modules in the existing frontend.

## Functional domains

1. Authentication and IAM: login, refresh/session management, roles, password changes/resets, MFA and backup codes.
2. Portfolio and data sources: holdings, uploads, allocation, market prices, cashflows, broker data, data-quality checks.
3. Bond intelligence and due diligence: scoring, issuer analysis, IM document processing, bond health, recommendations.
4. Compliance and IPS: pre/post-trade validation, rules, breach tracking, SEBI data, four-eyes approvals.
5. Risk: scenarios, VaR, yield curve, rate sensitivity, alerts.
6. Governance: decision registry, explainability, audit ledger, trust anchors, reports, export/download flows.
7. Operations: news/market-data ingestion, scheduled refreshes, monitoring, backup and restoration jobs.

## Required migration sequence

1. Extract the existing API contract and create endpoint-by-endpoint parity tests before changing behavior.
2. Convert `backend/app/shared/schema.sql` into ordered MySQL Flyway migrations, resolving duplicate declarations before creating JPA entities.
3. Build Spring Boot foundation: configuration, error responses, logging, security, JWT, role authorization, audit boundary, health checks, and OpenAPI.
4. Migrate domains in dependency order: IAM -> instruments/portfolio -> market data/quotes -> compliance/risk -> intelligence/IM -> governance/reports.
5. Replace frontend routes and flows one domain at a time with React TypeScript. Keep the legacy frontend available until each route passes parity tests.
6. Keep genuinely required Python ingest, document-processing, and scheduled jobs, then integrate them through explicit interfaces. Remove a Python component only after the replacement is tested.
7. Delete legacy source only after the whole parity suite, integration tests, database migration tests, and browser flows pass.

## Cleanup completed

`graphify-out/` was deleted on 2026-08-07. It contained generated graph-analysis cache and reports (about 19 MB), not application source or data.

## Cleanup intentionally deferred

`backend/`, `frontend/`, `jobs/`, and `scripts/` remain because they contain active features that have not yet been migrated and verified.
