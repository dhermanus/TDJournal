@echo off
setlocal
title TDJournal Setup
cd /d "%~dp0"

echo TDJournal setup
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3.11 or newer from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" in the installer, then run setup.bat again.
    goto :fail
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo Python 3.11 or newer is needed. This machine has:
    python --version
    goto :fail
)

where npm >nul 2>nul
if errorlevel 1 (
    echo Node.js was not found. Install the LTS version from https://nodejs.org/ and run setup.bat again.
    goto :fail
)

echo [1/4] Creating the Python environment in .venv
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto :fail
)

echo [2/4] Installing the backend packages
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r backend\requirements.txt
if errorlevel 1 goto :fail

echo [3/4] Installing the frontend packages (a few minutes the first time)
pushd frontend
call npm ci --no-audit --no-fund --loglevel=error
if errorlevel 1 (
    rem An older npm can reject a lock file written by a newer one; npm install reconciles it.
    echo       Retrying with npm install
    call npm install --no-audit --no-fund --loglevel=error
    if errorlevel 1 (
        popd
        goto :fail
    )
)
popd

echo [4/4] Creating backend\.env for your optional API keys
if exist "backend\.env" (
    echo       backend\.env already exists, left as it is
) else (
    copy ".env.example" "backend\.env" >nul
    echo       Created. Add keys later if you want the AI features or trade charts; the journal works without them.
)

echo.
echo Setup finished. Start the journal with launch.bat, then open http://localhost:3010
echo Your journal starts empty. Add an account, then import your broker's statement.
echo.
pause
exit /b 0

:fail
echo.
echo Setup stopped. Fix the problem above and run setup.bat again; finished steps are skipped or repeated safely.
pause
exit /b 1
