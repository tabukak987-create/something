$ErrorActionPreference = 'Stop'

$rules = @(
    @{ Name = 'POI Web (TCP 5173 - Local Subnet)'; Port = 5173 },
    @{ Name = 'POI API (TCP 8080 - Local Subnet)'; Port = 8080 }
)

$needsConfiguration = $false
foreach ($rule in $rules) {
    $existing = Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue
    if (-not $existing -or -not ($existing.Enabled -contains $true)) {
        $needsConfiguration = $true
    }
}

if (-not $needsConfiguration) {
    Write-Host 'POI local-network firewall access is already configured.' -ForegroundColor Green
    exit 0
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$isAdministrator = $principal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdministrator) {
    Write-Host 'Windows administrator approval is needed once to allow same-Wi-Fi access.' -ForegroundColor Yellow
    $process = Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -PassThru -ArgumentList @(
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-File', "`"$PSCommandPath`""
    )
    exit $process.ExitCode
}

foreach ($rule in $rules) {
    $existing = Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue
    if ($existing) {
        Enable-NetFirewallRule -DisplayName $rule.Name | Out-Null
        Write-Host "Enabled $($rule.Name)" -ForegroundColor Green
        continue
    }

    New-NetFirewallRule `
        -DisplayName $rule.Name `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort $rule.Port `
        -Profile Private `
        -RemoteAddress LocalSubnet | Out-Null
    Write-Host "Created $($rule.Name)" -ForegroundColor Green
}

Write-Host 'POI is allowed for devices on this private local subnet.' -ForegroundColor Green
