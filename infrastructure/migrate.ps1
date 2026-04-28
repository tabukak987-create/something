$ErrorActionPreference = "Stop"

# Create new directories
$dirs = @(
    "backend/app",
    "backend/tests",
    "frontend",
    "infrastructure/docker",
    "infrastructure/nginx",
    "infrastructure/scripts",
    "jobs/agents",
    "jobs/cron",
    "jobs/migrations",
    "docs/architecture",
    "docs/governance",
    "docs/runbooks",
    "scripts/windows"
)

foreach ($dir in $dirs) {
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
}

# 1. Move root scripts to jobs/ and scripts/
if (Test-Path "bond_im_agent.py") { Move-Item "bond_im_agent.py" "jobs/agents/" }
if (Test-Path "daily_monitor.py") { Move-Item "daily_monitor.py" "jobs/cron/" }
if (Test-Path "fetch_nse_bonds.py") { Move-Item "fetch_nse_bonds.py" "jobs/cron/" }
if (Test-Path "sync_holdings_sheet_to_gateway.py") { Move-Item "sync_holdings_sheet_to_gateway.py" "jobs/cron/" }
if (Test-Path "email_pdf_reader.py") { Move-Item "email_pdf_reader.py" "jobs/cron/" }
if (Test-Path "offsite_backup.py") { Move-Item "offsite_backup.py" "jobs/cron/" }
if (Test-Path "restore_test.py") { Move-Item "restore_test.py" "jobs/cron/" }
if (Test-Path "migrate_to_postgres.py") { Move-Item "migrate_to_postgres.py" "jobs/migrations/" }

Get-ChildItem -Filter "*.bat" -File | Move-Item -Destination "scripts/windows/"
Get-ChildItem -Filter "*.ps1" -File | Where-Object { $_.Name -ne "migrate.ps1" } | Move-Item -Destination "infrastructure/scripts/"
Get-ChildItem -Filter "*.xml" -File | Move-Item -Destination "infrastructure/scripts/"

# 2. Isolate the frontend
if (Test-Path "unified_gateway/static") {
    Get-ChildItem "unified_gateway/static/*" -Recurse | Move-Item -Destination "frontend/" -Force
    Remove-Item "unified_gateway/static" -Recurse -Force
}

# 3. Refactor Backend (Unified Gateway)
if (Test-Path "unified_gateway/tests") {
    Move-Item "unified_gateway/tests/*" "backend/tests/"
    Remove-Item "unified_gateway/tests" -Recurse -Force
}

if (Test-Path "unified_gateway") {
    Get-ChildItem "unified_gateway/*" | Move-Item -Destination "backend/app/"
    Remove-Item "unified_gateway" -Recurse -Force
}

# 4. Consolidate Documentation
if (Test-Path "governance") {
    Get-ChildItem "governance/*" | Move-Item -Destination "docs/governance/"
    Remove-Item "governance" -Recurse -Force
}

$rootDocs = @("INTELLIGENT_CONTROL_LAYER.md", "SYSTEM_SUMMARY.md", "DEPLOYMENT_CHECKLIST.md", "ROLLBACK_PROCEDURE.md", "MANIFEST.md", "README_BOND_AGENT.md", "QUICK_START.md")
foreach ($doc in $rootDocs) {
    if (Test-Path $doc) { Move-Item $doc "docs/architecture/" }
}

# 5. Centralize Infrastructure
if (Test-Path "Dockerfile") { Move-Item "Dockerfile" "infrastructure/docker/" }
if (Test-Path "docker-compose.yml") { Move-Item "docker-compose.yml" "infrastructure/docker/" }
if (Test-Path "docker-compose.staging.yml") { Move-Item "docker-compose.staging.yml" "infrastructure/docker/" }
if (Test-Path "nginx.conf") { Move-Item "nginx.conf" "infrastructure/nginx/" }
if (Test-Path "nginx_waf.conf") { Move-Item "nginx_waf.conf" "infrastructure/nginx/" }

# 6. Delete Zombie folders
if (Test-Path "engines") { Remove-Item "engines" -Recurse -Force }
if (Test-Path "backend/tests/perf/results") { Remove-Item "backend/tests/perf/results/*.csv" -Force; Remove-Item "backend/tests/perf/results/*.json" -Force }

Write-Output "Directory migration complete."
