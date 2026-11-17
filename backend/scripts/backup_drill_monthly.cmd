@echo off
rem NovelCraft monthly restore drill (scheduled task, 1st of month 04:00):
rem verify the newest backup and compare table row counts.
rem 2026-11-17: paths moved into workspace (Marvis runtime python sandbox cannot
rem E:\ ASCII paths avoid cmd GBK issues.
setlocal
set "BCK=E:\UserData\Documents\deepseek-harness\default-workspace\novelcraft-fastapi\backend"
set "PY=%BCK%\.venv\Scripts\python.exe"
set "OUT=E:\UserData\Documents\deepseek-harness\default-workspace\NovelCraft-backups\daily"
set "LOG=E:\UserData\Documents\deepseek-harness\default-workspace\NovelCraft-backups\logs"
if not exist "%LOG%" mkdir "%LOG%"
cd /d "%BCK%"
for /f "delims=" %%F in ('dir /b /o-d "%OUT%\*.db" 2^>nul') do (
    "%PY%" -m scripts.consistent_backup --verify "%OUT%\%%F" --drill >> "%LOG%\drill-%date:~0,4%%date:~5,2%%date:~8,2%.log" 2>&1
    goto :done
)
echo [drill] no backup found >> "%LOG%\drill-missing.log"
:done
