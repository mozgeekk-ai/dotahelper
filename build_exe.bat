@echo off
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --noconsole --name DotaAssistant dota_assistant.py
echo Готово: dist\DotaAssistant.exe
pause
