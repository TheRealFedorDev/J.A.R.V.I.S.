@echo off
chcp 65001 >nul
REM Останавливает фоновый сервер Berta, запущенный run_phone_bg.vbs.
echo Останавливаю фоновый сервер Berta...
powershell -NoProfile -Command "$ports = 8765,8077; $ports | ForEach-Object { Get-NetTCPConnection -LocalPort $_ -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue } }; Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*main.py*--host*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo Готово.
