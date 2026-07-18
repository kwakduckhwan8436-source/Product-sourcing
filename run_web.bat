@echo off
cd /d "%~dp0"
echo ===============================================
echo   Sourcing Workbench
echo ===============================================
python --version >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python not found. Install Python 3.10+ first.
  pause
  exit /b 1
)
echo [1/2] Installing packages ^(first run only^)...
python -m pip install -r "web\requirements.txt" --disable-pip-version-check
echo [2/2] Starting - a free port is chosen automatically
python run_web.py
pause
