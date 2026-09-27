"""Кастомный edge-tts сервис для pipecat (паттерн TTSService, BSD-2).

Стрим: edge-tts mp3-чанки → IncrementalMp3Decoder → TTSAudioRawFrame (PCM mono).
Женские голоса и pitch/rate — из jarvis.tts (TtsConfig / voices).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncGenerator
from typing import Any

from jarvis.tts.normalize import normalize_text
from jarvis.tts.service import TtsConfig
from jarvis.tts.stream import IncrementalMp3Decoder, edge_mp3_chunks
from jarvis.tts.voices import select_voice

log = logging.getLogger("jarvis.tts.edge")

_SENTINEL = object()
_BASE: type | None = None


def _ttsservice_base() -> type:
    global _BASE
    if _BASE is None:
        from pipecat.services.tts_service import TTSService

        _BASE = TTSService
    return _BASE


def create_edge_tts_service(
    *,
    tts_config: TtsConfig | None = None,
    sample_rate: int = 24000,
    **kwargs: Any,
) -> Any:
    """Создать EdgeTTSService с наследованием от pipecat TTSService.

    Ленивый импорт pipecat: jarvis.tts остаётся импортируемым без pipecat.
    """
    base = _ttsservice_base()

    class EdgeTTSService(base):  # type: ignore[misc,valid-type]
        def __init__(
            self,
            *,
            tts_config: TtsConfig | None = None,
            sample_rate: int = 24000,
            **init_kwargs: Any,
        ) -> None:
            from pipecat.services.settings import TTSSettings

            self._cfg = tts_config or TtsConfig.from_env()
            self._decoder_sr = int(sample_rate)
            # store-mode: edge-tts без model; voice/lang выбираются на лету в run_tts
            settings = init_kwargs.pop("settings", None) or TTSSettings(
                model=None,
                voice=self._cfg.voice,
                language=None,
            )
            super().__init__(
                sample_rate=sample_rate,
                settings=settings,
                **init_kwargs,
            )

        async def run_tts(
            self, text: str, context_id: str
        ) -> AsyncGenerator[Any, None]:
            from pipecat.frames.frames import TTSAudioRawFrame

            if not text or not text.strip():
                return
            if self._cfg.normalize:
                text = normalize_text(text)

            voice = select_voice(
                text, en_voice=self._cfg.voice, ru_voice=self._cfg.voice_ru
            )
            volume_pct = int((self._cfg.volume - 1.0) * 100)
            edge_kwargs = dict(
                voice=voice,
                rate=f"{int(self._cfg.rate):+d}%",
                volume=f"{volume_pct:+d}%",
                pitch=f"{int(self._cfg.pitch):+d}Hz",
            )

            decoder = IncrementalMp3Decoder(
                samplerate=self._decoder_sr, layout="mono"
            )
            loop = asyncio.get_running_loop()
            pcm_q: asyncio.Queue = asyncio.Queue()
            errors: list[BaseException] = []

            def _drain() -> None:
                try:
                    for pcm in decoder.decode():
                        asyncio.run_coroutine_threadsafe(pcm_q.put(pcm), loop)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
                finally:
                    asyncio.run_coroutine_threadsafe(
                        pcm_q.put(_SENTINEL), loop
                    )

            thread = threading.Thread(
                target=_drain, name="edge-tts-decode", daemon=True
            )
            thread.start()

            first = True
            try:
                async for chunk in edge_mp3_chunks(text, **edge_kwargs):
                    if first:
                        await self.stop_ttfb_metrics()
                        first = False
                    decoder.feed(chunk)
            finally:
                decoder.eof()

            while True:
                item = await pcm_q.get()
                if item is _SENTINEL:
                    break
                yield TTSAudioRawFrame(
                    item, self._decoder_sr, 1, context_id=context_id
                )

            thread.join(timeout=10)
            if errors:
                raise errors[0]
            log.debug("edge-tts: %s → %d симв.", voice, len(text))

    return EdgeTTSService(tts_config=tts_config, sample_rate=sample_rate, **kwargs)


__all__ = ["create_edge_tts_service"]
