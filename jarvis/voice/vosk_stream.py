"""Фоллбэк-декодер vosk: те же partial/final коллбэки, что у whisper."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

log = logging.getLogger("jarvis.voice.vosk")


class VoskStreamDecoder:
    """Потоковый vosk: feed(bytes) → on_partial / on_final."""

    def __init__(
        self,
        *,
        model_path: str,
        samplerate: int = 16000,
        on_partial: Callable[[str], None],
        on_final: Callable[[str], None],
    ) -> None:
        from vosk import KaldiRecognizer, Model

        if not Path(model_path).exists():
            raise FileNotFoundError(model_path)
        self._model = Model(model_path)
        self._rec = KaldiRecognizer(self._model, int(samplerate))
        self._rec.SetWords(False)
        self._on_partial = on_partial
        self._on_final = on_final
        self._last = ""

    @property
    def name(self) -> str:
        return "vosk"

    def feed(self, data: bytes) -> None:
        if self._rec.AcceptWaveform(data):
            res = json.loads(self._rec.Result())
            text = (res.get("text") or "").strip()
            if text:
                self._on_final(text)
            self._last = ""
        else:
            partial = (json.loads(self._rec.PartialResult()).get("partial") or "").strip()
            if partial and partial != self._last:
                self._last = partial
                self._on_partial(partial)
