@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo   Podcast Maker - launcher
echo   script -^> voice -^> subtitle -^> video -^> artifacts
echo ============================================================
echo.

rem ---------------- 1. interpreter ----------------
rem The main program runs on the Python installed on this machine. Probing by
rem version is the right thing here: this half of the program is plain stdlib
rem plus two small wheels, so any 3.11+ works.
rem Only the local voice service (tts_service\) carries its own interpreter -
rem its PyTorch stack pins one version and weighs 4.8 GB by itself, which is
rem no business of the main program.
set "PY="
for %%V in (3.11 3.12 3.13 3.14) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
  )
)
if not defined PY (
  py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  python -c "import sys" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [ERROR] No Python 3.11 or newer found on this machine.
  echo         Install it from https://www.python.org/downloads/
  echo         and leave the "py" launcher or "python" on PATH.
  pause
  exit /b 1
)

echo [1/4] Interpreter:
%PY% -V
echo.

rem ---------------- 2. dependencies ----------------
echo [2/4] Checking dependencies...
%PY% -c "import edge_tts, PIL" >nul 2>nul
if errorlevel 1 (
  echo       Missing packages, installing from requirements.txt ...
  echo       Source: pypi.tuna.tsinghua.edu.cn ^(see tools\sources.py^)
  %PY% -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple/ --progress-bar off
  if errorlevel 1 (
    echo [ERROR] pip install failed. Check your network and retry.
    pause
    exit /b 1
  )
) else (
  echo       OK
)
echo.

echo [2b] Local voice engine ^(optional^): pick Qwen3-TTS on the config page and
echo      press "build local voice environment" ^-^ it creates the service's own
echo      env, installs its packages and downloads the model. Or run
echo      tts_service\setup.bat for the same thing from a command line.
echo      Skip this if you only use edge-tts.
echo.

rem ---------------- 3. stop previous instance ----------------
echo [3/4] Stopping previous instance...
if exist "server.pid" (
  set /p OLDPID=<server.pid
  taskkill /PID !OLDPID! /F >nul 2>nul
  del "server.pid" >nul 2>nul
  echo       Stopped pid !OLDPID!
) else (
  echo       No pidfile.
)
rem Kill any stray server on another port, otherwise an old process keeps
rem holding the previous code and races with this one.
for /f %%P in ('powershell -NoProfile -Command "Get-CimInstance Win32_Process ^| Where-Object { $_.CommandLine -match 'main.py' -and $_.Name -like 'py*' } ^| ForEach-Object { $_.ProcessId }"') do (
  taskkill /PID %%P /F >nul 2>nul
  echo       Stopped stray pid %%P
)
echo.

rem ---------------- 4. start ----------------
echo [4/4] Starting server on port 8811 ...
echo       UI: http://127.0.0.1:8811
start "" http://127.0.0.1:8811/
echo.
%PY% main.py --port 8811 --pidfile server.pid

echo.
echo Server stopped.
pause
