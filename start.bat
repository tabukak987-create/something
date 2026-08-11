@echo off
setlocal
set ROOT=%~dp0
set "LAN_IP="
for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "$c=(Get-NetIPConfiguration).Where({$_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up'}, 'First'); $c.IPv4Address.IPAddress"`) do set "LAN_IP=%%I"
if not defined LAN_IP set "LAN_IP=localhost"
where mvn >nul 2>&1 || (echo Maven 3.9+ is required.& exit /b 1)
where npm >nul 2>&1 || (echo Node.js 20+ is required.& exit /b 1)
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%setup-local-network.ps1"
if errorlevel 1 echo WARNING: Firewall setup was not completed. Other Wi-Fi devices may be blocked.
powershell -NoProfile -Command "$c=New-Object Net.Sockets.TcpClient; try{$c.Connect('127.0.0.1',8080); exit 0}catch{exit 1}finally{$c.Dispose()}" >nul 2>&1
if errorlevel 1 (
    echo Starting Spring Boot API in a new window on http://localhost:8080
    start "POI Spring Boot API" /D "%ROOT%backend-springboot" cmd /k "call mvn spring-boot:run"
) else (
    echo Spring Boot API is already running on http://localhost:8080
)
cd /d "%ROOT%frontend-react"
if not exist node_modules call npm install
echo This computer: http://localhost:5173
echo Same Wi-Fi:    http://%LAN_IP%:5173
powershell -NoProfile -Command "$c=New-Object Net.Sockets.TcpClient; try{$c.Connect('127.0.0.1',5173); exit 0}catch{exit 1}finally{$c.Dispose()}" >nul 2>&1
if not errorlevel 1 (
    echo React app is already running.
    exit /b 0
)
call npm run dev
