"""Голосовой агент Berta на LiveKit Agents.

Работает в отдельном venv (.venv-livekit) и не импортирует jarvis.*,
поэтому запускается изолированно:

    .venv-livekit\\Scripts\\python.exe jarvis\\voice\\livekit_agent.py console

Весь контур локальный:
    VAD  -> silero (onnx, cpu)
    STT -> LOCAL_VOICE_URL (faster-whisper, см. jarvis.voice.local_voice_api)
    LLM -> OLLAMA_URL       (локальная модель, без интернета)
    TTS -> LOCAL_VOICE_URL (edge-tts)

Логику разговора, прерывания (barge-in) и стриминг обеспечивает LiveKit.

Команды задач (пример: «добавь задачу купить молоко») разбирает основной
процесс через POST {LOCAL_VOICE_BASE}/voicecmd — здесь только озвучка ответа.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any

# Windows-консоль по умолчанию cp1251: rich из livekit печатает эмодзи и падает
# с UnicodeEncodeError. Переводим потоки в UTF-8 до импорта livekit.
for _name in ("stdout", "stderr"):
    _stream = getattr(sys, _name, None)
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

# certifi нужен до импорта livekit: иначе он не находит системный trust store
# и блокирует event loop на ~2.5 c при создании httpx-клиента.
import certifi
import httpx
import openai as openai_mod

os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from livekit.agents import (  # noqa: E402
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    RunContext,
    cli,
    function_tool,
)
from livekit.agents.types import APIConnectOptions  # noqa: E402
from livekit.plugins import openai, silero  # noqa: E402


def _prewarm_tls() -> Any:
    """Собрать SSL-контекст ОДИН раз на импорте и отдать его httpx.

    Каждый httpx-клиент (STT, LLM, TTS) по умолчанию заново парсит CA-бандл
    certifi, и на Ryzen 5 7430U это 0.5-3.9 c ПРЯМО НА event loop агента —
    LiveKit ругается на такой stall сам. Кэша у create_default_context нет,
    поэтому «прогреть заранее» не выходит: нужен общий готовый объект.

    Именно синхронно на импорте: разбор PEM держит GIL, и прогрев в потоке лишь
    конкурировал с основным потоком, увеличивая блокировку вдвое (замерено).
    """
    import ssl

    t0 = time.monotonic()
    ctx = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE"))
    print(f"голос: TLS-контекст собран за {time.monotonic() - t0:.1f} c", flush=True)
    return ctx


_SSL_CTX = _prewarm_tls()

# read=5 c в дефолте плагина — короче, чем prefill на CPU (он один занимает
# ~10 c), из-за чего стрим обрывался ещё до первого токена.
_HTTP_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=60.0, pool=15.0)
_LIMITS = httpx.Limits(
    max_connections=50, max_keepalive_connections=50, keepalive_expiry=120
)


def _local_client(base_url: str, api_key: str) -> Any:
    """AsyncClient с уже собранным SSL-контекстом: без повторного парсинга CA."""
    return openai_mod.AsyncClient(
        max_retries=0,
        api_key=api_key,
        base_url=base_url,
        http_client=httpx.AsyncClient(
            verify=_SSL_CTX,
            timeout=_HTTP_TIMEOUT,
            follow_redirects=True,
            limits=_LIMITS,
        ),
    )


_search_client: Any = None


def _get_search_client() -> Any:
    global _search_client
    if _search_client is None:
        _search_client = httpx.AsyncClient(
            timeout=20.0,
            verify=_SSL_CTX,
            # обращаемся только к локальному shim на 127.0.0.1
            trust_env=False,
        )
    return _search_client


class LocalLLM(openai.LLM):
    """openai.LLM с нормальным таймаутом стриминга.

    Плагин подставляет DEFAULT_API_CONNECT_OPTIONS (timeout=10 c) как
    значение аргумента по умолчанию — из конструктора он не берётся, и
    подменить атрибут модуля уже нельзя (дефолт запечён в сигнатуре).
    На Ryzen 7430U один только prefill диалога к 4-му вопросу подходит к
    10 c, и стрим рвался на ровном месте.
    """

    def chat(self, **kwargs) -> Any:
        kwargs.setdefault(
            "conn_options",
            APIConnectOptions(
                max_retry=3,
                retry_interval=2.0,
                timeout=float(os.environ.get("LLM_STREAM_TIMEOUT", "60")),
            ),
        )
        return super().chat(**kwargs)

LOCAL_VOICE_URL = os.environ.get("LOCAL_VOICE_URL", "http://127.0.0.1:8077/v1")
LOCAL_VOICE_BASE = LOCAL_VOICE_URL.rsplit("/v1", 1)[0]
WEB_SEARCH_ENABLED = os.environ.get("VOICE_WEB_SEARCH", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "llama3.2:3b")
LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "ru")
# Хвост тишины, после которого VAD считает фразу законченной и отдаёт её в
# STT. Это чистая задержка перед началом распознавания, поэтому чем меньше —
# тем отзывчивее. Ниже ~0.4 c VAD начинает отрезать фразу на паузе внутри
# обычной речи («вот... значит»), поэтому 0.45 c — компромисс: -0.15 c
# относительно прежних 0.6 c без заметных обрывов.
MIN_SILENCE = float(os.environ.get("VOICE_MIN_SILENCE", "0.45"))
LOCAL_VOICE_KEY = os.environ.get("LOCAL_VOICE_API_KEY", "local")
# Голосовой ответ держим коротким: на Ryzen 5 7430U (6 ядер, CPU) llama3.2:3b
# выдаёт ~6.5 ток/с, то есть 0.16 с на токен. 64 токена ≈ 2-3 фразы ≈ 10 c.
MAX_TOKENS = int(os.environ.get("VOICE_LLM_MAX_TOKENS", "64"))
# Сколько Ollama держит модель в памяти после последнего запроса. Дефолт
# Ollama — 5 минут, потом модель выгружается и следующая реплика платит
# ~12.5 c холодной загрузки вместо 0.2 c тёплой. Принимается и через
# OpenAI-совместимый /v1/chat/completions (проверено: HTTP 200).
KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")

INSTRUCTIONS = os.environ.get(
    "BERTA_SYSTEM_PROMPT",
    "Ты — Берта, голосовой ассистент.\n\n"
    "ЯЗЫК (самое важное): отвечай ТОЛЬКО на русском языке. Не вставляй "
    "английские слова в русские фразы и не переписывай русские слова "
    "латиницей. Латиница допустима только в именах, аббревиатурах и коде.\n\n"
    "ФОРМАТ ОТВЕТА: одна-две короткие фразы, примерно до 15 слов. Без "
    "списков, таблиц, разметки, эмодзи и заголовков — это голос, его "
    "зачитывают вслух. Не начинай с вступлений вроде «Конечно!», «К "
    "сожалению», «Отличный вопрос» — сразу отвечай по делу. Числа и время "
    "пиши словами, если это не код.\n\n"
    "Есть инструмент web_search. Вызывай его только когда нужны актуальные "
    "данные, которых у тебя нет: погода, курсы валют, новости, свежие факты. "
    "На вопросы о коде, математике, определениях и общих знаниях отвечай сам, "
    "без поиска. Если данных нет — скажи об этом прямо и коротко, не выдумывай "
    "факты.",
)

server = AgentServer()


@function_tool
async def current_time(context: RunContext) -> str:
    """Узнать текущее время на компьютере пользователя."""
    from datetime import datetime

    return datetime.now().strftime("%H:%M")


@function_tool
async def web_search(context: RunContext, query: str) -> str:
    """Найти актуальные данные в интернете: погода, курсы валют, новости, свежие факты."""
    if not WEB_SEARCH_ENABLED:
        return "Поиск в интернете отключён."

    try:
        response = await _get_search_client().post(
            f"{LOCAL_VOICE_BASE}/websearch", json={"query": query}
        )
        answer = response.json().get("answer") or ""
    except Exception as exc:  # noqa: BLE001
        return f"Поиск не удался: {exc}"

    return answer or "В интернете ничего не нашлось."


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    # Полностью локальный контур, без обращений к облаку LiveKit Inference:
    #  - turn_detection "vad"  -> silero определяет конец фразы пользователя;
    #  - interruption mode "vad" -> отключает AdaptiveInterruptionDetector,
    #    который ходит в облако и падает без API-ключа (~3 c блокировки loop).
    turn_handling = {
        "turn_detection": "vad",
        "interruption": {"mode": "vad"},
    }

    session = AgentSession(
        turn_handling=turn_handling,
        vad=silero.VAD.load(min_silence_duration=MIN_SILENCE),
        stt=openai.STT(
            model="whisper-1",
            client=_local_client(LOCAL_VOICE_URL, LOCAL_VOICE_KEY),
            language=LANGUAGE,
        ),
        llm=LocalLLM(
            model=LLM_MODEL,
            client=_local_client(OLLAMA_URL, "ollama"),
            temperature=float(os.environ.get("VOICE_LLM_TEMPERATURE", "0.0")),
            # холодный старт Ollama + загрузка faster-whisper могут занять
            # больше 10 c и обрывать первый ответ; даём запас + ретраи
            timeout=float(os.environ.get("LLM_TIMEOUT", "60")),
            max_retries=3,
            # ВАЖНО: Ollama игнорирует max_completion_tokens (проверено: при
            # лимите 20 он спокойно отдал 989 токенов = 184 c озвучки), поэтому
            # лимит уходит в max_tokens через extra_body.
            # keep_alive не даёт Ollama выгрузить модель после 5 минут молчания:
            # холодный первый запрос стоит ~12.5 c против 0.2 c тёплого.
            extra_body={
                "max_tokens": MAX_TOKENS,
                "keep_alive": KEEP_ALIVE,
            },
        ),
        tts=openai.TTS(
            model="tts-1",
            client=_local_client(LOCAL_VOICE_URL, LOCAL_VOICE_KEY),
        ),
    )

    agent = Agent(
        instructions=INSTRUCTIONS,
        turn_handling=turn_handling,
        tools=[current_time, web_search] if WEB_SEARCH_ENABLED else [current_time],
    )

    # Orb/web живут в основном процессе, агент — в отдельном, поэтому состояние
    # передаём маркером в stdout; его разбирает LiveKitVoiceService.
    @session.on("agent_state_changed")
    def _on_state_changed(event) -> None:
        print(f"[[ORB]]{event.new_state}", flush=True)

    async def _handle_task_command(text: str) -> None:
        """Спросить основной процесс: это команда задач? — и озвучить ответ."""
        try:
            resp = await _get_search_client().post(
                f"{LOCAL_VOICE_BASE}/voicecmd", json={"text": text}
            )
            data = resp.json()
        except Exception:
            data = {}
        if data.get("matched"):
            print("[[ORB]]speaking", flush=True)
            await session.say(data.get("answer") or "", add_to_chat_ctx=True)

    # ВАЖНО: EventEmitter.on() принимает ТОЛЬКО синхронный колбэк — иначе
    # `ValueError: Cannot register an async callback with '.on()'`. Асинхронную
    # работу выносим в задачу (create_task из синхронного колбэка).
    @session.on("user_input_transcribed")
    def _on_user_speech(event) -> None:
        text = (event.transcript or "").strip()
        if not text:
            return
        print(f"[[SAID]]{text}", flush=True)
        asyncio.create_task(_handle_task_command(text), name="voice-task-cmd")

    await session.start(agent=agent, room=ctx.room)
    await session.generate_reply(instructions="Поздоровайся и спроси, чем помочь.")


if __name__ == "__main__":
    cli.run_app(server)
