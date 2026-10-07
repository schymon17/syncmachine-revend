@echo off
rem Instalacja agenta ReVend Sync bez PowerShella (np. Windows 7).
rem Skopiuj rozpakowana paczke na maszyne i uruchom jako administrator:
rem   install.cmd RV-XXXX-XXXX-XXXX
setlocal
net session >nul 2>&1
if errorlevel 1 (
  echo Uruchom ten plik jako administrator ^(prawy przycisk - Uruchom jako administrator^).
  pause
  exit /b 1
)
set "CODE=%~1"
if "%CODE%"=="" set /p CODE=Kod instalacyjny z panelu (RV-XXXX-XXXX-XXXX): 
"%~dp0revend-sync\revend-sync.exe" install --code "%CODE%"
set "RESULT=%ERRORLEVEL%"
echo.
if "%RESULT%"=="0" (echo Gotowe.) else (echo Instalacja nie powiodla sie - kod %RESULT%.)
pause
exit /b %RESULT%
