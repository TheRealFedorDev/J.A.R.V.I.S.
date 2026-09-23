"""Потоковое воспроизведение PCM через sounddevice.

`StreamPlayer` — буфер + callback PortAudio:
  * `feed(pcm)` можно звать из любого потока, пока идёт синтез;
  * устройство открыто сразу, до первых данных callback отдаёт тишину —
    речь начинается сразу, как пришёл первый декодированный чанк;
  * если сеть отстаёт — callback дорисовывает тишину (не обрывает поток);
  * `signal_drain()` + `wait_done()` — дождаться, пока всё доиграет;
  * `abort()` — мгновенная остановка (barge-in / stop()).
"""

from __future__ import annotations

import logging
import threading

import numpy as np

from jarvis.tts.stream import BYTES_PER_SAMPLE, OUTPUT_DTYPE, OUTPUT_SAMPLERATE

log = logging.getLogger("jarvis.tts.player")


class StreamPlayer:
    """Callback-плеер PCM s16 с буфером и прероллом."""

    def __init__(
        self,
        samplerate: int = OUTPUT_SAMPLERATE,
        channels: int = 2,
        blocksize: int = 1024,
    ) -> None:
        self._samplerate = samplerate
        self._channels = channels
        self._frame_bytes = channels * BYTES_PER_SAMPLE
        self._blocksize = blocksize

        self._lock = threading.Lock()
        self._buf = bytearray()
        self._stream = None
        self._started = False
        self._opened = False
        self._drain = False
        self._aborted = False
        self._done = threading.Event()

    # ------------------------------------------------------------------
    # Устройство
    # ------------------------------------------------------------------
    def open(self) -> None:
        """Открыть выходное устройство. Бросает исключение, если его нет."""
        import sounddevice as sd

        self._stream = sd.OutputStream(
            samplerate=self._samplerate,
            channels=self._channels,
            dtype=OUTPUT_DTYPE,
            blocksize=self._blocksize,
            latency="low",
            callback=self._callback,
        )
        self._stream.start()  # PortAudio-поток идёт; callback кормит тишиной до данных
        self._opened = True
        self._started = True
        log.debug(
            "player: открыт выход %d Гц, %d кан., block=%d",
            self._samplerate,
            self._channels,
            self._blocksize,
        )

    @property
    def opened(self) -> bool:
        return self._opened

    # ------------------------------------------------------------------
    # Приём PCM
    # ------------------------------------------------------------------
    def feed(self, pcm: bytes) -> None:
        """Положить PCM в буфер (вызывается из decode-потока)."""
        if not pcm:
            return
        with self._lock:
            if self._aborted:
                return
            self._buf += pcm

    def signal_drain(self) -> None:
        """Все данные отданы — доигрывать до конца."""
        with self._lock:
            self._drain = True
            if not self._buf:
                self._done.set()

    def wait_done(self, timeout: float | None = None) -> bool:
        """Дождаться окончания воспроизведения. True — всё доиграно/нечего играть."""
        if not self._opened:
            return True
        return self._done.wait(timeout)

    def abort(self) -> None:
        """Мгновенно сбросить буфер и остановить звук."""
        with self._lock:
            self._aborted = True
            self._buf.clear()
            self._done.set()
        stream = self._stream
        if stream is not None:
            try:
                stream.abort()
            except Exception as exc:
                log.debug("player: abort failed: %s", exc)

    def close(self) -> None:
        """Дождаться (по возможности), закрыть устройство."""
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            if not self._done.is_set():
                stream.stop()  # Pa_StopStream доигрывает буферы
        except Exception:
            try:
                stream.abort()
            except Exception:
                pass
        try:
            stream.close()
        except Exception as exc:
            log.debug("player: close failed: %s", exc)
        self._opened = False

    # ------------------------------------------------------------------
    # Callback PortAudio (отдельный поток аудиодрайвера)
    # ------------------------------------------------------------------
    def _callback(self, outdata, frames, time_info, status) -> None:  # noqa: ANN001
        need = frames * self._frame_bytes
        with self._lock:
            buf = self._buf
            avail = (len(buf) // self._frame_bytes) * self._frame_bytes  # выравнивание по кадру
            n = min(need, avail)
            chunk = bytes(buf[:n])
            del buf[:n]
            if self._drain and not buf:
                self._done.set()

        if chunk:
            arr = np.frombuffer(chunk, dtype=np.int16).reshape(-1, self._channels)
            outdata[: arr.shape[0]] = arr
            if arr.shape[0] < frames:
                outdata[arr.shape[0] :] = 0
        else:
            outdata[:] = 0  # перерыв сети/хвост — тишина, не обрыв
        if status:
            log.debug("player: status %s", status)
