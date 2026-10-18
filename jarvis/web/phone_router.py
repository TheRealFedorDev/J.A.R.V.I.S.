"""Phone API: тонкий REST-слой над AppState для мобильного клиента.

Мобильная веб-страница (/m) и любой внешний клиент обращаются сюда.
Все эндпоинты работают поверх того же AppState, что и веб-панель, поэтому
задачи и команды с телефона видны на хосте и наоборот.

    GET    /health            — живость
    GET    /state             -> профиль, флаги, счётчик задач
    GET    /tasks             -> список задач
    POST   /tasks             -> добавить {text}
    PATCH  /tasks/{id}        -> правка {text}
    DELETE /tasks/{id}        -> удаление
    POST   /cmd               -> текст → router {text}
    POST   /voice             -> multipart WAV -> STT → router
    POST   /telemetry         -> произвольный JSON в аудит
    POST   /mic  {"enabled"}  -> микрофон хоста вкл/выкл
    POST   /tts  {"enabled"}  -> озвучка хоста вкл/выкл
    GET    /bracelet          -> последние показания BLE-браслета
    POST   /bracelet          -> показание с телефона {device, kind, value}

Авторизация: заголовок `X-Phone-Key: <PHONE_API_KEY>` или
`Authorization: Bearer <PHONE_API_KEY>`. Ключ — из окружения.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

log = logging.getLogger("jarvis.web.phone")


# ---------------------------------------------------------------------------
# Аутентификация по ключу PHONE_API_KEY
# ---------------------------------------------------------------------------
def _phone_key() -> str:
    key = os.environ.get("PHONE_API_KEY") or ""
    if not key:
        raise HTTPException(status_code=503, detail="phone_api_not_configured")
    return key


async def require_phone_key(
    x_phone_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
) -> None:
    expected = _phone_key()
    provided = x_phone_key
    if not provided and authorization and authorization.startswith("Bearer "):
        provided = authorization.split(" ", 1)[1]
    if not provided or provided != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


router = APIRouter(dependencies=[Depends(require_phone_key)])


def _state(request: Request):
    st = getattr(request.app.state, "app_state", None)
    if st is None:
        raise HTTPException(status_code=503, detail="state_unavailable")
    return st


class TaskIn(BaseModel):
    text: str


class CmdIn(BaseModel):
    text: str


class EnabledIn(BaseModel):
    enabled: bool = True


class BraceletIn(BaseModel):
    """Показание BLE-браслета с телефона. Тело свободное: разные браслеты
    отдают разные метрики, поэтому храним как есть без жёсткой схемы."""

    device: Optional[str] = None
    kind: Optional[str] = None
    value: Any = None
    ts: Optional[float] = None
    extra: Optional[dict[str, Any]] = None


def _route_text(st, text: str) -> dict[str, Any]:
    """Прогнать текст через router; ответ зеркалим на веб-панель (WS)."""
    text = (text or "").strip()
    if not text or getattr(st, "router", None) is None:
        return {"ok": False, "reply": "", "source": "rules",
                "needs_confirm": False, "confirm_id": None}

    result = st.router.handle(text, source="phone")
    reply = result.reply
    if result.stream and result.source == "llm":
        # на телефоне стриминг не нужен — забираем готовый текст
        reply = st.router.stream_llm(text, "phone", lambda _chunk: None)

    st.bcast.push("router", {
        "text": text, "reply": reply, "source": result.source,
        "needs_confirm": result.needs_confirm, "confirm_id": result.confirm_id,
    })
    return {
        "ok": True, "reply": reply, "action": result.action,
        "source": result.source, "needs_confirm": result.needs_confirm,
        "confirm_id": result.confirm_id,
    }


# ---------------------------------------------------------------------------
# Служебное
# ---------------------------------------------------------------------------
@router.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "ts": time.time()}


@router.get("/state")
async def phone_state(request: Request) -> dict[str, Any]:
    st = _state(request)
    return {
        "ok": True,
        "profile": st.profiles.current_id,
        "emergency": st.emergency,
        "mic_muted": st.mic_muted,
        "tts_enabled": st.tts_enabled,
        "task_count": len(st.tasks.list()),
    }


# ---------------------------------------------------------------------------
# Задачник (то же правило и тот же файл, что у веб-панели)
# ---------------------------------------------------------------------------
@router.get("/tasks")
async def list_tasks(request: Request) -> dict[str, Any]:
    return {"tasks": _state(request).tasks.list()}


@router.post("/tasks")
async def add_task(request: Request, body: TaskIn) -> dict[str, Any]:
    st = _state(request)
    try:
        task = st.tasks.add(body.text)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    st.audit.add("phone", "task_add", detail=task["text"])
    return {"ok": True, "task": task}


@router.patch("/tasks/{task_id}")
async def update_task(request: Request, task_id: int, body: TaskIn) -> dict[str, Any]:
    st = _state(request)
    try:
        task = st.tasks.update(task_id, body.text)
    except KeyError:
        return JSONResponse({"detail": "task not found"}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    st.audit.add("phone", "task_update", detail=task["text"])
    return {"ok": True, "task": task}


@router.delete("/tasks/{task_id}")
async def delete_task(request: Request, task_id: int) -> dict[str, Any]:
    st = _state(request)
    try:
        st.tasks.remove(task_id)
    except KeyError:
        return JSONResponse({"detail": "task not found"}, status_code=404)
    st.audit.add("phone", "task_delete", detail=str(task_id))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Команды: текст и голос
# ---------------------------------------------------------------------------
@router.post("/cmd")
async def cmd(request: Request, body: CmdIn) -> dict[str, Any]:
    st = _state(request)
    return await asyncio.to_thread(_route_text, st, body.text)


@router.post("/voice")
async def voice(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    st = _state(request)
    audio = await file.read()
    try:
        from jarvis.voice.local_voice_api import _decode_to_mono_16k, _transcribe

        samples = await asyncio.to_thread(_decode_to_mono_16k, audio)
        transcript = await asyncio.to_thread(_transcribe, samples)
    except Exception:
        log.exception("STT недоступен")
        return JSONResponse(
            {"transcript": "", "reply": "", "source": "rules",
             "error": "stt_unavailable"},
            status_code=503,
        )

    if not transcript.strip():
        return {"transcript": "", "reply": "", "source": "rules",
                "needs_confirm": False, "confirm_id": None}

    routed = await asyncio.to_thread(_route_text, st, transcript)
    return {
        "transcript": transcript,
        "reply": routed.get("reply", ""),
        "source": routed.get("source", "rules"),
        "needs_confirm": routed.get("needs_confirm", False),
        "confirm_id": routed.get("confirm_id"),
    }


# ---------------------------------------------------------------------------
# Телеметрия (телефон → журнал аудита)
# ---------------------------------------------------------------------------
@router.post("/telemetry")
async def telemetry(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    st = _state(request)
    import json

    st.audit.add("phone", "telemetry", detail=json.dumps(payload, ensure_ascii=False))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Микрофон / TTS на машине-хосте
# ---------------------------------------------------------------------------
@router.post("/mic")
async def mic(request: Request, body: Optional[EnabledIn] = None) -> dict[str, Any]:
    enabled = True if body is None else body.enabled
    st = _state(request)
    if st.voice is None:
        return {"ok": False, "detail": "voice unavailable"}
    if enabled:
        st.voice.start()
        st.mic_muted = False
    else:
        st.voice.stop()
        st.mic_muted = True
    st.bcast.push("mic", {"muted": st.mic_muted})
    return {"ok": True, "muted": st.mic_muted}


@router.post("/tts")
async def tts(request: Request, body: Optional[EnabledIn] = None) -> dict[str, Any]:
    enabled = True if body is None else body.enabled
    st = _state(request)
    st.tts_enabled = enabled
    if not enabled and st.tts is not None:
        st.tts.stop()
    st.bcast.push("tts", {"enabled": st.tts_enabled})
    return {"ok": True, "enabled": st.tts_enabled}


# ---------------------------------------------------------------------------
# Браслет (телефон ловит BLE и шлёт показания сюда)
# ---------------------------------------------------------------------------
@router.get("/bracelet")
async def bracelet_get(request: Request) -> dict[str, Any]:
    st = _state(request)
    return {"ok": True, "last": st.bracelet_last, "readings": st.bracelet_recent(50)}


@router.post("/bracelet")
async def bracelet_post(request: Request, body: BraceletIn) -> dict[str, Any]:
    st = _state(request)
    now = time.time()
    ts = float(body.ts) if body.ts is not None else now
    # Защита от мусора: время не из будущего и не старше суток.
    if not (now - 86400.0 <= ts <= now + 300.0):
        ts = now
    extra = body.extra or {}
    entry = {
        "ts": ts,
        "received": now,
        "device": (body.device or "")[:64],
        "kind": (body.kind or "raw")[:32],
        "value": body.value,
        "extra": extra,
    }
    st.add_bracelet(entry)
    # Событие подключения/регистрации — единственное, что стоит в аудите.
    if entry["kind"] == "connect":
        st.audit.add("phone", "bracelet_connect", detail=entry["device"] or "unknown")
    return {"ok": True, "entry": entry}
