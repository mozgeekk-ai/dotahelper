@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Установите Python с python.org и поставьте галочку "Add Python to PATH", затем запустите этот файл снова.
  pause
  exit /b
)
python -m pip install -q -r requirements.txt
start "" pythonw dota_assistant.py
