@echo off
chcp 65001 >nul
rem Разрешает телефону в домашней Wi-Fi открыть панель бота (порт 8787). Нужны права администратора.
net session >nul 2>&1
if %errorlevel% neq 0 (
  powershell -Command "Start-Process '%~f0' -Verb RunAs"
  exit /b
)
netsh advfirewall firewall delete rule name="sigbot panel" >nul 2>&1
netsh advfirewall firewall add rule name="sigbot panel" dir=in action=allow protocol=TCP localport=8787 profile=any
echo.
echo Готово: телефон в той же Wi-Fi может открыть панель. Окно можно закрыть.
pause