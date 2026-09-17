@echo off
cd /d "%~dp0"

rem Run in the foreground on the Python installed on this machine, using the
rem same probe order as setup.bat: 3.11 through 3.14, then the py launcher's
rem default, then a bare "python" on PATH.
rem Only tts_service\ carries its own interpreter; the main program does not.
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
  pause
  exit /b 1
)

%PY% main.py --port 8811
