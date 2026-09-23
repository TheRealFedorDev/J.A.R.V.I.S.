"""Тесты TTS: конфиг, голоса, нормализация, стриминг, декодер, очередь."""

from __future__ import annotations

import asyncio
import io
import threading
import time

import numpy as np

from jarvis.tts import (
    DEFAULT_EN_VOICE,
    DEFAULT_RU_VOICE,
    IncrementalMp3Decoder,
    StreamPlayer,
    TtsConfig,
    TextToSpeech,
    edge_mp3_chunks,
    is_russian,
    normalize_text,
    select_voice,
)


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------
def test_tts_config_defaults():
    cfg = TtsConfig()
    assert cfg.enabled is True
    assert cfg.engine == "auto"
    assert cfg.voice == "en-GB-SoniaNeural"
    assert cfg.voice_ru == "ru-RU-SvetlanaNeural"
    assert cfg.normalize is True
    assert cfg.voice == DEFAULT_EN_VOICE
    assert cfg.voice_ru == DEFAULT_RU_VOICE


def test_tts_config_from_env(monkeypatch):
    monkeypatch.setenv("TTS_ENABLED", "false")
    monkeypatch.setenv("TTS_ENGINE", "pyttsx3")
    monkeypatch.setenv("TTS_VOICE", "test-voice")
    monkeypatch.setenv("TTS_VOICE_RU", "test-voice-ru")
    monkeypatch.setenv("TTS_RATE", "10")
    monkeypatch.setenv("TTS_VOLUME", "0.8")
    monkeypatch.setenv("TTS_NORMALIZE", "false")
    cfg = TtsConfig.from_env()
    assert cfg.enabled is False
    assert cfg.engine == "pyttsx3"
    assert cfg.voice == "test-voice"
    assert cfg.voice_ru == "test-voice-ru"
    assert cfg.rate == 10
    assert cfg.volume == 0.8
    assert cfg.normalize is False


# ---------------------------------------------------------------------------
# Голоса / язык
# ---------------------------------------------------------------------------
def test_tts_voice_auto_selection():
    """Русский текст → Svetlana, английский → Sonia (женские, живые голоса)."""
    assert select_voice("Кто такой Петр Первый?") == "ru-RU-SvetlanaNeural"
    assert select_voice("Hello world") == "en-GB-SoniaNeural"
    assert TextToSpeech._select_edge_voice("Привет") == "ru-RU-SvetlanaNeural"
    assert TextToSpeech._select_edge_voice("JARVIS status: active") == "en-GB-SoniaNeural"


def test_tts_voice_selection_respects_config():
    tts = TextToSpeech.__new__(TextToSpeech)
    tts.config = TtsConfig(voice="en-US-AriaNeural", voice_ru="ru-RU-DmitryNeural")
    assert tts._pick_voice("Привет") == "ru-RU-DmitryNeural"
    assert tts._pick_voice("Hello") == "en-US-AriaNeural"


def test_is_russian():
    assert is_russian("привет мир") is True
    assert is_russian("hello world") is False
    assert is_russian("") is False
    assert is_russian("это русский текст") is True


# ---------------------------------------------------------------------------
# Нормализация
# ---------------------------------------------------------------------------
def test_tts_normalize_units():
    assert normalize_text("5 м/с") == "5 метров в секунду"
    assert normalize_text("10 км/ч") == "10 километров в час"
    assert normalize_text("25°C") == "25 градусов Цельсия"
    assert normalize_text("30%") == "30 процентов"
    assert normalize_text("5 м/с²") == "5 метров в секунду квадратных"
    assert normalize_text("м²") == "квадратных метров"
    assert TextToSpeech._normalize_text("2 кГц") == "2 килогерц"


def test_tts_normalize_chained():
    text = "Температура 25°C, скорость 5 м/с, 30% влажности."
    result = normalize_text(text)
    assert "градусов Цельсия" in result
    assert "метров в секунду" in result
    assert "процентов" in result


# ---------------------------------------------------------------------------
# Поведение сервиса
# ---------------------------------------------------------------------------
def test_tts_speak_noop_without_engine():
    cfg = TtsConfig(enabled=True, engine="none")
    tts = TextToSpeech(cfg)
    tts.speak("Привет")  # логирует, не падает, воркер не создаётся
    assert tts._worker is None
    assert tts.wait() is True


def test_tts_speak_is_nonblocking_and_queues():
    """speak() не блокирует вызывающий поток; stop() чистит очередь."""
    tts = TextToSpeech(TtsConfig(engine="none"))
    t0 = time.perf_counter()
    for _ in range(50):
        tts.speak("фраза")
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.5  # 50 очередных вызовов — миллисекунды, не секунды


def test_tts_stop_clears_queue():
    tts = TextToSpeech(TtsConfig(engine="none"))
    tts._engine = "edge"  # эмулируем активный движок без сети
    tts._start_worker()  # не будет speak'ов — просто воркер на месте
    try:
        for _ in range(5):
            tts._queue.put("фраза")
        tts.stop()
        assert tts._queue.empty()
        assert tts._idle.is_set()
    finally:
        tts.close()


