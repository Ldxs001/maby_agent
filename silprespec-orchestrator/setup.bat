@echo off
setlocal enabledelayedexpansion
title Silprespec Orchestrator

set PORT=8789

echo ========================================
echo    Silprespec Orchestrator (port %PORT%)
echo ========================================
echo.

python --version >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] Python found
    goto START
)
echo [FAIL] Python not found. Install Python 3.11+
pause
exit /b 1

:START
echo Installing dependencies...
python -m pip install -r "%~dp0requirements.txt" 2>nul
echo.

cd /d "%~dp0"

if exist "%~dp0server.pid" del "%~dp0server.pid" 2>nul

for /f "tokens=5" %%a in ('netstat -ano ^| find ":%PORT% " 2^>nul') do taskkill /f /pid %%a >nul 2>&1

start /B "" python main.py --web --port %PORT% --pidfile "%~dp0server.pid"

set WAIT=0
:WAIT_LOOP
netstat -ano | find ":%PORT% " >nul 2>&1
if !errorlevel! equ 0 goto PORT_FOUND
ping 127.0.0.1 -n 2 >nul
set /a WAIT+=2
if %WAIT% lss 15 goto WAIT_LOOP
echo [WARN] Server not ready, opening http://localhost:%PORT%...
start http://localhost:%PORT%
goto RUNNING

:PORT_FOUND
echo Server started on port: %PORT%
start http://localhost:%PORT%

:RUNNING
echo.
echo ========================================
echo  Server is running.
echo  Press any key to STOP the server and exit.
echo ========================================
pause >nul

echo Stopping server...
if exist "%~dp0server.pid" (
    set /p SPID=<"%~dp0server.pid"
    taskkill /f /pid !SPID! >nul 2>&1
    del "%~dp0server.pid" 2>nul
)
echo Server stopped.