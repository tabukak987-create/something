# Key Files

- `backend/main.py` → Unified entry point, lifespan management, and global middlewares.
- `backend/app/shared/database.py` → Core database connection, querying, and async pool management.
- `backend/app/services/decision_orchestrator.py` → Central router enforcing compliance and tracing for investment decisions.
- `backend/app/services/trust_decision_pipeline.py` → Pipeline handling pre-trade, four-eyes approval, and execution intents.
- `backend/app/services/ips_engine.py` → Engine for enforcing Investment Policy Statement constraints.
- `backend/app/shared/auth.py` → Authentication handlers, role requirements, and token validation.
- `backend/app/shared/env_bootstrap.py` → Environment variable loader and safe bootstrap handler.
- `frontend/index.html` → Main structural HTML layout and sidebar navigation.
- `frontend/js/app.js` → Core frontend logic, UI routing, and page rendering.
- `frontend/js/api.js` → Frontend wrapper for interacting with backend REST endpoints.
- `infrastructure/docker-compose.yml` → Main container definition for deployment.
- `infrastructure/nginx.conf` → Reverse proxy routing configuration and caching.
- `start.ps1` → Developer bootstrap script for local execution.
