@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

echo.
echo   [1/2] Локальный voice API: faster-whisper + edge-tts (127.0.0.1:8077)
start "Berta voice API" /min "%~dp0.venv\Scripts\python.exe" -m jarvis.voice.local_voice_api

echo   [2/2] Ждём прогрев модели...
timeout /t 8 /nobreak >nul

echo.
echo   Запускаю голосового агента LiveKit. Говорите — я отвечу голосом.
echo   Выход: Ctrl+C
echo.
"%~dp0.venv-livekit\Scripts\python.exe" jarvis\voice\livekit_agent.py console

echo.
echo   Агент остановлен. Закрываю voice API...
taskkill /FI "WINDOWTITLE eq Berta voice API*" /T >nul 2>&1
pause
