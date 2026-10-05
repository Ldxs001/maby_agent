@echo off
setlocal enabledelayedexpansion
chcp 437 >nul 2>&1
cd /d "%~dp0"

echo ==============================================================
echo   Podcast Maker - Local TTS service (Qwen3-TTS) - one-click setup
echo ==============================================================
echo.

rem ---------------- bootstrap interpreter ----------------
rem This script needs *a* Python 3.11+ only to bootstrap: that interpreter
rem creates the service's own virtual env. The service never runs on it
rem afterwards - setup_env.py builds tts_service\.venv and everything the
rem service needs goes in there.
rem 3.11 first: the PyTorch CUDA wheels cover cp310-cp314, but the packages
rem around them often have no wheel yet on the newest Python.
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
  echo         It is needed once, to create the service's own environment.
  echo         Install it from https://www.python.org/downloads/
  pause
  exit /b 1
)
echo [bootstrap] interpreter:
%PY% -V
echo.

rem ---------------- everything else ----------------
rem The env, the packages, the model: all of it is setup_env.py's job. The
rem config page's "build local voice environment" button runs that same
rem file, so there is exactly one implementation and the two can't drift.
%PY% "%~dp0setup_env.py"
set RC=%ERRORLEVEL%

echo.
if not "%RC%"=="0" (
  echo [WARN] Setup did not finish cleanly ^(exit %RC%^).
  echo        Rerun this script - finished steps are skipped, downloads resume.
) else (
  echo   Run the service by hand:
  echo       .venv\Scripts\python.exe serve.py
  echo.
  echo   Verify it works ^(this never touches LM Studio^):
  echo       .venv\Scripts\python.exe check.py
  echo.
  echo   Normally you do not start it yourself: the main program brings it up
  echo   before synthesis and stops it when the batch is done, so the VRAM
  echo   goes back.
)
echo.
pause
