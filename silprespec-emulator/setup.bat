@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title 前置规范效果实验台

set PORT=8805

echo ========================================
echo    前置规范效果实验台 (port %PORT%)
echo ========================================
echo.

REM --- Python detection ---
set "PYCMD=python"
%PYCMD% --version >nul 2>&1
if errorlevel 1 (
    echo [FAIL] Python not found. Please install Python 3.11+
    echo         https://www.python.org/downloads/
    pause
    exit /b 1
)
%PYCMD% --version
echo.

REM --- Dependencies (仅标准库，跳过 pip；保留兼容性提示) ---
echo [*] Dependencies: pure standard library, no pip install needed.
echo.

REM --- Kill old instances on port ---
echo [*] Cleaning old process on port %PORT% ...
for /f "tokens=5" %%a in ('netstat -ano ^| find ":%PORT% " 2^>nul') do taskkill /f /pid %%a >nul 2>&1
timeout /t 1 /nobreak >nul

REM --- Start server ---
echo [*] Starting server...
cd /d "%~dp0"
start /B "" %PYCMD% main.py --web --port %PORT% --pidfile "%~dp0server.pid"

REM --- Wait for server ---
echo Waiting for server...
set WAIT=0
:wait_loop
timeout /t 1 /nobreak >nul 2>&1
netstat -ano 2>nul | find ":%PORT% " | findstr "LISTENING" >nul
if errorlevel 1 (
    set /a WAIT+=1
    if %WAIT% lss 20 goto wait_loop
    echo [WARN] Server not ready, opening http://localhost:%PORT% ...
    start http://localhost:%PORT%
    goto running
)

REM --- Open browser ---
start http://localhost:%PORT%

:running
echo.
echo ========================================
echo   Running at: http://localhost:%PORT%
echo   Close this window to stop.
echo ========================================
echo.
pause >nul

REM --- Stop server & cleanup ---
echo Stopping server...
if exist "%~dp0server.pid" (
    set /p SPID=<"%~dp0server.pid"
    taskkill /f /pid !SPID! >nul 2>&1
    del "%~dp0server.pid" 2>nul
)
echo Server stopped.