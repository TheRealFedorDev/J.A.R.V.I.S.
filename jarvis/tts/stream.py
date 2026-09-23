"""Стриминговый синтез: edge-tts → поток mp3-чанков → инкрементальный PCM.

Вместо «сгенерировать весь файл → потом проигрывать»:
  * `edge_mp3_chunks()` — async-генератор, отдаёт сырые mp3-куски по мере
    поступления от Azure (первые байты приходят за ~0.3 с);
  * `IncrementalMp3Decoder` — кросс-поточный декодер mp3 → PCM s16
    (48 кГц, стерео): producer заливает чанки (`feed`), consumer крутит
    `decode()` в своём потоке и пишет PCM прямо в аудио-плеер.

Сетевой поток и декодирование идут параллельно — речь начинается до того,
как фраза досинтезирована.
"""

from __future__ import annotations

import io
import logging
import threading
from typing import AsyncIterator, Callable, Iterator

import numpy as np

log = logging.getLogger("jarvis.tts.stream")

# Параметры PCM на выходе декодера (удобный универсальный формат для sounddevice).
OUTPUT_LAYOUT = "stereo"
OUTPUT_SAMPLERATE = 48000
OUTPUT_DTYPE = "int16"
BYTES_PER_SAMPLE = 2
OUTPUT_CHANNELS = {"mono": 1, "stereo": 2}[OUTPUT_LAYOUT]
FRAME_BYTES = OUTPUT_CHANNELS * BYTES_PER_SAMPLE


async def edge_mp3_chunks(
    text: str,
    *,
    voice: str,
    rate: str = "+0%",
    volume: str = "+0%",
    pitch: str = "+0Hz",
    cancel: threading.Event | None = None,
) -> AsyncIterator[bytes]:
    """Стрим сырых mp3-чанков от edge-tts (одна async-итерация = один чанк)."""
    import edge_tts

    comm = edge_tts.Communicate(
        text,
        voice,
        rate=rate,
        volume=volume,
        pitch=pitch,
        boundary="WordBoundary",
    )
    async for chunk in comm.stream():
        if cancel is not None and cancel.is_set():
            return
        if chunk.get("type") == "audio":
            data = chunk.get("data")
            if data:
                yield data


class _GrowingReader(io.RawIOBase):
    """Файловый объект растущего буфера для PyAV.

    producer (asyncio-поток) вызывает `feed()`/`close_stream()`;
    demuxer PyAV блокируется в `readinto()`, пока не появятся данные или EOF.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._eof = False
        self._cv = threading.Condition()

    def feed(self, data: bytes) -> None:
        with self._cv:
            self._buf += data
            self._cv.notify_all()

    def close_stream(self) -> None:
        with self._cv:
            self._eof = True
            self._cv.notify_all()

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def readinto(self, b) -> int:  # type: ignore[override]
        view = memoryview(b)
        with self._cv:
            while not self._buf and not self._eof:
                self._cv.wait(timeout=0.5)
            if not self._buf:
                return 0  # EOF для demuxer'а
            n = min(len(view), len(self._buf))
            view[:n] = self._buf[:n]
            del self._buf[:n]
            return n


def pcm_bytes(frame) -> bytes:
    """AudioFrame (s16) → interleaved PCM-байты для sounddevice."""
    arr = frame.to_ndarray()
    if arr.dtype != np.int16:
        arr = arr.astype(np.int16)
    if arr.ndim == 2 and arr.shape[0] > 1:
        arr = arr.T  # planar (каналы, семплы) → interleaved (семплы, каналы)
    return np.ascontiguousarray(arr).tobytes()


class IncrementalMp3Decoder:
    """Кросс-поточный декодер: mp3-чанки на входе, PCM s16 на выходе.

    Потокобезопасен: `feed()`/`eof()` из одного потока, `decode()` — из другого.
    Декодер блокируется в ожидании данных (это и есть backpressure), но
    producer при этом продолжает заливать — пока не запомнит всю фразу
    (при 48 кбит/с речь весит ~6 КБ/с — буфер копеечный).
    """

    def __init__(
        self,
        samplerate: int = OUTPUT_SAMPLERATE,
        layout: str = OUTPUT_LAYOUT,
    ) -> None:
        self._reader = _GrowingReader()
        self._samplerate = samplerate
        self._layout = layout

    def feed(self, data: bytes) -> None:
        self._reader.feed(data)

    def eof(self) -> None:
        self._reader.close_stream()

    def cancel(self) -> None:
        """Прервать декодирование на середине (демuxer увидит EOF)."""
        self._reader.close_stream()

    def decode(self) -> Iterator[bytes]:
        """PCM-чанки; завершается после eof() и вычитки всех буферов."""
        import av

        container = av.open(self._reader, format="mp3")
        try:
            astream = container.streams.audio[0]
            resampler = av.AudioResampler(
                format="s16", layout=self._layout, rate=self._samplerate
            )
            for frame in container.decode(astream):
                for out in resampler.resample(frame):
                    yield pcm_bytes(out)
            for out in resampler.resample(None):  # сброс хвоста ресемплера
                yield pcm_bytes(out)
        finally:
            container.close()


def decode_to_sink(
    decoder: IncrementalMp3Decoder,
    sink: Callable[[bytes], None],
    cancel: threading.Event | None = None,
) -> None:
    """Крутит `decoder.decode()` и скармливает PCM в `sink` (для decode-потока)."""
    for pcm in decoder.decode():
        if cancel is not None and cancel.is_set():
            break
        sink(pcm)
