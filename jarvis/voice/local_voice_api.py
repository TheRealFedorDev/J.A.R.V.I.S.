"""Локальный OpenAI-совместимый сервер распознавания и синтеза речи.

Зачем он нужен: плагины LiveKit Agents умеют только сетевые провайдеры,
но все три аргумента (`base_url`) позволяют pointed на любой локальный
сервер. Этот модуль — тонкий HTTP-адаптер над уже проверенными
компонентами проекта, чтобы голосовой контур LiveKit работал офлайн:

    POST /v1/audio/transcriptions  ->  faster-whisper   (STT)
    POST /v1/audio/speech         ->  edge-tts         (TTS)
    GET  /v1/models               ->  список моделей

Логика разговора, VAD, прерывания и стриминг берёт на себя LiveKit.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import threading
import wave
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

import av
import edge_tts
import numpy as np
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse

from jarvis.core.backends import resolve_stt
from jarvis.core.config import AppConfig
from jarvis.core.logging import setup_logging
from jarvis.tts.voices import DEFAULT_EN_VOICE, DEFAULT_RU_VOICE, is_russian

log = logging.getLogger("voice.local_api")

CONFIG = AppConfig.load(Path(".env"))
VOICE = CONFIG.voice
# План STT считаем один раз при импорте: он читает железо через nvidia-smi,
# а модель грузится лениво при первом запросе.
STT_PLAN = resolve_stt()

HOST = os.environ.get("LOCAL_VOICE_HOST", "127.0.0.1")
PORT = int(os.environ.get("LOCAL_VOICE_PORT", "8077"))
# Голоса берём из jarvis.tts.voices — там уже валидные имена.
# Microsoft периодически удаляет голоса, и мёртвое имя даёт NoAudioReceived.
TTS_VOICE = os.environ.get("TTS_VOICE_RU") or DEFAULT_RU_VOICE
TTS_EN_VOICE = os.environ.get("TTS_VOICE") or DEFAULT_EN_VOICE
TTS_RATE = os.environ.get("TTS_RATE", "+0%")

TARGET_RATE = 16000

# Краткая подсказка whisper: защищает имя «Берта» и русскую орфографию.
# Без неё base-модель на коротких репликах гасит имена собственные.
WHISPER_PROMPT = "Берта."

# Сегмент считается «фантомом» (whisper озвучил тишину/шум), если его
# no_speech_prob не ниже порога. На уровне 0.6 отсеиваются галлюцинации
# на паузах («Берта.», «Стоять стеки…»), а живая короткая речь обычно
# держит no_speech_prob < 0.2 и проходит.
NO_SPEECH_KEEP = 0.6

app = FastAPI(title="Berta local voice API", docs_url=None, redoc_url=None)

_model: Any = None
_model_lock = threading.Lock()
_stt_lock = asyncio.Lock()


class _SegmentLike:
    """Объект с .text/.no_speech_prob — любой сегмент faster-whisper."""


def _filter_and_join_segments(
    segments: Iterable[_SegmentLike], *, no_speech_keep: float = NO_SPEECH_KEEP
) -> str:
    """Выкинуть фантомы тишины/шума и собрать текст реплики.

    faster-whisper сам оценивает, была ли в сегменте речь (no_speech_prob).
    Без этого фильтра модель озвучивает паузу мусором вроде «Берта.», и он
    уходит в LLM/web-поиск, а ответ выходит белебердой.
    """
    parts: list[str] = []
    for segment in segments:
        text = (segment.text or "").strip()
        # 1 символ (или пусто) без смысла («ы», «а») — почти всегда шум
        if len(text) < 2:
            continue
        if float(getattr(segment, "no_speech_prob", 0.0)) >= no_speech_keep:
            continue
        parts.append(text)
    return " ".join(parts).strip()


def _get_model() -> Any:
    """Ленивая загрузка faster-whisper: модель тяжёлая, грузим при первом запросе."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from faster_whisper import WhisperModel

                plan = STT_PLAN
                log.info(
                    "Загружаю faster-whisper: model=%s device=%s compute=%s "
                    "lang=%s (модель:%s девайс:%s)",
                    plan.model, plan.device, plan.compute_type,
                    VOICE.whisper_language,
                    plan.model_source, plan.device_source,
                )
                _model = WhisperModel(
                    plan.model,
                    device=plan.device,
                    compute_type=plan.compute_type,
                )
    return _model


def _decode_to_mono_16k(audio: bytes) -> np.ndarray:
    """Любой входной формат -> mono float32 16 кГц (то, что ждёт faster-whisper).

    LiveKit присылает WAV 24 кГц, но формат может поменяться, поэтому
    декодируем через PyAV, а не разбираем заголовок вручную.
    """
    if not audio:
        return np.zeros(0, dtype=np.float32)

    try:
        with av.open(io.BytesIO(audio)) as container:
            if not container.streams.audio:
                return np.zeros(0, dtype=np.float32)
            stream = container.streams.audio[0]
            resampler = av.AudioResampler(format="s16", layout="mono", rate=TARGET_RATE)
            chunks: list[np.ndarray] = []
            for frame in container.decode(stream):
                for resampled in resampler.resample(frame):
                    chunks.append(resampled.to_ndarray().reshape(-1))
            for resampled in resampler.resample(None):
                chunks.append(resampled.to_ndarray().reshape(-1))
    except av.AVError:
        # WAV без контейнера PyAV — пробуем разобрать вручную
        return _decode_raw_wav(audio)

    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def _decode_raw_wav(audio: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(audio), "rb") as wav_file:
        rate = wav_file.getframerate()
        width = wav_file.getsampwidth()
        channels = wav_file.getnchannels()
        raw = wav_file.readframes(wav_file.getnframes())

    if width == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        return np.zeros(0, dtype=np.float32)

    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)

    if rate != TARGET_RATE and data.size:
        count = int(round(data.size * TARGET_RATE / rate))
        data = np.interp(
            np.linspace(0.0, 1.0, count, endpoint=False),
            np.linspace(0.0, 1.0, data.size, endpoint=False),
            data,
        ).astype(np.float32)
    return data


