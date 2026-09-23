"""Голосовое управление — стриминговый конвейер.

Три независимых потока, соединённых очередями (захват никогда не ждёт
декодера, декодер не ждёт медленных подписчиков шины):

    [PortAudio callback] --буферы 0.25 c--> [декодер vosk]
                                                │  partial → VoicePartialEvent  (на лету)
                                                │  final   → parse_command
                                                ▼
                                         [диспетчер команд]
                                                ▼
                                         EventBus (VoiceCommandEvent)

Частичные результаты публикуются сразу, пока человек ещё говорит — UI
видит текст без ожидания конца фразы.
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
    """Поток распознавания голосовых команд (захват → очередь → vosk → шина)."""

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

        def _on_audio(indata, frames, time_info, status) -> None:  # noqa: ANN001
            """Callback PortAudio: захват НЕ ждёт декодера (никогда не блокируется)."""
            data = bytes(indata)
            try:
                self._audio_q.put_nowait(data)
            except queue.Full:
                # дренируем самое старое — распознаём актуальное «здесь и сейчас»
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
                log.info("голос: потоковый захват запущен (callback, блок %.2f c)", chunk / sr)
                while not self._stop_event.is_set():
                    try:
                        data = self._audio_q.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    if data is None:
                        break
                    self._decode(rec, data)
        finally:
            self._cmd_q.put(_DISPATCH_SENTINEL)
            dispatcher.join(timeout=2)

    # ------------------------------------------------------------------
    def _decode(self, rec, data: bytes) -> None:
        """Один блок аудио → частичный или финальный результат."""
        if rec.AcceptWaveform(data):
            res = json.loads(rec.Result())
            text = (res.get("text") or "").strip()
            if text:
                # финал: показать полную фразу, затем уйти в команду
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
        else:
            partial = (json.loads(rec.PartialResult()).get("partial") or "").strip()
            if partial and partial != self._last_partial:
                self._last_partial = partial
                self._publish_partial(partial, final=False)

    def _publish_partial(self, text: str, *, final: bool) -> None:
        """Частичные/финальный текст — сразу в шину (стриминг для UI)."""
        try:
            self._bus.publish(VoicePartialEvent(text=text, final=final))
        except Exception:
            log.exception("голос: не удалось опубликовать partial")

    # ------------------------------------------------------------------
    def _dispatch_loop(self) -> None:
        """Отдельный поток для команд: медленные подписчики не тормозят vosk."""
        while True:
            item = self._cmd_q.get()
            if item is _DISPATCH_SENTINEL:
                return
            try:
                self._bus.publish(item)
            except Exception:
                log.exception("голос: подписчик команды упал")
