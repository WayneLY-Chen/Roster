@echo off
REM ---------------------------------------------------------------
REM  Roster in the browser. Same database as the desktop launcher.
REM  ASCII-only on purpose -- see the comment in the setup file.
REM
REM  This console window IS the web server: the page works while it
REM  is open and stops when you close it. Only this computer can
REM  reach the page; see web/app.py.
REM ---------------------------------------------------------------
setlocal
pushd "%~dp0.."
title Roster - web

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

chcp 65001 >nul
echo.
type "%CD%\assets\pets.txt"

".venv\Scripts\python.exe" main.py web
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo [ERROR] The web interface exited with code %RC%.
    echo See the messages above, or open logs\error.log for details.
    echo.
    popd
    pause
    exit /b %RC%
)

popd
endlocal