def _transcribe(samples: np.ndarray) -> str:
    if samples.size == 0:
        return ""
    segments, _info = _get_model().transcribe(
        samples,
        language=VOICE.whisper_language,
        beam_size=5,
        # без этого whisper способен зациклиться и «придумать» повтор
        condition_on_previous_text=False,
        # Подсказка по стилю: уводит whisper в русскую орфографию и подтягивает
        # имена собственные, которые на коротких репликах он иначе коверкает
        # («Пётр Первый» -> «пылья, певый»).
        initial_prompt=WHISPER_PROMPT,
        # страховка от вырожденного повтора: полезнее пустая реплика, чем «я я я»
        repetition_penalty=1.05,
        no_speech_threshold=0.6,
    )
    return _filter_and_join_segments(segments)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "asr": STT_PLAN.model,
        "device": STT_PLAN.device,
        "compute_type": STT_PLAN.compute_type,
        "language": VOICE.whisper_language,
        "tts_voice": TTS_VOICE,
    }


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    now = 0
    return {
        "object": "list",
        "data": [
            {
                "id": STT_PLAN.model,
                "object": "model",
                "created": now,
                "owned_by": "local",
            },
            {"id": "tts-1", "object": "model", "created": now, "owned_by": "local"},
        ],
    }


@app.post("/v1/audio/transcriptions")
async def transcriptions(
    file: UploadFile = File(...),
    model: str = Form("whisper-1"),
    language: str | None = Form(None),
    response_format: str = Form("json"),
) -> JSONResponse:
    audio = await file.read()
    samples = await asyncio.to_thread(_decode_to_mono_16k, audio)
    # faster-whisper не потокобезопасен — гоняем запросы по одному
    async with _stt_lock:
        text = await asyncio.to_thread(_transcribe, samples)

    log.info("STT (%s): %s", model, text or "<пусто>")
    return JSONResponse({"text": text})


@app.post("/websearch")
async def websearch(payload: dict[str, Any]) -> JSONResponse:
    """Прокси к jarvis.voice.web_search — логика поиска живёт в основном venv."""
    if os.environ.get("VOICE_WEB_SEARCH", "1").strip().lower() in ("0", "false", "no"):
        return JSONResponse({"answer": ""})

    from jarvis.voice.web_search import web_search_answer

    query = (payload.get("query") or "").strip()
    if not query:
        return JSONResponse({"answer": ""})
    return JSONResponse({"answer": await web_search_answer(query)})


@app.post("/voicecmd")
async def voicecmd(payload: dict[str, Any]) -> JSONResponse:
    """Задачи: спрашивает livekit-агент (отдельный venv без jarvis).

    Возвращает matched=True, если фраза — голосовая команда задач; тогда
    answer готов к озвучке, и агент не уходит в LLM.
    """
    from jarvis.voice.tasks_rule import parse_task_command, run, HttpTaskAPI

    text = (payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"matched": False, "answer": ""})
    cmd = parse_task_command(text)
    if cmd is None:
        return JSONResponse({"matched": False, "answer": ""})
    answer = await asyncio.to_thread(run, cmd, HttpTaskAPI())
    return JSONResponse({"matched": True, "answer": answer})


@app.post("/v1/audio/speech")
async def speech(payload: dict[str, Any]) -> Response:
    text = (payload.get("input") or "").strip()
    if not text:
        return Response(content=b"", media_type="audio/mpeg")

    default_voice = TTS_VOICE if is_russian(text) else TTS_EN_VOICE
    voice = payload.get("voice") or default_voice
    # LiveKit шлёт имена вида "alloy"/"alloy-voice": это не edge-голоса
    if not voice.endswith("Neural"):
        voice = default_voice

    async def iter_mp3() -> AsyncIterator[bytes]:
        """Отдаём mp3 кусками по мере синтеза, а не целиком.

        Раньше весь ответ копился в bytearray и уезжал одним куском:edge-tts
        к моменту отправки уже успевал отдать всё, и LiveKit начинал plays
        только после конца предложения. LiveKit 1.8 сам режет поток LLM на
        предложения и шлёт их в TTS по одному, поэтому побайтовый стриминг
        здесь сдвигает начало озвучки к середине синтеза, а не к его концу.
        Content-Type остаётся audio/mpeg - плагин openai по нему выбирает
        ветку stream.iter_bytes() (SSE-ветку не включаем).
        """
        communicate = edge_tts.Communicate(text, voice, rate=TTS_RATE)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio" and chunk["data"]:
                yield chunk["data"]

    log.info("TTS (%s, поток): %s", voice, text[:80])
    return StreamingResponse(
        iter_mp3(),
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )


def main() -> None:
    import uvicorn

    setup_logging()
    log.info("Локальный voice API: http://%s:%s", HOST, PORT)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
