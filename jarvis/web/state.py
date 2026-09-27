"""Состояние web-приложения: шина, сервисы, профили, аудит, рассылка WS."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarvis.core.config import AppConfig
from jarvis.core.events import (
    BotReplyEvent,
    CameraFrameEvent,
    EventBus,
    GazeSample,
    HandSample,
    PointerStatusEvent,
    RecenteringEvent,
    TrackerStatusEvent,
    VoiceCommandEvent,
    VoicePartialEvent,
    VoiceStateEvent,
)

log = logging.getLogger("jarvis.web")

_THROTTLE = {"gaze": 0.08, "hand": 0.10}   # мин. интервал пересылки, с
_AUDIT_MAX = 300
_PENDING_TTL = 120.0                        # жизнь confirm-запроса, с


def _py(value):
    """numpy/скаляр → чистый Python-тип (np.bool_ не сериализуется в JSON)."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_py(v) for v in value]
    try:
        import numpy as np

        if isinstance(value, np.bool_):
            return bool(value)
        if isinstance(value, np.integer):
            return int(value)
        if isinstance(value, np.floating):
            return float(value)
        if isinstance(value, np.ndarray):
            return _py(value.tolist())
    except ImportError:
        pass
    if isinstance(value, (int, float)):
        return value
    return str(value)


def json_default(obj):
    """default= для json.dumps — гасит numpy-типы из eyetrax/mediapipe."""
    return _py(obj)


# ---------------------------------------------------------------------------
# Профили (ТЗ §4: Дом / Ассист / Учёба)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Profile:
    id: str
    title: str
    description: str
    assist: bool = False          # крупный UI, голосовые подсказки
    sensitivity: float = 1.0      # множитель чувствительности курсора


PROFILES: dict[str, Profile] = {
    "home": Profile(
        "home", "Дом", "Бытовые команды, браузер, презентации, музыка",
    ),
    "assist": Profile(
        "assist", "Ассист", "Крупные элементы, минимум движений, подсказки",
        assist=True, sensitivity=1.25,
    ),
    "study": Profile(
        "study", "Учёба", "Переключение окон, рутина, свои команды (P1)",
    ),
}


class ProfileManager:
    """Текущий профиль: переключение + сохранение в models/profile.json."""

    def __init__(self, path: Path, on_change) -> None:
        self._path = path
        self._on_change = on_change
        self._current = "home"
        try:
            if path.exists():
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved.get("current") in PROFILES:
                    self._current = saved["current"]
        except Exception:
            pass

    @property
    def current_id(self) -> str:
        return self._current

    @property
    def current(self) -> Profile:
        return PROFILES[self._current]

    def set(self, profile_id: str) -> Profile:
        if profile_id not in PROFILES:
            raise KeyError(profile_id)
        self._current = profile_id
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"current": profile_id}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            log.warning("не удалось сохранить профиль в %s", self._path)
        self._on_change(self.current)
        return self.current


# ---------------------------------------------------------------------------
# Аудит действий (ТЗ §9: журнал, где видно, что Berta делала)
# ---------------------------------------------------------------------------
@dataclass
class AuditEntry:
    ts: float
    source: str       # voice | web | router | system
    action: str
    ok: bool = True
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "ts": self.ts, "source": self.source, "action": self.action,
            "ok": self.ok, "detail": self.detail,
        }


class AuditLog:
    """Кольцевой буфер + дозапись в models/audit.jsonl (только локально)."""

    def __init__(self, path: Path | None = None) -> None:
        self._entries: deque[AuditEntry] = deque(maxlen=_AUDIT_MAX)
        self._path = path
        self._lock = threading.Lock()

    def add(
        self, source: str, action: str, ok: bool = True, detail: str = ""
    ) -> AuditEntry:
        entry = AuditEntry(
            ts=time.time(), source=source, action=action, ok=ok, detail=detail
        )
        with self._lock:
            self._entries.append(entry)
            if self._path is not None:
                try:
                    self._path.parent.mkdir(parents=True, exist_ok=True)
                    with self._path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(entry.as_dict(), ensure_ascii=False) + "\n")
                except OSError:
                    pass
        return entry

    def tail(self, n: int = 50) -> list[dict]:
        with self._lock:
            items = list(self._entries)[-n:]
        return [e.as_dict() for e in reversed(items)]


