"""Тесты pipecat-интеграции: конфиг, мост транскрипций, EdgeTTS-фабрика."""

from __future__ import annotations

from jarvis.core.config import VoiceConfig
from jarvis.core.events import EventBus, VoiceCommandEvent, VoicePartialEvent
from jarvis.voice.commands import parse_command


# ---------------------------------------------------------------------------
# Конфиг VOICE_AGENT
# ---------------------------------------------------------------------------
def test_voice_config_agent_default_legacy():
    cfg = VoiceConfig.from_env()
    assert cfg.agent in ("legacy", "pipecat")
    # без env агент legacy (не ломаем существующие установки)
    assert cfg.agent == "legacy"


def test_voice_config_agent_env_pipecat(monkeypatch):
    monkeypatch.setenv("VOICE_AGENT", "pipecat")
    cfg = VoiceConfig.from_env()
    assert cfg.agent == "pipecat"


def test_voice_config_agent_env_invalid(monkeypatch):
    monkeypatch.setenv("VOICE_AGENT", "bogus")
    cfg = VoiceConfig.from_env()
    assert cfg.agent == "legacy"


# ---------------------------------------------------------------------------
# dispatch_transcription (чистая логика моста)
# ---------------------------------------------------------------------------
def test_dispatch_interim_publishes_partial_only():
    from jarvis.voice.pipecat_agent import dispatch_transcription

    bus = EventBus()
    events = []
    bus.subscribe(VoicePartialEvent, events.append)
    bus.subscribe(VoiceCommandEvent, events.append)

    forward = dispatch_transcription(bus, "привет мир", final=False)
    assert forward is True
    assert len(events) == 1
    assert isinstance(events[0], VoicePartialEvent)
    assert events[0].final is False
    assert events[0].text == "привет мир"


def test_dispatch_final_command_not_forwarded():
    from jarvis.voice.pipecat_agent import dispatch_transcription

    bus = EventBus()
    partials, commands = [], []
    bus.subscribe(VoicePartialEvent, partials.append)
    bus.subscribe(VoiceCommandEvent, commands.append)

    forward = dispatch_transcription(bus, "клик", final=True)
    assert forward is False  # команда не уходит в LLM
    assert len(partials) == 1 and partials[0].final is True
    assert len(commands) == 1
    assert commands[0].action == "click"
    assert commands[0].text == "клик"


def test_dispatch_final_speech_forwarded_to_llm():
    from jarvis.voice.pipecat_agent import dispatch_transcription

    bus = EventBus()
    commands = []
    bus.subscribe(VoiceCommandEvent, commands.append)

    forward = dispatch_transcription(
        bus, "расскажи шутку", final=True
    )
    assert forward is True  # разговорная фраза → LLM
    assert commands == []  # VoiceCommandEvent с action=None не публикуется


def test_dispatch_empty_text():
    from jarvis.voice.pipecat_agent import dispatch_transcription

    bus = EventBus()
    events = []
    bus.subscribe(VoicePartialEvent, events.append)
    assert dispatch_transcription(bus, "  ", final=True) is False
    assert events == []


def test_dispatch_matches_legacy_parse_command():
    """Мост обязан использовать те же правила, что и legacy VoiceService."""
    from jarvis.voice.pipecat_agent import dispatch_transcription

    for phrase, action in (
        ("клик", "click"),
        ("двойной клик", "double_click"),
        ("правый клик", "right_click"),
        ("калибровка", "calibrate"),
    ):
        assert parse_command(phrase).action == action
        bus = EventBus()
        cmds = []
        bus.subscribe(VoiceCommandEvent, cmds.append)
        assert dispatch_transcription(bus, phrase, final=True) is False
        assert cmds[0].action == action


