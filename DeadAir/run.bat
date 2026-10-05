@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title DeadAir
echo.
echo  DeadAir - Remove dead air. Keep the story.
echo  ------------------------------------------
echo.

REM ---- 1. Python 3.10+ -------------------------------------------------------
set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
    where python >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY goto :nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto :nopython

REM ---- 2. Virtual environment + requirements ---------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    %PY% -m venv .venv
    if errorlevel 1 goto :venvfail
)
echo Checking requirements...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
    echo.
    echo  WARNING: pip could not install requirements. DeadAir has no required
    echo  third-party packages, so continuing anyway.
    echo.
)

REM ---- 3. FFmpeg / FFprobe ---------------------------------------------------
REM Looks on PATH first, then in DeadAir\ffmpeg\bin\ (you can drop a portable build there).
set "HAVE_FFMPEG=0"
where ffmpeg >nul 2>nul && where ffprobe >nul 2>nul && set "HAVE_FFMPEG=1"
if exist "%~dp0ffmpeg\bin\ffmpeg.exe" if exist "%~dp0ffmpeg\bin\ffprobe.exe" set "HAVE_FFMPEG=1"
if "%HAVE_FFMPEG%"=="1" goto :start

echo  FFmpeg was not found on this computer.
echo.
where winget >nul 2>nul
if errorlevel 1 goto :manualffmpeg
choice /C YN /M "Install FFmpeg now using winget"
if errorlevel 2 goto :manualffmpeg
winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
echo.
echo  If the install succeeded, CLOSE this window and double-click run.bat again
echo  so Windows picks up the new PATH.
echo.
pause
exit /b 0

:start
echo Starting DeadAir...
echo (Your browser will open. Close this window or press Ctrl+C to stop DeadAir.)
echo.
".venv\Scripts\python.exe" -m app.main
if errorlevel 1 (
    echo.
    echo  DeadAir stopped with an error. See the messages above.
    pause
)
exit /b 0

:nopython
echo  Python 3.10 or newer was not found.
echo.
echo  1. Download Python from https://www.python.org/downloads/windows/
echo  2. In the installer, tick "Add python.exe to PATH"
echo  3. Run run.bat again
echo.
echo  (If you only have the Microsoft Store "python" shortcut, install the real Python.)
echo.
pause
exit /b 1

:venvfail
echo.
echo  Could not create the virtual environment. Try deleting the .venv folder
echo  and running run.bat again.
echo.
pause
exit /b 1

:manualffmpeg
echo  Install FFmpeg manually:
echo    Option A (easiest):  open Command Prompt and run:  winget install Gyan.FFmpeg
echo    Option B: download a build from https://www.gyan.dev/ffmpeg/builds/
echo              (ffmpeg-release-essentials.zip), unzip it, and either
echo                - add its "bin" folder to your PATH, or
echo                - copy ffmpeg.exe and ffprobe.exe into  %~dp0ffmpeg\bin\
echo  Then run run.bat again.
echo.
pause
exit /b 1