# ---------------------------------------------------------------------------
# Стрим edge-tts (офлайн: фейковый Communicate)
# ---------------------------------------------------------------------------
def test_edge_mp3_chunks_filters_non_audio(monkeypatch):
    import edge_tts

    class FakeComm:
        def __init__(self, text, voice, **kwargs):
            pass

        async def stream(self):
            yield {"type": "WordBoundary", "text": "hi", "offset": 0, "duration": 1}
            yield {"type": "audio", "data": b"AAA"}
            yield {"type": "audio", "data": b""}
            yield {"type": "audio", "data": b"BBB"}

    monkeypatch.setattr(edge_tts, "Communicate", FakeComm)
    chunks = asyncio.run(_collect_chunks("hi", "voice"))
    assert chunks == [b"AAA", b"BBB"]


async def _collect_chunks(text: str, voice: str) -> list[bytes]:
    return [c async for c in edge_mp3_chunks(text, voice=voice)]


def test_edge_mp3_chunks_honors_cancel(monkeypatch):
    import edge_tts

    produced = []

    class FakeComm:
        def __init__(self, text, voice, **kwargs):
            pass

        async def stream(self):
            for i in range(10):
                produced.append(i)
                yield {"type": "audio", "data": bytes([i])}

    monkeypatch.setattr(edge_tts, "Communicate", FakeComm)
    cancel = threading.Event()

    async def run() -> list[bytes]:
        out = []
        async for c in edge_mp3_chunks("x", voice="v", cancel=cancel):
            out.append(c)
            if len(out) == 2:
                cancel.set()
        return out

    got = asyncio.run(run())
    assert len(got) == 2          # после cancel генератор останавливается
    assert len(produced) <= 4     # и новых чанков почти не запрашивается


# ---------------------------------------------------------------------------
# Декодер mp3 → PCM (офлайн: локальная кодировка libmp3lame)
# ---------------------------------------------------------------------------
def _make_test_mp3(duration: float = 0.4, sr: int = 24000) -> bytes:
    """Сгенерировать настоящий mp3 локально (440 Гц, mono)."""
    from fractions import Fraction

    import av

    buf = io.BytesIO()
    out = av.open(buf, mode="w", format="mp3")
    st = out.add_stream("libmp3lame", rate=sr)
    st.layout = "mono"

    t = np.arange(int(duration * sr))
    samples = (np.sin(2 * np.pi * 440 * t / sr) * 0.5 * 32767).astype(np.int16)
    frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
    frame.sample_rate = sr
    frame.time_base = Fraction(1, sr)
    frame.pts = 0
    for packet in st.encode(frame):
        out.mux(packet)
    for packet in st.encode(None):
        out.mux(packet)
    out.close()
    return buf.getvalue()


def test_incremental_decoder_decodes_local_mp3():
    mp3 = _make_test_mp3(duration=0.4, sr=24000)
    assert mp3[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"ID3") or mp3[0] in (0x49, 0xFF)

    decoder = IncrementalMp3Decoder(samplerate=48000, layout="stereo")
    pcm = bytearray()
    errors: list[BaseException] = []

    def _run():
        try:
            for chunk in decoder.decode():
                pcm.extend(chunk)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    th = threading.Thread(target=_run, daemon=True)
    th.start()

    # заливаем по кусочкам, как edge-tts
    step = 200
    for i in range(0, len(mp3), step):
        decoder.feed(mp3[i : i + step])
    decoder.eof()
    th.join(timeout=10)
    assert not th.is_alive()
    assert not errors, errors

    # 0.4 c тона на 48 кГц stereo int16 ≈ 0.4 * 48000 * 4 = 76800 байт (±кодек-хвосты)
    assert 60000 <= len(pcm) <= 120000
    arr = np.frombuffer(bytes(pcm), dtype=np.int16)
    assert len(arr) > 0
    assert np.abs(arr).max() > 1000  # там реально есть сигнал, не тишина


def test_incremental_decoder_cancel_stops_decode():
    decoder = IncrementalMp3Decoder()
    decoder.feed(_make_test_mp3(duration=0.2))
    got = []
    th = threading.Thread(
        target=lambda: got.extend(decoder.decode()), daemon=True
    )
    th.start()
    time.sleep(0.1)
    decoder.cancel()
    th.join(timeout=5)
    assert not th.is_alive()


# ---------------------------------------------------------------------------
# Плеер (без звукового устройства: логика буфера)
# ---------------------------------------------------------------------------
def test_stream_player_buffers_without_device():
    player = StreamPlayer()
    player.feed(b"\x00\x01" * 100)
    player.signal_drain()
    assert player.wait_done(timeout=0.1) is True  # не opened — сразу готов
    player.close()


def test_stream_player_abort_clears_buffer():
    player = StreamPlayer()
    player.feed(b"\x00\x01" * 100)
    player.abort()
    player.signal_drain()
    assert player.wait_done(timeout=0.1) is True
    # после abort новые данные не буферизуются
    player.feed(b"\x02\x03" * 10)
    assert player._buf == bytearray()
