@echo off
chcp 65001 >nul
title fina_mainbz sync
cd /d "%~dp0"

REM Skip if another sync window is already running (tasklist matches window title)
tasklist /FI "WINDOWTITLE eq fina_mainbz sync*" 2>nul | find /I "cmd.exe" >nul
if %errorlevel%==0 (
    echo [SKIP] Another fina_mainbz sync window is already running.
    pause
    goto :eof
)

echo ============================================
echo  Tushare fina_mainbz (main business mix) -^> PostgreSQL
echo  Resumable backfill. Ctrl+C stops anytime,
echo  rerun continues from checkpoint.
echo  Log file: logs\sync_mainbz.log
echo ============================================
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1

.venv\Scripts\python.exe -u -m tools.market.sync_mainbz %*
echo.
echo [DONE] fina_mainbz sync finished. Press any key to close...
pause >nul
