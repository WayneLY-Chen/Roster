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
REM  The project root, captured once. %CD% changes if anything below cd's,
REM  and the launch at the end must not depend on that.
set "ROOT=%CD%"
title Roster - setup

REM  The mascots live in assets\pets.txt, not in here.
REM
REM  Two reasons. The drawing needs full-width characters, and this file
REM  is ASCII-only on purpose (see the header). And even if it were not,
REM  every | / < / > in the artwork would have to be escaped with a caret
REM  or cmd.exe would try to run it as a pipe or a redirect.
REM
REM  Codepage 65001 is UTF-8. It is switched back straight after, so the
REM  rest of the session keeps whatever the user had.
for /f "tokens=2 delims=:" %%P in ('chcp') do set "OLDCP=%%P"
set "OLDCP=%OLDCP: =%"
chcp 65001 >nul
echo.
type "%ROOT%\assets\pets.txt"
chcp %OLDCP% >nul

echo.
echo  Roster setup
echo  Install location: %CD%
echo.

REM ------------------------------------------------------------ Python
echo [1/4] Checking Python...

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
echo [2/4] Creating the virtual environment...
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
echo [3/4] Installing packages...
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

REM ----------------------------------------------------------- browser
REM  Some directories only build their listing after the page has loaded;
REM  those need a real browser. Chromium is not a pip package, so it cannot
REM  live in requirements.txt -- it has to be downloaded separately.
REM
REM  A failure here is not fatal: every ordinary site still works without it.
echo.
echo [4/4] Downloading the built-in browser (about 120 MB)...
echo       Needed only for sites that build their listing with JavaScript.
echo.
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 (
    echo.
    echo       [Note] The browser could not be downloaded. Everything else
    echo       works; sites that need it will say so when you try them.
    echo       To retry later: .venv\Scripts\python.exe -m playwright install chromium
)

echo.
echo  Setup complete.
echo.
echo  To start the application: double-click the launcher in this
echo  same folder. The command line one is next to it.
echo.

REM  Launch python directly, not the Chinese-named launcher script.
REM
REM  This file is ASCII-only on purpose (see the header). Referring to
REM  "\U555F\U52D5.bat" from in here broke that rule and the prompt failed with
REM  "path not found" -- and %~dp0 already ends in a backslash, so the path
REM  had a doubled separator on top of the mangled name. Reported.
REM
REM  pythonw.exe, not python.exe: the GUI should not drag a console window
REM  along behind it.
choice /c YN /m "Start Roster now"
if errorlevel 2 goto :done
if exist "%ROOT%\.venv\Scripts\pythonw.exe" (
    start "" "%ROOT%\.venv\Scripts\pythonw.exe" "%ROOT%\main.py" gui
) else (
    start "" "%ROOT%\.venv\Scripts\python.exe" "%ROOT%\main.py" gui
)

:done
popd
endlocal
