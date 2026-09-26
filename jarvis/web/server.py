"""FastAPI web-приложение Berta: статика, WebSocket, MJPEG, REST API."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

log = logging.getLogger("jarvis.web")

_STATIC = Path(__file__).resolve().parent / "static"


def _dumps(obj) -> str:
    """JSON с фоллбэком на numpy-типы (np.bool_ из eyetrax и т.п.)."""
    from jarvis.web.state import json_default

    return json.dumps(obj, ensure_ascii=False, default=json_default)


# ---------------------------------------------------------------------------
# Модели запросов
# ---------------------------------------------------------------------------
class CommandIn(BaseModel):
    text: str


class ConfirmIn(BaseModel):
    id: str
    ok: bool = True


class ProfileIn(BaseModel):
    name: str


class EnabledIn(BaseModel):
    enabled: bool


# ---------------------------------------------------------------------------
# Фабрика приложения
# ---------------------------------------------------------------------------
def create_app(state) -> FastAPI:
    """state — jarvis.web.state.AppState (сервисы могут быть None в тестах)."""
    from jarvis.web.router import CommandRouter

    state.router = CommandRouter(state)
    app = FastAPI(title="Berta", version="1.0", docs_url="/api/docs")
    app.state.app_state = state

    # ------------------------------------------------------------------
    # Жизненный цикл: цикл asyncio для Broadcaster'а
    # ------------------------------------------------------------------
    @app.on_event("startup")
    async def _startup() -> None:  # pragma: no cover - event loop
        state.bcast.bind_loop(asyncio.get_running_loop())
        state.audit.add("system", "web_start")

    @app.on_event("shutdown")
    async def _shutdown() -> None:  # pragma: no cover
        state.audit.add("system", "web_stop")

    # ------------------------------------------------------------------
    # Статика (SPA)
    # ------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def index() -> Response:
        index_path = _STATIC / "index.html"
        return HTMLResponse(index_path.read_text(encoding="utf-8"))

    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    # ------------------------------------------------------------------
    # WebSocket: снапшот + события шины в реальном времени
    # ------------------------------------------------------------------
    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        state.bcast.add(q)
        try:
            await websocket.send_text(_dumps(
                {"type": "snapshot", "data": state.snapshot()},
            ))
            while True:
                msg = await q.get()
                await websocket.send_text(_dumps(msg))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            state.bcast.remove(q)

    # ------------------------------------------------------------------
    # Камера: MJPEG-поток (слева-снизу, ~1/4 экрана) + один кадр
    # ------------------------------------------------------------------
    @app.get("/stream.mjpg")
    async def stream_mjpg() -> Response:
        async def _gen():
            boundary = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
            last_seq = -1
            idle = 0.0
            while True:
                seq = state.camera_seq
                jpeg = state.camera_jpeg
                if jpeg is not None and seq != last_seq:
                    last_seq = seq
                    idle = 0.0
                    yield boundary + jpeg + b"\r\n"
                    await asyncio.sleep(0.03)
                else:
                    idle += 0.05
                    if idle > 10.0:
                        # тишина >10 с — пустой JPEG не шлём, просто ждём
                        idle = 0.0
                    await asyncio.sleep(0.05)

        return StreamingResponse(
            _gen(),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )

    @app.get("/api/camera.jpg")
    async def camera_jpeg() -> Response:
        if state.camera_jpeg is None:
            return JSONResponse(
                {"detail": "камера ещё не прислала кадр"}, status_code=503
            )
        return Response(content=state.camera_jpeg, media_type="image/jpeg")

    # ------------------------------------------------------------------
    # Статус / аудит / профили
    # ------------------------------------------------------------------
    @app.get("/api/status")
    async def status() -> dict:
        return state.snapshot()

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "title": "Berta"}

    @app.get("/api/audit")
    async def audit(n: int = 50) -> dict:
        return {"entries": state.audit.tail(n)}

    @app.get("/api/profiles")
    async def profiles() -> dict:
        from jarvis.web.state import PROFILES

        return {
            "current": state.profiles.current_id,
            "items": [
                {"id": p.id, "title": p.title, "description": p.description,
                 "assist": p.assist}
                for p in PROFILES.values()
            ],
        }

    @app.post("/api/profile")
    async def set_profile(body: ProfileIn) -> dict:
        try:
            profile = state.profiles.set(body.name)
        except KeyError:
            return JSONResponse(
                {"detail": f"unknown profile {body.name}"}, status_code=404
            )
        return {"current": profile.id, "title": profile.title,
                "assist": profile.assist}

    @app.post("/api/command")
    async def command(body: CommandIn) -> dict:
        text = body.text.strip()
        if not text:
            return JSONResponse({"detail": "empty"}, status_code=400)

        result = state.router.handle(text, source="web")

        # LLM-стриминг: чанки уходят по WS, здесь — только маркер
        if result.stream and result.source == "llm":
            extra = result.extra
            state.bcast.push("router_start", {"text": extra.get("text", text)})
            loop = asyncio.get_running_loop()
            payload = {"text": extra.get("text", text), "source": "llm",
                       "reply": "", "streaming": True}

            def _on_chunk(chunk: str) -> None:
                state.bcast.push("llm_chunk", {"chunk": chunk})

            def _run() -> None:
                full = state.router.stream_llm(
                    extra.get("text", text),
                    extra.get("source", "web"),
                    _on_chunk,
                )
                state.bcast.push("router_end", {
                    "text": payload["text"], "reply": full, "source": "llm",
                })
                # озвучка ответа после стрима
                if state.tts is not None and state.tts_enabled and full:
                    state.bcast.push("orb_state", {"state": "speaking"})
                    state.tts.speak_sync(full, timeout=30)

            loop.run_in_executor(None, _run)
            return {"ok": True, "source": "llm", "streaming": True,
                    "reply": "", "needs_confirm": False}

        # немедленный ответ (правила / веб-поиск / fallback) + озвучка
        reply = result.reply
        if reply and state.tts is not None and state.tts_enabled:
            loop = asyncio.get_running_loop()
            def _speak() -> None:
                state.bcast.push("orb_state", {"state": "speaking"})
                state.tts.speak_sync(reply, timeout=30)
            loop.run_in_executor(None, _speak)

        return {
            "ok": True,
            "reply": reply,
            "action": result.action,
            "source": result.source,
            "needs_confirm": result.needs_confirm,
            "confirm_id": result.confirm_id,
        }

    @app.post("/api/confirm")
    async def confirm(body: ConfirmIn) -> dict:
        if not body.ok:
            state.pop_pending(body.id)
            state.audit.add("web", "confirm_rejected")
            return {"ok": True, "reply": "Отменено."}
        reply = state.router.run_pending(body.id)
        return {"ok": True, "reply": reply}

    # ------------------------------------------------------------------
    # Управление (калибровка, курсор, микрофон, СТОП, TTS)
    # ------------------------------------------------------------------
    @app.post("/api/calibrate")
    async def calibrate() -> dict:
        if state.tracker is not None:
            state.tracker.request_calibration()
            state.audit.add("web", "calibrate")
            return {"ok": True, "reply": "Калибровка запущена."}
        return {"ok": False, "reply": "Трекер недоступен."}

    @app.post("/api/recenter")
    async def recenter() -> dict:
        if state.pointer is not None:
            state.pointer.start_recenter()
            state.audit.add("web", "recenter")
            return {"ok": True, "reply": "Смотрите на центр экрана ~1.5 с."}
        return {"ok": False, "reply": "Курсор недоступен."}

    @app.post("/api/offset/reset")
    async def offset_reset() -> dict:
        if state.pointer is not None:
            state.pointer.reset_offset()
            state.audit.add("web", "offset_reset")
            return {"ok": True, "reply": "Смещение сброшено."}
        return {"ok": False, "reply": "Курсор недоступен."}

    @app.post("/api/pointer")
    async def pointer_toggle(body: EnabledIn) -> dict:
        if state.pointer is None:
            return {"ok": False, "enabled": None}
        state.pointer.set_enabled(body.enabled)
        state.audit.add("web", "pointer", detail=str(body.enabled))
        return {"ok": True, "enabled": state.pointer.enabled}

    @app.post("/api/emergency")
    async def emergency(body: EnabledIn | None = None) -> dict:
        on = True if body is None else body.enabled
        from jarvis.web.router import CommandRouter  # локально — нет цикла

        result = state.router._emergency(on, "web")
        return {"ok": True, "active": state.emergency, "reply": result.reply}

    @app.post("/api/mic")
    async def mic(body: EnabledIn) -> dict:
        if state.voice is None:
            return {"ok": False, "detail": "voice unavailable"}
        if body.enabled:
            state.voice.start()
            state.mic_muted = False
            state.audit.add("web", "mic_on")
        else:
            state.voice.stop()
            state.mic_muted = True
            state.audit.add("web", "mic_off")
        state.bcast.push("mic", {"muted": state.mic_muted})
        return {"ok": True, "muted": state.mic_muted}

    @app.post("/api/tts")
    async def tts_toggle(body: EnabledIn) -> dict:
        state.tts_enabled = body.enabled
        if not body.enabled and state.tts is not None:
            state.tts.stop()
        state.audit.add("web", "tts", detail=str(body.enabled))
        state.bcast.push("tts", {"enabled": state.tts_enabled})
        return {"ok": True, "enabled": state.tts_enabled}

    @app.post("/api/speak")
    async def speak(body: CommandIn) -> dict:
        if state.tts is None or not state.tts_enabled:
            return {"ok": False, "detail": "tts off"}
        state.tts.speak(body.text)
        return {"ok": True}

    return app
