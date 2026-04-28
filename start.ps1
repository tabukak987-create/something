# ============================================================
# POI_CLEAN — Self-Contained Startup Script (PowerShell)
# Run from the POI_CLEAN directory:  .\start.ps1
# ============================================================

$ErrorActionPreference = "Stop"
$ROOT = $PSScriptRoot          # directory containing this script
$PORT = if ($env:PORT) { $env:PORT } else { "9005" }
$PYTHON = "python"

Write-Host ""
Write-Host "  ╔══════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "  ║   PF Trust Investment System — POI_CLEAN         ║" -ForegroundColor Cyan
Write-Host "  ║   Unified Bond Investment Gateway v3             ║" -ForegroundColor Cyan
Write-Host "  ╚══════════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# ── Python check ──────────────────────────────────────────
try {
    $pyVer = & $PYTHON --version 2>&1
    Write-Host "  [✓] $pyVer" -ForegroundColor Green
} catch {
    Write-Host "  [✗] Python not found. Install Python 3.10+ and re-run." -ForegroundColor Red
    exit 1
}

# ── Install / verify dependencies ─────────────────────────
Write-Host "  [→] Checking dependencies..." -ForegroundColor Yellow
& $PYTHON -m pip install -r "$ROOT\backend\requirements.txt" --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [✗] Dependency install failed. Run: pip install -r backend\requirements.txt" -ForegroundColor Red
    exit 1
}
Write-Host "  [✓] Dependencies OK" -ForegroundColor Green

# ── Set environment ────────────────────────────────────────
$env:PYTHONPATH     = "$ROOT;$ROOT\backend;$ROOT\scripts"
$env:PYTHONIOENCODING = "utf-8"
$env:PORT           = $PORT
$env:GATEWAY_ENV    = "development"
$env:EXPOSE_API_DOCS = "true"
$env:ALLOW_EXTERNAL_DATA_EGRESS = "false"
$env:REDIS_REQUIRED = "false"
$env:RATE_LIMIT_RPM = "600"

Write-Host ""
Write-Host "  [→] Starting gateway on http://localhost:$PORT ..." -ForegroundColor Yellow
Write-Host ""
Write-Host "  ┌──────────────────────────────────────────────────┐"
Write-Host "  │  Main UI:     http://localhost:$PORT             │"
Write-Host "  │  API Docs:    http://localhost:$PORT/docs        │"
Write-Host "  │  Health:      http://localhost:$PORT/api/v1/health│"
Write-Host "  │  Login:       admin / GatewayA@2026             │"
Write-Host "  └──────────────────────────────────────────────────┘"
Write-Host ""
Write-Host "  Press Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""

# ── Launch uvicorn ─────────────────────────────────────────
& $PYTHON -m uvicorn backend.main:app `
    --host 127.0.0.1 `
    --port $PORT `
    --reload `
    --reload-dir "$ROOT\backend" `
    --reload-dir "$ROOT\scripts"
