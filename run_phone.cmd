@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
cd /d "%~dp0"

set KEY=
for /f "usebackq tokens=1,* delims==" %%a in ("%~dp0.env") do (
  if /i "%%a"=="PHONE_API_KEY" set KEY=%%b
)

echo.
echo   ============================================================
echo     Berta - запуск для телефона (доступ по Wi-Fi)
echo   ============================================================
echo.
echo   IP-адреса этого ПК (телефон должен быть в той же сети):
powershell -NoProfile -Command "Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | ForEach-Object { '     ' + $_.IPAddress + '   (' + $_.InterfaceAlias + ')' }"
echo.
if defined KEY (
  echo   Откройте на телефоне:  http://^<IP^>:8765/m?key=!KEY!
) else (
  echo   [!] PHONE_API_KEY пуст в .env - мобильная страница будет недоступна.
)
echo.
echo   Остановить сервер - Ctrl+C в этом окне.
echo.

".venv\Scripts\python.exe" main.py --host=0.0.0.0
pause
