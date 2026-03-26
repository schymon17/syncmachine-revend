@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "ROOT=%~dp0"
set "DAEMON=%ROOT%daemon.bat"
set "LOG=%ROOT%sync-daemon-autostart.log"

echo [%DATE% %TIME%] daemon-autostart invoked >> "%LOG%"

if not exist "%DAEMON%" (
  echo [%DATE% %TIME%] ERROR daemon.bat not found at "%DAEMON%" >> "%LOG%"
  exit /b 1
)

start "" /min "%ComSpec%" /c call "\"%DAEMON%\""
set "EXITCODE=%ERRORLEVEL%"
echo [%DATE% %TIME%] start command exit code %EXITCODE% >> "%LOG%"
exit /b %EXITCODE%
