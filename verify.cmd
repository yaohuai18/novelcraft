@echo off
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0verify.ps1" %*
set "RC=%errorlevel%"
echo.
if "%RC%"=="0" (
  echo [VERIFY OK] all checks passed
) else (
  echo [VERIFY FAIL] exit code %RC%
)
exit /b %RC%