# ---------------------------------------------------------------------------
# EdgeTTS-фабрика (без запуска сети)
# ---------------------------------------------------------------------------
def test_create_edge_tts_service_builds():
    pytest = __import__("pytest")

    pytest.importorskip("pipecat")
    from jarvis.tts.edge_service import create_edge_tts_service
    from jarvis.tts.service import TtsConfig

    svc = create_edge_tts_service(
        tts_config=TtsConfig(), sample_rate=24000
    )
    assert hasattr(svc, "run_tts")
    # база pipecat держит запрос в _init_sample_rate; _sample_rate — после setup
    assert getattr(svc, "_init_sample_rate", None) == 24000
    assert svc._decoder_sr == 24000


def test_voice_config_llm_defaults(monkeypatch):
    """LLM_* читается из окружения в pipecat_agent, не ломает VoiceConfig."""
    monkeypatch.setenv("LLM_MODEL", "llama3.2:3b")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    import os

    assert os.environ["LLM_MODEL"] == "llama3.2:3b"
    cfg = VoiceConfig.from_env()
    assert cfg.whisper_model == "medium"


# ---------------------------------------------------------------------------
# InterimSTT (stream partials) — логика без запуска конвейера
# ---------------------------------------------------------------------------
def test_interim_factory_builds():
    pytest = __import__("pytest")
    pytest.importorskip("pipecat")
    from jarvis.voice.pipecat_agent import _make_interim_class

    InterimSTT = _make_interim_class()
    bus = EventBus()
    svc = InterimSTT(
        bus,
        whisper_model=None,
        language="ru",
        sample_rate=16000,
        partial_seconds=0.85,
        vad_threshold=0.012,
        silence_end=0.55,
    )
    assert svc.name == "jarvis-interim-stt"
    assert svc._sr == 16000
    assert svc._speech is False


def test_interim_energy_vad_starts_and_schedules():
    pytest = __import__("pytest")
    pytest.importorskip("pipecat")
    import numpy as np

    from jarvis.voice.pipecat_agent import _make_interim_class

    InterimSTT = _make_interim_class()
    bus = EventBus()
    events: list = []
    bus.subscribe(VoicePartialEvent, events.append)
    svc = InterimSTT(
        bus,
        whisper_model=None,  # partial не запустится без модели
        language="ru",
        sample_rate=16000,
        partial_seconds=0.85,
        vad_threshold=0.012,
        silence_end=0.55,
    )

    # громкий блок (voiced) → старт utterance + «…»
    loud = (np.sin(np.linspace(0, 100, 4000)) * 0.5).astype(np.float32)
    pcm16 = (loud * 32767).astype(np.int16).tobytes()
    svc._feed(pcm16, 16000)
    assert svc._speech is True
    assert any(e.final is False and e.text == "…" for e in events)

    # тишина дольше silence_end → сброс
    silence = np.zeros(16000, dtype=np.int16).tobytes()
    for _ in range(int(0.55 / (16000 / 16000)) + 2):
        svc._feed(silence, 16000)
    # 16000 сэмплов = 1 с > 0.55 → сброс после достаточно длинной тишины
    svc._feed(np.zeros(16000 * 2, dtype=np.int16).tobytes(), 16000)
    assert svc._speech is False


