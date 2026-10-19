@echo off
chcp 65001 >nul
REM Останавливает фоновый сервер Berta, запущенный run_phone_bg.vbs.
echo Останавливаю фоновый сервер Berta...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*main.py*--host*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo Готово.
