@echo off
net session >nul 2>&1
if errorlevel 1 (
  echo Please right-click restart.bat and choose "Run as administrator".
  pause
  exit /b 1
)
schtasks /End /TN "BasketsApp" >nul 2>&1
taskkill /F /IM python.exe >nul 2>&1
timeout /t 3 /nobreak >nul
schtasks /Run /TN "BasketsApp"
echo Restarted.
timeout /t 5 /nobreak >nul
