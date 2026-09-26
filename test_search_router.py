"""Интеграция TurnRouter: локальный LLM — путь по умолчанию, веб — исключение.

Живые баги, которые здесь закрыты:
* вопросительный знак отправлял весь разговор в веб мимо LLM;
* агрегатор pipecat не отдавал ход (User stopped speaking -> strategy: None),
  запрос к Ollama не уходил вовсе — теперь ход обслуживает роутер;
* stop-кадры не должны уходить вниз до готовности ответа, иначе пустая
  агрегация закрывает ход без ответа.

Проверяем:
* обычная реплика -> Ollama (стрим) -> BotReplyEvent + TTSSpeak + stop;
* веб включается только по явной просьбе/живым данным;
* пустой веб-ответ -> откат на LLM, а не TranscriptionFrame вниз;
* barge-in отменяет незавершённый ответ и чистит буфер stop.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone

import pytest

pytest.importorskip("pipecat")

from jarvis.core.events import BotReplyEvent, EventBus  # noqa: E402
from jarvis.voice.pipecat_agent import _make_search_router_class  # noqa: E402

from pipecat.frames.frames import (  # noqa: E402
    TTSSpeakFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection  # noqa: E402


def _tx(text: str) -> TranscriptionFrame:
    return TranscriptionFrame(
        text=text,
        user_id="user",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _wire(router, pushed, tasks):
    async def fake_push(frame, direction=None):
        pushed.append(frame)

    def fake_create_task(coro, name=None, context=None):
        t = asyncio.get_event_loop().create_task(coro)
        tasks.append(t)
        return t

    router.create_task = fake_create_task  # type: ignore[method-assign]
    router.push_frame = fake_push  # type: ignore[method-assign]


def _stub_llm(router, answer: str = "Да, работаю."):
    """Подмена локального LLM: тесты не ходят в сеть.

    Стаб синхронный — ровно как реальный `_stream_llm_sync`, который
    вызывается через asyncio.to_thread.
    """
    started = threading.Event()
    release = threading.Event()

    def fake_stream(text: str, on_chunk) -> str:
        started.set()
        release.wait(5)
        return answer

    router._stream_llm_sync = fake_stream  # type: ignore[assignment]
    return started, release


async def _wait_started(started: threading.Event) -> None:
    while not started.is_set():
        await asyncio.sleep(0.01)


def _run(coro):
    return asyncio.run(coro)


async def _feed(router, frame):
    await router.process_frame(frame, FrameDirection.DOWNSTREAM)


# ---------------------------------------------------------------------------
# путь по умолчанию: локальный LLM
# ---------------------------------------------------------------------------
def test_plain_question_goes_to_local_llm():
    SearchRouter = _make_search_router_class()
    bus = EventBus()
    replies: list[BotReplyEvent] = []
    bus.subscribe(BotReplyEvent, replies.append)
    turn: dict[str, str] = {"query": ""}
    router = SearchRouter(bus, turn)
    pushed: list = []
    tasks: list[asyncio.Task] = []
    _wire(router, pushed, tasks)
    started, release = _stub_llm(router)

    async def scenario():
        await _feed(router, _tx("Ты работаешь?"))
        assert router._searching is True
        await _wait_started(started)
        # stop пока ответ готовится -> в буфер, не вниз
        await _feed(router, UserStoppedSpeakingFrame())
        assert isinstance(router._pending_stop, UserStoppedSpeakingFrame)
        assert pushed == []
        release.set()
        await asyncio.gather(*tasks)

    _run(scenario())

    assert turn["query"] == "Ты работаешь?"
    assert replies and replies[0].source == "llm"
    assert "работаю" in replies[0].text
    # ответ уходит в TTS и НЕ идёт в LLMUserAggregator
    assert any(isinstance(f, TTSSpeakFrame) for f in pushed)
    assert not any(isinstance(f, TranscriptionFrame) for f in pushed)
    assert isinstance(pushed[-1], UserStoppedSpeakingFrame)


def test_llm_reply_is_pushed_to_tts_and_chat():
    SearchRouter = _make_search_router_class()
    bus = EventBus()
    replies: list[BotReplyEvent] = []
    bus.subscribe(BotReplyEvent, replies.append)
    router = SearchRouter(bus, {"query": ""})
    pushed: list = []
    tasks: list[asyncio.Task] = []
    _wire(router, pushed, tasks)
    _stub_llm(router, "Конечно, работаю.")

    async def scenario():
        await _feed(router, _tx("расскажи шутку"))
        await asyncio.gather(*tasks)

    _run(scenario())

    tts = [f for f in pushed if isinstance(f, TTSSpeakFrame)]
    assert tts and tts[0].text == "Конечно, работаю."
    assert replies and replies[0].source == "llm"
    assert isinstance(pushed[-1], UserStoppedSpeakingFrame)


def test_web_search_disabled_routes_everything_to_llm(monkeypatch):
    """VOICE_WEB_SEARCH=0 -> даже «погода» идёт в локальную модель."""

    async def boom(text: str) -> str:
        raise AssertionError("веб-поиск не должен вызываться")

    monkeypatch.setattr("jarvis.voice.pipecat_agent.maybe_web_search", boom)

    SearchRouter = _make_search_router_class()
    router = SearchRouter(EventBus(), {"query": ""}, web_search_enabled=False)
    pushed: list = []
    tasks: list[asyncio.Task] = []
    _wire(router, pushed, tasks)
    _stub_llm(router)

    async def scenario():
        await _feed(router, _tx("Какая сейчас погода?"))
        await asyncio.gather(*tasks)

    _run(scenario())

    assert any(isinstance(f, TTSSpeakFrame) for f in pushed)
    assert not any(isinstance(f, TranscriptionFrame) for f in pushed)


# ---------------------------------------------------------------------------
# веб — исключение
# ---------------------------------------------------------------------------
def test_search_success_buffers_stop_then_tts(monkeypatch):
    SearchRouter = _make_search_router_class()
    bus = EventBus()
    replies: list[BotReplyEvent] = []
    bus.subscribe(BotReplyEvent, replies.append)
    turn: dict[str, str] = {"query": ""}
    router = SearchRouter(bus, turn)
    pushed: list = []
    tasks: list[asyncio.Task] = []
    _wire(router, pushed, tasks)

    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_search(text: str) -> str:
        started.set()
        await release.wait()
        return "Пушкин — русский поэт."

    monkeypatch.setattr(
        "jarvis.voice.pipecat_agent.maybe_web_search", fake_search
    )

    async def scenario():
        await _feed(router, _tx("найди кто такой Пушкин"))
        assert router._searching is True
        await started.wait()
        await _feed(router, UserStoppedSpeakingFrame())
        assert isinstance(router._pending_stop, UserStoppedSpeakingFrame)
        assert pushed == []
        release.set()
        await asyncio.gather(*tasks)

    _run(scenario())

    assert replies and replies[0].source == "search"
    assert "Пушкин" in replies[0].text
    assert any(isinstance(f, TTSSpeakFrame) for f in pushed)
    assert isinstance(pushed[-1], UserStoppedSpeakingFrame)


def test_empty_search_falls_back_to_llm(monkeypatch):
    """Провал веба -> локальный LLM (а не TranscriptionFrame вниз)."""
    SearchRouter = _make_search_router_class()
    bus = EventBus()
    replies: list[BotReplyEvent] = []
    bus.subscribe(BotReplyEvent, replies.append)
    turn: dict[str, str] = {"query": ""}
    router = SearchRouter(bus, turn)
    pushed: list = []
    tasks: list[asyncio.Task] = []
    _wire(router, pushed, tasks)

    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_search(text: str) -> str:
        started.set()
        await release.wait()
        return ""

    monkeypatch.setattr(
        "jarvis.voice.pipecat_agent.maybe_web_search", fake_search
    )
    llm_started, llm_release = _stub_llm(router, "Не нашёл в интернете.")

    async def scenario():
        await _feed(router, _tx("найди что-нибудь"))
        await started.wait()
        await _feed(router, UserStoppedSpeakingFrame())
        assert isinstance(router._pending_stop, UserStoppedSpeakingFrame)
        release.set()
        await _wait_started(llm_started)
        llm_release.set()
        await asyncio.gather(*tasks)

    _run(scenario())

    assert turn["query"] == "найди что-нибудь"
    assert any(isinstance(f, TTSSpeakFrame) for f in pushed)
    assert not any(isinstance(f, TranscriptionFrame) for f in pushed)
    assert replies and replies[-1].source == "llm"
    assert isinstance(pushed[-1], UserStoppedSpeakingFrame)


def test_search_without_pending_stop_pushes_synthetic(monkeypatch):
    SearchRouter = _make_search_router_class()
    bus = EventBus()
    bus.subscribe(BotReplyEvent, lambda e: None)
    turn: dict[str, str] = {"query": ""}
    router = SearchRouter(bus, turn)
    pushed: list = []
    tasks: list[asyncio.Task] = []
    _wire(router, pushed, tasks)

    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_search(text: str) -> str:
        started.set()
        await release.wait()
        return "15C"

    monkeypatch.setattr(
        "jarvis.voice.pipecat_agent.maybe_web_search", fake_search
    )

    async def scenario():
        await _feed(router, UserStoppedSpeakingFrame())
        assert router._pending_stop is None
        await _feed(router, _tx("погода в Москве"))
        await started.wait()
        assert router._searching is True
        release.set()
        await asyncio.gather(*tasks)

    _run(scenario())

    assert any(isinstance(f, TTSSpeakFrame) for f in pushed)
    assert isinstance(pushed[-1], UserStoppedSpeakingFrame)


# ---------------------------------------------------------------------------
# barge-in
# ---------------------------------------------------------------------------
def test_barge_in_cancels_search_and_clears_buffer(monkeypatch):
    SearchRouter = _make_search_router_class()
    bus = EventBus()
    bus.subscribe(BotReplyEvent, lambda e: None)
    turn: dict[str, str] = {"query": ""}
    router = SearchRouter(bus, turn)
    pushed: list = []
    tasks: list[asyncio.Task] = []
    _wire(router, pushed, tasks)

    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_search(text: str) -> str:
        started.set()
        await release.wait()
        return "не должен звучать"

    monkeypatch.setattr(
        "jarvis.voice.pipecat_agent.maybe_web_search", fake_search
    )

    async def scenario():
        await _feed(router, _tx("найди новости"))
        await started.wait()
        await _feed(router, UserStoppedSpeakingFrame())
        assert router._pending_stop is not None
        await _feed(router, UserStartedSpeakingFrame())
        assert router._searching is False
        assert router._pending_stop is None
        release.set()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    _run(scenario())

    assert not any(isinstance(f, TTSSpeakFrame) for f in pushed)


def test_barge_in_cancels_llm_turn():
    SearchRouter = _make_search_router_class()
    bus = EventBus()
    bus.subscribe(BotReplyEvent, lambda e: None)
    router = SearchRouter(bus, {"query": ""})
    pushed: list = []
    tasks: list[asyncio.Task] = []
    _wire(router, pushed, tasks)
    started, release = _stub_llm(router, "долгий ответ")

    async def scenario():
        await _feed(router, _tx("расскажи анекдот"))
        await _wait_started(started)
        await _feed(router, UserStartedSpeakingFrame())
        assert router._searching is False
        assert router._pending_stop is None
        release.set()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    _run(scenario())


# ---------------------------------------------------------------------------
# пустая реплика проходит насквозь
# ---------------------------------------------------------------------------
def test_empty_transcription_passes_through():
    SearchRouter = _make_search_router_class()
    router = SearchRouter(EventBus(), {"query": ""})
    pushed: list = []
    _wire(router, pushed, [])

    async def scenario():
        await _feed(router, _tx("   "))

    _run(scenario())
    assert len(pushed) == 1
    assert isinstance(pushed[0], TranscriptionFrame)
    assert router._searching is False
