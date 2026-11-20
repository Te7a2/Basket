@echo off
cd /d "%~dp0"
"%~dp0venv\Scripts\python.exe" -m streamlit run app.py >> "%~dp0server.log" 2>&1
