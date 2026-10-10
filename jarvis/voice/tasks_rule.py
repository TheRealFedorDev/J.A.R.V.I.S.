"""Голосовые правила задачника.

Чистая детерминированная логика: распознанный текст -> TaskCommand,
без LLM. Весь текст на русском. Никаких комментариев в коде (кроме docstring).
"""

from __future__ import annotations

import os as _os
import json as _json
import re as _re
import urllib.request as _urllib
import urllib.error as _uerr
import urllib.parse as _parse
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TaskCommand:
    op: str
    task_id: int | None = None
    text: str = ""
    raw: str = field(default="", repr=False)


def _first_number(text: str) -> tuple[int, int, int] | None:
    """Найти первое число в тексте.

    Понимает: цифры, кардинальные 1–10 и основы порядковых (склонения).
    Возвращает (значение, start, end) первого числа по порядку появления,
    end — позиция сразу после последнего символа числа.
    """
    m = _re.search(r"\d+", text)
    best = (int(m.group()), m.start(), m.end()) if m else None

    nums_map = {
        "ноль": 0,
        "один": 1,
        "одна": 1,
        "две": 2,
        "два": 2,
        "три": 3,
        "четыре": 4,
        "четырёх": 4,
        "пять": 5,
        "пяти": 5,
        "шесть": 6,
        "шести": 6,
        "семь": 7,
        "семи": 7,
        "восемь": 8,
        "восьми": 8,
        "девять": 9,
        "девят": 9,
        "десять": 10,
        "десяти": 10,
        "первый": 1,
        "первого": 1,
        "первое": 1,
        "второй": 2,
        "второго": 2,
        "второе": 2,
        "третий": 3,
        "третьего": 3,
        "третье": 3,
        "четвертый": 4,
        "четвертого": 4,
        "четвертое": 4,
        "четвёртый": 4,
        "четвёртого": 4,
        "четвёртое": 4,
        "пятый": 5,
        "пятого": 5,
        "пятое": 5,
        "шестой": 6,
        "шестого": 6,
        "шестое": 6,
        "седьмой": 7,
        "седьмого": 7,
        "седьмое": 7,
        "седьмая": 7,
        "седьмую": 7,
        "седьмой": 7,
        "восьмой": 8,
        "восьмого": 8,
        "восьмое": 8,
        "восьмая": 8,
        "восьмую": 8,
        "девятый": 9,
        "девятого": 9,
        "девятое": 9,
        "девятая": 9,
        "девятую": 9,
        "десятый": 10,
        "десятого": 10,
        "десятое": 10,
        "десятая": 10,
        "десятую": 10,
    }

    ord_map = {
        "перв": 1,
        "втор": 2,
        "трет": 3,
        "четв": 4,
        "пят": 5,
        "шест": 6,
        "седьм": 7,
        "восьм": 8,
        "девят": 9,
        "десят": 10,
    }

    for word, val in nums_map.items():
        idx = text.find(word)
        if idx != -1 and (best is None or idx < best[1]):
            best = (val, idx, idx + len(word))

    for word, val in ord_map.items():
        idx = text.find(word)
        if idx != -1 and (best is None or idx < best[1]):
            best = (val, idx, idx + len(word))

    return best


def _find_number(text: str) -> int | None:
    """Первое число из текста или None (см. _first_number)."""
    hit = _first_number(text)
    return hit[0] if hit else None


def parse_task_command(text: str) -> TaskCommand | None:
    """Перевести распознанную фразу в TaskCommand или None (не команда)."""

    if not text or not text.strip():
        return None

    orig = text.strip()
    # s — для поиска ключей (нижний регистр), w — той же длины, но с
    # исходным регистром: текст задачи берём из w, чтобы не терять имена.
    s = " " + orig.lower() + " "
    w = " " + orig + " "

    # Критически важно: если нет слова "задач" — НЕ трогаем.
    # Фраза вроде «добавь купить молоко» без слова «задач» уходит в LLM.
    if "задач" not in s:
        return None

    # ---------- list ----------
    if (
        s.startswith(" ")
        and ("покажи" in s or "список" in s or "какие" in s or "что там" in s)
    ):
        return TaskCommand(op="list", raw=orig)

    # ---------- add ----------
    if any(verb in s for verb in ("добав", "запиш", "новую")):
        for verb in ("добав", "запиш", "новую"):
            idx = s.find(" " + verb)
            if idx != -1:
                # срезаем весь глагол-слово: «добавь задачу …» -> «задачу …»
                head, _, _tail = s[idx + len(verb) + 1 :].partition(" ")
                body_start = idx + len(verb) + 1 + len(head) + 1
                after = w[body_start:].strip()
                after = _re.sub(r"^задач[а-яё]*\s*", "", after, flags=_re.I)
                return TaskCommand(op="add", text=after, raw=orig)

    # ---------- remove ----------
    if any(verb in s for verb in ("удали", "убери", "вычеркн", "зачеркн")):
        num = _find_number(s)
        if num is not None:
            return TaskCommand(op="remove", task_id=num, raw=orig)
        return TaskCommand(op="remove", raw=orig)

    # ---------- update / replace ----------
    if any(verb in s for verb in ("замени", "исправ", "поправ", "измени")):
        hit = _first_number(s)
        if hit is not None:
            value, _, end = hit
            after = w[end:].strip()
            # срезаем предлог («на купить соль» -> «купить соль»)
            after = _re.sub(
                r"^(?:на|о|об|про|по)(?:\s+|$)", "", after, flags=_re.I
            )
            return TaskCommand(
                op="update", task_id=value, text=after, raw=orig
            )

    return None