# ---------------------------------------------------------------------------
# Рассылка в WebSocket-клиенты (из любых потоков шины)
# ---------------------------------------------------------------------------
class Broadcaster:
    """Клиенты — asyncio.Queue; push безопасен из потоков шины."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queues: set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        self._last_sent: dict[str, float] = {}

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def add(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._queues.add(q)

    def remove(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._queues.discard(q)

    def push(self, type_: str, data: Any, *, throttle_key: str | None = None) -> None:
        """Отправить сообщение всем клиентам; throttle_key — троттлинг по типу."""
        if throttle_key:
            min_interval = _THROTTLE.get(throttle_key, 0.0)
            now = time.monotonic()
            last = self._last_sent.get(throttle_key, 0.0)
            if now - last < min_interval:
                return
            self._last_sent[throttle_key] = now
        loop = self._loop
        if loop is None:
            return
        msg = {"type": type_, "data": data}
        with self._lock:
            queues = list(self._queues)
        for q in queues:
            try:
                loop.call_soon_threadsafe(self._safe_put, q, msg)
            except RuntimeError:
                pass  # цикл уже закрыт

    @staticmethod
    def _safe_put(q: asyncio.Queue, msg: dict) -> None:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass


# ---------------------------------------------------------------------------
# Состояние приложения
# ---------------------------------------------------------------------------
class AppState:
    """Всё общее, что нужно web-серверу: сервисы, флаги, шина, буфер кадра."""

    def __init__(
        self,
        config: AppConfig,
        bus: EventBus,
        *,
        tracker=None,
        pointer=None,
        voice=None,
        tts=None,
        project_root: Path | None = None,
    ) -> None:
        self.config = config
        self.bus = bus
        self.tracker = tracker
        self.pointer = pointer
        self.voice = voice
        self.tts = tts
        self.project_root = project_root or Path.cwd()

        data_dir = self.project_root / "models"
        self.audit = AuditLog(data_dir / "audit.jsonl")
        self.profiles = ProfileManager(
            data_dir / "profile.json", self._on_profile_change
        )
        self.bcast = Broadcaster()

        # флаги P0
        self.emergency = False
        self.mic_muted = False
        self.tts_enabled = bool(tts is not None and tts.config.enabled)
        self.voice_model_ok = voice is not None

        # агрегаты последних событий (для snapshot'а при коннекте)
        self.tracker_status: dict = {"state": "STARTING", "message": "", "fps": 0.0}
        self.pointer_status: dict = {"enabled": True, "message": ""}
        self.gaze: dict = {"x": None, "y": None, "face": False, "blink": False, "noise": None}
        self.hand: dict = {"x": None, "y": None, "pinch": None, "gesture": None,
                           "landmarks": None, "confidence": 0.0}
        self.voice_partial: dict = {"text": "", "final": False}
        self.last_command: dict = {"action": None, "text": "", "ts": 0.0}

        # буфер JPEG для MJPEG-потока
        self.camera_jpeg: bytes | None = None
        self.camera_seq: int = 0

        # ожидающие подтверждения опасных действий (ТЗ §9)
        self._pending: dict[str, dict] = {}
        self._pending_lock = threading.Lock()

        # подписки шины
        bus.subscribe(TrackerStatusEvent, self._on_tracker_status)
        bus.subscribe(PointerStatusEvent, self._on_pointer_status)
        bus.subscribe(GazeSample, self._on_gaze)
        bus.subscribe(HandSample, self._on_hand)
        bus.subscribe(VoicePartialEvent, self._on_voice_partial)
        bus.subscribe(VoiceCommandEvent, self._on_voice_command)
        bus.subscribe(BotReplyEvent, self._on_bot_reply)
        bus.subscribe(VoiceStateEvent, self._on_voice_state)
        bus.subscribe(RecenteringEvent, self._on_recentering)
        bus.subscribe(CameraFrameEvent, self._on_camera)

    # ------------------------------------------------------------------
    # Обработчики шины (любой поток) → агрегат + WS
    # ------------------------------------------------------------------
    def _on_tracker_status(self, e: TrackerStatusEvent) -> None:
        self.tracker_status = {
            "state": e.state, "message": e.message, "fps": e.fps,
        }
        self.bcast.push("tracker_status", self.tracker_status)

    def _on_pointer_status(self, e: PointerStatusEvent) -> None:
        self.pointer_status = {"enabled": e.enabled, "message": e.message}
        self.bcast.push("pointer_status", self.pointer_status)

    def _on_gaze(self, e: GazeSample) -> None:
        self.gaze = {
            "x": _py(e.x), "y": _py(e.y),
            "face": bool(e.face), "blink": bool(e.blink),
            "noise": _py(e.noise),
        }
        self.bcast.push("gaze", self.gaze, throttle_key="gaze")

    def _on_hand(self, e: HandSample) -> None:
        from jarvis.input.gestures import classify_gesture

        gesture = classify_gesture(e.landmarks, e.pinch)
        # лендмарки тяжелее — реже (front рисует скелет ~10 FPS)
        lms = e.landmarks
        self.hand = {
            "x": _py(e.x), "y": _py(e.y), "pinch": _py(e.pinch),
            "gesture": gesture,
            "landmarks": [[float(px), float(py)] for px, py in lms] if lms else None,
            "confidence": float(e.confidence or 0.0),
        }
        self.bcast.push("hand", self.hand, throttle_key="hand")

    def _on_voice_partial(self, e: VoicePartialEvent) -> None:
        self.voice_partial = {"text": e.text, "final": e.final}
        self.bcast.push("voice_partial", self.voice_partial)

    def _on_voice_command(self, e: VoiceCommandEvent) -> None:
        self.last_command = {
            "action": e.action, "text": e.text, "ts": e.timestamp,
        }
        self.bcast.push("voice_command", self.last_command)
        if e.action:
            self.audit.add("voice", e.action, detail=e.text)
            self.bcast.push("audit", self.audit.tail(1))
        elif e.text:
            # фраза без известной команды — пробуем роутер (LLM/правила)
            self._route_from_voice(e.text)

    def _on_bot_reply(self, e: BotReplyEvent) -> None:
        """Ответ Berta (веб-поиск / LLM) → реплики в чате панели."""
        if e.query:
            self.bcast.push("voice_user", {"text": e.query})
        if e.text:
            # дублируем в консоль: иначе голосовой ответ слышен, но не виден
            log.info("Berta (%s): %s", e.source or "llm", e.text)
            self.bcast.push("router", {
                "text": e.query, "reply": e.text, "source": e.source,
                "needs_confirm": False, "confirm_id": None,
            })

    def _on_voice_state(self, e: VoiceStateEvent) -> None:
        """Состояние голосового агента (LiveKit) → Orb в браузере."""
        self.bcast.push("orb_state", {"state": e.state})

    def _route_from_voice(self, text: str) -> None:
        """Голос → WebSearch/LLM → TTS (STT → ответ → озвучка).

        Конвейер в отдельном потоке (не блокирует STT):
          1. router.handle() → web search / быстрые правила / LLM-streaming
          2. ответ → TTS (speak_sync) + orb speaking
          3. orb → idle
        """
        import threading as _t

        def _run() -> None:
            try:
                from jarvis.web.router import CommandRouter

                router = getattr(self, "router", None)
                if router is None:
                    return

                # орб: думает
                self.bcast.push("orb_state", {"state": "thinking"})

                result = router.handle(text, source="voice")

                # --- быстрый ответ: веб-поиск / правила / fallback ---
                if result.reply and not (result.stream and result.source == "llm"):
                    log.info("Berta (%s): %s", result.source, result.reply)
                    self.bcast.push("router", {
                        "text": text, "reply": result.reply,
                        "source": result.source,
                        "needs_confirm": result.needs_confirm,
                        "confirm_id": result.confirm_id,
                    })
                    if self.tts is not None and self.tts_enabled:
                        self.bcast.push("orb_state", {"state": "speaking"})
                        # speak_sync дожидается конца озвучки → orb не гаснет прежде
                        self.tts.speak_sync(result.reply, timeout=30)

                # --- стриминг LLM: чанки → чат → TTS ---
                elif result.stream and result.source == "llm":
                    self.bcast.push("router_start", {"text": text})

                    def _on_chunk(chunk: str) -> None:
                        self.bcast.push("llm_chunk", {"chunk": chunk})
                        self.bcast.push("orb_state", {"state": "speaking"})

                    full = router.stream_llm(text, "voice", _on_chunk)
                    self.bcast.push("router_end", {
                        "text": text, "reply": full, "source": "llm",
                    })
                    if full:
                        log.info("Berta (llm): %s", full)
                    if self.tts is not None and self.tts_enabled and full:
                        self.bcast.push("orb_state", {"state": "speaking"})
                        self.tts.speak_sync(full, timeout=30)

                # --- fallback без ответа (confirm и т.п.) ---
                else:
                    self.bcast.push("router", {
                        "text": text, "reply": result.reply,
                        "source": result.source,
                        "needs_confirm": result.needs_confirm,
                        "confirm_id": result.confirm_id,
                    })
            except Exception:
                log.exception("роутер (голос) упал")
            finally:
                self.bcast.push("orb_state", {"state": "idle"})

        _t.Thread(target=_run, name="web-router-voice", daemon=True).start()

    def _on_recentering(self, e: RecenteringEvent) -> None:
        self.bcast.push("recentering", {"active": e.active})

    def _on_camera(self, e: CameraFrameEvent) -> None:
        try:
            import cv2

            ok, buf = cv2.imencode(
                ".jpg", e.frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82]
            )
            if ok:
                self.camera_jpeg = buf.tobytes()
                self.camera_seq += 1
        except Exception:
            log.debug("JPEG-кодирование кадра не удалось", exc_info=True)

    # ------------------------------------------------------------------
    # Подтверждения опасных действий
    # ------------------------------------------------------------------
    def add_pending(self, payload: dict) -> str:
        cid = uuid.uuid4().hex[:12]
        with self._pending_lock:
            now = time.time()
            self._pending = {
                k: v for k, v in self._pending.items()
                if now - v.get("_ts", 0) < _PENDING_TTL
            }
            payload["_ts"] = now
            payload["_exec"] = payload.get("_exec")  # callable | None
            self._pending[cid] = payload
        return cid

    def pop_pending(self, cid: str) -> dict | None:
        with self._pending_lock:
            return self._pending.pop(cid, None)

    # ------------------------------------------------------------------
    # Профиль → применение к сервисам
    # ------------------------------------------------------------------
    def _on_profile_change(self, profile: Profile) -> None:
        if self.pointer is not None:
            try:
                self.pointer._config.sensitivity = profile.sensitivity
            except Exception:
                pass
        self.audit.add("system", "profile", detail=profile.id)
        self.bcast.push("profile", {
            "current": profile.id,
            "title": profile.title,
            "assist": profile.assist,
        })

    # ------------------------------------------------------------------
    # Снимок состояния (GET /api/status + WS snapshot)
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        cursor_enabled = self.pointer.enabled if self.pointer is not None else None
        return {
            "tracker": self.tracker_status,
            "pointer": {**self.pointer_status, "enabled": cursor_enabled},
            "gaze": self.gaze,
            "hand": self.hand,
            "voice_partial": self.voice_partial,
            "last_command": self.last_command,
            "profile": {
                "current": self.profiles.current_id,
                "title": self.profiles.current.title,
                "assist": self.profiles.current.assist,
            },
            "profiles": [
                {"id": p.id, "title": p.title, "description": p.description,
                 "assist": p.assist}
                for p in PROFILES.values()
            ],
            "emergency": self.emergency,
            "mic_muted": self.mic_muted,
            "tts_enabled": self.tts_enabled,
            "voice_model_ok": self.voice_model_ok,
            "audit": self.audit.tail(30),
        }
