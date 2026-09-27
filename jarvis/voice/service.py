"""Голосовое управление — стриминговый конвейер (faster-whisper / vosk).

Три независимых потока, соединённых очередями (захват никогда не ждёт
декодера, декодер не ждёт медленных подписчиков шины):

    [PortAudio callback] --буферы 0.25 c--> [декодер STT]
                                                │  partial → VoicePartialEvent  (на лету)
                                                │  final   → parse_command
                                                ▼
                                         [диспетчер команд]
                                                ▼
                                         EventBus (VoiceCommandEvent)

По умолчанию faster-whisper large-v3:
- энергетический VAD режет речь на фразы;
- пока человек говорит, каждые partial_seconds буфер перетранскрибируется
  (beam=1) → partial-событие в UI;
- тишина silence_end сек → финальный проход (beam=5) → команда.
Фоллбэк: VOICE_ASR=vosk или сбой загрузки whisper → прежний vosk-путь.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from pathlib import Path

from jarvis.core.config import VoiceConfig
from jarvis.core.events import EventBus, VoiceCommandEvent, VoicePartialEvent
from jarvis.core.service import BaseService
from jarvis.voice.commands import parse_command

log = logging.getLogger("jarvis.voice")

_QUEUE_MAX = 64          # ~16 секунд буфера при блоке 0.25 с
_DISPATCH_SENTINEL = object()


class VoiceService(BaseService):
    """Поток распознавания голосовых команд (захват → очередь → STT → шина)."""

    def __init__(self, config: VoiceConfig, bus: EventBus) -> None:
        super().__init__(name="voice")
        self._cfg = config
        self._bus = bus
        self._audio_q: queue.Queue[bytes | None] = queue.Queue(maxsize=_QUEUE_MAX)
        self._cmd_q: queue.Queue = queue.Queue()
        self._last_partial = ""

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        import sounddevice as sd

        sr = int(self._cfg.samplerate)
        chunk = int(sr * max(self._cfg.block_seconds, 0.1))
        asr = (self._cfg.asr or "faster-whisper").lower()

        decoder = None
        if asr == "faster-whisper":
            try:
                decoder = self._open_whisper()
            except Exception as exc:
                log.warning("faster-whisper недоступен (%s) — пробуем vosk", exc)
        if decoder is None:
            decoder = self._open_vosk()
            if decoder is None:
                log.error("STT недоступен: ни whisper, ни vosk")
                return

        def _on_audio(indata, frames, time_info, status) -> None:  # noqa: ANN001
            """Callback PortAudio: захват НЕ ждёт декодера (никогда не блокируется)."""
            data = bytes(indata)
            try:
                self._audio_q.put_nowait(data)
            except queue.Full:
                try:
                    self._audio_q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._audio_q.put_nowait(data)
                except queue.Full:
                    pass

        dispatcher = threading.Thread(
            target=self._dispatch_loop, name="voice-cmd", daemon=True
        )
        dispatcher.start()

        try:
            with sd.InputStream(
                samplerate=sr,
                channels=1,
                dtype="int16",
                blocksize=chunk,
                device=self._cfg.device or None,
                callback=_on_audio,
            ):
                log.info(
                    "голос: захват запущен (%s, callback, блок %.2f c)",
                    decoder.name, chunk / sr,
                )
                while not self._stop_event.is_set():
                    try:
                        data = self._audio_q.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    if data is None:
                        break
                    decoder.feed(data)
        finally:
            self._cmd_q.put(_DISPATCH_SENTINEL)
            dispatcher.join(timeout=2)

    # ------------------------------------------------------------------
    # faster-whisper: стриминг по фразам + rolling partial
    # ------------------------------------------------------------------
    def _open_whisper(self):
        from jarvis.voice.whisper_stream import WhisperStreamDecoder

        cfg = self._cfg
        dec = WhisperStreamDecoder(
            model_name=cfg.whisper_model,
            device=cfg.whisper_device,
            compute_type=cfg.whisper_compute_type,
            language=cfg.whisper_language or None,
            samplerate=int(cfg.samplerate),
            block_seconds=float(cfg.block_seconds),
            vad_threshold=float(cfg.vad_threshold),
            silence_end=float(cfg.silence_end),
            partial_seconds=float(cfg.partial_seconds),
            on_partial=lambda t: self._publish_partial(t, final=False),
            on_final=self._on_final,
        )
        dec.load()
        return dec

    def _open_vosk(self):
        from jarvis.voice.vosk_stream import VoskStreamDecoder

        model_path = self._cfg.model_path
        if not Path(model_path).exists():
            log.error("модель vosk не найдена: %s", model_path)
            return None
        try:
            return VoskStreamDecoder(
                model_path=str(model_path),
                samplerate=int(self._cfg.samplerate),
                on_partial=lambda t: self._publish_partial(t, final=False),
                on_final=self._on_final,
            )
        except Exception as exc:
            log.error("vosk не загрузился: %s", exc)
            return None

    def _on_final(self, text: str) -> None:
        """Финальная фраза: показать → распарсить → положить команду в очередь."""
        if not text:
            self._last_partial = ""
            return
        self._publish_partial(text, final=True)
        cmd = parse_command(text)
        self._cmd_q.put(
            VoiceCommandEvent(
                action=cmd.action if cmd else None,
                args=cmd.args if cmd else {},
                text=text,
            )
        )
        if cmd:
            log.info("голос: %s | %s", text, cmd.action)
        else:
            log.debug("голос: фраза без команды: %s", text)
        self._last_partial = ""

    def _publish_partial(self, text: str, *, final: bool) -> None:
        """Частичные/финальный текст — сразу в шину (стриминг для UI)."""
        try:
            self._bus.publish(VoicePartialEvent(text=text, final=final))
        except Exception:
            log.exception("голос: не удалось опубликовать partial")

    # ------------------------------------------------------------------
    def _dispatch_loop(self) -> None:
        """Отдельный поток для команд: медленные подписчики не тормозят STT."""
        while True:
            item = self._cmd_q.get()
            if item is _DISPATCH_SENTINEL:
                return
            try:
                self._bus.publish(item)
            except Exception:
                log.exception("голос: подписчик команды упал")


__all__ = ["VoiceService"]
