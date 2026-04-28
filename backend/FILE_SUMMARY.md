# Backend Folder Summary

## Purpose
The `backend` directory houses the FastAPI application that acts as the core gateway, integrating compliance, risk, portfolio management, and market data operations.

## Key Files
- **[ENTRY]** `main.py`: Bootstraps the application, mounts routes, manages the lifespan, and initializes schedulers.
- **[CRITICAL]** `app/shared/database.py`: Core database connectivity and utilities.
- **[CRITICAL]** `app/services/decision_orchestrator.py`: The single funnel for critical, auditable investment decisions.

## Structure
- `app/api/`: REST endpoints grouped by domain.
- `app/services/`: Core business logic (currently being refactored into `app/domain/`).
- `app/shared/`: Cross-cutting concerns (DB, Logging, Auth).
- `app/utils/`: Generic helper functions.

## Token Efficiency Notes
- **HEAVY FILES**: Files like `portfolio_dataset_service.py` (>90KB) exist in `app/services/`. Avoid opening them completely if only making small fixes. Use `grep_search` and line-range viewing.
