@echo off
REM ASCII-only on purpose; see the note in the launcher batch file.
setlocal
pushd "%~dp0"
title Roster - command line

if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] Virtual environment not found. Run: python -m venv .venv
    popd
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"

echo.
echo  Roster - virtual environment is active
echo  ---------------------------------------------------
echo   python main.py --help           list every command
echo   python main.py crawl -s sample  crawl the bundled sample
echo   python main.py crawl --url URL  crawl any page by URL
echo   python main.py export -f excel  export to Excel
echo   python main.py stats            database summary
echo   python main.py gui              open the window
echo.

cmd /k
