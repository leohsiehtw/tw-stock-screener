@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem setup.bat - every executable line is ASCII only; Chinese text lives in the data block at the end
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

set "HERE=%CD%"
if not exist logs mkdir logs
set "LOG=%HERE%\logs\setup.log"
set "TWSTOCK_LOG=%LOG%"
call :msgl LOGSTART

echo(
call :msg BANNER
call :msg INTRO
if /i "%HERE:onedrive=%"=="%HERE%" goto :od_ok
echo(
call :msg ONEDRIVE
:od_ok

echo(
call :msg STEP1
call :findpy
if not defined PYCMD goto :nopython
:py_ok
call :msg PYFOUND
%PYCMD% --version

echo(
call :msg STEP2
for %%d in (data cache logs "%F_OUT%") do if not exist "%%~d" mkdir "%%~d"

echo(
call :msg STEP3
if not exist "%F_CFG%" goto :cfg_new
call :msg CFG_HAVE
%PYCMD% screener.py config --show "%F_CFG%"
echo(
call :ask_yn Q_RECFG N
if /i "%YN%"=="N" goto :cfg_done

:cfg_new
:q_capital
echo(
call :msg Q1
set "IN_CAPITAL="
set /p "IN_CAPITAL=> "
%PYCMD% screener.py config --check capital
if errorlevel 1 goto :q_capital

:q_risk
echo(
call :msg Q2
set "IN_RISK="
set /p "IN_RISK=> "
%PYCMD% screener.py config --check risk
if errorlevel 1 goto :q_risk

echo(
call :msg Q3
call :ask_12 Q3_PROMPT 1
set "IN_MODE=%CH%"
if "%IN_MODE%"=="2" call :prep_watchlist

%PYCMD% screener.py config --write "%F_CFG%"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :fail
echo(
call :msg CFG_SAVED
%PYCMD% screener.py config --show "%F_CFG%"
:cfg_done

echo(
call :msg STEP4
if exist cache\latest.txt goto :dl_have
call :msg DL_NEW
call :ask_yn Q_DL_NEW Y
goto :dl_asked
:dl_have
call :msg DL_HAVE
call :ask_yn Q_DL_HAVE Y
:dl_asked
if /i "%YN%"=="N" goto :dl_skip
call :msgl LOGDL
%PYCMD% fetch_data.py backfill
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :fail
goto :first_scan
:dl_skip
if exist cache\latest.txt goto :first_scan
echo(
call :msg SKIPPED
goto :end

:first_scan
echo(
call :msg STEP5
%PYCMD% screener.py scan --data data --config "%F_CFG%" --open
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :fail
echo(
call :msg DONE
goto :end

:prep_watchlist
if exist "%F_WL%" goto :wl_have
copy "%F_WLEX%" "%F_WL%" >nul
call :msg WL_NEW
goto :wl_tell
:wl_have
call :msg WL_HAVE
:wl_tell
call :msg WL_TELL
%PYCMD% screener.py config --check-watchlist "%F_WL%"
if errorlevel 1 call :msg WL_BAD
exit /b 0

:ask_yn
setlocal EnableDelayedExpansion
call :msgv %~1 Q
set "D=%~2"
:ask_yn_loop
set "ANS="
set /p "ANS=!Q! "
if not defined ANS set "ANS=!D!"
set "R="
if /i "!ANS!"=="Y" set "R=Y"
if /i "!ANS!"=="N" set "R=N"
if defined R goto :ask_yn_done
call :msg BAD_YN
goto :ask_yn_loop
:ask_yn_done
endlocal & set "YN=%R%"
exit /b 0

:ask_12
setlocal EnableDelayedExpansion
call :msgv %~1 Q
set "D=%~2"
:ask_12_loop
set "ANS="
set /p "ANS=!Q! "
if not defined ANS set "ANS=!D!"
set "R="
if "!ANS!"=="1" set "R=1"
if "!ANS!"=="2" set "R=2"
if defined R goto :ask_12_done
call :msg BAD_12
goto :ask_12_loop
:ask_12_done
endlocal & set "CH=%R%"
exit /b 0

:fail
echo(
call :msg FAIL
call :msgl LOGFAIL
goto :end

:end
if defined TWSTOCK_NOPAUSE goto :done
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
echo(
call :ask_yn Q_INSTALL N
if /i "%YN%"=="Y" goto :inst_go
call :msgl LOGNOINST
echo(
call :msg NOPY_STOP
call :msg NOPY_HOW
echo(
call :msg NOPY_TIP
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

:inst_go
echo(
call :msg INST_START
call :msgl LOGINST
set PS="%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist %PS% set "PS=powershell"
set "WRC=1"
if defined TWSTOCK_TEST_WINGET goto :wg_stub
winget --version >nul 2>&1
if errorlevel 1 goto :wg_none
echo(
call :msg WG_RUN
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
set "WRC=%ERRORLEVEL%"
goto :wg_done
:wg_stub
if /i "%TWSTOCK_TEST_WINGET%"=="missing" goto :wg_none
if /i "%TWSTOCK_TEST_WINGET%"=="ok" set "WRC=0"
:wg_done
call :msgl LOGWG
call :redetect
if defined PYCMD goto :inst_ok
if "%WRC%"=="0" goto :inst_reopen
echo(
call :msg WG_FAIL
goto :inst_download
:wg_none
echo(
call :msg WG_NONE
call :msgl LOGWG
goto :inst_download

:inst_download
set "PYVER=3.12.10"
set "PYARCH=%PROCESSOR_ARCHITECTURE%"
if defined PROCESSOR_ARCHITEW6432 set "PYARCH=%PROCESSOR_ARCHITEW6432%"
set "PYFILE=python-%PYVER%-amd64.exe"
if /i "%PYARCH%"=="ARM64" set "PYFILE=python-%PYVER%-arm64.exe"
if /i "%PYARCH%"=="x86" set "PYFILE=python-%PYVER%.exe"
set "PYURL=https://www.python.org/ftp/python/%PYVER%/%PYFILE%"
set "PYDIR="
set "PYEXE="
set "PYSHA="
if defined TEMP set "PYDIR=%TEMP%\twstock-python-setup"
if not defined PYDIR goto :dlpy_fail
call :rm_pydir
mkdir "%PYDIR%"
set "PYEXE=%PYDIR%\%PYFILE%"
echo(
call :msg DLPY
call :msgl LOGDLPY
if defined TWSTOCK_TEST_DOWNLOAD goto :dlpy_stub
%PS% -NoProfile -NonInteractive -Command "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';[Net.ServicePointManager]::SecurityProtocol=[Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12;Invoke-WebRequest -UseBasicParsing -MaximumRedirection 0 -Uri $env:PYURL -OutFile $env:PYEXE" <nul >> "%LOG%" 2>&1
if errorlevel 1 goto :dlpy_fail
goto :dlpy_have
:dlpy_stub
if /i "%TWSTOCK_TEST_DOWNLOAD%"=="fail" goto :dlpy_fail
> "%PYEXE%" echo stub
:dlpy_have
if not exist "%PYEXE%" goto :dlpy_fail
for /f "usebackq delims=" %%h in (`call %PS% -NoProfile -NonInteractive -Command "(Get-FileHash -Algorithm SHA256 -LiteralPath $env:PYEXE).Hash" ^<nul 2^>nul`) do set "PYSHA=%%h"
if not defined PYSHA call :msgv SHA_NA PYSHA
echo(
call :msg DLPY_HASH
if /i "%TWSTOCK_TEST_DOWNLOAD%"=="ok" goto :dlpy_sigskip
if /i "%TWSTOCK_TEST_DOWNLOAD%"=="instfail" goto :dlpy_sigskip
set "SIGOK="
for /f "usebackq delims=" %%s in (`call %PS% -NoProfile -NonInteractive -Command "$s=Get-AuthenticodeSignature -LiteralPath $env:PYEXE;if($s.Status -eq 'Valid' -and $s.SignerCertificate.Subject -match 'O=Python Software Foundation'){'SIGOK'}" ^<nul 2^>nul`) do set "SIGOK=%%s"
if not "%SIGOK%"=="SIGOK" goto :dlpy_badsig
:dlpy_sigskip
call :msg DLPY_SIGOK
echo(
call :msg PY_INSTALLING
call :msgl LOGPYINST
set "IRC=0"
if defined TWSTOCK_TEST_DOWNLOAD goto :dlpy_inst_stub
"%PYEXE%" /quiet InstallAllUsers=0 InstallLauncherAllUsers=0 PrependPath=1 Include_test=0 /log "%HERE%\logs\python_install.log"
set "IRC=%ERRORLEVEL%"
goto :dlpy_inst_done
:dlpy_inst_stub
if /i "%TWSTOCK_TEST_DOWNLOAD%"=="instfail" set "IRC=1603"
:dlpy_inst_done
if "%IRC%"=="3010" set "IRC=0"
call :rm_pydir
if not "%IRC%"=="0" goto :pyinst_fail
call :redetect
if defined PYCMD goto :inst_ok
goto :inst_reopen

:dlpy_fail
echo(
call :msg DLPY_FAIL
call :rm_pydir
goto :inst_fail

:dlpy_badsig
echo(
call :msg DLPY_BADSIG
call :rm_pydir
goto :inst_fail

:pyinst_fail
echo(
call :msg PYINST_FAIL
goto :inst_fail

:inst_ok
set "RC=0"
echo(
call :msg INST_OK
call :msgl LOGINSTOK
goto :py_ok

:inst_reopen
echo(
call :msg INST_REOPEN
call :msgl LOGREOPEN
goto :end

:inst_fail
echo(
call :msg INST_FAIL
call :msg NOPY_HOW
call :msgl LOGINSTFAIL
goto :end

:rm_pydir
if not defined PYDIR exit /b 0
if exist "%PYDIR%" rd /s /q "%PYDIR%"
exit /b 0

:redetect
echo(
call :msg REDETECT
set "NEWPATH=;"
if not defined TWSTOCK_TEST_REGPATH for /f "usebackq delims=" %%p in (`call %PS% -NoProfile -NonInteractive -Command "([Environment]::GetEnvironmentVariable('Path','Machine')+';'+[Environment]::GetEnvironmentVariable('Path','User')).Replace([string][char]34,'')" ^<nul 2^>nul`) do set "NEWPATH=%%p"
if not defined TWSTOCK_TEST_REGPATH if not "%NEWPATH:system32=%"=="%NEWPATH%" set "PATH=%NEWPATH%"
if defined TWSTOCK_TEST_REGPATH set "PATH=%TWSTOCK_TEST_REGPATH%"
call :findpy
if defined PYCMD exit /b 0
call :probe_py "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" -3
if defined PYCMD exit /b 0
for /d %%d in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if not defined PYCMD call :probe_py "%%~d\python.exe"
exit /b 0

:probe_py
if not exist "%~1" exit /b 0
"%~1" %~2 -c "import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
if errorlevel 1 exit /b 0
if "%~2"=="" set PYCMD="%~1"
if not "%~2"=="" set PYCMD="%~1" %~2
exit /b 0

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
::NOPY|【找不到 Python】
::NOPY|原因：這台電腦還沒有安裝 Python（需要 3.9 以上的版本），或安裝時沒有讓系統找得到它。
::NOPY|我可以幫你安裝 Python 3.12：用 Python 官方的安裝程式，只裝給你這個 Windows 帳號，不需要系統管理員權限。需要網路，通常幾分鐘就好。
::Q_INSTALL|要不要幫你安裝？（Y/N，直接按 Enter = N）：
::NOPY_STOP|【沒有完成】還沒有 Python，設定先停在這裡。
::NOPY_HOW|怎麼自己裝 Python：
::NOPY_HOW|  1. 用瀏覽器打開 https://www.python.org/downloads/ ，下載最新版的 Python 3（Windows 版）。
::NOPY_HOW|  2. 執行下載的安裝程式。如果畫面上有「Add python.exe to PATH」，請把它打勾。
::NOPY_HOW|  3. 按 Install Now（或安裝程式上意思相同的安裝按鈕），等它裝完，然後把視窗關掉。
::NOPY_HOW|  4. 再雙擊一次 setup.bat。
::NOPY_TIP|想讓 setup.bat 幫你安裝的話：再雙擊一次 setup.bat，問「要不要幫你安裝？」時打 Y 再按 Enter。
::INST_START|好，開始安裝。安裝完成前請不要關掉這個視窗。
::WG_RUN|正在用 Windows 內建的 winget 安裝 Python 3.12……
::WG_RUN|（如果跳出「使用者帳戶控制」視窗，按「是」就可以。）
::WG_NONE|這台電腦沒有 winget，改從 Python 官方網站下載安裝程式。
::WG_FAIL|winget 沒有裝好（代碼 !WRC!），改從 Python 官方網站下載安裝程式。
::DLPY|正在下載 Python !PYVER! 安裝程式，通常幾分鐘內完成，請稍等……
::DLPY|來源網址：!PYURL!
::DLPY_FAIL|下載失敗：連不上 Python 官方網站，或網路中斷了。
::DLPY_HASH|下載完成。檔案的 SHA256 雜湊值：!PYSHA!
::SHA_NA|（無法計算）
::DLPY_SIGOK|安全檢查通過：檔案有 Python 官方（Python Software Foundation）的有效數位簽章。
::DLPY_BADSIG|安全檢查沒有通過：這個檔案沒有 Python 官方（Python Software Foundation）的有效數位簽章，所以不會執行它。
::PY_INSTALLING|正在安裝 Python，通常幾分鐘內完成。這段時間畫面不會動，請耐心等，不要關掉視窗……
::PYINST_FAIL|安裝程式沒有成功（代碼 !IRC!）。
::REDETECT|正在重新尋找 Python……
::INST_OK|找到 Python 了，繼續設定。
::INST_REOPEN|【還差一步】Python 應該已經裝好了，但這個視窗還找不到它。
::INST_REOPEN|怎麼做：關掉這個視窗，再雙擊一次 setup.bat，就會接著設定。
::INST_REOPEN|（再試一次還是說找不到 Python 的話，請重新開機後再雙擊 setup.bat。）
::INST_FAIL|【沒有完成】沒辦法自動安裝 Python（原因在上面）。
::LOGINST|使用者選擇自動安裝 Python
::LOGNOINST|使用者選擇不安裝 Python
::LOGWG|winget 步驟結束 代碼 !WRC!
::LOGDLPY|開始從 Python 官方網站下載 !PYURL!
::LOGPYINST|開始執行 Python 安裝程式
::LOGINSTOK|安裝後找到 Python 了
::LOGREOPEN|Python 已安裝但這個視窗找不到，請重開 setup.bat
::LOGINSTFAIL|自動安裝 Python 失敗
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
::TITLE|台股盤後選股 - 初次設定
::LOGSTART|開始 setup
::LOGDL|開始下載股價資料
::LOGFAIL|失敗 代碼 !RC!
::BANNER|============================================================
::BANNER| 台股盤後選股 - 初次設定
::BANNER|============================================================
::INTRO|接下來會問 3 個問題（總資金、每筆風險、選股範圍），都可以直接按 Enter 用預設值。
::INTRO|設定會存在這個資料夾的「設定.txt」。之後想改，再雙擊一次 setup.bat 就可以了。
::ONEDRIVE|提醒：這個資料夾看起來在 OneDrive 裡面。股價資料有兩千多個小檔案，同步會很慢；
::ONEDRIVE|      建議把整個資料夾搬到 D:\tw-stock-screener 這類不同步的位置。（不搬也可以繼續）
::STEP1|[1/5] 檢查 Python
::PYFOUND|已找到 Python：
::STEP2|[2/5] 準備資料夾
::STEP3|[3/5] 設定
::CFG_HAVE|目前已經有 設定.txt，內容是：
::Q_RECFG|要重新設定嗎？（Y 重新設定／N 保留現有設定，直接按 Enter = N）：
::Q1|問題 1／3：你的總資金大約是多少元？
::Q1|  程式會用它算「每檔候選大概可以買幾股」。只打半形數字，例如 1000000。
::Q1|  不想算股數的話，直接按 Enter 跳過。
::Q2|問題 2／3：每一筆交易，最多願意虧掉總資金的百分之幾？
::Q2|  打 2 到 5 之間的數字，例如 2（總資金 100 萬、2％ = 單筆最多虧 2 萬元）。直接按 Enter = 2。
::Q3|問題 3／3：要掃描哪些股票？
::Q3|  1 = 全市場（上市＋上櫃＋ETF，約 2300 檔，每天找新標的）
::Q3|  2 = 只掃描自選股清單（盯著自己研究過的股票）
::Q3_PROMPT|請輸入 1 或 2（直接按 Enter = 1）：
::CFG_SAVED|設定已經存好，內容是：
::STEP4|[4/5] 下載股價資料
::DL_NEW|還沒有股價資料。第一次要下載約一年半的資料，大約需要 30～60 分鐘（官方網站限速，程式會慢慢抓）。
::DL_NEW|下載中間關掉視窗也沒關係，之後再雙擊 setup.bat 就會接著抓，不會重來。
::Q_DL_NEW|要現在下載嗎？（Y 現在下載／N 以後再說，直接按 Enter = Y）：
::DL_HAVE|已經有股價資料了。可以現在補抓到最新的（通常幾分鐘）。
::Q_DL_HAVE|要現在補抓嗎？（Y/N，直接按 Enter = Y）：
::SKIPPED|設定完成，但還沒下載股價。
::SKIPPED|之後要下載時，再雙擊 setup.bat 並選 Y（或在這個資料夾執行 python fetch_data.py backfill）；
::SKIPPED|下載好以後，每天雙擊 run.bat 就可以選股了。
::STEP5|[5/5] 第一次選股
::DONE|============================================================
::DONE| 設定完成！結果頁已經用瀏覽器打開，檔案位置列在上面。
::DONE| 以後每天收盤後（下午 2 點半以後）雙擊 run.bat 就可以了。
::DONE|============================================================
::WL_NEW|已經幫你建立「自選股.csv」（內容是範例）。
::WL_HAVE|找到你現有的「自選股.csv」，不會覆蓋它。
::WL_TELL|請用「記事本」打開它，改成你要追蹤的股票（一行一檔，第一欄是代號）。
::WL_TELL|不要用 Excel 編輯：Excel 會把 0050 變成 50。
::WL_BAD|提醒：自選股清單有問題，設定會先存起來；改好 自選股.csv 之後再執行 run.bat 就可以了。
::BAD_YN|請輸入 Y 或 N。
::BAD_12|請輸入 1 或 2。
::FAIL|============================================================
::FAIL| 沒有完成（代碼 !RC!）。原因和建議做法在上面「原因：」「怎麼做：」。
::FAIL| 已經下載的資料會保留；問題排除後再雙擊 setup.bat 即可接著做。
::FAIL| 紀錄檔：!LOG!
::FAIL|============================================================
