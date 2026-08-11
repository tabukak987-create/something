$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$api = Join-Path $root 'backend-springboot'
$web = Join-Path $root 'frontend-react'
$lanIp = (Get-NetIPConfiguration |
    Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' } |
    Select-Object -First 1).IPv4Address.IPAddress
if (-not $lanIp) { $lanIp = 'localhost' }

if (-not (Get-Command mvn -ErrorAction SilentlyContinue)) { throw 'Maven is required. Install Maven 3.9+ and ensure it is on PATH.' }
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw 'Node.js and npm are required. Install Node.js 20+ and ensure it is on PATH.' }

& (Join-Path $root 'setup-local-network.ps1')
if ($LASTEXITCODE -ne 0) {
    Write-Warning 'Firewall setup was not completed. This computer will work, but other Wi-Fi devices may be blocked.'
}

function Test-LocalPort([int] $port) {
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $client.Connect('127.0.0.1', $port)
        return $true
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

if (Test-LocalPort 8080) {
    Write-Host 'Spring Boot API is already running on http://localhost:8080' -ForegroundColor Cyan
} else {
    Write-Host 'Starting Spring Boot API in a separate window on http://localhost:8080' -ForegroundColor Cyan
    Start-Process -FilePath 'cmd.exe' -ArgumentList '/k', "cd /d `"$api`" && mvn spring-boot:run"
}

Set-Location $web
if (-not (Test-Path 'node_modules')) { npm install }
Write-Host 'This computer: http://localhost:5173' -ForegroundColor Cyan
Write-Host "Same Wi-Fi:    http://${lanIp}:5173" -ForegroundColor Green
if (Test-LocalPort 5173) {
    Write-Host 'React app is already running.' -ForegroundColor Cyan
    exit 0
}
npm run dev
