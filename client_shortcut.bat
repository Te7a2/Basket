@echo off
cd /d "%~dp0"
title AFAQ - Create desktop shortcut on this computer
echo.
echo This creates the AFAQ icon on THIS computer's desktop. It opens in Google Chrome.
echo Type the SERVER address, for example 192.168.1.20
echo.
set "SRV="
set /p SRV=Server address: 
if "%SRV%"=="" (
  echo No address entered.
  pause
  exit /b 1
)
echo %SRV% | findstr /i "http" >nul && (set "URL=%SRV%") || (set "URL=http://%SRV%:8501")
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make_shortcut.ps1" -Url "%URL%"
echo.
pause
