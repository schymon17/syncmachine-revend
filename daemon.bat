@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "ROOT=%~dp0"
set "LOG=%ROOT%sync-daemon.log"
set "PHP_EXE=%ROOT%php\php.exe"
set "SYNC_CLI=%ROOT%bin\sync.php"
set "RESTART_DELAY=10"

echo [%DATE% %TIME%] daemon.bat started >> "%LOG%"

:loop
if not exist "%PHP_EXE%" (
  echo [%DATE% %TIME%] ERROR php.exe not found at "%PHP_EXE%" >> "%LOG%"
  call :sleep %RESTART_DELAY%
  goto loop
)

if not exist "%SYNC_CLI%" (
  echo [%DATE% %TIME%] ERROR sync CLI not found at "%SYNC_CLI%" >> "%LOG%"
  call :sleep %RESTART_DELAY%
  goto loop
)

echo [%DATE% %TIME%] Starting daemon iteration >> "%LOG%"
"%PHP_EXE%" "%SYNC_CLI%" daemon >> "%LOG%" 2>&1
set "EXITCODE=%ERRORLEVEL%"
echo [%DATE% %TIME%] daemon exited with code %EXITCODE% >> "%LOG%"

if "%EXITCODE%"=="20" (
  echo [%DATE% %TIME%] Update prepared, running apply-update >> "%LOG%"
  "%PHP_EXE%" "%SYNC_CLI%" apply-update >> "%LOG%" 2>&1
  echo [%DATE% %TIME%] apply-update exit code !ERRORLEVEL! >> "%LOG%"
)

call :sleep %RESTART_DELAY%
goto loop

:sleep
set "S=%~1"
if "%S%"=="" set "S=10"
timeout /t %S% /nobreak >nul 2>nul
if errorlevel 1 ping 127.0.0.1 -n %S% >nul
exit /b 0
