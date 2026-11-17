@echo off
setlocal EnableExtensions EnableDelayedExpansion
rem NovelCraft frontend autostart (vite :5173)

rem ---- log file: autostart_logs\frontend-YYYYMMDD-HHmmss.log (one per start) ----
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

set "LOG_BASE=%LOG_DIR%\frontend-%STAMP%"
set "LOG_FILE=%LOG_BASE%.log"
set "DUP=1"
:pick_log
if exist "%LOG_FILE%" (
  set /a DUP+=1
  set "LOG_FILE=%LOG_BASE%-!DUP!.log"
  goto pick_log
)

rem ---- retention: keep newest 20 frontend-*.log, move older ones OUTSIDE the repo ----
rem Non-fatal: retention problems never block the frontend from starting.
if exist "%~dp0scripts\archive-autostart-logs.ps1" powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\archive-autostart-logs.ps1" -Prefix frontend -Keep 20 -LogDir "%LOG_DIR%" -ActiveLog "%LOG_FILE%" 2>nul

netstat -an | findstr ":5173" | findstr "LISTENING" >nul
if %errorlevel%==0 (
  echo [skip] port 5173 already listening >> "%LOG_FILE%"
  exit /b 0
)
cd /d "%~dp0frontend"
npm run dev >> "%LOG_FILE%" 2>&1
