"""Голосовой контур на LiveKit Agents как сервис J.A.R.V.I.S.

LiveKit-агент живёт в отдельном venv (.venv-livekit) и не импортирует
jarvis.*, поэтому запускается дочерним процессом. Этот сервис поднимает
пару процессов и следит за ними:

    1. .venv        -> jarvis.voice.local_voice_api  (STT/TTS, порт 8077)
    2. .venv-livekit-> jarvis/voice/livekit_agent.py console  (микрофон + динамики)

Микрофон занимает только агент, поэтому VOICE_AGENT=pipecat и
VOICE_AGENT=livekit взаимно исключают друг друга.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import IO, Any

from jarvis.core.config import VoiceConfig
from jarvis.core.events import (
    BotReplyEvent,
    EventBus,
    VoicePartialEvent,
    VoiceStateEvent,
)
from jarvis.core.service import BaseService

log = logging.getLogger("jarvis.voice.livekit")

ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = ROOT / ".venv" / "Scripts" / "python.exe"
AGENT_PY = ROOT / ".venv-livekit" / "Scripts" / "python.exe"
AGENT_SCRIPT = ROOT / "jarvis" / "voice" / "livekit_agent.py"

HOST = os.environ.get("LOCAL_VOICE_HOST", "127.0.0.1")
PORT = int(os.environ.get("LOCAL_VOICE_PORT", "8077"))
HEALTH_URL = f"http://{HOST}:{PORT}/health"


class LiveKitVoiceService(BaseService):
    """Поднимает и сопровождает локальный voice API + агента LiveKit."""

    def __init__(self, config: VoiceConfig, bus: EventBus) -> None:
        super().__init__(name="voice-livekit")
        self._cfg = config
        self._bus = bus
        self._shim: subprocess.Popen[bytes] | None = None
        self._agent: subprocess.Popen[bytes] | None = None

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        if not self._prepare():
            self._stop_event.set()
            return
        try:
            self._run()
        except Exception:
            log.exception("живой цикл LiveKit упал")
            self._stop_event.set()

    def _prepare(self) -> bool:
        """Проверить окружение и поднять voice API."""
        for label, path in (("основной venv", MAIN_PY), ("venv LiveKit", AGENT_PY)):
            if not path.exists():
                log.error("%s не найден: %s", label, path)
                return False
        if not AGENT_SCRIPT.exists():
            log.error("нет файла агента: %s", AGENT_SCRIPT)
            return False
        if self._port_busy():
            # Иначе uvicorn молча падает с 10048, а /health отвечает от
            # прошлого экземпляра — и агент годами бьётся в чужой процесс.
            log.error(
                "порт %s уже занят. Похоже, voice API от прошлого запуска ещё "
                "жив: taskkill /FI \"IMAGENAME eq python.exe\" /T, либо смените "
                "LOCAL_VOICE_PORT",
                PORT,
            )
            return False

        if not self._start_shim():
            return False
        if not self._wait_healthy(timeout=60.0):
            log.error("voice API не ответил на /health за 60 c — агент не поднимаю")
            self._kill(self._shim)
            self._shim = None
            return False
        return True

    @staticmethod
    def _port_busy() -> bool:
        import socket

        with socket.socket() as sock:
            sock.settimeout(0.5)
            return sock.connect_ex((HOST, PORT)) == 0

    def _start_shim(self) -> bool:
        env = self._child_env()
        try:
            self._shim = subprocess.Popen(
                [str(MAIN_PY), "-m", "jarvis.voice.local_voice_api"],
                cwd=str(ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            log.error("не запустился voice API: %s", exc)
            return False
        self._pump(self._shim, "voice-api")
        log.info("голос: voice API (Whisper + edge-tts) на %s:%s", HOST, PORT)
        return True

    def _run(self) -> None:
        """Держим агента живым: он и есть голосовой ввод/вывод."""
        backoff = 2.0
        while not self._stop_event.is_set():
            if self._agent is None or self._agent.poll() is not None:
                if self._agent is not None:
                    code = self._agent.returncode
                    log.warning("агент LiveKit завершился (код %s), перезапуск", code)
                    self._agent = None
                    if not self._stop_event.is_set():
                        log.info("перезапуск агента через %.0f c", backoff)
                        if self._stop_event.wait(backoff):
                            return
                        backoff = min(backoff * 2, 30.0)
                if not self._start_agent():
                    return
            self._stop_event.wait(0.5)
        self._shutdown_children()

    def _start_agent(self) -> bool:
        env = self._child_env()
        try:
            self._agent = subprocess.Popen(
                [str(AGENT_PY), str(AGENT_SCRIPT), "console"],
                cwd=str(ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            log.error("не запустился агент LiveKit: %s", exc)
            return False
        self._pump(self._agent, "livekit")
        log.info("голос: агент LiveKit запущен, слушаю микрофон")
        return True

    # ------------------------------------------------------------------
    def _child_env(self) -> dict[str, str]:
        env = os.environ.copy()
        # дочерние процессы наследуют UTF-8, иначе rich/edge-tts падают на кириллице
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env["LOCAL_VOICE_HOST"] = HOST
        env["LOCAL_VOICE_PORT"] = str(PORT)
        env["LOCAL_VOICE_URL"] = f"http://{HOST}:{PORT}/v1"
        env["LLM_MODEL"] = os.environ.get("LLM_MODEL", "llama3.2:3b")
        env["WHISPER_LANGUAGE"] = self._cfg.whisper_language
        return env

    def _pump(self, proc: subprocess.Popen[bytes], prefix: str) -> None:
        """Читать вывод ребёнка в наш лог, иначе труба переполнится и он встанет."""
        stream: IO[bytes] | None = proc.stdout
        if stream is None:
            return

        import threading

        def reader() -> None:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                # Агент и voice API не знают про нашу шину: состояние и реплики
                # приходят маркерами/логами, разбираем их здесь.
                if line.startswith("[[ORB]]"):
                    self._publish_state(line[7:].strip())
                    continue
                if line.startswith("[[SAID]]"):
                    self._publish_speech(line[7:].strip())
                    continue
                if prefix == "voice-api":
                    self._publish_from_shim(line)
                    continue
                log.info("[%s] %s", prefix, line)

        threading.Thread(target=reader, name=f"{prefix}-log", daemon=True).start()

    def _publish_state(self, state: str) -> None:
        if state not in ("idle", "listening", "thinking", "speaking", "initializing"):
            return
        log.info("голос: состояние агента -> %s", state)
        self._bus.publish(VoiceStateEvent(state=state))

    def _publish_speech(self, text: str) -> None:
        if not text:
            return
        self._bus.publish(VoicePartialEvent(text=text, final=True))

    _STT_RE = re.compile(r"STT \([^)]*\):\s*(.*)$")
    _TTS_RE = re.compile(r"TTS \([^)]*\):\s*(.*)$")

    def _publish_from_shim(self, line: str) -> None:
        """Реплики из логов voice API: STT → речь пользователя, TTS → ответ."""
        match = self._STT_RE.search(line)
        if match:
            text = match.group(1).strip()
            if text and text != "<пусто>":
                self._publish_speech(text)
            return
        match = self._TTS_RE.search(line)
        if match:
            text = match.group(1).strip()
            # Первый TTS-запрос LiveKit иногда несёт служебное слово-заглушку
            # ("assistant") — в чате это лишняя реплика.
            if text and text.lower() not in ("assistant", "<пусто>"):
                self._bus.publish(BotReplyEvent(text=text, source="llm"))
            return
        log.info("[voice-api] %s", line)

    def _wait_healthy(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._stop_event.is_set():
                return False
            if self._shim is not None and self._shim.poll() is not None:
                log.error("voice API упал при старте (код %s)", self._shim.returncode)
                return False
            try:
                with urllib.request.urlopen(HEALTH_URL, timeout=2.0) as response:
                    if response.status == 200:
                        payload: Any = json.loads(response.read().decode("utf-8"))
                        log.info(
                            "voice API готов: asr=%s lang=%s tts=%s",
                            payload.get("asr"),
                            payload.get("language"),
                            payload.get("tts_voice"),
                        )
                        return True
            except (urllib.error.URLError, OSError, ValueError):
                pass
            self._stop_event.wait(0.5)
        return False

    # ------------------------------------------------------------------
    def stop(self) -> None:
        log.info("голос: останавливаю агента LiveKit")
        # Сначала флаг остановки + join потока: иначе цикл успевает увидеть
        # убитого агента и залогировать "перезапуск" уже во время shutdown.
        super().stop()
        self._shutdown_children()
        log.info("голос: агент LiveKit остановлен")

    def _shutdown_children(self) -> None:
        for proc in (self._agent, self._shim):
            if proc is not None and proc.poll() is None:
                self._kill(proc)
        self._agent = None
        self._shim = None

    @staticmethod
    def _kill(proc: subprocess.Popen[bytes]) -> None:
        """Убить процесс вместе с детьми: агент сам поднимает job runner."""
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
        else:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


__all__ = ["LiveKitVoiceService"]