# ── Protocol TaskAPI ──────────────────────────────────────
class TaskAPI:
    """Duck-typing protocol for task storage back-ends."""

    def list(self) -> list[dict]: ...

    def add(self, text: str) -> dict: ...

    def update(self, task_id: int, text: str) -> dict: ...

    def remove(self, task_id: int) -> None: ...


# ── class HttpTaskAPI ─────────────────────────────────────────────
class TasksUnavailable(RuntimeError):
    """Выброшено HttpTaskAPI при недоступности панели."""


class HttpTaskAPI(TaskAPI):
    """HTTP-клиент, разговаривающий с панелью /api/tasks ..."""

    def __init__(self, base_url: str | None = None) -> None:
        self._base = base_url or _os.environ.get(
            "BERTA_WEB_URL", "http://127.0.0.1:8765"
        )

    # helpers
    def _url(self, path: str) -> str:
        return _parse.urljoin(self._base, path.lstrip("/"))

    def _get(self, path: str) -> _urllib.addinfourl:
        return _urllib.urlopen(_urllib.Request(self._url(path)), timeout=5)

    def _post(self, path: str, json_body: dict) -> _urllib.addinfourl:
        data = _json.dumps(json_body).encode("utf-8")
        req = _urllib.Request(
            self._url(path), data=data, headers={"Content-Type": "application/json"}
        )
        return _urllib.urlopen(req, timeout=5)

    def _patch(self, path: str, json_body: dict) -> _urllib.addinfourl:
        data = _json.dumps(json_body).encode("utf-8")
        req = _urllib.Request(
            self._url(path),
            data=data,
            headers={"Content-Type": "application/json"},
            method="PATCH",
        )
        return _urllib.urlopen(req, timeout=5)

    def _delete(self, path: str) -> _urllib.addinfourl:
        req = _urllib.Request(self._url(path), method="DELETE")
        return _urllib.urlopen(req, timeout=5)

    # ── list ────────────────────────────────────────────
    def list(self) -> list[dict]:
        resp = self._get("/api/tasks")
        body = _json.load(resp)
        return body.get("tasks", [])

    # ── add ─────────────────────────────────────────────
    def add(self, text: str) -> dict:
        resp = self._post("/api/tasks", {"text": text})
        body = _json.load(resp)
        if resp.status == 200:
            return body
        raise _uerr.HTTPError(resp.read().decode())

    # ── update ──────────────────────────────────────────
    def update(self, task_id: int, text: str) -> dict:
        resp = self._patch(f"/api/tasks/{task_id}", {"text": text})
        body = _json.load(resp)
        if resp.status == 200:
            return body
        raise _uerr.HTTPError(resp.read().decode())

    # ── remove ──────────────────────────────────────────
    def remove(self, task_id: int) -> None:
        resp = self._delete(f"/api/tasks/{task_id}")
        body = _json.load(resp)
        if resp.status != 200:
            raise _uerr.HTTPError(resp.read().decode())


# ── run ───────────────────────────────────────────────────
def run(cmd: TaskCommand, api: TaskAPI) -> str:
    """Вернуть финальную озвучиваемую строку по TaskCommand и api."""

    try:
        match cmd.op:
            case "list":
                tasks = api.list()
                if not tasks:
                    return "Задач пока нет. Скажи: добавь задачу что-нибудь."
                parts: list[str] = []
                shown = 0
                for t in tasks:
                    shown += 1
                    parts.append(f"Задача {shown}: «{t['text']}»")
                    if shown >= 10:
                        break
                result = "Задач: " + str(len(tasks)) + ". " + ". ".join(parts)
                if len(tasks) > 10:
                    result += f". И ещё {len(tasks) - 10} задач. Скажи номер, чтобы удалить или заменить."
                return result

            case "add":
                if not cmd.text.strip():
                    return "Не поняла, что добавить. Скажи: добавь задачу что-нибудь."
                api.add(cmd.text)
                n = len(api.list())
                return f"Добавила: «{cmd.text}». Теперь задач {n}."

            case "remove":
                if cmd.task_id is not None:
                    api.remove(cmd.task_id)
                    return f"Удалила задачу {cmd.task_id}."
                tasks = api.list()
                needle = cmd.raw
                for verb in ("удали", "убери", "вычеркн", "зачеркн"):
                    needle = needle.replace(verb + " ", "")
                for word in ("задачу", "задача", "задачи"):
                    needle = needle.replace(word + " ", "")
                # выкидываем служебные слова и предлоги: «про хлеб» -> «хлеб»
                stop = {"про", "об", "обо", "о", "на", "по", "за", "у"}
                needle = " ".join(
                    w for w in needle.split() if w.lower() not in stop
                ).strip()
                matches = [t for t in tasks if needle.lower() in t["text"].lower()]
                if len(matches) == 1:
                    api.remove(matches[0]["id"])
                    return f"Удалила задачу {matches[0]['id']}."
                if len(matches) > 1:
                    ids = ", ".join(str(t["id"]) for t in matches)
                    return f"Нашла несколько подходящих: {ids}. Уточни номер."
                return f"Не нашла задачу про «{needle}»."

            case "update":
                if cmd.task_id is None or not cmd.text:
                    return "Что записать вместо? Скажи: замени задачу N на текст."
                api.update(cmd.task_id, cmd.text)
                return f"Заменила задачу {cmd.task_id}: «{cmd.text}»."

            case _:
                return "Панель задач недоступна — веб-сервер не запущен."

    except Exception:  # noqa: BLE001
        return "Панель задач недоступна — веб-сервер не запущен."