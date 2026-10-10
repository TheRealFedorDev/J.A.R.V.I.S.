@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

echo.
echo   ============================================================
echo     J.A.R.V.I.S. - установка на новой машине
echo   ============================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo   [X] Python не найден в PATH. Установите Python 3.11-3.13
  echo       с python.org и отметьте "Add to PATH".
  echo.
  pause
  exit /b 1
)

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo   [1/5] Создан .env из .env.example
) else (
  echo   [1/5] .env уже есть - пропускаю
)

if not exist ".venv\Scripts\python.exe" (
  echo   [2/5] Создаю основное окружение .venv (это на пару минут)
  python -m venv .venv
  if errorlevel 1 goto :fail
) else (
  echo   [2/5] .venv уже есть - пропускаю
)

echo   [3/5] Ставлю зависимости основного окружения
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail

where py >nul 2>&1
if errorlevel 1 (
  echo   [4/5] Launcher `py` не найден - пропускаю голосовой контур.
  echo         Он нужен только для запуска голоса.
) else (
  if not exist ".venv-livekit\Scripts\python.exe" (
    echo   [4/5] Создаю голосовое окружение .venv-livekit (нужен Python 3.11)
    py -3.11 -m venv .venv-livekit
    if not errorlevel 1 (
      ".venv-livekit\Scripts\python.exe" -m pip install --upgrade pip --quiet
      ".venv-livekit\Scripts\python.exe" -m pip install -r requirements-livekit.txt
    ) else (
      echo         [!] Python 3.11 не найден. Голос пока не запустится,
      echo             остальное работает.
    )
  ) else (
    echo   [4/5] .venv-livekit уже есть - пропускаю
  )
)

echo   [5/5] Проверяю Ollama
where ollama >nul 2>&1
if errorlevel 1 (
  echo         Ollama не найден. Установите с https://ollama.com/download
  echo         и выполните:  ollama pull llama3.2:3b
) else (
  ollama list
)

echo.
echo   ============================================================
echo     Диагностика
echo   ============================================================
echo.
".venv\Scripts\python.exe" -m jarvis.doctor
set DOCTOR_CODE=%errorlevel%

echo.
if "%DOCTOR_CODE%"=="0" (
  echo   Готово. Запуск:  .venv\Scripts\python.exe main.py
) else (
  echo   Есть проблемы - посмотрите строку "Чинить в первую очередь" выше.
  echo   Диагностика идёт по тем же цепочкам, что и живой код,
  echo   поэтому список неполон только если сломана сама диагностика.
)
echo.
pause
exit /b %DOCTOR_CODE%

:fail
echo.
echo   [X] Установка не завершилась. Текст ошибки выше.
echo.
pause
exit /b 1