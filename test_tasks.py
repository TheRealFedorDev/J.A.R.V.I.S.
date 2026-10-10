"""Тесты задачника: хранилище, WS-рассылка, HTTP-API, разметка панели."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from jarvis.core.config import AppConfig
from jarvis.core.events import EventBus


@pytest.fixture()
def state(tmp_path):
    from jarvis.web.state import AppState

    return AppState(AppConfig(), EventBus(), project_root=tmp_path)


@pytest.fixture()
def client(state):
    from jarvis.web import create_app

    app = create_app(state)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Хранилище (правило, а не LLM: детерминировано и работает без Ollama)
# ---------------------------------------------------------------------------
def test_add_assigns_ids_in_order_and_persists(state):
    first = state.tasks.add("купить хлеб")
    second = state.tasks.add("позвонить Роме")

    assert first["id"] == 1 and second["id"] == 2
    assert [t["text"] for t in state.tasks.list()] == ["купить хлеб", "позвонить Роме"]

    path = state.project_root / "models" / "tasks.json"
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == [
        {"id": 1, "text": "купить хлеб"},
        {"id": 2, "text": "позвонить Роме"},
    ]


def test_reload_keeps_order_and_does_not_reuse_deleted_id(state):
    from jarvis.web.state import TaskManager

    state.tasks.add("первая")
    state.tasks.add("вторая")
    state.tasks.remove(1)

    reloaded = TaskManager(state.project_root / "models" / "tasks.json", lambda _t: None)
    assert [t["text"] for t in reloaded.list()] == ["вторая"]
    # id не переиспользуется: иначе голос «удали задачу два» попадёт мимо
    assert reloaded.add("третья")["id"] == 3


def test_update_rewrites_text(state):
    state.tasks.add("старое")
    assert state.tasks.update(1, "новое")["text"] == "новое"
    assert state.tasks.list()[0]["text"] == "новое"
    with pytest.raises(KeyError):
        state.tasks.update(99, "нет такого")


def test_remove_unknown_raises(state):
    with pytest.raises(KeyError):
        state.tasks.remove(7)
    assert state.tasks.list() == []


def test_rejects_empty_and_overlong_text(state):
    with pytest.raises(ValueError):
        state.tasks.add("   ")
    with pytest.raises(ValueError):
        state.tasks.add("а" * (state.tasks._MAX_LEN + 1))


def test_missing_and_corrupt_files_are_empty(tmp_path):
    from jarvis.web.state import TaskManager

    assert TaskManager(tmp_path / "нет.json", lambda _t: None).list() == []

    broken = tmp_path / "битый.json"
    broken.write_text("{это не json", encoding="utf-8")
    assert TaskManager(broken, lambda _t: None).list() == []


def test_task_cap_stops_runaway_voice_adds(state):
    from jarvis.web.state import TaskManager

    for i in range(TaskManager._MAX_TASKS):
        state.tasks.add(f"задача {i}")
    with pytest.raises(ValueError):
        state.tasks.add("ещё одна")


def test_list_returns_a_copy(state):
    state.tasks.add("оригинал")
    snapshot = state.tasks.list()
    snapshot[0]["text"] = "испорчено извне"
    assert state.tasks.list()[0]["text"] == "оригинал"


def test_mutation_pushes_tasks_over_ws(state, monkeypatch):
    pushed = []
    monkeypatch.setattr(state.bcast, "push", lambda *a, **k: pushed.append(a))

    state.tasks.add("через шину")

    assert pushed and pushed[0][0] == "tasks"
    assert pushed[0][1]["tasks"][0]["text"] == "через шину"


def test_snapshot_carries_tasks(state):
    state.tasks.add("из снапшота")
    assert state.snapshot()["tasks"][0]["text"] == "из снапшота"


# ---------------------------------------------------------------------------
# HTTP-API
# ---------------------------------------------------------------------------
def test_api_tasks_crud(client):
    assert client.get("/api/tasks").json() == {"tasks": []}

    created = client.post("/api/tasks", json={"text": "сделать демо"}).json()
    assert created["ok"] is True
    assert created["task"] == {"id": 1, "text": "сделать демо"}

    updated = client.patch("/api/tasks/1", json={"text": "сделать демо блеском"})
    assert updated.json()["task"]["text"] == "сделать демо блеском"
    assert client.get("/api/tasks").json()["tasks"][0]["text"] == "сделать демо блеском"

    assert client.delete("/api/tasks/1").json()["ok"] is True
    assert client.get("/api/tasks").json()["tasks"] == []


def test_api_tasks_validation(client):
    assert client.post("/api/tasks", json={"text": "  "}).status_code == 400
    assert client.patch("/api/tasks/42", json={"text": "x"}).status_code == 404
    assert client.delete("/api/tasks/42").status_code == 404


def test_api_tasks_reach_ws(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()                       # snapshot при коннекте
        client.post("/api/tasks", json={"text": "по ws"})
        msg = ws.receive_json()
    assert msg["type"] == "tasks"
    assert msg["data"]["tasks"][0]["text"] == "по ws"


def test_api_tasks_write_audit(client):
    client.post("/api/tasks", json={"text": "в журнал"})
    entries = client.get("/api/audit").json()["entries"]
    assert entries[0]["action"] == "task_add"


# ---------------------------------------------------------------------------
# Панель: строка не сжимается и не обрезается (ТЗ)
# ---------------------------------------------------------------------------
def test_index_html_shows_task_panel(client):
    body = client.get("/").text
    for marker in ("taskPanel", "taskList", "taskToggleBtn", "taskForm", "taskInput"):
        assert marker in body
    # панель не тащит за собой вырезанные блоки старего HUD
    for banned in ("cam-dock", "stream.mjpg", "composer", "auditList"):
        assert banned not in body


def test_task_text_rule_never_clips(client):
    """Задача «не сжимается ничём, но и не вылезает за рамки»."""
    css = client.get("/static/style.css").text
    rule = css.split(".task-text {", 1)[1].split("}", 1)[0]
    assert "white-space: pre-wrap" in rule
    assert "overflow-wrap: anywhere" in rule
    assert "ellipsis" not in rule
    assert "line-clamp" not in rule
    assert "max-height" not in rule
