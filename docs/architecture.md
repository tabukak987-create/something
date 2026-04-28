# Architecture Overview

## Tech Stack
- Frontend: Vanilla HTML, CSS, JavaScript, Chart.js
- Backend: Python 3.10+, FastAPI, Uvicorn
- Database: PostgreSQL (production) / SQLite (local), Redis (optional for caching)
- Infra: Docker, Docker Compose, Nginx

## Entry Points
- Backend: `backend/main.py` (FastAPI app)
- Frontend: `frontend/index.html` (Single page app root)
- Startup: `start.ps1` or `start.bat` (Local development)

## System Flow
User (Browser) → Nginx (if deployed) → FastAPI Gateway (`backend/main.py`) → Domain Services (`backend/services`) → Database (PostgreSQL/SQLite) & External APIs (SEBI, NSE, BSE)

## Core Modules
- `backend/api` → FastAPI route handlers and controllers
- `backend/services` → Core business logic (Compliance, Alerts, Intelligence, Recommendations)
- `backend/shared` → Cross-cutting concerns (DB, Auth, Logging, Metrics)
- `frontend/js` → Frontend logic, API communication, and UI rendering
- `infrastructure` → Deployment and environment configurations
- `jobs/scripts` → Schedulers and offline background workers

## Notes
- The system is designed as a unified monolithic gateway consolidating intelligence, compliance, quotes, and daily agent functions.
- External egress (fetching market data) is controlled via `ALLOW_EXTERNAL_DATA_EGRESS`.
- Enforces strict startup validations and HTTPs configurations based on environment.
