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
    # без voice-сервиса микрофон недоступен, но запрос корректен
    assert c.post("/api/phone/mic", headers=h, json={"enabled": False}).json()["ok"] is False
    assert c.post("/api/phone/tts", headers=h, json={"enabled": False}).json()["ok"]


def test_phone_state_and_cmd_wired_to_state():
    c = _client()
    h = {"X-Phone-Key": "testkey"}
    st = c.get("/api/phone/state", headers=h).json()
    assert st["ok"] and "profile" in st and "task_count" in st

    c.post("/api/phone/tasks", headers=h, json={"text": "one"})
    st = c.get("/api/phone/state", headers=h).json()
    assert st["task_count"] == 1

    r = c.post("/api/phone/cmd", headers=h, json={"text": "привет"}).json()
    assert r["ok"] and r["reply"]


def test_phone_mobile_page():
    c = _client()
    r = c.get("/m")
    assert r.status_code == 200 and "BERTA" in r.text
    assert c.get("/m/manifest.webmanifest").json()["start_url"] == "/m"
    assert "addEventListener" in c.get("/m/sw.js").text
    assert c.get("/m/icon.svg").headers["content-type"].startswith("image/svg")


def test_phone_bracelet():
    c = _client()
    h = {"X-Phone-Key": "testkey"}
    empty = c.get("/api/phone/bracelet", headers=h).json()
    assert empty["ok"] and empty["last"] is None and empty["readings"] == []

    r = c.post(
        "/api/phone/bracelet", headers=h,
        json={"device": "Band", "kind": "heart_rate", "value": 72},
    ).json()
    assert r["ok"] and r["entry"]["kind"] == "heart_rate"

    got = c.get("/api/phone/bracelet", headers=h).json()
    assert got["last"]["value"] == 72 and len(got["readings"]) == 1

    # без ключа — 401
    assert c.post("/api/phone/bracelet", json={"kind": "raw", "value": 1}).status_code == 401
