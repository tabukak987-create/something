@echo off
setlocal
set "ROOT=%~dp0..\.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
echo.
echo  Starting the self-contained local gateway.
echo.
call "%ROOT%\start.bat"
exit /b %ERRORLEVEL%
