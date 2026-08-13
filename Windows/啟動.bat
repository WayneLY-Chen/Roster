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

REM  The mascots live in assets\pets.txt, not in here -- this file is
REM  ASCII-only on purpose (see the header), and every | / < / > in the
REM  artwork would otherwise have to be escaped or cmd.exe would treat it
REM  as a pipe or a redirect.
REM
REM  Codepage 65001 is UTF-8; the old one is put back straight after so the
REM  rest of the session keeps whatever the user had. Same block as the one
REM  in the setup file.
for /f "tokens=2 delims=:" %%P in ('chcp') do set "OLDCP=%%P"
set "OLDCP=%OLDCP: =%"
chcp 65001 >nul
echo.
type "%CD%\assets\pets.txt"
if defined OLDCP chcp %OLDCP% >nul

echo.
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
