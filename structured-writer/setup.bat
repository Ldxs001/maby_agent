@echo off
title Structured Writer
cd /d "%~dp0"

echo ========================================
echo   Structured Writer
echo ========================================
echo.

REM --- Python detection (system default; llama.cpp 已废弃，无需 3.10~3.11 限定) ---
REM 判定模型 8B/7B 走 LM Studio（lms），3B/1.5B 判定与抽取仅需 transformers/torch（任意版本）。
set "PYCMD=python"

%PYCMD% --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.11+
    echo         https://www.python.org/downloads/
    pause
    exit /b 1
)

:py_ok
%PYCMD% --version
echo.

:env_check
REM --- Check / auto-install R1/3B inference packages (transformers/torch) ---
REM Missing packages auto-installed (aliyun mirror) so models downloaded in UI
REM are guaranteed runnable. Failure is WARN, not fatal.
echo [*] Checking model inference packages (transformers/torch)...
%PYCMD% -m structured_writer.novel.model_env_check
if errorlevel 1 echo [WARN] Model packages incomplete - R1/3B local inference may fail, server still starts
echo.

REM --- Kill ALL old structured-writer processes (any port) to avoid stale server ---
echo [*] Cleaning old process (all main.py instances)...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*main.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 1 /nobreak >nul

REM --- Start server ---
echo [*] Starting server...
start /B "" %PYCMD% main.py --port 8770

REM --- Wait for server ---
echo Waiting for server...
:wait_loop
timeout /t 1 /nobreak >nul
netstat -ano | findstr ":8770 " | findstr "LISTENING" >nul
if errorlevel 1 goto wait_loop

REM --- Open browser ---
start http://localhost:8770

echo.
echo ========================================
echo   Running at: http://localhost:8770
echo   Close this window to stop.
echo ========================================
echo.
