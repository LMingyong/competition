@echo off
setlocal EnableExtensions
chcp 65001 >nul

rem 每 5 分钟对仓库执行一次 git pull --ff-only。
rem 这是 scripts\auto_git_pull.sh 的 Windows 版。
rem
rem 用法:
rem   scripts\auto_git_pull.bat
rem   scripts\auto_git_pull.bat --once
rem   set INTERVAL=60
rem   scripts\auto_git_pull.bat
rem
rem 选项:
rem   --once           只拉取一次后退出
rem   --interval 秒    轮询间隔，默认 300，也可用环境变量 INTERVAL
rem   --repo 目录      仓库目录，默认为本脚本所在仓库根
rem   --log 文件       日志文件，默认 仓库\logs\auto_pull.log
rem   --remote 名称    远程名，默认 origin（实际仍走当前分支的上游）
rem   -h, --help       显示帮助
rem
rem 需要 Git for Windows 和 Windows PowerShell，并且 git 在 PATH 里。
rem 关掉这个窗口就会停止轮询。

if /I "%~1"=="-h" goto show_help
if /I "%~1"=="--help" goto show_help

where git >nul 2>&1
if errorlevel 1 (
  1>&2 echo 需要 Git for Windows，并且 git 在 PATH 中。
  exit /b 1
)
where powershell >nul 2>&1
if errorlevel 1 (
  1>&2 echo 需要 Windows PowerShell。
  exit /b 1
)

set "ONCE=0"
if not defined INTERVAL set "INTERVAL=300"
if not defined REMOTE set "REMOTE=origin"

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--once" (
  set "ONCE=1"
  shift
  goto parse_args
)
if /I "%~1"=="--interval" (
  set "INTERVAL=%~2"
  shift
  shift
  goto parse_args
)
if /I "%~1"=="--repo" (
  set "REPO_DIR=%~2"
  shift
  shift
  goto parse_args
)
if /I "%~1"=="--log" (
  set "LOG_FILE=%~2"
  shift
  shift
  goto parse_args
)
if /I "%~1"=="--remote" (
  set "REMOTE=%~2"
  shift
  shift
  goto parse_args
)
if /I "%~1"=="-h" goto show_help
if /I "%~1"=="--help" goto show_help
1>&2 echo 未知参数: %~1
set "HELP_RC=2"
goto show_help

