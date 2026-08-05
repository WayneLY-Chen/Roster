@echo off
REM ---------------------------------------------------------------
REM  Roster launcher.
REM  Kept ASCII-only on purpose: cmd.exe mis-parses the rest of a
REM  batch file when it contains non-ASCII text, so all Chinese lives
REM  in the application itself, not in here.
REM ---------------------------------------------------------------
setlocal
pushd "%~dp0.."
title Roster

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo [ERROR] Not installed yet.
    echo.
    echo Double-click the setup file in this same folder first.
    echo.
    popd
    pause
    exit /b 1
)

echo Starting Roster...
echo (Close the application window to quit.)
echo.

".venv\Scripts\python.exe" main.py gui
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo [ERROR] The application exited with code %RC%.
    echo See the messages above, or open logs\error.log for details.
    echo.
    popd
    pause
    exit /b %RC%
)

popd
endlocal
