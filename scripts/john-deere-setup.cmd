@echo off
REM Double-click this. It asks for your John Deere client id and secret, then
REM signs you in and shows what is in your Operations Center account.
REM
REM The window stays open at the end so you can read what happened, and so an
REM error is not lost when the window closes.

setlocal
cd /d "%~dp0.."

echo ============================================================
echo  John Deere setup
echo ============================================================
echo.
echo  You will be asked for two values. Get them from your app at
echo  developer.deere.com:
echo.
echo     Client ID       - paste it, press Enter
echo     Client secret   - paste it, press Enter. NOTHING WILL APPEAR
echo                       on screen as you type. That is deliberate.
echo     Redirect URI    - just press Enter to accept the default
echo.
echo  Do not paste a command at any of these prompts.
echo.
echo ============================================================
echo.

".venv\Scripts\python.exe" dev\jd_setup.py
if errorlevel 1 goto done

echo.
echo ============================================================
echo  Signing in - your browser will open
echo ============================================================
echo.

".venv\Scripts\python.exe" dev\jd_explore.py

:done
echo.
echo ============================================================
pause
