@echo off
rem NovelCraft 一键启动（2026-11-16）：拉起后端 + 前端 + 打开浏览器
chcp 65001 >nul
set "ROOT=%~dp0"

rem 1) 后端 8000：没在跑才启动
netstat -an | findstr ":8000" | findstr "LISTENING" >nul
if errorlevel 1 (
  echo [1/3] 正在启动后端...
  start "NovelCraft-Backend" "%ROOT%autostart_backend.bat"
) else (
  echo [1/3] 后端已在运行
)

rem 2) 前端 5173：没在跑才启动
netstat -an | findstr ":5173" | findstr "LISTENING" >nul
if errorlevel 1 (
  echo [2/3] 正在启动前端...
  start "NovelCraft-Frontend" "%ROOT%autostart_frontend.bat"
) else (
  echo [2/3] 前端已在运行
)

rem 3) 等服务就绪后打开浏览器
echo [3/3] 等待服务就绪...
timeout /t 10 /nobreak >nul
start "" http://localhost:5173
echo 完成。如果页面还没就绪，等几秒刷新浏览器即可。
timeout /t 3 >nul
