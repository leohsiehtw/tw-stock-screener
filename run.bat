@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem run.bat - every executable line is ASCII only; Chinese text lives in the data block at the end
chcp 65001 >nul
set PYTHONUTF8=1
set "RC=0"
set "SELF=%~f0"
pushd "%~dp0" || goto :nocd
call :msgv TITLE TTL
title %TTL%
call :msgv F_CFG F_CFG
call :msgv F_CFG2 F_CFG2
call :msgv F_OUT F_OUT
call :msgv F_WL F_WL
call :msgv F_WLEX F_WLEX
if not exist screener.py goto :notextracted
if not exist fetch_data.py goto :notextracted

if not exist logs mkdir logs
set "TODAY=unknown"
set "DOW=x"
set "TNOW="
for /f %%i in ('powershell -NoProfile -Command "(Get-Date -Format yyyyMMdd)+[int](Get-Date).DayOfWeek"') do set "TNOW=%%i"
if defined TNOW set "TODAY=%TNOW:~0,8%"
if defined TNOW set "DOW=%TNOW:~8,1%"
if defined TWSTOCK_TODAY set "TODAY=%TWSTOCK_TODAY%"
if defined TWSTOCK_DOW set "DOW=%TWSTOCK_DOW%"
set "LOG=%CD%\logs\run_%TODAY%.log"
set "TWSTOCK_LOG=%LOG%"
set "ARGS=%*"
call :msgl LOGSTART

