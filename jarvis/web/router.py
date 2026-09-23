"""Роутер команд Berta (ТЗ §2).

Правила (regex/подстроки) → действие ≤100 мс; сложные фразы → локальная
LLM (Ollama + Qwen2.5, стриминг); если LLM недоступна — fallback на
жёсткие правила. Опасные действия требуют подтверждения (ТЗ §9).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import webbrowser
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import quote_plus

log = logging.getLogger("jarvis.router")

_LLM_TIMEOUT = 25.0

# «открой X» → что запускаем
_APPS: dict[str, str] = {
    "блокнот": "notepad",
    "калькулятор": "calc",
    "проводник": "explorer",
    "explorer": "explorer",
    "браузер": "msedge",
    "интернет": "msedge",
    "edge": "msedge",
    "хром": "chrome",
    "chrome": "chrome",
    "команд": "cmd",          # командная строка
    "cmd": "cmd",
    "powershell": "powershell",
    "word": "winword",
    "ворт": "winword",
    "excel": "excel",
    "таблиц": "excel",
    "powerpoint": "powerpnt",
    "презентац": "powerpnt",
    "vscode": "code",
    "код": "code",
    "visual studio code": "code",
    "paint": "mspaint",
    "кино": "msedge",
    "музыку": "msedge",
    "spotify": "spotify",
    "telegram": "telegram",
}

# опасные действия (ТЗ §9: без подтверждения не выполняются)
_DANGEROUS = re.compile(
    r"(удал|сотри|стир)|"
    r"(закрой\s+(всё|окно|без\s+сохранения|принудительно))|"
    r"(выключи\s+(компьютер|пк|систему|комп))|"
    r"(перезагруз)|"
    r"(форматир)|"
    r"(отключ(и|ить)?\s+(питание|подачу))",
    re.IGNORECASE,
)

_PROFILE_RE = re.compile(
    r"(профиль|режим)\s*(дом|ассист|уч[её]ба)|"
    r"(дом|ассист|уч[её]ба)\s*(режим|профиль)|"
    r"режим\s*(дом|ассист|уч[её]ба)",
    re.IGNORECASE,
)
_OPEN_RE = re.compile(
    r"(открой|запусти|включи|стартан)\s+(.+)", re.IGNORECASE
)
_SEARCH_RE = re.compile(
    r"(найди|погода|поиск|загугли|загугли|погоду)\s*(.*)", re.IGNORECASE
)


@dataclass
class RouteResult:
    """Итог маршрутизации одного запроса."""

    reply: str = ""
    action: str | None = None          # машинное имя (для аудита)
    source: str = "rules"              # rules | llm | confirm
    needs_confirm: bool = False
    confirm_id: str | None = None
    stream: bool = False               # ответ будет стримиться по WS
    extra: dict = field(default_factory=dict)


class CommandRouter:
    """Текст → действие/ответ. Правила первыми, LLM — fallback (ТЗ §2)."""

    def __init__(self, state) -> None:
        """
        state — jarvis.web.state.AppState (даёт pointer/tracker/bus/audit/…).
        """
        self._state = state
        self._llm_model = os.environ.get("LLM_MODEL", "qwen2.5:3b")
        self._llm_lock = threading.Lock()

    # ==================================================================
    # Вход
    # ==================================================================
    def handle(self, text: str, *, source: str = "web") -> RouteResult:
        """Синхронная маршрутизация (≤100 мс для правил)."""
        t = (text or "").strip()
        if not t:
            return RouteResult(reply="Пустая команда.", source="rules")

        # --- 1. экстренная остановка / включение ---
        low = t.lower()
        if any(w in low for w in ("экстр", "стоп режим", "аварийн")) or low in (
            "стоп", "хватит", "пауза режим"
        ):
            return self._emergency(True, source)
        if any(w in low for w in ("сними экстр", "сброс стоп", "продолжай", "отбой")):
            return self._emergency(False, source)

        # --- 2. профили (ТЗ §4) ---
        if _PROFILE_RE.search(t):
            pid = None
            for key, value in (
                ("дом", "home"), ("ассист", "assist"),
                ("учёба", "study"), ("учеба", "study"),
            ):
                if key in low:
                    pid = value
                    break
            if pid:
                return self._set_profile(pid, source)

        # --- 3. опасные действия → подтверждение (ТЗ §9) ---
        if _DANGEROUS.search(t):
            return self._ask_confirm(t, source)

        # --- 4. известные голосовые команды → шина (PointerService) ---
        from jarvis.voice.commands import parse_command

        cmd = parse_command(t)
        if cmd is not None and cmd.action in (
            "click", "double_click", "right_click", "scroll"
        ):
            from jarvis.core.events import VoiceCommandEvent

            self._state.bus.publish(VoiceCommandEvent(
                action=cmd.action, args=cmd.args, text=t,
            ))
            labels = {
                "click": "Клик", "double_click": "Двойной клик",
                "right_click": "Правый клик", "scroll": "Скролл",
            }
            self._state.audit.add(source, cmd.action, detail=t)
            return RouteResult(
                reply=f"{labels[cmd.action]} — выполнено.",
                action=cmd.action, source="rules",
            )

        # --- 5. вкл/выкл курсора (короткая фраза или явно про курсор) ---
        if cmd is not None and cmd.action in ("enable", "disable"):
            short = len(t.split()) <= 3
            about_mouse = any(
                w in low for w in ("курсор", "мыш", "управл", "глаз", "взгляд")
            )
            if short or about_mouse:
                self._pointer_enabled(cmd.action == "enable")
                self._state.audit.add(source, cmd.action, detail=t)
                on = cmd.action == "enable"
                return RouteResult(
                    reply="Курсор включён." if on else "Курсор выключен.",
                    action=cmd.action, source="rules",
                )
            cmd = None  # «включ музыку» и т.п. — не команда курсора
        if cmd is not None and cmd.action == "calibrate":
            if self._state.tracker is not None:
                self._state.tracker.request_calibration()
            self._state.audit.add(source, "calibrate", detail=t)
            return RouteResult(
                reply="Запускаю калибровку — смотрите на зелёные точки.",
                action="calibrate", source="rules",
            )

        # --- 6. открыть приложение ---
        m = _OPEN_RE.search(t)
        if m:
            target = m.group(2).strip(" .!؟…")
            res = self._open_app(target, source)
            if res is not None:
                return res

        # --- 7. поиск/погода → браузер (fallback ТЗ §5) ---
        m = _SEARCH_RE.search(t)
        if m and (m.group(1) in ("найди", "загугли", "поиск", "погода", "погоду")):
            query = (m.group(2) or "").strip() or m.group(1)
            return self._search(query, source)

        # --- 8. известные короткие ответы (правила — раньше LLM, ТЗ §2) ---
        quick = self._quick_reply(t, source)
        if quick is not None:
            return quick

        # --- 9. LLM (Ollama), фоллбэк — правила ---
        llm = self._try_llm_route(t, source)
        if llm is not None:
            return llm

        # --- 10. fallback-правила ---
        return self._fallback(t, source)

    # ==================================================================
    # Действия правил
    # ==================================================================
    def _emergency(self, on: bool, source: str) -> RouteResult:
        st = self._state
        st.emergency = on
        if st.pointer is not None:
            st.pointer.set_enabled(not on)
        if on and st.tts is not None:
            st.tts.stop()
        st.audit.add(source, "emergency_on" if on else "emergency_off")
        st.bcast.push("emergency", {"active": on})
        return RouteResult(
            reply=("Экстренная остановка. Курсор выключен, озвучка прервана."
                   if on else "Режим снят. Курсор снова управляется."),
            action="emergency", source="rules",
        )

    def _set_profile(self, pid: str, source: str) -> RouteResult:
        try:
            profile = self._state.profiles.set(pid)
        except KeyError:
            return RouteResult(reply=f"Неизвестный профиль: {pid}", source="rules")
        return RouteResult(
            reply=f"Профиль «{profile.title}» включён.",
            action="profile", source="rules", extra={"profile": pid},
        )

    def _pointer_enabled(self, on: bool) -> None:
        if self._state.pointer is not None:
            self._state.pointer.set_enabled(on)

    def _open_app(self, target: str, source: str) -> RouteResult | None:
        low = target.lower()
        exe = None
        for key, value in _APPS.items():
            if key in low:
                exe = value
                break
        if exe is None:
            return None
        try:
            subprocess.Popen(  # noqa: S603 — локальный запуск по имени
                exe, shell=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._state.audit.add(source, "open_app", detail=exe)
            return RouteResult(
                reply=f"Запускаю «{target}».", action="open_app", source="rules",
            )
        except FileNotFoundError:
            self._state.audit.add(source, "open_app", ok=False, detail=exe)
            return RouteResult(
                reply=f"Не нашёл «{target}» на компьютере.",
                action="open_app", source="rules",
            )

    def _search(self, query: str, source: str) -> RouteResult:
        url = f"https://yandex.ru/search/?text={quote_plus(query)}"
        try:
            webbrowser.open(url)
            self._state.audit.add(source, "search", detail=query)
            return RouteResult(
                reply=f"Ищу «{query}» в браузере.",
                action="search", source="rules",
            )
        except Exception as exc:
            self._state.audit.add(source, "search", ok=False, detail=str(exc))
            return RouteResult(
                reply="Браузер не открылся.", action="search", source="rules",
            )

    def _ask_confirm(self, text: str, source: str) -> RouteResult:
        """Опасное действие → подтверждение (выполнится после confirm)."""
        st = self._state

        def _exec() -> str:
            # правил-подтверждение: для MVP отвечаем, что действие снято
            # пользователем (реальные деструктивные операции не выполняем
            # автоматически — только осознанный confirm из UI).
            st.audit.add("confirm", "dangerous_ack", detail=text)
            return "Подтверждено. (Операция помечена в аудите; осторожно!)"

        cid = st.add_pending({
            "text": text, "source": source, "_exec": _exec, "_ts": 0.0,
        })
        st.audit.add(source, "confirm_required", detail=text)
        st.bcast.push("confirm", {"id": cid, "text": text})
        return RouteResult(
            reply="Это опасное действие. Подтвердите в окне на сайте.",
            action="confirm", source="confirm",
            needs_confirm=True, confirm_id=cid,
        )

    def run_pending(self, cid: str) -> str:
        """Выполнить подтверждённое действие. Возвращает ответ."""
        item = self._state.pop_pending(cid)
        if item is None:
            return "Подтверждение истекло или не найдено."
        fn: Callable[[], str] | None = item.get("_exec")
        if fn is None:
            return "Нечего выполнять."
        try:
            return fn()
        except Exception as exc:
            log.exception("подтверждённое действие упало")
            self._state.audit.add("confirm", "exec", ok=False, detail=str(exc))
            return f"Ошибка: {exc}"

    # ==================================================================
    # LLM (Ollama) — стриминг, фоллбэк на правила
    # ==================================================================
    def _llm_available(self) -> bool:
        """Сервер Ollama жив? Кэшируем ответ на 30 с (не дёргаем на каждый чих)."""
        import time as _t

        now = _t.monotonic()
        cached = getattr(self, "_llm_cached", None)
        if cached is not None and now - cached[0] < 30.0:
            return cached[1]
        ok = False
        try:
            import ollama

            ollama.list()
            ok = True
        except Exception as exc:
            log.debug("ollama недоступна: %s", exc)
        self._llm_cached = (now, ok)
        return ok

    def _try_llm_route(self, text: str, source: str) -> RouteResult | None:
        """Пробуем Ollama; недоступна → None (пойдём в fallback-правила)."""
        try:
            import ollama  # noqa: F401
        except ImportError:
            return None
        if not self._llm_available():
            return None
        # чанки уходят асинхронно через stream_llm (WS llm_chunk)
        return RouteResult(
            reply="", source="llm", stream=True,
            extra={"text": text, "source": source},
        )

    def stream_llm(self, text: str, source: str, on_chunk) -> str:
        """Стримит ответ Ollama в on_chunk(str); возвращает полный текст.

        При недоступности LLM — синхронный fallback-правила.
        """
        full: list[str] = []
        try:
            import ollama

            messages = [
                {
                    "role": "system",
                    "content": (
                        "Ты — Berta, локальный ассистент для Windows. "
                        "Отвечай кратко, по-русски, дружелюбно. "
                        "Не используй разметку markdown."
                    ),
                },
                {"role": "user", "content": text},
            ]
            with self._llm_lock:
                stream = ollama.chat(
                    model=self._llm_model, messages=messages, stream=True,
                )
                for part in stream:
                    chunk = (part.get("message") or {}).get("content") or ""
                    if chunk:
                        full.append(chunk)
                        on_chunk(chunk)
            answer = "".join(full)
            self._state.audit.add(source, "llm_reply", detail=text[:120])
            return answer
        except Exception as exc:
            log.info("LLM недоступна (%s) — fallback на правила", exc)
            fb = self._fallback(text, source)
            on_chunk(fb.reply)
            return fb.reply

    # ==================================================================
    # Известные короткие ответы и fallback-правила (ТЗ: «если LLM не
    # справилась — жёсткие правила»)
    # ==================================================================
    _QUICK: tuple[tuple[tuple[str, ...], tuple[str, str]], ...] = (
        (("привет", "здравств", "хай"), ("Привет! Я Berta, чем помочь?", "greet")),
        (("спасибо", "благодар"), ("Пожалуйста!", "thanks")),
        (("который час", "текущее время"), (None, "time")),  # None → _now_str
        (("как дела",), ("Работаю в штатном режиме.", "status")),
        (("помощь", "что умеешь", "команды"), (
            "Умею: открыть приложение, клик и скролл голосом, поиск в "
            "браузере, профили Дом/Ассист/Учёба, калибровку камеры "
            "и экстренную остановку.", "help",
        )),
    )

    def _quick_reply(self, text: str, source: str) -> RouteResult | None:
        low = text.lower()
        for keys, (reply, action) in self._QUICK:
            if any(k in low for k in keys):
                answer = self._now_str() if reply is None else reply
                self._state.audit.add(source, action, detail=text)
                return RouteResult(reply=answer, action=action, source="rules")
        return None

    def _fallback(self, text: str, source: str) -> RouteResult:
        quick = self._quick_reply(text, source)
        if quick is not None:
            return quick
        self._state.audit.add(source, "no_understand", detail=text)
        return RouteResult(
            reply=(
                "Не совсем поняла. Попробуйте: «открой блокнот», «клик», "
                "«найди погоду» или скажите что-нибудь сложное — "
                "подключу локальную LLM, если Ollama запущена."
            ),
            action="no_understand", source="rules",
        )

    @staticmethod
    def _now_str() -> str:
        import datetime as dt

        return dt.datetime.now().strftime("%H:%M")
