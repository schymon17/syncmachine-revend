@echo off
cd /d "%~dp0"

set LOG=%~dp0sync-daemon.log

echo Starting daemon... >> "%LOG%"

:loop
php\php.exe bin\sync.php daemon >> "%LOG%" 2>&1

echo Restarting daemon %DATE% %TIME% >> "%LOG%"
timeout /t 10 /nobreak >nul
goto loop