# System Entry Points

This document outlines all executable entry points for the application, designed for quick scanning by AI agents.

## Backend Startup
* **Main Application Server**
  ```bash
  uvicorn backend.main:app --host 127.0.0.1 --port 9005 --reload
  ```
  *(Or executed via `Start_Gateway.bat` if present on Windows).*

* **Docker Deployment**
  ```bash
  docker-compose up --build -d
  ```

## Core API Root Routes (via `main.py`)
* **`/api/v1/auth`** → Authentication & Token Issuance
* **`/api/v1/portfolio`** → Portfolio Management & Institutional Overrides
* **`/api/v1/risk`** → Yield Curve, VaR, and Spread Simulation
* **`/api/v1/compliance`** → Pre-trade / Post-trade Validators
* **`/api/v1/market-data`** → Live Bond Pricing & Quotes
* **`/api/v1/intelligence`** → Scoring and Recommendation Algorithms
* **`/api/v1/health`** → Deep System Health & Subsystem Status
* **`/ws/{topic}`** → Live Pricing WebSocket Feeds

## Job Runners & Agents
* **Bond Agent (Daily PDF Report)**
  ```bash
  python jobs/agents/bond_im_agent.py
  ```
  *(Generates `Daily_Bond_Recommendation_Report.pdf` based on yield heuristics).*

* **APScheduler Daemon**
  * Automatically initialized inside `unified_gateway/main.py`.
  * Runs `daily_market_refresh@07:30IST`, `exchange_trade_fetch@16:30IST`, `four_eyes_expiry_sweep@60min`.

## Script Entry Commands
* **Database Migrations**
  ```powershell
  .\migrate.ps1
  ```
* **Disaster Recovery Drill**
  ```bash
  python scripts/dr_drill.py
  ```
* **Resilience Chaos Testing**
  ```bash
  python scripts/chaos_testing.py
  ```
* **Populate Bond Universe (Seed)**
  ```bash
  python scripts/seeds/seed_indian_bonds.py
  ```
