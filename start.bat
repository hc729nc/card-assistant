@echo off
REM Double-click to start the card dashboard (Windows).
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python is not installed.
  echo Get it from https://www.python.org/downloads/ and tick "Add python.exe to PATH" during install.
  pause
  exit /b
)
echo Getting things ready (first time takes a minute)...
python -m pip install --quiet --disable-pip-version-check -r requirements.txt
python dashboard.py %*
pause
