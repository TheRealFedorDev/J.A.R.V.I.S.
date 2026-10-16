import os
from pathlib import Path
from tempfile import mkdtemp

from fastapi.testclient import TestClient


def _client():
    os.environ["PHONE_API_KEY"] = "testkey"
    from jarvis.web.state import AppState
    from jarvis.core.config import AppConfig
    from jarvis.core.events import EventBus
    from jarvis.web import create_app

    tmp = mkdtemp()
    state = AppState(AppConfig(), EventBus(), project_root=Path(tmp))
    app = create_app(state)
    return TestClient(app)


def test_phone_health_auth():
    c = _client()
    assert c.get("/api/phone/health").status_code == 401
    r = c.get("/api/phone/health", headers={"X-Phone-Key": "testkey"})
    assert r.status_code == 200 and r.json().get("ok")


def test_phone_tasks_crud():
    c = _client()
    h = {"X-Phone-Key": "testkey"}
    assert c.get("/api/phone/tasks", headers=h).json()["tasks"] == []
    r = c.post("/api/phone/tasks", headers=h, json={"text": "buy milk"})
    assert r.json()["ok"]
    r = c.patch("/api/phone/tasks/1", headers=h, json={"text": "buy bread"})
    assert r.json()["task"]["text"] == "buy bread"
    r = c.delete("/api/phone/tasks/1", headers=h)
    assert r.json()["ok"]


def test_phone_cmd():
    c = _client()
    h = {"X-Phone-Key": "testkey"}
    r = c.post("/api/phone/cmd", headers=h, json={"text": "test"})
    assert r.status_code == 200


def test_phone_mic_tts():
    c = _client()
    h = {"X-Phone-Key": "testkey"}
    assert c.post("/api/phone/mic", headers=h, json={"enabled": False}).json()["ok"]
    assert c.post("/api/phone/tts", headers=h, json={"enabled": False}).json()["ok"]
