@echo off
:: ============================================================
:: POI_CLEAN — Self-Contained Startup Script (Windows CMD)
:: Double-click or run from POI_CLEAN directory:  start.bat
:: ============================================================

setlocal enabledelayedexpansion

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set PORT=9005
set PYTHON=python

echo.
echo   ╔══════════════════════════════════════════════════╗
echo   ║   PF Trust Investment System — POI_CLEAN         ║
echo   ║   Unified Bond Investment Gateway v3             ║
echo   ╚══════════════════════════════════════════════════╝
echo.

:: ── Python check ──────────────────────────────────────────
%PYTHON% --version >nul 2>&1
if errorlevel 1 (
    echo   [ERROR] Python not found. Install Python 3.10+ and add to PATH.
    pause
    exit /b 1
)
for /f "tokens=*" %%i in ('%PYTHON% --version 2^>^&1') do echo   [OK] %%i

:: ── Install / verify dependencies ─────────────────────────
echo   [->] Checking dependencies...
%PYTHON% -m pip install -r "%ROOT%\backend\requirements.txt" --quiet --disable-pip-version-check
if errorlevel 1 (
    echo   [ERROR] Dependency install failed.
    echo          Run: pip install -r backend\requirements.txt
    pause
    exit /b 1
)
echo   [OK] Dependencies OK

:: ── Set environment ────────────────────────────────────────
set PYTHONPATH=%ROOT%;%ROOT%\backend;%ROOT%\scripts
set PYTHONIOENCODING=utf-8
set GATEWAY_ENV=development
set EXPOSE_API_DOCS=true
set ALLOW_EXTERNAL_DATA_EGRESS=false
set REDIS_REQUIRED=false
set RATE_LIMIT_RPM=600

echo.
echo   [->] Starting gateway on http://localhost:%PORT% ...
echo.
echo   ┌──────────────────────────────────────────────────┐
echo   │  Main UI:     http://localhost:%PORT%            │
echo   │  API Docs:    http://localhost:%PORT%/docs       │
echo   │  Health:      http://localhost:%PORT%/api/v1/health│
echo   │  Login:       admin / GatewayA@2026             │
echo   └──────────────────────────────────────────────────┘
echo.
echo   Press Ctrl+C to stop.
echo.

:: ── Open browser after 3 seconds ──────────────────────────
start "" /b cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:%PORT%"

:: ── Launch uvicorn ─────────────────────────────────────────
%PYTHON% -m uvicorn backend.main:app --host 127.0.0.1 --port %PORT% --reload --reload-dir "%ROOT%\backend" --reload-dir "%ROOT%\scripts"
