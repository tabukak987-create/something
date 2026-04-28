# POI_CLEAN — PF Trust Investment System

> **Fully self-contained** full-stack bond investment platform.  
> Backend: **FastAPI + SQLite** (auto-upgradeable to PostgreSQL)  
> Frontend: **Vanilla HTML/CSS/JS** served by the backend  
> No Docker, no Node.js, no external services required for local development.

---

## 🚀 Quick Start (Windows)

### Option A — Double-click (easiest)
```
Double-click: start.bat
```

### Option B — PowerShell
```powershell
cd c:\Portfolio_final\POI_CLEAN
.\start.ps1
```

### Option C — Manual (any terminal)
```powershell
cd c:\Portfolio_final\POI_CLEAN
$env:PYTHONPATH = "$PWD;$PWD\backend;$PWD\scripts"
$env:PYTHONIOENCODING = "utf-8"
python -m uvicorn backend.main:app --host 127.0.0.1 --port 9005 --reload
```

---

## 🌐 URLs (after starting)

| URL | Description |
|-----|-------------|
| http://localhost:9005 | Main UI (login page → dashboard) |
| http://localhost:9005/docs | Swagger API docs |
| http://localhost:9005/api/v1/health | Health check |
| http://localhost:9005/dashboards | Dashboard index |

---

## 🔑 Default Login Credentials

| Role | Username | Password |
|------|----------|----------|
| Admin | `admin` | `GatewayA@2026` |
| Viewer | `viewer` | `GatewayV@2026` |

> Credentials are set in `.env` (root) and `backend/.env`.

---

## 📁 Project Structure

```
POI_CLEAN/
├── start.bat              ← Windows double-click launcher
├── start.ps1              ← PowerShell launcher
├── .env                   ← Root environment config (credentials)
├── VERSION                ← App version (3.0.1)
│
├── backend/               ← FastAPI application
│   ├── main.py            ← App entry point (uvicorn target)
│   ├── .env               ← Backend-specific env overrides
│   ├── requirements.txt   ← Python dependencies
│   └── app/
│       ├── api/           ← REST API route handlers
│       ├── services/      ← Business logic services
│       ├── shared/        ← Database, auth, logging utilities
│       └── data/          ← SQLite database (auto-created)
│
├── frontend/              ← Static HTML/CSS/JS (served at /static)
│   ├── index.html         ← Main SPA entry point
│   ├── css/app.css        ← Styles
│   ├── js/                ← JavaScript modules
│   └── dashboards/        ← Feature dashboards
│
├── scripts/
│   └── seeds/             ← Database seed scripts
│       └── seed_indian_bonds.py
│
├── jobs/                  ← Background jobs & migrations
├── infrastructure/        ← Docker, Nginx config (optional)
└── docs/                  ← Architecture documentation
```

---

## ⚙️ Prerequisites

- **Python 3.10+** (tested on 3.10.8)
- All Python packages auto-installed from `backend/requirements.txt`
- No PostgreSQL needed for local dev (SQLite fallback is automatic)
- No Redis needed (disabled by default via `REDIS_REQUIRED=false`)

---

## 🔧 Environment Variables

Key vars (set in `.env` at project root):

| Variable | Default | Purpose |
|----------|---------|---------|
| `PORT` | `9005` | HTTP port |
| `GATEWAY_ENV` | `development` | Environment mode |
| `ADMIN_USERNAME` | `admin` | Admin login |
| `ADMIN_PASSWORD` | `GatewayA@2026` | Admin password |
| `GATEWAY_DB_URL` | *(SQLite fallback)* | PostgreSQL URL for production |
| `ALLOW_EXTERNAL_DATA_EGRESS` | `false` | Enable live market data fetching |
| `EXPOSE_API_DOCS` | `true` | Show Swagger /docs |

---

## 🗄️ Database

- **Development**: SQLite database at `backend/app/data/bond_gateway.db` (auto-created)
- **Production**: Set `GATEWAY_DB_URL=postgresql+asyncpg://user:pass@host/db` in `.env`

---

## 🩺 Health Check

```bash
curl http://localhost:9005/api/v1/health
# → {"status":"OK","version":"3.0.1","database":{"status":"OK"}}
```
