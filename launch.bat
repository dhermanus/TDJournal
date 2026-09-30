@echo off
title TDJournal Launcher

echo Starting TDJournal...
echo.

REM Use the repo's virtualenv if it exists
if exist "%~dp0.venv\Scripts" set "PATH=%~dp0.venv\Scripts;%PATH%"

REM Start backend
start "TDJournal - Backend" cmd /k "cd /d %~dp0backend && python -m uvicorn main:app --reload --port 8010"

REM Wait a moment then start frontend
timeout /t 2 /nobreak >nul
start "TDJournal - Frontend" cmd /k "cd /d %~dp0frontend && set PORT=3010&& npm start"

echo Backend starting on http://localhost:8010
echo Frontend starting on http://localhost:3010
echo.
echo Both servers are launching in separate windows.
echo Close those windows to stop the servers.
pause
