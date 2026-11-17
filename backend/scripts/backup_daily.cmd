@echo off
rem NovelCraft daily backup + rotation (scheduled task, 03:00)
rem 2026-11-17 fix: output dir moved into the workspace. The Marvis runtime python
rem note: some sandboxes block writes to user-profile paths; use ASCII paths
rem to open database file). Tasks on 11-16 and 11-17 failed silently (LastTaskResult=1).
rem E:\ paths used on purpose: pure ASCII, immune to cmd.exe GBK codepage.
rem Logging added so failures are no longer silent.
setlocal
set "BCK=E:\UserData\Documents\deepseek-harness\default-workspace\novelcraft-fastapi\backend"
set "PY=%BCK%\.venv\Scripts\python.exe"
set "OUT=E:\UserData\Documents\deepseek-harness\default-workspace\NovelCraft-backups\daily"
set "LOG=E:\UserData\Documents\deepseek-harness\default-workspace\NovelCraft-backups\logs"
if not exist "%OUT%" mkdir "%OUT%"
if not exist "%LOG%" mkdir "%LOG%"
cd /d "%BCK%"
"%PY%" -m scripts.consistent_backup --source "%BCK%\novelcraft.db" --out "%OUT%" >> "%LOG%\backup-%date:~0,4%%date:~5,2%%date:~8,2%.log" 2>&1
"%PY%" -m scripts.rotate_backups --apply --backup-dir "%OUT%" >> "%LOG%\rotate-%date:~0,4%%date:~5,2%%date:~8,2%.log" 2>&1
