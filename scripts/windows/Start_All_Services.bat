@echo off
setlocal
set "ROOT=%~dp0..\.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
echo.
echo  The legacy multi-service stack is not bundled here.
echo  Starting the self-contained local gateway instead.
echo.
call "%ROOT%\start.bat"
exit /b %ERRORLEVEL%
