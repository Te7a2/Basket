@echo off
cd /d "%~dp0"
title Baskets App - Installer
set PORT=8501
set TASK=BasketsApp

net session >nul 2>&1
if errorlevel 1 (
  echo.
  echo ERROR: Please right-click install.bat and choose "Run as administrator".
  echo.
  pause
  exit /b 1
)

echo.
echo [1/7] Looking for Python...
set "PYEXE="
py -3 -c "import sys" >nul 2>&1 && set "PYEXE=py -3"
if not defined PYEXE python -c "import sys" >nul 2>&1 && set "PYEXE=python"
if not defined PYEXE (
  echo Python was not found. Downloading Python 3.12, please wait...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe -OutFile '%TEMP%\python_setup.exe'"
  if not exist "%TEMP%\python_setup.exe" (
    echo.
    echo Could not download Python. Please install Python from python.org and run this file again.
    pause
    exit /b 1
  )
  "%TEMP%\python_setup.exe" /quiet InstallAllUsers=1 PrependPath=1 Include_test=0
  set "PYEXE=py -3"
  %PYEXE% -c "import sys" >nul 2>&1
  if errorlevel 1 (
    echo.
    echo Python installation failed. Please install Python manually from python.org and run this file again.
    pause
    exit /b 1
  )
)
echo Using: %PYEXE%

echo.
echo [2/7] Creating private Python environment...
if not exist "venv\Scripts\python.exe" (
  %PYEXE% -m venv venv
  if errorlevel 1 (
    echo ERROR: could not create the environment.
    pause
    exit /b 1
  )
)

echo.
echo [3/7] Installing libraries. Internet is needed, this can take a few minutes...
"venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>&1
"venv\Scripts\python.exe" -m pip install -U streamlit pandas openpyxl streamlit-keyup playwright
if errorlevel 1 (
  echo.
  echo ERROR: library installation failed. Check the internet connection and run this file again.
  pause
  exit /b 1
)

echo.
echo [3b/7] Installing the PDF engine - optional, about 150 MB. If it fails the program still works...
set PLAYWRIGHT_BROWSERS_PATH=0
"venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 echo PDF engine was not installed. PDF will try Microsoft Edge, otherwise use Print and Save as PDF.

echo.
echo [4/7] Writing settings...
if not exist ".streamlit" mkdir ".streamlit"
(
echo [server]
echo address = "0.0.0.0"
echo port = %PORT%
echo headless = true
echo.
echo [browser]
echo gatherUsageStats = false
) > ".streamlit\config.toml"

echo.
echo [5/7] Opening port %PORT% in Windows Firewall...
netsh advfirewall firewall delete rule name="Baskets App" >nul 2>&1
netsh advfirewall firewall add rule name="Baskets App" dir=in action=allow protocol=TCP localport=%PORT% profile=any >nul

echo.
echo [6/7] Setting automatic start with the server...
schtasks /End /TN "%TASK%" >nul 2>&1
schtasks /Delete /TN "%TASK%" /F >nul 2>&1
schtasks /Create /TN "%TASK%" /SC ONSTART /RU SYSTEM /RL HIGHEST /TR "\"%~dp0run_server.bat\"" /F
if errorlevel 1 (
  echo ERROR: could not create the startup task.
  pause
  exit /b 1
)
schtasks /Run /TN "%TASK%" >nul
echo Starting the program, please wait...
timeout /t 10 /nobreak >nul

echo.
echo [7/7] Creating the AFAQ desktop shortcut - opens in Google Chrome...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make_shortcut.ps1" -Url "http://localhost:%PORT%"
if errorlevel 1 echo Could not create the desktop shortcut. You can run client_shortcut.bat later.

echo.
echo ============================================================
echo  DONE. Open this address in the browser on ANY device:
echo.
ipconfig | findstr /C:"IPv4"
echo.
echo  Use the IPv4 number above like this:  http://THE-NUMBER:%PORT%
echo  On this server you can also use:      http://localhost:%PORT%
echo ============================================================
echo.
pause
