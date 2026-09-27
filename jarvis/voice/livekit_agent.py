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
"""

from __future__ import annotations

import os
import sys

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
from livekit.plugins import openai, silero  # noqa: E402

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
MIN_SILENCE = float(os.environ.get("VOICE_MIN_SILENCE", "0.6"))
LOCAL_VOICE_KEY = os.environ.get("LOCAL_VOICE_API_KEY", "local")

INSTRUCTIONS = os.environ.get(
    "BERTA_SYSTEM_PROMPT",
    "Ты — Берта, голосовой ассистент. Отвечай на русском языке, кратко и "
    "по делу, одной-двумя фразами. Не используй списки и разметку, говори "
    "живой речью. Если не знаешь ответа, честно скажи об этом.\n\n"
    "У тебя есть инструмент web_search. Вызывай его только когда нужны "
    "актуальные данные, которых у тебя нет: погода, курсы валют, новости, "
    "свежие факты. На вопросы о коде, математике, определениях и общих "
    "знаниях отвечай сам, без поиска.",
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

    import httpx

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
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
            base_url=LOCAL_VOICE_URL,
            api_key=LOCAL_VOICE_KEY,
            language=LANGUAGE,
        ),
        llm=openai.LLM(
            model=LLM_MODEL,
            base_url=OLLAMA_URL,
            api_key="ollama",
            temperature=0.6,
            # холодный старт Ollama + загрузка faster-whisper могут занять
            # больше 10 c и обрывать первый ответ; даём запас + ретраи
            timeout=float(os.environ.get("LLM_TIMEOUT", "60")),
            max_retries=3,
        ),
        tts=openai.TTS(
            model="tts-1",
            base_url=LOCAL_VOICE_URL,
            api_key=LOCAL_VOICE_KEY,
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

    @session.on("user_input_transcribed")
    def _on_user_speech(event) -> None:
        text = (event.transcript or "").strip()
        if text:
            print(f"[[SAID]]{text}", flush=True)

    await session.start(agent=agent, room=ctx.room)
    await session.generate_reply(instructions="Поздоровайся и спроси, чем помочь.")


if __name__ == "__main__":
    cli.run_app(server)
