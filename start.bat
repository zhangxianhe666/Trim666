@echo off
rem ============================================================
rem  Start ds-trim-gateway on Windows (Flask gateway + optional Gradio UI)
rem  Usage:
rem    start.bat          -> gateway only (8001)
rem    start.bat --ui     -> gateway (8001) + Gradio config UI (7860)
rem  Equivalent to start.sh. Messages are ASCII-only on purpose:
rem  cmd.exe reads .bat files in the OEM code page, so non-ASCII text
rem  would be garbled on some locales.
rem ============================================================
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo [ERROR] venv not found: %PY%
  echo.
  echo   Run these first:
  echo     python -m venv .venv
  echo     .venv\Scripts\pip.exe install -r requirements.txt
  echo.
  exit /b 1
)

rem The gateway already picks a platform-appropriate log dir on its own
rem (%TEMP% on Windows, /tmp elsewhere), but pin it explicitly here so the
rem paths are predictable and easy to point the recipient at when debugging.
rem Pre-set values (if the user already defined them) win.
if not defined DS_GW_LOG_FILE set "DS_GW_LOG_FILE=%TEMP%\ds-gw-server.log"
if not defined DS_GW_PARSEFAIL_FILE set "DS_GW_PARSEFAIL_FILE=%TEMP%\ds-gw-parsefail.log"

if /i "%~1"=="--ui" goto withui

echo Starting gateway on http://127.0.0.1:8001  ^(add --ui to also open the config UI^)
echo Log file: %DS_GW_LOG_FILE%
echo.
"%PY%" server.py --port 8001
exit /b %ERRORLEVEL%

:withui
echo Starting Gradio UI on http://127.0.0.1:7860 and gateway on http://127.0.0.1:8001 ...
echo Log file: %DS_GW_LOG_FILE%
echo.
start "ds-trim-gateway-UI" "%PY%" ui.py --port 7860
"%PY%" server.py --port 8001
rem Gateway exited (usually Ctrl+C) -> shut the UI window down too.
rem If this fails, just close the "ds-trim-gateway-UI" window by hand.
taskkill /FI "WINDOWTITLE eq ds-trim-gateway-UI*" /T /F >nul 2>&1
exit /b 0
