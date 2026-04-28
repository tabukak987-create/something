@echo off
setlocal
set "ROOT=%~dp0..\.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
set "PYTHON=python"
pushd "%ROOT%"
%PYTHON% -m uvicorn backend.main:app --host 127.0.0.1 --port 9005 1>>"%ROOT%\gateway-start.log" 2>>"%ROOT%\gateway-start.err.log"
popd
