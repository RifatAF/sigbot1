@echo off
chcp 65001 >nul
cd /d "%~dp0"
set SIGBOT_SUPERVISED=1
:loop
git pull --ff-only >nul 2>nul
call .venv\Scripts\activate.bat
python -m pip install -q -r requirements.txt >nul 2>nul
python -m sigbot run >> sigbot.log 2>&1
echo %date% %time% sigbot остановился, перезапуск через 30 секунд >> sigbot.log
timeout /t 20 /nobreak >nul
goto loop