:args_done
echo(%INTERVAL%| findstr /R "^[1-9][0-9]*$" >nul
if errorlevel 1 (
  1>&2 echo 间隔必须是正整数秒: %INTERVAL%
  exit /b 2
)

if not defined REPO_DIR (
  for %%I in ("%~dp0..") do set "REPO_DIR=%%~fI"
)
if not defined LOG_FILE set "LOG_FILE=%REPO_DIR%\logs\auto_pull.log"

for %%I in ("%LOG_FILE%") do set "LOG_DIR=%%~dpI"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

set "GIT_TERMINAL_PROMPT=0"
set "LOCK_FILE=%LOG_FILE%.lock"
set "AGP_STARTED=%TEMP%\agp_%RANDOM%%RANDOM%.started"
set "AGP_EXIT=%TEMP%\agp_%RANDOM%%RANDOM%.exit"
del "%AGP_STARTED%" 2>nul
del "%AGP_EXIT%" 2>nul

rem 用文件句柄独占锁。进程退出或窗口关掉后锁会自动放开。
2>nul (
  9>>"%LOCK_FILE%" (
    >"%AGP_STARTED%" echo 1
    call :body
    call :save_rc
  )
)

if not exist "%AGP_STARTED%" (
  1>&2 echo 已有一个 auto_git_pull 在运行（锁: %LOCK_FILE%）
  exit /b 1
)

set "RC=0"
if exist "%AGP_EXIT%" set /p RC=<"%AGP_EXIT%"
del "%AGP_STARTED%" 2>nul
del "%AGP_EXIT%" 2>nul
exit /b %RC%

:save_rc
>"%AGP_EXIT%" echo %ERRORLEVEL%
exit /b 0

:body
if "%ONCE%"=="1" goto body_once
set "LOGMSG=开始轮询，间隔 %INTERVAL% 秒，仓库 %REPO_DIR%，远程 %REMOTE%"
call :write_log
:body_loop
call :pull_once
powershell -NoProfile -Command "Start-Sleep -Seconds %INTERVAL%"
goto body_loop

:body_once
call :pull_once
exit /b %ERRORLEVEL%

:pull_once
git -C "%REPO_DIR%" rev-parse HEAD >"%TEMP%\agp_before.txt" 2>&1
if errorlevel 1 goto pull_no_head
set /p BEFORE=<"%TEMP%\agp_before.txt"

git -C "%REPO_DIR%" rev-parse --abbrev-ref --symbolic-full-name "@{u}" >"%TEMP%\agp_up.txt" 2>&1
if errorlevel 1 goto pull_no_upstream
set /p UPSTREAM=<"%TEMP%\agp_up.txt"
for /f "tokens=1 delims=/" %%R in ("%UPSTREAM%") do set "UPSTREAM_REMOTE=%%R"
if /I not "%UPSTREAM_REMOTE%"=="%REMOTE%" goto pull_remote_mismatch

git -C "%REPO_DIR%" pull --ff-only >"%TEMP%\agp_pull.txt" 2>&1
if errorlevel 1 goto pull_failed

git -C "%REPO_DIR%" rev-parse HEAD >"%TEMP%\agp_after.txt" 2>&1
set /p AFTER=<"%TEMP%\agp_after.txt"
if /I not "%BEFORE%"=="%AFTER%" goto pull_updated
set "LOGMSG=已是最新 %BEFORE:~0,12%"
call :write_log
exit /b 0

:pull_updated
set LOGMSG=有更新 %BEFORE:~0,12% -^> %AFTER:~0,12%
call :write_log
git -C "%REPO_DIR%" log --oneline "%BEFORE%..%AFTER%" >"%TEMP%\agp_commits.txt" 2>&1
for /f "usebackq delims=" %%L in ("%TEMP%\agp_commits.txt") do call :log_line "%%L"
exit /b 0

:pull_no_head
set "LOGMSG=拉取失败: 不是 git 仓库或没有 HEAD: "
set "AGP_ERRFILE=%TEMP%\agp_before.txt"
call :log_with_errfile
exit /b 1

:pull_no_upstream
set "LOGMSG=拉取失败: 当前分支没有上游。请先执行 git branch -u %REMOTE%/分支名"
call :write_log
exit /b 1

:pull_remote_mismatch
set "LOGMSG=拉取失败: 当前上游在 %UPSTREAM_REMOTE%，与指定远程 %REMOTE% 不一致"
call :write_log
exit /b 1

:pull_failed
set "LOGMSG=拉取失败: "
set "AGP_ERRFILE=%TEMP%\agp_pull.txt"
call :log_with_errfile
exit /b 1

:log_line
set "LOGMSG=提交 %~1"
call :write_log
exit /b 0

:log_with_errfile
powershell -NoProfile -Command "$parts=@(Get-Content -LiteralPath $env:AGP_ERRFILE -ErrorAction SilentlyContinue | Where-Object { $_.Trim() -ne '' }); $msg=$env:LOGMSG; if ($parts.Count -gt 0) { $msg = $msg + ($parts -join ' | ') }; $line=(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')+' '+$msg; $enc=New-Object System.Text.UTF8Encoding $false; [IO.File]::AppendAllText($env:LOG_FILE,$line+[Environment]::NewLine,$enc); [Console]::Out.WriteLine($line)"
exit /b 0

:write_log
powershell -NoProfile -Command "$line=(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')+' '+$env:LOGMSG; $enc=New-Object System.Text.UTF8Encoding $false; [IO.File]::AppendAllText($env:LOG_FILE,$line+[Environment]::NewLine,$enc); [Console]::Out.WriteLine($line)"
exit /b 0

:show_help
echo 每 5 分钟对仓库执行一次 git pull --ff-only。
echo 拉到新提交时把提交写进日志；失败只记录，下一轮继续。
echo.
echo 用法:
echo   scripts\auto_git_pull.bat
echo   scripts\auto_git_pull.bat --once
echo   set INTERVAL=60 ^& scripts\auto_git_pull.bat
echo.
echo 选项:
echo   --once           只拉取一次后退出
echo   --interval 秒    轮询间隔，默认 300，也可用环境变量 INTERVAL
echo   --repo 目录      仓库目录，默认为本脚本所在仓库根
echo   --log 文件       日志文件，默认 仓库\logs\auto_pull.log
echo   --remote 名称    远程名，默认 origin（实际仍走当前分支的上游）
echo   -h, --help       显示帮助
if not defined HELP_RC set "HELP_RC=0"
exit /b %HELP_RC%
