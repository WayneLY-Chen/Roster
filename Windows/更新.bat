@echo off
REM ---------------------------------------------------------------
REM  Roster updater -- double-click this to get the newest version.
REM
REM  ASCII-only on purpose: cmd.exe mis-parses the rest of a batch
REM  file when it contains non-ASCII text, so all Chinese lives in
REM  the application itself, not in here.
REM
REM  Nothing you own is touched. The database (data\), attachments,
REM  exports, backups, mail templates and user_settings.yaml are all
REM  outside git, so a pull cannot reach them.
REM
REM  config.yaml IS tracked, so edits to it would block the pull.
REM  They are stashed and put back around it rather than lost.
REM
REM  There is no database step here on purpose: the app runs its own
REM  migration at start-up (database/session.py init_db), so a new
REM  column appears the first time you open it.
REM ---------------------------------------------------------------
setlocal
pushd "%~dp0.."
REM  Captured once -- %CD% would move if anything below cd'd.
set "ROOT=%CD%"
title Roster - update

REM  The mascots. Same block as the setup file -- see the comment there.
chcp 65001 >nul
echo.
type "%ROOT%\assets\pets.txt"

echo.
echo  Roster update
echo  Folder: %ROOT%
echo.

REM ------------------------------------------------------- can we update?
REM  Downloading the ZIP gives you the files without the .git folder, and
REM  without it there is no record of where this came from. Say that
REM  plainly instead of letting the first git call fail with its own words.
if not exist "%ROOT%\.git" goto :not_a_clone

git --version >nul 2>&1
if errorlevel 1 goto :no_git

set "OLDVER="
for /f "tokens=2 delims==" %%V in ('findstr /b /c:"VERSION = " "%ROOT%\core\constants.py"') do if not defined OLDVER set "OLDVER=%%V"
set "OLDVER=%OLDVER: =%"
set OLDVER=%OLDVER:"=%
echo  Installed version: %OLDVER%
echo.

REM ------------------------------------------------------------ [1/3]
echo [1/3] Checking for a newer version...
git fetch --quiet
if errorlevel 1 goto :no_network

set "REMOTE="
for /f "delims=" %%H in ('git rev-parse "@{u}" 2^>nul') do set "REMOTE=%%H"
if not defined REMOTE goto :no_upstream

REM  Ancestor, not equality. Someone who has committed something of their
REM  own is ahead of GitHub rather than behind it, and has nothing to fetch;
REM  a plain "are the hashes the same" test would send them into the pull.
git merge-base --is-ancestor %REMOTE% HEAD
if not errorlevel 1 goto :already_current

REM  Local edits to a tracked file (config.yaml, almost always) would make
REM  the pull refuse. Put them aside and bring them back afterwards.
set "STASHED=0"
git diff --quiet HEAD
if errorlevel 1 set "STASHED=1"
if "%STASHED%"=="0" goto :do_pull

echo       You have edited a tracked file -- most likely config.yaml.
echo       Setting your edits aside and restoring them after the update.
git stash push --quiet -m "roster-update"
if errorlevel 1 goto :stash_failed

REM ------------------------------------------------------------ [2/3]
:do_pull
echo.
echo [2/3] Downloading the new version...
git pull --ff-only --quiet
if errorlevel 1 goto :pull_failed

if "%STASHED%"=="0" goto :packages
git stash pop --quiet
if errorlevel 1 goto :pop_failed

REM ------------------------------------------------------------ [3/3]
:packages
echo.
echo [3/3] Installing anything newly required...
if not exist "%ROOT%\.venv\Scripts\python.exe" goto :no_venv
REM  No --upgrade on purpose. requirements.txt uses lower bounds, so a plain
REM  install adds what is missing and leaves working versions alone; with
REM  --upgrade every package would jump to its newest release, which is a
REM  much bigger change than "update Roster".
"%ROOT%\.venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
if errorlevel 1 goto :pip_failed

