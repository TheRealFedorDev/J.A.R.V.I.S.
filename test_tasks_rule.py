"""Тесты голосовых правил задачника: чистая логика, без LLM/ livekit."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
import re as _re

import pytest

from jarvis.voice.tasks_rule import (
    HttpTaskAPI,
    TaskAPI,
    TaskCommand,
    TasksUnavailable,
    _find_number,
    parse_task_command,
    run,
)


# ---------------------------------------------------------------------------
# parse_task_command
# ---------------------------------------------------------------------------
def test_none_when_no_tasks_keyword():
    """«добавь купить молоко» без слова «задач» уходит в LLM."""
    assert parse_task_command("добавь купить молоко") is None
    assert parse_task_command("найди последние новости") is None
    assert parse_task_command("привет") is None
    assert parse_task_command("сколько времени") is None
    assert parse_task_command("") is None
    assert parse_task_command("   ") is None


def test_list_ops():
    assert parse_task_command("покажи задачи").op == "list"
    assert parse_task_command("список задач").op == "list"
    assert parse_task_command("какие у меня задачи").op == "list"
    assert parse_task_command("что там с задачами").op == "list"


def test_add_ops():
    cmd = parse_task_command("добавь задачу купить молоко")
    assert cmd.op == "add" and cmd.text == "купить молоко"
    assert parse_task_command("запиши задачу позвонить бабушке").op == "add"
    assert parse_task_command("добавь новую задачу съездить к врачу").op == "add"
    # пустой текст после глагола — разрешён, run() спросит
    assert parse_task_command("добавь задачу").op == "add"
    assert parse_task_command("добавь задачу").text == ""


def test_remove_op_by_number():
    assert parse_task_command("удали задачу 3").task_id == 3
    assert parse_task_command("убери задачу пять").task_id == 5
    assert parse_task_command("вычеркни задачу десят").task_id == 10
    assert parse_task_command("удали задачу три").task_id == 3
    assert parse_task_command("убери задачу третью").task_id == 3


def test_remove_op_by_substring():
    cmd = parse_task_command("удали задачу про презентацию")
    assert cmd.op == "remove" and cmd.task_id is None
    cmd = parse_task_command("зачеркни задачу купить хлеб")
    assert cmd.op == "remove" and cmd.task_id is None


def test_update_op():
    cmd = parse_task_command("замени задачу 2 на позвонить Роме")
    assert cmd.op == "update" and cmd.task_id == 2 and cmd.text == "позвонить Роме"
    cmd = parse_task_command("исправь задачу три купить молоко")
    assert cmd.op == "update" and cmd.task_id == 3 and cmd.text == "купить молоко"
    cmd = parse_task_command("поправь задачу 1 на встретиться в пятницу")
    assert cmd.op == "update" and cmd.task_id == 1 and cmd.text == "встретиться в пятницу"
    cmd = parse_task_command("измени задачу пять на купить соль")
    assert cmd.op == "update" and cmd.task_id == 5 and cmd.text == "купить соль"


def test_update_missing_text():
    assert parse_task_command("замени задачу 3").text == ""
    assert parse_task_command("замени задачу 3 на ").text == ""


def test_number_parsing():
    assert _find_number("задачу 3") == 3
    assert _find_number("две") == 2
    assert _find_number("пятую") == 5
    assert _find_number("второй") == 2
    assert _find_number("первый") == 1
    assert _find_number("четвёртый") == 4
    assert _find_number("седьмая") == 7
    assert _find_number("первая из них") == 1
    assert _find_number("восьмая") == 8
    assert _find_number("десяти") == 10


# ---------------------------------------------------------------------------
# run() c mock TaskAPI
# ---------------------------------------------------------------------------
class MockTaskAPI(TaskAPI):
    def __init__(self, tasks=None):
        self.tasks = list(tasks) if tasks else []
        self.calls = []

    def list(self):
        self.calls.append("list")
        return list(self.tasks)

    def add(self, text):
        self.calls.append("add")
        task = {"id": len(self.tasks) + 1, "text": text}
        self.tasks.append(task)
        return task

    def update(self, task_id, text):
        self.calls.append("update")
        for t in self.tasks:
            if t["id"] == task_id:
                t["text"] = text
                return t
        raise KeyError(task_id)

    def remove(self, task_id):
        self.calls.append("remove")
        for t in self.tasks:
            if t["id"] == task_id:
                self.tasks.remove(t)
                return
        raise KeyError(task_id)


def test_run_list():
    api = MockTaskAPI([{"id": 1, "text": "хлеб"}, {"id": 2, "text": "молоко"}])
    cmd = TaskCommand(op="list", raw="покажи задачи")
    out = run(cmd, api)
    assert "Задач: 2." in out
    assert "Задача 1: «хлеб»." in out
    assert "Задача 2: «молоко»" in out


def test_run_list_empty():
    api = MockTaskAPI()
    cmd = TaskCommand(op="list", raw="какие задачи")
    assert run(cmd, api) == "Задач пока нет. Скажи: добавь задачу что-нибудь."


def test_run_list_overflow_10():
    tasks = [{"id": i, "text": f"задача {i}"} for i in range(1, 25)]
    api = MockTaskAPI(tasks)
    cmd = TaskCommand(op="list", raw="список задач")
    out = run(cmd, api)
    assert "И ещё 14 задач. Скажи номер, чтобы удалить или заменить." in out
    assert "Задача 10: «задача 10»" in out


def test_run_add():
    api = MockTaskAPI([{"id": 1, "text": "хлеб"}])
    cmd = TaskCommand(op="add", text="купить молоко", raw="добавь задачу купить молоко")
    out = run(cmd, api)
    assert out.startswith("Добавила: «купить молоко».")
    assert "Теперь задач 2." in out


def test_run_add_empty():
    api = MockTaskAPI()
    cmd = TaskCommand(op="add", text="", raw="добавь задачу")
    assert run(cmd, api) == "Не поняла, что добавить. Скажи: добавь задачу что-нибудь."


def test_run_remove_by_id():
    api = MockTaskAPI([{"id": 1, "text": "хлеб"}])
    cmd = TaskCommand(op="remove", task_id=1, raw="удали задачу один")
    out = run(cmd, api)
    assert out == "Удалила задачу 1."
    assert api.calls == ["remove"]
    assert api.list() == []


def test_run_remove_by_substring_single():
    api = MockTaskAPI([{"id": 1, "text": "купить хлеб"}, {"id": 2, "text": "молоко"}])
    cmd = TaskCommand(op="remove", task_id=None, raw="удали задачу про хлеб")
    out = run(cmd, api)
    assert out == "Удалила задачу 1."
    assert api.list() == [{"id": 2, "text": "молоко"}]


def test_run_remove_by_substring_multiple():
    api = MockTaskAPI([
        {"id": 1, "text": "купить хлеб"},
        {"id": 2, "text": "купить молоко"},
    ])
    cmd = TaskCommand(op="remove", task_id=None, raw="удали задачу про купить")
    out = run(cmd, api)
    assert "Нашла несколько подходящих: 1, 2. Уточни номер." in out


def test_run_remove_by_substring_none():
    api = MockTaskAPI([{"id": 1, "text": "хлеб"}])
    cmd = TaskCommand(op="remove", task_id=None, raw="удали задачу про проездной")
    out = run(cmd, api)
    assert "Не нашла задачу про «проездной»" in out


def test_run_update():
    api = MockTaskAPI([{"id": 1, "text": "хлеб"}])
    cmd = TaskCommand(
        op="update", task_id=1, text="мука", raw="замени задачу 1 на муку"
    )
    out = run(cmd, api)
    assert out == "Заменила задачу 1: «мука»."
    assert api.list()[0]["text"] == "мука"


def test_run_update_missing_text():
    api = MockTaskAPI([{"id": 1, "text": "хлеб"}])
    cmd = TaskCommand(op="update", task_id=1, text="", raw="замени задачу 1")
    assert run(cmd, api) == "Что записать вместо? Скажи: замени задачу N на текст."


def test_run_api_unavailable():
    api = MockTaskAPI()
    raise_error = MockTaskAPI()
    raise_error.list = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    cmd = TaskCommand(op="list", raw="покажи задачи")
    assert run(cmd, raise_error) == "Панель задач недоступна — веб-сервер не запущен."


# ---------------------------------------------------------------------------
# HttpTaskAPI against local fake server
# ---------------------------------------------------------------------------
class _FakeHandler(BaseHTTPRequestHandler):
    store: list[dict] = []
    seq: int = 1

    def log_message(self, *a, **k):
        pass

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        assert self.path == "/api/tasks"
        self._send(200, json.dumps({"tasks": self.store}).encode())

    def do_POST(self):
        if self.path == "/api/tasks":
            length = int(self.headers["Content-Length"])
            body = self.rfile.read(length)
            data = json.loads(body.decode())
            task = {"id": self.seq, "text": data["text"]}
            self.seq += 1
            self.store.append(task)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "task": task}).encode())
            return
        self._send(404, b'{"detail": "not found"}')

    def do_PATCH(self):
        m = _re.match(r"/api/tasks/(\d+)$", self.path)
        if not m:
            self._send(404, b'{"detail": "not found"}')
            return
        task_id = int(m.group(1))
        length = int(self.headers["Content-Length"])
        body = self.rfile.read(length)
        data = json.loads(body.decode())
        for t in self.store:
            if t["id"] == task_id:
                t["text"] = data["text"]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps({"ok": True, "task": t}).encode()
                )
                return
        self._send(404, b'{"detail": "task not found"}')

    def do_DELETE(self):
        m = _re.match(r"/api/tasks/(\d+)$", self.path)
        if not m:
            self._send(404, b'{"detail": "not found"}')
            return
        task_id = int(m.group(1))
        for i, t in enumerate(self.store):
            if t["id"] == task_id:
                del self.store[i]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')
                return
        self._send(404, b'{"detail": "task not found"}')


def _start_fake_server():
    _FakeHandler.store = [{"id": 1, "text": "тест"}]
    _FakeHandler.seq = 2
    srv = HTTPServer(("127.0.0.1", 0), _FakeHandler)
    port = srv.server_address[1]

    def serve():
        srv.serve_forever()

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    return srv, port


@pytest.fixture()
def fake_server():
    srv, port = _start_fake_server()
    try:
        yield f"http://127.0.0.1:{port}", srv
    finally:
        srv.shutdown()


def test_http_task_api_list(fake_server):
    url, _ = fake_server
    api = HttpTaskAPI(base_url=url)
    tasks = api.list()
    assert tasks == [{"id": 1, "text": "тест"}]


def test_http_task_api_add(fake_server):
    url, _ = fake_server
    api = HttpTaskAPI(base_url=url)
    created = api.add("добавить http")
    assert created["ok"] is True and created["task"]["text"] == "добавить http"


def test_http_task_api_remove(fake_server):
    url, _ = fake_server
    api = HttpTaskAPI(base_url=url)
    api.remove(1)
    assert api.list() == []


def test_http_task_api_unavailable(fake_server):
    url, _ = fake_server
    api = HttpTaskAPI(base_url="http://127.0.0.1:1")
    cmd = TaskCommand(op="list", raw="покажи задачи")
    out = run(cmd, api)
    assert out == "Панель задач недоступна — веб-сервер не запущен."


def test_http_task_api_full_flow(fake_server):
    url, srv = fake_server
    api = HttpTaskAPI(base_url=url)
    api.add("новая")
    api.update(2, "обновлённая")
    assert api.list()[1]["text"] == "обновлённая"
    api.remove(2)


def test_import_no_livekit():
    import subprocess, sys
    out = subprocess.run(
        [sys.executable, "-c", "from jarvis.voice.tasks_rule import parse_task_command, run, HttpTaskAPI"],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    assert "livekit" not in out.stderr.lower()
    assert "ollama" not in out.stderr.lower()