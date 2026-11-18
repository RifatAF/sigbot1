@echo off
chcp 65001 >nul
rem Разрешает телефону в домашней Wi-Fi открыть панель бота (порт 8787). Нужны права администратора.
net session >nul 2>&1
if %errorlevel% neq 0 (
  powershell -Command "Start-Process '%~f0' -Verb RunAs"
  exit /b
)
echo Убираю запреты Windows для Python (появляются, если когда-то нажать «Отмена» в окне брандмауэра)...
powershell -NoProfile -Command "Get-NetFirewallApplicationFilter | Where-Object { $_.Program -like '*python*' } | Get-NetFirewallRule | Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Block' } | ForEach-Object { Write-Host ('  убран запрет: ' + $_.DisplayName); Remove-NetFirewallRule -Name $_.Name }"
netsh advfirewall firewall delete rule name="sigbot panel" >nul 2>&1
netsh advfirewall firewall add rule name="sigbot panel" dir=in action=allow protocol=TCP localport=8787 profile=any
echo.
echo Проверяю, что бот слушает порт 8787:
netstat -an | findstr ":8787" | findstr "LISTENING"
echo.
echo Если выше есть строка 0.0.0.0:8787 LISTENING, открой на телефоне ссылку из /menu - Панель на телефоне.
echo Если строки нет: бот не запущен. Если там 127.0.0.1:8787: бот не обновился, перезапусти его.
pause
