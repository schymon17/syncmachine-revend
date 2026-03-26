@echo off
setlocal EnableExtensions
cd /d "%~dp0"

start "Revend Sync Daemon" /min cmd /c "\"%~dp0daemon.bat\""
