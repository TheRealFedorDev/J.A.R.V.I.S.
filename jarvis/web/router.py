"""Роутер команд Berta (ТЗ §2).

Правила (regex/подстроки) → действие ≤100 мс; сложные фразы → локальная
LLM (Ollama + llama3.2, стриминг); если LLM недоступна — fallback на
жёсткие правила. Опасные действия требуют подтверждения (ТЗ §9).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import threading
import time
import webbrowser
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import quote_plus

from jarvis.core.backends import (
    AllProvidersFailed,
    CLOUD_PROVIDERS,
    Provider,
    cloud_api_key,
    cloud_auth_headers,
    provider_chain,
    run_with_fallback,
)

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
        self._llm_model = os.environ.get("LLM_MODEL", "llama3.2:3b")
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

        # --- 7. быстрый веб-поиск: сначала пробуем ответить сами (погода,
        # курс, факты) — и только если не вышло, открываем браузер (ТЗ §5) ---
        m = _SEARCH_RE.search(t)
        if m and (m.group(1) in ("найди", "загугли", "поиск", "погода", "погоду")):
            # ищем по исходной фразе: «Какая сейчас погода в Долгопрудном»
            # целиком, а не по обрывку «в долгопрудном»
            answer = self._try_web_search(t, source)
            if answer:
                return RouteResult(
                    reply=answer, action="web_search", source="search",
                )
            return self._search(t.strip(" .!؟…"), source)

        # --- 8. известные короткие ответы (правила — раньше LLM, ТЗ §2) ---
        quick = self._quick_reply(t, source)
        if quick is not None:
            return quick

        # --- 8.5 веб-поиск и для прочих фраз (курс валют, «сколько стоит…») ---
        search_answer = self._try_web_search(t, source)
        if search_answer:
            return RouteResult(
                reply=search_answer, action="web_search", source="search",
            )

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
            log.info("router: open_app «%s» → %s (%s)", target, exe, source)
            return RouteResult(
                reply=f"Запускаю «{target}».", action="open_app", source="rules",
            )
        except FileNotFoundError:
            self._state.audit.add(source, "open_app", ok=False, detail=exe)
            log.warning("router: open_app «%s» — %s не найден", target, exe)
            return RouteResult(
                reply=f"Не нашёл «{target}» на компьютере.",
                action="open_app", source="rules",
            )

    def _search(self, query: str, source: str) -> RouteResult:
        # в браузер уходит ИСХОДНАЯ фраза: из «погода в Долгопрудном»
        # нельзя выдёргивать «в долгопрудном» — это мусорный запрос
        url = f"https://yandex.ru/search/?text={quote_plus(query)}"
        try:
            self._state.bcast.push("status", {
                "text": f"Открываю поиск: {query}", "kind": "browser",
            })
            webbrowser.open(url)
            self._state.audit.add(source, "search", detail=query)
            log.info("router: browser search «%s» (%s)", query, source)
            return RouteResult(
                reply=f"Ищу «{query}» в браузере.",
                action="search", source="rules",
            )
        except Exception as exc:
            self._state.audit.add(source, "search", ok=False, detail=str(exc))
            log.warning("router: browser search «%s» упал: %s", query, exc)
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
    # LLM: цепочка провайдеров — стриминг, фоллбэк на правила
    # ==================================================================
    def _probe_llm_provider(self, provider: Provider) -> str:
        """Проверка доступности одного LLM-провайдера. Ошибка = он не в цепочке."""
        if provider.name in ("ollama", "local"):
            import ollama

            ollama.list()
            model = provider.arg or self._llm_model
            return f"ollama:{model}"
        spec = CLOUD_PROVIDERS.get(provider.name)
        if spec is None:
            raise RuntimeError(f"неизвестный LLM-провайдер «{provider.name}»")
        if spec.kind != "llm":
            raise RuntimeError(f"{provider.name} — провайдер {spec.kind}, а не LLM")
        if not cloud_api_key(provider.name):
            raise LookupError(f"не задан {spec.env_var}")
        if not provider.arg:
            raise RuntimeError(f"для {provider.name} нужна модель: {provider.name}:<model>")
        return f"{provider.name}:{provider.arg}"

    def _resolve_llm_provider(self) -> Provider | None:
        """Первый доступный провайдер из LLM_CHAIN. Кэш на 30 с.

        Кэш обязателен: без него каждый запрос дёргал бы сеть проверкой.
        """
        now = time.monotonic()
        cached = getattr(self, "_llm_provider_cache", None)
        if cached is not None and now - cached[0] < 30.0:
            return cached[1]
        resolved: Provider | None = None
        try:
            result = run_with_fallback(provider_chain("llm"), self._probe_llm_provider)
            resolved = result.provider
            if result.degraded:
                log.warning(
                    "LLM: основной провайдер молчит, работаем на запасном %s",
                    resolved,
                )
        except AllProvidersFailed as exc:
            log.info("LLM-цепочка недоступна: %s", exc)
        self._llm_provider_cache = (now, resolved)
        return resolved

    def _llm_available(self) -> bool:
        """Доступен ли хоть один LLM-провайдер из цепочки."""
        return self._resolve_llm_provider() is not None

    def llm_provider(self) -> Provider | None:
        """Какой провайдер ответит сейчас, None — цепочка молчит."""
        return self._resolve_llm_provider()

    def _llm_messages(self, text: str) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "Ты — Berta, локальный ассистент для Windows. "
                    "Отвечай кратко, по-русски, дружелюби. "
                    "Не используй разметку markdown."
                ),
            },
            {"role": "user", "content": text},
        ]

    def _stream_chunks(
        self, provider: Provider, messages: list[dict[str, str]]
    ) -> Iterator[str]:
        """Куски ответа выбранного провайдера. Исключение = переход на фоллбэк."""
        max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "220"))
        temperature = float(os.environ.get("VOICE_LLM_TEMPERATURE", "0.0"))

        if provider.name in ("ollama", "local"):
            import ollama

            with ollama.chat(
                model=provider.arg or self._llm_model,
                messages=messages,
                stream=True,
                options={"temperature": temperature, "num_predict": max_tokens},
            ) as stream:
                for part in stream:
                    chunk = (part.get("message") or {}).get("content") or ""
                    if chunk:
                        yield chunk
            return

        spec = CLOUD_PROVIDERS.get(provider.name)
        if spec is None:
            raise RuntimeError(f"неизвестный LLM-провайдер «{provider.name}»")
        if not spec.openai_compatible:
            raise RuntimeError(f"{provider.name} не поддерживает OpenAI-совместимый API")

        import httpx

        url = f"{spec.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            **cloud_auth_headers(provider.name),
        }
        payload = {
            "model": provider.arg,
            "messages": messages,
            "stream": True,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        timeout = httpx.Timeout(float(os.environ.get("LLM_TIMEOUT", "60")), connect=10.0)
        with httpx.Client(timeout=timeout) as client:
            with client.stream("POST", url, headers=headers, json=payload) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except ValueError:
                        continue
                    choices = event.get("choices") or [{}]
                    delta = choices[0].get("delta") or {}
                    chunk = delta.get("content") or ""
                    if chunk:
                        yield chunk

    def _try_llm_route(self, text: str, source: str) -> RouteResult | None:
        """Пробуем LLM-цепочку; недоступна — None (пойдём в fallback-правила)."""
        if self._resolve_llm_provider() is None:
            return None
        return RouteResult(
            reply="", source="llm", stream=True,
            extra={"text": text, "source": source},
        )

    def stream_llm(self, text: str, source: str, on_chunk) -> str:
        """Стримит ответ выбранного провайдера в on_chunk(str); возвращает текст.

        При недоступности провайдера кэш сбрасывается, и следующий запрос
        снова честно перебирает цепочку.
        """
        provider = self._resolve_llm_provider()
        if provider is None:
            fallback = self._fallback(text, source)
            on_chunk(fallback.reply)
            return fallback.reply

        full: list[str] = []
        try:
            with self._llm_lock:
                for chunk in self._stream_chunks(provider, self._llm_messages(text)):
                    full.append(chunk)
                    on_chunk(chunk)
        except Exception as exc:  # noqa: BLE001 — провайдер упал, это ожидаемо
            log.info("LLM %s недоступна (%s) — fallback на правила", provider, exc)
            self._llm_provider_cache = (time.monotonic(), None)
            fallback = self._fallback(text, source)
            on_chunk(fallback.reply)
            return fallback.reply

        answer = "".join(full)
        self._state.audit.add(source, "llm_reply", detail=text[:120])
        return answer

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

    def _try_web_search(self, text: str, source: str) -> str:
        """Быстрый ответ из интернета (погода/курс/факты).

        Thread-safe: работает в собственном потоке с отдельным asyncio-loop,
        чтобы не конфликтовать с запущенным в FastAPI loop.
        Возвращает готовую фразу для TTS или "" (→ переходим к LLM).
        """
        try:
            from jarvis.voice.web_search import maybe_web_search
        except ImportError:
            return ""

        result_box: list[str] = [""]
        budget = float(os.environ.get("WEB_SEARCH_TIMEOUT", "6.0"))

        self._state.bcast.push("status", {
            "text": "Ищу в интернете…", "kind": "search",
        })

        def _run() -> None:
            try:
                answer = asyncio.run(maybe_web_search(text, timeout=budget))
                result_box[0] = answer or ""
            except Exception:
                log.debug("web_search: сбой для %r", text, exc_info=True)

        t = threading.Thread(target=_run, name="web-search", daemon=True)
        t.start()
        t.join(timeout=budget + 1.0)
        answer = result_box[0]
        if answer:
            self._state.audit.add(source, "web_search", detail=text[:120])
            log.info("router: web_search → %s", answer[:100])
        else:
            log.info("router: web_search пусто для «%s»", text[:120])
        return answer

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
