@echo off
setlocal EnableExtensions EnableDelayedExpansion
rem NovelCraft backend autostart (uvicorn :8000)

rem ---- log file: autostart_logs\backend-YYYYMMDD-HHmmss.log (one per start) ----
set "LOG_DIR=%~dp0autostart_logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
if not exist "%LOG_DIR%" (
  echo [error] cannot create log dir: "%LOG_DIR%"
  exit /b 1
)

rem timestamp comes from PowerShell; it is used only for the file name
set "STAMP="
for /f "usebackq delims=" %%I in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Date -Format yyyyMMdd-HHmmss"`) do set "STAMP=%%I"
if not defined STAMP set "STAMP=19700101-000000"

set "LOG_BASE=%LOG_DIR%\backend-%STAMP%"
set "LOG_FILE=%LOG_BASE%.log"
set "DUP=1"
:pick_log
if exist "%LOG_FILE%" (
  set /a DUP+=1
  set "LOG_FILE=%LOG_BASE%-!DUP!.log"
  goto pick_log
)

rem ---- retention: keep newest 20 backend-*.log, move older ones OUTSIDE the repo ----
rem Non-fatal: retention problems never block the backend from starting.
if exist "%~dp0scripts\archive-autostart-logs.ps1" powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\archive-autostart-logs.ps1" -Prefix backend -Keep 20 -LogDir "%LOG_DIR%" -ActiveLog "%LOG_FILE%" 2>nul

netstat -an | findstr ":8000" | findstr "LISTENING" >nul
if %errorlevel%==0 (
  echo [skip] port 8000 already listening >> "%LOG_FILE%"
  exit /b 0
)
cd /d "%~dp0backend"
set "VENV_PY=%~dp0backend\.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
  echo [error] backend venv python not found: "%VENV_PY%" >> "%LOG_FILE%"
  echo [error] backend venv python not found: "%VENV_PY%"
  echo [error] create it first: cd backend ^&^& python -m venv .venv ^&^& .venv\Scripts\python.exe -m pip install -r requirements.lock.txt
  exit /b 1
)
set "PYTHONUNBUFFERED=1"
"%VENV_PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 >> "%LOG_FILE%" 2>&1
exit /b %errorlevel%
