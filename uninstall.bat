@echo off
net session >nul 2>&1
if errorlevel 1 (
  echo Please right-click uninstall.bat and choose "Run as administrator".
  pause
  exit /b 1
)
schtasks /End /TN "BasketsApp" >nul 2>&1
schtasks /Delete /TN "BasketsApp" /F >nul 2>&1
netsh advfirewall firewall delete rule name="Baskets App" >nul 2>&1
taskkill /F /IM python.exe >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make_shortcut.ps1" -Remove >nul 2>&1
echo Automatic start, firewall rule and desktop shortcut were removed.
echo Your data file vouchers.db was NOT deleted. You can delete this folder if you want.
pause
