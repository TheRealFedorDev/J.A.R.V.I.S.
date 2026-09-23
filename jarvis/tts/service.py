"""TTS-сервис: потоковая озвучка женскими нейроголосами.

Архитектура (всё потоково, вызывающий поток не блокируется):

    speak(text) ──► очередь ──► worker-поток
                                   │  asyncio-цикл на одну фразу:
                                   │    edge-tts .stream() ─чанки mp3─►
                                   │    IncrementalMp3Decoder ─PCM─► StreamPlayer
                                   │         (decode в отдельном потоке)
                                   ▼
                        при сбоях: pyttsx3 / .NET SAPI5 / WAV-фоллбэк

Движки (`TtsConfig.engine`):
  * edge    — Microsoft neural через edge-tts, СТРИМ (основной, лучшее качество);
  * pyttsx3 — SAPI5, офлайн;
  * net_sapi5 — Windows System.Speech (subprocess);
  * none    — только лог;
  * auto    — edge → pyttsx3 → net_sapi5.

Голоса (см. jarvis.tts.voices): EN en-GB-SoniaNeural, RU ru-RU-SvetlanaNeural —
выбор по языку текста, оба женские и проверены в списке edge-tts 7.x.
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from jarvis.tts.normalize import is_russian, normalize_text
from jarvis.tts.player import StreamPlayer
from jarvis.tts.stream import (
    OUTPUT_CHANNELS,
    OUTPUT_SAMPLERATE,
    IncrementalMp3Decoder,
    decode_to_sink,
    edge_mp3_chunks,
)
from jarvis.tts.voices import DEFAULT_EN_VOICE, DEFAULT_RU_VOICE, select_voice

log = logging.getLogger("jarvis.tts")

_ENGINES = ("auto", "edge", "pyttsx3", "net_sapi5", "none")


@dataclass
class TtsConfig:
    """Настройки озвучки."""

    enabled: bool = True
    engine: str = "auto"                       # auto | edge | pyttsx3 | net_sapi5 | none
    voice: str = DEFAULT_EN_VOICE              # голос для английского
    voice_ru: str = DEFAULT_RU_VOICE           # голос для русского
    rate: int = 0                              # скорость (-100..+100)
    volume: float = 1.0                        # громкость (0..1)
    pitch: int = -10                           # сдвиг тона (лёгкий Ж-эффект)
    normalize: bool = True                     # разворачивать сокращения/единицы

    @classmethod
    def from_env(cls) -> "TtsConfig":
        return cls(
            enabled=os.environ.get("TTS_ENABLED", "true").lower() in ("1", "true", "yes"),
            engine=os.environ.get("TTS_ENGINE", "auto"),
            voice=os.environ.get("TTS_VOICE", DEFAULT_EN_VOICE),
            voice_ru=os.environ.get("TTS_VOICE_RU", DEFAULT_RU_VOICE),
            rate=int(os.environ.get("TTS_RATE", "0")),
            volume=float(os.environ.get("TTS_VOLUME", "1.0")),
            pitch=int(os.environ.get("TTS_PITCH", "-10")),
            normalize=os.environ.get("TTS_NORMALIZE", "true").lower() in ("1", "true", "yes"),
        )


class TextToSpeech:
    """Потоковая озвучка: `speak()` не блокирует вызывающий поток.

    Фразы кладутся в очередь; worker-поток синтезирует их стримом
    (сеть → mp3-чанки → PCM → звук), начиная говорить до конца синтеза.
    """

    def __init__(self, config: TtsConfig) -> None:
        self.config = config
        self._engine: str | None = None
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._cancel = threading.Event()
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._worker: threading.Thread | None = None
        self._player: StreamPlayer | None = None
        self._init()

    # ==================================================================
    # Инициализация движка
    # ==================================================================
    def _init(self) -> None:
        if not self.config.enabled:
            log.info("TTS: выключен")
            return
        if self.config.engine not in _ENGINES:
            log.warning("TTS: неизвестный движок %r — auto", self.config.engine)
        engine = self.config.engine if self.config.engine in _ENGINES else "auto"

        if engine in ("auto", "edge"):
            if self._probe_edge():
                self._engine = "edge"
                log.info(
                    "TTS: edge-tts стрим (EN=%s, RU=%s)",
                    self.config.voice,
                    self.config.voice_ru,
                )
                return
            if engine == "edge":
                log.warning("TTS: edge-tts не установлен")
                return

        if engine in ("auto", "pyttsx3"):
            if self._probe_pyttsx3():
                self._engine = "pyttsx3"
                return
            if engine == "pyttsx3":
                return

        if engine in ("auto", "net_sapi5") and os.name == "nt":
            if self._has_net_sapi5():
                self._engine = "net_sapi5"
                log.info("TTS: .NET SAPI5 (System.Speech)")
                return

        if engine == "none":
            self._engine = None
            log.info("TTS: движок отключён (none)")
            return
        log.info("TTS: движок не найден — ответы только в лог")

    @staticmethod
    def _probe_edge() -> bool:
        try:
            import edge_tts  # noqa: F401
            return True
        except ImportError:
            return False

    def _probe_pyttsx3(self) -> bool:
        try:
            import pyttsx3  # noqa: F401

            log.info("TTS: pyttsx3 доступен — офлайн-фоллбэк")
            return True
        except Exception as exc:
            log.warning("TTS: pyttsx3 недоступен: %s", exc)
            return False

    @staticmethod
    def _has_net_sapi5() -> bool:
        import subprocess

        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Add-Type -AssemblyName System.Speech; "
                    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                    "$s.GetInstalledVoices().VoiceInfo.Name | Out-String",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return bool(result.stdout.strip())
        except Exception:
            return False

    # ==================================================================
    # Язык / голоса / нормализация (совместимость со старым API)
    # ==================================================================
    @staticmethod
    def _is_russian_text(text: str) -> bool:
        return is_russian(text)

    @staticmethod
    def _select_edge_voice(text: str) -> str:
        return select_voice(text)

    @staticmethod
    def _normalize_text(text: str) -> str:
        return normalize_text(text)

    def _pick_voice(self, text: str) -> str:
        return select_voice(
            text, en_voice=self.config.voice, ru_voice=self.config.voice_ru
        )

    @staticmethod
    def _select_voice_pyttsx3(engine) -> None:
        """Выбрать женский системный голос (Zira/Eva/Alana/русские)."""
        try:
            voices = engine.getProperty("voices")
            selected = None
            for v in voices:
                name = v.name.lower()
                if ("russian" in name or "ru" in name) and (
                    "female" in name or "kseniya" in name or "daria" in name
                ):
                    selected = v
                    break
            if not selected:
                for v in voices:
                    name = v.name.lower()
                    if "female" in name or "zira" in name or "eva" in name or "alana" in name:
                        selected = v
                        break
            if selected:
                engine.setProperty("voice", selected.id)
                log.info("TTS pyttsx3: выбран голос %s", selected.name)
        except Exception:
            pass

    # ==================================================================
    # Public API
    # ==================================================================
    def speak(self, text: str) -> None:
        """Поставить фразу в очередь озвучки. НЕ блокирует вызывающий поток."""
        if not self.config.enabled or not text or not text.strip():
            return
        if self.config.normalize:
            text = normalize_text(text)

        if not self._engine:
            log.info("TTS (лог): %s", text[:200])
            return

        self._start_worker()
        self._cancel.clear()
        self._idle.clear()
        self._queue.put(text)

    def speak_sync(self, text: str, timeout: float | None = None) -> None:
        """Озвучить и дождаться очереди (удобно для скриптов/тестов)."""
        self.speak(text)
        if self._engine:
            self.wait(timeout)

    def wait(self, timeout: float | None = None) -> bool:
        """Дождаться, пока очередь опустеет и текущая фраза доиграет."""
        if self._worker is None:
            return True
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._idle.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True

    def stop(self) -> None:
        """Прервать текущую озвучку и очистить очередь (barge-in)."""
        self._cancel.set()
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        player = self._player
        if player is not None:
            player.abort()
        self._idle.set()

    def close(self) -> None:
        """Остановить worker-поток."""
        self._stop.set()
        self._cancel.set()
        self._queue.put(None)
        player = self._player
        if player is not None:
            player.abort()
        if self._worker is not None:
            self._worker.join(timeout=5)
            self._worker = None

    def __enter__(self) -> "TextToSpeech":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ==================================================================
    # Worker-поток
    # ==================================================================
    def _start_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._worker_loop, name="tts-worker", daemon=True
        )
        self._worker.start()

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                self._speak_one(item)
            except Exception:
                log.exception("TTS: сбой озвучки, пробуем фоллбэк")
                try:
                    self._fallback_speak(item)
                except Exception:
                    log.exception("TTS: фоллбэк тоже не удался")
            finally:
                self._queue.task_done()
                if self._queue.empty():
                    self._idle.set()
        self._idle.set()

    def _speak_one(self, text: str) -> None:
        if self._engine == "edge":
            asyncio.run(self._stream_utterance(text))
        elif self._engine == "pyttsx3":
            self._speak_pyttsx3(text)
        elif self._engine == "net_sapi5":
            self._speak_net_sapi5(text)

    # ==================================================================
    # Стрим edge-tts: сеть → mp3 → PCM → звук
    # ==================================================================
    async def _stream_utterance(self, text: str) -> None:
        voice = self._pick_voice(text)
        volume_pct = int((self.config.volume - 1.0) * 100)
        kwargs = dict(
            voice=voice,
            rate=f"{int(self.config.rate):+d}%",
            volume=f"{volume_pct:+d}%",
            pitch=f"{int(self.config.pitch):+d}Hz",
        )
        log.info(
            "TTS: стрим %s (%d симв.)", voice, len(text)
        )

        decoder = IncrementalMp3Decoder()
        player: StreamPlayer | None = None
        wav_path: str | None = None
        sink: Callable[[bytes], None]

        # Пробуем открыть звуковое устройство; нет его — пишем WAV и играем winsound'ом.
        try:
            player = StreamPlayer()
            player.open()
            self._player = player
            sink = player.feed
        except Exception as exc:
            log.warning("TTS: аудиоустройство недоступно (%s) — WAV-фоллбэк", exc)
            player = None
            self._player = None
            wav_path = os.path.join(
                tempfile.gettempdir(), f"jarvis_tts_{os.getpid()}_{int(time.time()*1000)}.wav"
            )
            writer = wave.open(wav_path, "wb")
            writer.setnchannels(OUTPUT_CHANNELS)
            writer.setsampwidth(2)
            writer.setframerate(OUTPUT_SAMPLERATE)
            sink = writer.writeframesraw  # type: ignore[assignment]

        decode_errors: list[BaseException] = []

        def _decode_thread() -> None:
            try:
                decode_to_sink(decoder, sink, cancel=self._cancel)
            except BaseException as exc:  # noqa: BLE001 — пробрасываем наверх через список
                decode_errors.append(exc)
            finally:
                if wav_path is not None:
                    try:
                        writer.close()
                    except Exception:
                        pass

        t = threading.Thread(target=_decode_thread, name="tts-decode", daemon=True)
        t.start()
        try:
            async for chunk in edge_mp3_chunks(text, cancel=self._cancel, **kwargs):
                decoder.feed(chunk)
        finally:
            decoder.eof()

        t.join(timeout=60)
        if t.is_alive():
            decoder.cancel()
            t.join(timeout=5)
            raise RuntimeError("TTS: декодер завис")

        if decode_errors:
            raise decode_errors[0]
        if self._cancel.is_set():
            if player is not None:
                player.abort()
            return

        if player is not None:
            player.signal_drain()
            player.wait_done(timeout=120)
            player.close()
            self._player = None
        elif wav_path is not None:
            self._play_wav_file(wav_path)
        log.info("TTS: стрим завершён")

    # ==================================================================
    # Фоллбэки
    # ==================================================================
    def _fallback_speak(self, text: str) -> None:
        if self._engine == "edge":
            # основной движок сломался на этой фразе — пробуем офлайн
            if self._probe_pyttsx3():
                self._speak_pyttsx3(text)
            elif os.name == "nt" and self._has_net_sapi5():
                self._speak_net_sapi5(text)
            return
        # офиный движок упал — последний шанс: edge (если вдруг доступен)
        if self._engine != "edge" and self._probe_edge():
            try:
                asyncio.run(self._stream_utterance(text))
            except Exception:
                log.exception("TTS: edge-фоллбэк не удался")

    def _speak_pyttsx3(self, text: str) -> None:
        try:
            import pyttsx3

            engine = pyttsx3.init()
            self._select_voice_pyttsx3(engine)
            engine.say(text)
            engine.runAndWait()
        except Exception as exc:
            log.warning("TTS pyttsx3: %s", exc)
            raise

    def _speak_net_sapi5(self, text: str) -> None:
        import subprocess

        safe_text = text.replace("'", "''")
        ps_cmd = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$voices = $s.GetInstalledVoices(); "
            "$female = $voices | Where-Object { $_.VoiceInfo.Name -match 'Zira|Eva|Alana|Katyusha|Daria' }; "
            "if ($female) { $s.SelectVoice($female.VoiceInfo.Name) }; "
            "$s.Rate = 0; "
            f"$s.Speak('{safe_text}')"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        except Exception as exc:
            log.warning("TTS net SAPI5: %s", exc)
            raise

    @staticmethod
    def _play_wav_file(path: str) -> None:
        """Проиграть WAV, когда sounddevice недоступен (win-фоллбэк)."""
        log.info("TTS: WAV-фоллбэк: %s", path)
        if os.name == "nt":
            try:
                import winsound

                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_SYNC)
                os.remove(path)
                return
            except Exception as exc:
                log.debug("winsound не сработал: %s", exc)
        try:
            import subprocess

            safe_path = path.replace("'", "''")
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    f"$p = New-Object System.Media.SoundPlayer '{safe_path}'; $p.PlaySync()",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
        except Exception as exc:
            log.warning("TTS WAV-фоллбэк не удался: %s", exc)
        finally:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass
