@echo off
REM Run the bid scraper by hand, right now, with the output on screen.
REM Double-click this file.
REM
REM Safe to use while the scheduled task exists: a machine-wide mutex lets only
REM one run through, so if the 8:15/8:45 task is already going this exits
REM immediately instead of both scraping and then fighting over the commit.

setlocal
cd /d "%~dp0.."

echo.
echo   Grain map - manual bid refresh
echo   ------------------------------
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\update-bids.ps1" -Verbose
set RC=%ERRORLEVEL%

echo.
echo   ------------------------------
if "%RC%"=="0" (
  echo   Finished. Source health:
  echo.
  ".venv\Scripts\python.exe" scrapers\source_status.py
) else (
  echo   FAILED with exit code %RC% - see .build\update-bids.log
)

echo.
echo   Status page: https://superspyn.github.io/grainbidmap-data/status.html
echo.
pause