def test_interim_partial_work_is_bounded(monkeypatch):
    """Partial не должен перетранскрибировать всю фразу заново (выедает CPU).

    Регрессия: буфер рос до 20 c, interim гонял Whisper по всей фразе каждые
    1.5 c -> event loop голодал, ответ приходил через минуты.
    """
    pytest = __import__("pytest")
    pytest.importorskip("pipecat")
    import numpy as np

    from jarvis.voice.pipecat_agent import _make_interim_class

    InterimSTT = _make_interim_class()
    svc = InterimSTT(
        EventBus(),
        whisper_model=object(),  # транскрибирование подменено ниже
        language="ru",
        sample_rate=16000,
        partial_seconds=1.5,
        vad_threshold=0.012,
        silence_end=0.55,
        partial_max_seconds=12.0,
        partial_work_budget=24.0,
    )

    seen: list[float] = []

    class _Done:
        def done(self) -> bool:
            return True

    # синхронная заглушка: пишем сразу при вызове, чтобы не нужен event loop
    def fake_run(audio):
        seen.append(len(audio) / 16000.0)

    def fake_create_task(_coro):
        return _Done()

    svc._run_partial = fake_run
    svc.create_task = fake_create_task

    # 30 с непрерывной речи блоками по 0.25 с
    loud = (np.sin(np.linspace(0, 100, 4000)) * 0.5).astype(np.float32)
    pcm16 = (loud * 32767).astype(np.int16).tobytes()
    svc._start_utterance()
    for _ in range(120):
        svc._feed(pcm16, 16000)

    assert seen, "partial должен был запускаться"
    assert max(seen) <= 12.0 + 1e-6, f"окно partial превышено: {max(seen)}"
    assert sum(seen) <= 24.0 + 1e-6, f"бюджет превышен: {sum(seen)}"
    # без ограничений было бы ~20 проходов суммарно по ~210 c аудио
    assert len(seen) <= 8, f"слишком много проходов: {len(seen)}"


def test_dispatch_and_interim_share_event_type():
    """Interim публикует тот же VoicePartialEvent, что и legacy/most."""
    from jarvis.voice.pipecat_agent import dispatch_transcription

    bus = EventBus()
    got: list = []
    bus.subscribe(VoicePartialEvent, got.append)
    dispatch_transcription(bus, "привет", final=False)
    assert got and got[0].final is False and got[0].text == "привет"


# ---------------------------------------------------------------------------
# SearchRouter / ReplyTap (фабрики — ленивый импорт pipecat)
# ---------------------------------------------------------------------------
def test_search_router_factory_builds():
    pytest = __import__("pytest")
    pytest.importorskip("pipecat")
    from jarvis.voice.pipecat_agent import _make_search_router_class

    SearchRouter = _make_search_router_class()
    bus = EventBus()
    turn: dict[str, str] = {"query": ""}
    svc = SearchRouter(bus, turn)
    assert svc.name == "jarvis-search-router"


def test_reply_tap_factory_builds():
    pytest = __import__("pytest")
    pytest.importorskip("pipecat")
    from jarvis.voice.pipecat_agent import _make_reply_tap_class

    ReplyTap = _make_reply_tap_class()
    bus = EventBus()
    turn: dict[str, str] = {"query": ""}
    svc = ReplyTap(bus, turn)
    assert svc.name == "jarvis-reply-tap"


def test_turn_router_llm_is_default_and_search_is_exception():
    """Обычный вопрос -> локальный LLM; веб только по явной просьбе/живым данным."""
    from jarvis.voice.web_search import needs_web_search

    assert not needs_web_search("Ты работаешь?")
    assert not needs_web_search("что такое Python")
    assert needs_web_search("найди новости")
    assert needs_web_search("Какая сейчас погода?")


def test_turn_router_factory_accepts_llm_options():
    pytest = __import__("pytest")
    pytest.importorskip("pipecat")
    from jarvis.voice.pipecat_agent import _make_search_router_class

    SearchRouter = _make_search_router_class()
    svc = SearchRouter(
        EventBus(),
        {"query": ""},
        llm_model="llama3.2:3b",
        system_prompt="тест",
        max_tokens=64,
        web_search_enabled=False,
    )
    assert svc.name == "jarvis-search-router"
    assert svc._llm_model == "llama3.2:3b"
    assert svc._max_tokens == 64
    assert svc._web_search_enabled is False


def test_needs_web_search_exported_from_agent():
    """needs_web_search доступен из pipecat_agent (одна точка входа)."""
    from jarvis.voice.pipecat_agent import needs_web_search

    assert needs_web_search("Какая сейчас погода?")
    assert not needs_web_search("привет")
