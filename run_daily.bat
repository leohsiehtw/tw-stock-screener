@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=python"
if defined TWSTOCK_PY set "PY=%TWSTOCK_PY%"
set PYTHONUTF8=1
if not exist logs mkdir logs
if not exist 結果 mkdir 結果
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set TODAY=%%i
for /f %%i in ('powershell -NoProfile -Command "[int](Get-Date).DayOfWeek"') do set DOW=%%i
set "LOG=logs\run_%TODAY%.log"
call :log 開始 日期 %TODAY% 星期代碼 %DOW%
if /i "%~1"=="force" goto run
if "%DOW%"=="0" goto weekend
if "%DOW%"=="6" goto weekend
goto run

:weekend
call :log 週末略過
exit /b 0

:run
call :log 更新資料 fetch_data.py daily
"%PY%" fetch_data.py daily >> "%LOG%" 2>&1
if errorlevel 1 goto fail
set "LATEST="
set /p LATEST=<cache\latest.txt
if "%LATEST%"=="" goto fail
call :log 最新交易日 %LATEST%
if exist "結果\盤後選股_%LATEST%.md" goto nonew
call :log 執行選股 screener.py scan
"%PY%" screener.py scan --data data --out "結果\盤後選股_%LATEST%.md" --txt "結果\盤後選股_%LATEST%.txt" --json "結果\盤後選股_%LATEST%.json" >> "%LOG%" 2>&1
if errorlevel 1 goto fail
call :log 完成 結果\盤後選股_%LATEST%
exit /b 0

:nonew
call :log 今日無新K，視為休市
exit /b 0

:fail
call :log 失敗 請看上面的錯誤訊息
exit /b 1

:log
echo [%TIME%] %*
>> "%LOG%" echo [%TIME%] %*
exit /b 0
