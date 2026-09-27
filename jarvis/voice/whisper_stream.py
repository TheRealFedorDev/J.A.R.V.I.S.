"""Стриминговый декодер faster-whisper: VAD-фразы + rolling partial."""

from __future__ import annotations

import logging
import time
from typing import Callable

import numpy as np

from jarvis.voice.stt import is_voiced, join_segments, to_float32

log = logging.getLogger("jarvis.voice.whisper")


class WhisperStreamDecoder:
    """Накапливает речь, режет по тишине, публикует partial/final.

    partial: каждые `partial_seconds` речи — быстрый проход (beam=1)
    по текущему буферу; final: тишина `silence_end` сек — полный проход.
    """

    def __init__(
        self,
        *,
        model_name: str = "medium",
        device: str = "auto",
        compute_type: str = "default",
        language: str | None = "ru",
        samplerate: int = 16000,
        block_seconds: float = 0.25,
        vad_threshold: float = 0.012,
        silence_end: float = 0.55,
        partial_seconds: float = 0.85,
        on_partial: Callable[[str], None],
        on_final: Callable[[str], None],
    ) -> None:
        self._model_name = model_name
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._sr = int(samplerate)
        self._block = float(block_seconds)
        self._vad = float(vad_threshold)
        self._silence_end = float(silence_end)
        self._partial_every = float(partial_seconds)
        self._on_partial = on_partial
        self._on_final = on_final

        self._model = None
        self._buf: list[np.ndarray] = []
        self._speech = False
        self._silence = 0.0
        self._speech_len = 0.0
        self._next_partial = 0.0
        self._last_partial_text = ""

    @property
    def name(self) -> str:
        return f"faster-whisper:{self._model_name}"

    def load(self) -> None:
        from faster_whisper import WhisperModel

        device = self._device
        ct = self._compute_type
        if ct == "default":
            ct = "float16" if device == "cuda" else "int8"
        t0 = time.monotonic()
        self._model = WhisperModel(
            self._model_name,
            device=device if device and device != "auto" else "auto",
            compute_type=ct,
            cpu_threads=0,  # auto
        )
        log.info(
            "голос: faster-whisper %s готов за %.1f c (device=%s, ct=%s)",
            self._model_name, time.monotonic() - t0, device, ct,
        )

    # ------------------------------------------------------------------
    def feed(self, data: bytes) -> None:
        pcm = to_float32(data)
        voiced = is_voiced(pcm, self._vad)

        if voiced:
            if not self._speech:
                self._speech = True
                self._buf = []
                self._speech_len = 0.0
                self._silence = 0.0
                self._next_partial = self._partial_every
                self._last_partial_text = ""
                log.debug("голос: начало фразы")
            self._buf.append(pcm)
            self._speech_len += self._block
            self._silence = 0.0
            if self._speech_len >= self._next_partial:
                self._next_partial += self._partial_every
                self._emit_partial()
            return

        if not self._speech:
            return

        # тишина внутри/после фразы
        self._buf.append(pcm)
        self._silence += self._block
        if self._silence >= self._silence_end:
            self._finalize()

    # ------------------------------------------------------------------
    def _audio(self) -> np.ndarray | None:
        if not self._buf:
            return None
        return np.concatenate(self._buf)

    def _emit_partial(self) -> None:
        audio = self._audio()
        if audio is None or audio.size < int(self._sr * 0.3):
            return
        try:
            text = self._transcribe(audio, fast=True)
        except Exception:
            log.exception("голос: partial-распознавание упало")
            return
        if text and text != self._last_partial_text:
            self._last_partial_text = text
            try:
                self._on_partial(text)
            except Exception:
                log.exception("голос: on_partial упал")

    def _finalize(self) -> None:
        audio = self._audio()
        self._reset_utterance()
        if audio is None:
            return
        # хвост тишины не нужен whisper'у — обрезаем по голосу примерно
        try:
            text = self._transcribe(audio, fast=False)
        except Exception:
            log.exception("голос: финальное распознавание упало")
            return
        if text:
            try:
                self._on_final(text)
            except Exception:
                log.exception("голос: on_final упал")

    def _reset_utterance(self) -> None:
        self._speech = False
        self._buf = []
        self._silence = 0.0
        self._speech_len = 0.0
        self._next_partial = 0.0
        self._last_partial_text = ""

    def _transcribe(self, audio: np.ndarray, *, fast: bool) -> str:
        if self._model is None:
            return ""
        segments, _info = self._model.transcribe(
            audio,
            language=self._language,
            beam_size=1 if fast else 5,
            best_of=1 if fast else 5,
            condition_on_previous_text=False,
            vad_filter=False,
            without_timestamps=True,
            temperature=0.0,
        )
        return join_segments(segments)
