@echo off
chcp 65001 >nul
title Alpha Finance Radar Server
cd /d "%~dp0"

REM Skip if already running
netstat -ano | findstr ":8208 .*LISTENING" >nul 2>&1
if %errorlevel%==0 goto :attach

echo ============================================
echo  Alpha Finance Radar - starting server...
echo  Logs below. Ctrl+C to stop. Browser opens in 6s.
echo ============================================
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1

REM Open browser after 6s (ping = cmd native delay)
start "" /b cmd /c "ping -n 7 127.0.0.1 >nul && start "" "https://app.alpharadar.link""

REM Foreground server: logs stay in this window
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8208
echo.
echo [WARN] Server exited. Press any key to close...
pause >nul
goto :eof

REM Server already running (autostart task): open page + live tail the log file
:attach
echo [Alpha Finance Radar] Server already running: http://127.0.0.1:8208
start "" "https://app.alpharadar.link"
echo.
echo ============================================
echo  LIVE LOG TAIL (logs\server.log)
echo  Close this window to stop viewing -
echo  the server keeps running in background.
echo ============================================
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -Path 'logs\server.log' -Tail 30 -Wait"
goto :eof