echo(
call :msg BANNER
call :findpy
if not defined PYCMD goto :nopython

call :msg STEP1
%PYCMD% fetch_data.py daily
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :fail

set "LATEST="
if exist cache\latest.txt set /p LATEST=<cache\latest.txt
if not defined LATEST goto :nolatest
set "SHOW=%LATEST:~0,4%-%LATEST:~4,2%-%LATEST:~6,2%"
if "%LATEST%"=="%TODAY%" goto :msg_today
if "%DOW%"=="0" goto :msg_weekend
if "%DOW%"=="6" goto :msg_weekend
call :msg MSG_NOTYET
goto :scan
:msg_today
call :msg MSG_TODAY
goto :scan
:msg_weekend
call :msg MSG_WEEKEND

:scan
if exist "%F_CFG%" goto :scan_cfg
if exist "%F_CFG2%" call :msg TIP_CFG2
call :msg NOCFG
call :msg STEP2
%PYCMD% screener.py scan --data data --out-dir "%F_OUT%" --open
goto :scan_done
:scan_cfg
call :msg STEP2
%PYCMD% screener.py scan --data data --config "%F_CFG%" --open
:scan_done
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :fail
if not exist "%F_CFG%" goto :alldone
set "SHARE_ASK=--ask"
if defined TWSTOCK_NOPAUSE set "SHARE_ASK="
%PYCMD% screener.py share --config "%F_CFG%" --publish %SHARE_ASK%
set "RC=%ERRORLEVEL%"
call :msgl LOGSHARE
if "%RC%"=="0" goto :alldone
if "%RC%"=="10" goto :share_wait
echo(
call :msg SHARE_FAIL
goto :end
:share_wait
echo(
call :msg SHARE_WAIT
set "RC=0"
set "KEEPOPEN=1"
goto :end
:alldone
echo(
call :msg DONE
goto :end

:nolatest
set "RC=6"
echo(
call :msg NOLATEST
goto :fail

:fail
echo(
call :msg FAIL
if defined LOG call :msg FAILLOG
call :msg FAILBAR
call :msgl LOGFAIL
goto :end

:end
if defined TWSTOCK_NOPAUSE goto :done
if not "%RC%"=="0" goto :dopause
if defined KEEPOPEN goto :dopause
echo(
call :msg CLOSE
timeout /t 15
goto :done
:dopause
echo(
pause
:done
popd
exit /b %RC%

:findpy
set "PYCMD="
if not defined TWSTOCK_PY goto :findpy_auto
set "TWSTOCK_PY=%TWSTOCK_PY:"=%"
if not defined TWSTOCK_PY goto :findpy_auto
"%TWSTOCK_PY%" -c "import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
if errorlevel 1 goto :findpy_badenv
set PYCMD="%TWSTOCK_PY%"
exit /b 0
:findpy_badenv
call :msg PYENV_BAD
:findpy_auto
python -c "import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
if not errorlevel 1 set "PYCMD=python"
if not errorlevel 1 exit /b 0
py -3 -c "import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
if not errorlevel 1 set "PYCMD=py -3"
if not errorlevel 1 exit /b 0
python3 -c "import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
if not errorlevel 1 set "PYCMD=python3"
if not errorlevel 1 exit /b 0
exit /b 0

:nopython
set "RC=20"
echo(
call :msg NOPY
goto :end

:notextracted
set "RC=21"
echo(
call :msg NOEXTRACT
goto :end

:nocd
set "RC=22"
echo(
call :msg NOCD
goto :end

:msg
setlocal EnableDelayedExpansion
for /f "usebackq tokens=1,* delims=|" %%a in (`findstr /b /c:"::%~1|" "%SELF%"`) do echo(%%b& if defined LOG >> "%LOG%" echo(%%b
endlocal
exit /b 0

:msgl
setlocal EnableDelayedExpansion
for /f "usebackq tokens=1,* delims=|" %%a in (`findstr /b /c:"::%~1|" "%SELF%"`) do if defined LOG >> "%LOG%" echo [!TIME!] %%b
endlocal
exit /b 0

:msgv
set "%~2="
for /f "usebackq tokens=1,* delims=|" %%a in (`findstr /b /c:"::%~1|" "%SELF%"`) do set "%~2=%%b"
exit /b 0

rem ===== messages below are data read by :msg and :msgv; cmd never executes them =====
::PYENV_BAD|注意：環境變數 TWSTOCK_PY 指定的 Python 不能用，改找電腦上的 Python。
::NOPY|【沒有完成】找不到 Python
::NOPY|原因：這台電腦還沒有安裝 Python（需要 3.9 以上的版本），或安裝時沒有讓系統找得到它。
::NOPY|怎麼做：雙擊同一個資料夾裡的 setup.bat，它會問你「要不要幫你安裝？」，打 Y 就會幫你安裝 Python。
::NOPY|也可以自己裝：用瀏覽器打開 https://www.python.org/downloads/ ，下載安裝程式並執行（畫面上有「Add python.exe to PATH」請打勾），裝完把視窗關掉，再雙擊一次 run.bat。
::NOEXTRACT|【沒有完成】請先解壓縮
::NOEXTRACT|原因：同一個資料夾裡找不到 screener.py，可能是直接在 zip 檔裡雙擊，或只複製了 .bat 檔。
::NOEXTRACT|怎麼做：在 zip 檔上按右鍵選「全部解壓縮」，再打開解壓縮後的資料夾雙擊 setup.bat。
::NOCD|【沒有完成】無法進入程式所在的資料夾
::NOCD|原因：Windows 打不開這個路徑。
::NOCD|怎麼做：把整個程式資料夾複製到本機磁碟（例如 D:\tw-stock-screener）再雙擊 setup.bat。
::F_CFG|設定.txt
::F_CFG2|設定.txt.txt
::F_OUT|結果
::F_WL|自選股.csv
::F_WLEX|自選股.範例.csv
::TITLE|台股盤後選股 - 每日更新
::LOGSTART|開始 日期 !TODAY! 星期代碼 !DOW! 參數 !ARGS!
::BANNER|============================================================
::BANNER| 台股盤後選股 - 每日更新
::BANNER|============================================================
::STEP1|[1/2] 更新股價資料……（通常 1 分鐘內；官方網站限速，請不要關掉視窗）
::MSG_TODAY|已經更新到今天 !SHOW! 的收盤資料。
::MSG_WEEKEND|今天是週末，不是交易日，以下用最近一個交易日 !SHOW! 的資料。
::MSG_NOTYET|今天還沒有新的收盤資料（休市，或官方還沒公布），以下用最近一個交易日 !SHOW! 的資料。
::TIP_CFG2|提醒：找到「設定.txt.txt」，請把檔名改成「設定.txt」（Windows 可能隱藏了副檔名）。
::NOCFG|（還沒有 設定.txt，先用預設值：全市場、不試算部位。要改設定請雙擊 setup.bat。）
::STEP2|[2/2] 選股，產生結果頁……
::DONE|完成！結果頁已經用瀏覽器打開，檔案位置列在上面。
::DONE|以後每天收盤後（下午 2 點半以後）雙擊 run.bat 就可以了。
::NOLATEST|【沒有完成】
::NOLATEST|原因：讀不到最新交易日（cache\latest.txt 不見了或是空的）。
::NOLATEST|怎麼做：在程式資料夾執行 python fetch_data.py build 重建，或重新雙擊 setup.bat 選擇下載。
::FAIL|============================================================
::FAIL| 沒有完成（代碼 !RC!）。原因和建議做法在上面「原因：」「怎麼做：」。
::FAILLOG| 紀錄檔：!LOG!
::FAILBAR|============================================================
::LOGFAIL|失敗 代碼 !RC!
::LOGSHARE|分享版公開步驟結束 代碼 !RC!
::SHARE_WAIT|選股已經完成，結果頁也打開了。分享版還沒有公開：第一次公開需要你確認（說明在上面）。
::SHARE_WAIT|不想公開的話，把 設定.txt 的 share_publish 改成 0。
::SHARE_FAIL|選股已經完成，結果頁也打開了；只有「公開分享版」沒有完成（代碼 !RC!）。
::SHARE_FAIL|原因和建議做法在上面「原因：」「怎麼做：」；今天的選股結果不受影響。
::CLOSE|這個視窗 15 秒後會自動關閉（按任意鍵可以馬上關閉）。
