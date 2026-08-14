@echo off
setlocal
cd /d "%~dp0"
title TornTools v0.3.3 Backend

echo TornTools v0.3.3 Backend
echo Dashboard: http://127.0.0.1:8765
echo Mug Scout: http://127.0.0.1:8765/static/mug-scout.html
echo.
start "" powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8765'"
".venv\Scripts\python.exe" -m uvicorn mug_scout:app --host 127.0.0.1 --port 8765 --reload
pause
