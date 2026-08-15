@echo off
title Structured Writer
cd /d "%~dp0"

echo ========================================
echo   Structured Writer
echo ========================================
echo.

:: Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Please install Python 3.11+
    echo         https://www.python.org/downloads/
    pause
    exit /b 1
)
python --version

echo.

:: Kill ALL old structured-writer processes (any port) to avoid stale server
echo [*] Cleaning old process (all main.py instances)...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*main.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 1 /nobreak >nul

:: Start server
echo [*] Starting server...
start /B "" python main.py --port 8770

:: Wait for server
echo Waiting for server...
:wait_loop
timeout /t 1 /nobreak >nul
netstat -ano | findstr ":8770 " | findstr "LISTENING" >nul
if errorlevel 1 goto wait_loop

:: Open browser
start http://localhost:8770

echo.
echo ========================================
echo   Running at: http://localhost:8770
echo   Close this window to stop.
echo ========================================
echo.
