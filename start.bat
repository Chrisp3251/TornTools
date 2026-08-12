@echo off
setlocal
cd /d "%~dp0"
title Torn Market Scanner Backend

echo Torn Market Scanner V0.1
echo Dashboard: http://127.0.0.1:8765
echo.
start "" powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8765'"
".venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8765
pause
