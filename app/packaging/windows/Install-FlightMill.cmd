@echo off
setlocal
title Flight Mill installation
set "FLIGHTMILL_POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if exist "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe" set "FLIGHTMILL_POWERSHELL=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
"%FLIGHTMILL_POWERSHELL%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-FlightMill.ps1"
set "FLIGHTMILL_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %FLIGHTMILL_EXIT%
