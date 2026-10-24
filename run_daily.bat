@echo off
rem run_daily.bat - compatibility wrapper for scheduled tasks (ASCII only).
rem Same as run.bat, but never pauses and never opens the browser.
set TWSTOCK_NOPAUSE=1
set TWSTOCK_NO_OPEN=1
call "%~dp0run.bat" %*
exit /b %ERRORLEVEL%
