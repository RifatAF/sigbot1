@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo === sigbot: установка на Windows ===

where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% --version || (echo Нужен Python 3.11+ с python.org, при установке отметьте "Add to PATH" & pause & exit /b 1)

if not exist .venv %PY% -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt || (echo Ошибка установки зависимостей & pause & exit /b 1)
python -m playwright install chromium

if not exist .env (
  echo.
  echo Ключи пишутся только в файл .env на этом компьютере.
  set /p TG=Токен Telegram бота: 
  set /p GM=Ключ Gemini: 
  call :writeenv
)

echo.
echo Загрузка истории для бэктеста и портфеля (около 1-2 часов, можно оставить на ночь)...
python -m sigbot backfill

echo.
echo Автозапуск: задача "sigbot" при входе в систему, перезапуск при падении.
schtasks /Create /F /TN "sigbot" /SC ONLOGON /RL LIMITED /TR "\"%~dp0run_sigbot.bat\"" >nul && echo Задача создана.
start "" "%~dp0run_sigbot.bat"
echo Готово. Напишите боту /menu в Telegram.
pause
exit /b 0

:writeenv
(
  echo TELEGRAM_BOT_TOKEN=%TG%
  echo TELEGRAM_CHANNEL_ID=-1003595508596
  echo TELEGRAM_OWNER_ID=569798955
  echo GEMINI_API_KEY=%GM%
) > .env
exit /b 0
