@echo off
title Bond Investment Recommendation Agent
color 0A

echo ============================================================
echo   DAILY BOND INVESTMENT RECOMMENDATION AGENT
echo ============================================================
echo.
echo   Starting the Bond Analysis Agent...
echo   Please wait...
echo.
echo ============================================================
echo.

cd /d "%~dp0"

REM Check if virtual environment exists
if exist ".venv\Scripts\python.exe" (
    echo [OK] Python environment found
    echo.
    
    REM Start the Flask server
    echo Starting web server...
    echo.
    echo The agent will open in your browser at:
    echo http://localhost:5000
    echo.
    echo ============================================================
    echo   Press Ctrl+C to stop the server
    echo ============================================================
    echo.
    
    start http://localhost:5000
    .venv\Scripts\python.exe bond_agent_server.py
) else (
    echo [ERROR] Python environment not found!
    echo.
    echo Please ensure the virtual environment is set up.
    echo.
    pause
)
