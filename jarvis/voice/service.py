"""Голосовое управление: микрофон → vosk (оффлайн) → команды в шину.

Сервис в отдельном потоке слушает микрофон (через sounddevice) и кормит
распознаватель vosk'а (модель small-ru). Как только декодер выдаёт готовую
фразу (final result), текст разбирается грамматикой `commands` и публикуется
событием `VoiceCommandEvent` — подписчики (курсор, калибровка) выполняют
действие. Голос — командный канал (дискретные действия), не курсор.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from jarvis.core.config import VoiceConfig
from jarvis.core.events import EventBus, VoiceCommandEvent
from jarvis.core.service import BaseService
from jarvis.voice.commands import parse_command

log = logging.getLogger("jarvis.voice")


class VoiceService(BaseService):
    """Поток распознавания голосовых команд."""

    def __init__(self, config: VoiceConfig, bus: EventBus) -> None:
        super().__init__(name="voice")
        self._cfg = config
        self._bus = bus

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        import sounddevice as sd
        from vosk import KaldiRecognizer, Model

        model_path = self._cfg.model_path
        if not Path(model_path).exists():
            log.error("модель vosk не найдена: %s", model_path)
            return
        model = Model(str(model_path))
        sr = int(self._cfg.samplerate)
        rec = KaldiRecognizer(model, sr)
        rec.SetWords(False)
        log.info("голос: vosk готов (модель %s)", Path(model_path).name)

        chunk = int(sr * max(self._cfg.block_seconds, 0.1))
        with sd.InputStream(
            samplerate=sr,
            channels=1,
            dtype="int16",
            blocksize=chunk,
            device=self._cfg.device or None,
        ) as stream:
            while not self._stop_event.is_set():
                data, overflowed = stream.read(chunk)
                if overflowed:
                    continue
                if rec.AcceptWaveform(data.tobytes()):
                    res = json.loads(rec.Result())
                    text = (res.get("text") or "").strip()
                    if text:
                        cmd = parse_command(text)
                        self._bus.publish(VoiceCommandEvent(
                            action=cmd.action if cmd else None,
                            args=cmd.args if cmd else {},
                            text=text,
                        ))
                        if cmd:
                            log.info("голос: %s | %s", text, cmd.action)
                    elif time.monotonic() % 5 > 4.5:
                        log.debug("голос: тишина/шум, команда не распознана")