:report
set "NEWVER="
for /f "tokens=2 delims==" %%V in ('findstr /b /c:"VERSION = " "%ROOT%\core\constants.py"') do if not defined NEWVER set "NEWVER=%%V"
set "NEWVER=%NEWVER: =%"
set NEWVER=%NEWVER:"=%

echo.
echo  Updated: %OLDVER%  --^>  %NEWVER%
echo.
echo  Your companies, attachments and settings are untouched.
echo  What changed in this version: see CHANGELOG.md
echo.
choice /c YN /m "Start Roster now"
if errorlevel 2 goto :done
if exist "%ROOT%\.venv\Scripts\pythonw.exe" (
    start "" "%ROOT%\.venv\Scripts\pythonw.exe" "%ROOT%\main.py" gui
) else (
    start "" "%ROOT%\.venv\Scripts\python.exe" "%ROOT%\main.py" gui
)
goto :done

REM ------------------------------------------------------------ endings
:already_current
echo.
echo  Already up to date -- you are on the newest version (%OLDVER%).
echo.
popd
pause
exit /b 0

:not_a_clone
echo  [ERROR] This folder did not come from git, so there is nothing
echo  to update from. (You downloaded the ZIP -- it has no .git folder.)
echo.
echo  Two ways forward:
echo.
echo    1) Download this folder once with git, and this file works
echo       from then on:
echo.
echo         git clone https://github.com/WayneLY-Chen/Roster.git
echo.
echo       Then copy your data\ folder into the new one.
echo.
echo    2) Or download the new ZIP and replace this folder -- but
echo       first move these out and put them back afterwards, they
echo       are yours and are not in the download:
echo.
echo         data\               the database
echo         attachments\        files attached to companies
echo         output\             exported lists
echo         backups\            backups
echo         user_settings.yaml  your settings
echo         templates\mail\     your mail templates
echo.
popd
pause
exit /b 1

:no_git
echo  [ERROR] git is not installed, so this file cannot fetch anything.
echo.
echo  Install it from https://git-scm.com/download/win and run this
echo  file again. The default options are fine.
echo.
popd
pause
exit /b 1

:no_network
echo.
echo  [ERROR] Could not reach GitHub. Check your internet connection
echo  and try again. Nothing was changed.
echo.
popd
pause
exit /b 1

:no_upstream
echo.
echo  [ERROR] This copy is not tracking a branch on GitHub, so there
echo  is nowhere to update from. Nothing was changed.
echo.
echo  If you know git: set an upstream with
echo    git branch --set-upstream-to=origin/main
echo.
popd
pause
exit /b 1

:stash_failed
echo.
echo  [ERROR] Could not set your local edits aside. Nothing was
echo  changed. The messages above explain why.
echo.
popd
pause
exit /b 1

:pull_failed
echo.
echo  [ERROR] The update could not be applied. The messages above
echo  explain why -- the usual cause is that this copy has its own
echo  commits, which cannot be fast-forwarded.
echo.
if "%STASHED%"=="1" echo  Your local edits are still saved. Get them back with: git stash pop
if "%STASHED%"=="1" echo.
popd
pause
exit /b 1

:pop_failed
echo.
echo  The new version is installed, but your own edits could not be
echo  put back automatically -- the same lines changed on both sides.
echo.
echo  They are not lost. To see them:      git stash show -p
echo  To apply them and fix by hand:       git stash pop
echo.
popd
pause
exit /b 1

:no_venv
echo.
echo  The new version is downloaded, but there is no .venv here yet.
echo  Double-click the setup file in this same folder to finish.
echo.
popd
pause
exit /b 1

:pip_failed
echo.
echo  [ERROR] The new version is downloaded, but installing its
echo  packages failed. The messages above explain why -- a network
echo  problem is the most common cause. Run this file again.
echo.
popd
pause
exit /b 1

:done
popd
endlocal
