@echo off
REM Copyright 2026 wUwproject
REM
REM Licensed under the Apache License, Version 2.0 (the "License");
REM you may not use this file except in compliance with the License.
REM You may obtain a copy of the License at
REM
REM     http://www.apache.org/licenses/LICENSE-2.0
REM
REM Unless required by applicable law or agreed to in writing, software
REM distributed under the License is distributed on an "AS IS" BASIS,
REM WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
REM See the License for the specific language governing permissions and
REM limitations under the License.

chcp 65001 >nul
setlocal enabledelayedexpansion
title LC-MS Group Advisor

set PORT=8810

echo ========================================
echo    LC-MS Group Advisor (port %PORT%)
echo ========================================
echo.

set "PYCMD="
py -3.11 --version >nul 2>&1
if not errorlevel 1 set "PYCMD=py -3.11"
if defined PYCMD goto have_py
python --version >nul 2>&1
if not errorlevel 1 set "PYCMD=python"
if defined PYCMD goto have_py
echo [FAIL] Python not found. Please install Python 3.11+
pause
exit /b 1

:have_py
echo [*] Interpreter: %PYCMD%
%PYCMD% --version
echo.

echo [*] Dependencies: pure standard library, no pip install needed.
echo.

echo [*] Cleaning old process on port %PORT% ...
for /f "tokens=5" %%a in ('netstat -ano ^| find ":%PORT% " 2^>nul') do taskkill /f /pid %%a >nul 2>&1
timeout /t 1 /nobreak >nul

echo [*] Starting server...
cd /d "%~dp0"
start /B "" %PYCMD% main.py --port %PORT% --pidfile "%~dp0server.pid"

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

start http://localhost:%PORT%

:running
echo.
echo ========================================
echo   Running at: http://localhost:%PORT%
echo   Close this window to stop.
echo ========================================
echo.
pause >nul

echo Stopping server...
if exist "%~dp0server.pid" (
    set /p SPID=<"%~dp0server.pid"
    taskkill /f /pid !SPID! >nul 2>&1
    del "%~dp0server.pid" 2>nul
)
echo Server stopped.
