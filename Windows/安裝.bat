@echo off
REM ---------------------------------------------------------------
REM  Roster setup -- Windows users just double-click this one.
REM
REM  ASCII-only on purpose: cmd.exe mis-parses the rest of a batch
REM  file when it contains non-ASCII text, so all Chinese lives in
REM  the application itself, not in here.
REM
REM  Safe to run again: an existing .venv is reused, only missing
REM  pieces are installed.
REM ---------------------------------------------------------------
setlocal
pushd "%~dp0.."
title Roster - setup

echo.
echo  Roster setup
echo  Install location: %CD%
echo.

REM ------------------------------------------------------------ Python
echo [1/3] Checking Python...

set "PY="
for %%C in (py python) do (
    if not defined PY (
        %%C -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)" >nul 2>&1
        if not errorlevel 1 set "PY=%%C"
    )
)

if not defined PY (
    echo.
    echo  [ERROR] Python 3.12 or newer was not found.
    echo.
    echo  Install it from https://www.python.org/downloads/
    echo  IMPORTANT: tick "Add python.exe to PATH" in the installer.
    echo.
    echo  Then run this file again.
    echo.
    popd
    pause
    exit /b 1
)
for /f "delims=" %%V in ('%PY% --version') do echo       Using %%V

REM -------------------------------------------------------------- venv
echo.
echo [2/3] Creating the virtual environment...
if exist ".venv\Scripts\python.exe" (
    echo       .venv already exists, skipping
) else (
    %PY% -m venv .venv
    if errorlevel 1 (
        echo.
        echo  [ERROR] Could not create the virtual environment.
        popd
        pause
        exit /b 1
    )
    echo       Created .venv
)

REM ---------------------------------------------------------- packages
echo.
echo [3/3] Installing packages...
echo       The first run downloads about 150 MB and takes a few minutes.
echo.
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo  [ERROR] Package installation failed. The messages above explain
    echo  why -- a network problem is the most common cause.
    popd
    pause
    exit /b 1
)

echo.
echo  Setup complete.
echo.
echo  To start the application: double-click the launcher in this
echo  same folder. The command line one is next to it.
echo.

choice /c YN /m "Start Roster now"
if errorlevel 2 goto :done
start "" "%~dp0\啟動.bat"

:done
popd
endlocal
