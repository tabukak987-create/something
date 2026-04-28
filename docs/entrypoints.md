# Entry Points

## Backend
- `backend/main.py` → Primary execution point for the FastAPI application (Uvicorn).
- `start.ps1` & `start.bat` → Local development startup scripts for Windows environments.

## Frontend
- `frontend/index.html` → Single-page application root for the primary user interface.

## Other
- `scripts/seeds/seed_indian_bonds.py` → Database initialization and seeding script.
- APScheduler tasks inside `backend/main.py` (e.g., `daily_market_refresh`, `sebi_api_sync`).

## Execution Flow
1. Check Python version and verify dependencies (`start.ps1`).
2. Load environment variables via `.env` files (or OS vars).
3. Initialize the FastAPI app via Uvicorn in `backend/main.py`.
4. Trigger Lifespan context: initialize database schema, load audit/compliance services.
5. If required tables are empty, seed universal bond master and fetch initial market data.
6. Start APScheduler for periodic tasks (refreshing data, health pings).
7. Start listening on HTTP/HTTPS for requests.
