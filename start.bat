@echo off
setlocal
cd /d "%~dp0"
title TornTools v0.3.5 Backend

echo TornTools v0.3.5 Backend
echo Dashboard: http://127.0.0.1:8765
echo Mug Scout: http://127.0.0.1:8765/static/mug-scout.html
echo.
echo API key preflight from %CD%\.env:
".venv\Scripts\python.exe" -c "import app,mug_scout; print('  TORN_API_KEY:       ' + ('LOADED' if app._api_key else 'MISSING')); k,s=mug_scout._ffscouter_key(); print('  FFSCOUTER_API_KEY:  ' + ('LOADED ('+s+')' if k else 'MISSING'))"
echo.
start "" powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8765'"
".venv\Scripts\python.exe" -m uvicorn mug_scout_v034:app --host 127.0.0.1 --port 8765 --reload
pause
