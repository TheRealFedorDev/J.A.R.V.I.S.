"""Тесты web-панели Berta: роутер, профили, аудит, API, жесты."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jarvis.core.config import AppConfig
from jarvis.core.events import EventBus, VoiceCommandEvent
from jarvis.input.gestures import (
    GESTURE_LABELS,
    HAND_CONNECTIONS,
    classify_gesture,
)


# ---------------------------------------------------------------------------
# Жесты (ТЗ §1)
# ---------------------------------------------------------------------------
def _circle_pts(cx=0.5, cy=0.5, r=0.2):
    """21 лендмарк-заглушка: кончики дальше от запястья (рука «вытянута»)."""
    import math

    pts = [(0.5, 0.9)]  # wrist внизу кадра
    for i in range(1, 21):
        a = i * 0.3
        pts.append((cx + r * math.cos(a) * (1 + i * 0.03),
                    cy - r * math.sin(a) * (1 + i * 0.03)))
    # кончики дальше wrist
    for tip in (4, 8, 12, 16, 20):
        pts[tip] = (0.5 + 0.05 * (tip - 8), 0.1)
    return tuple(pts)


def test_gestures_labels_cover_basic():
    for name in ("fist", "palm", "thumbs_up", "point", "pinch"):
        assert name in GESTURE_LABELS


def test_classify_none_without_landmarks():
    assert classify_gesture(None) is None
    assert classify_gesture(((0.0, 0.0),)) is None


def test_classify_fist():
    # кулак: кончики ближе к запястью, чем PIP
    pts = [(0.5, 0.5)] * 21
    pts[0] = (0.5, 0.9)
    for tip, pip in ((8, 6), (12, 10), (16, 14), (20, 18)):
        pts[pip] = (0.5, 0.55)       # pip дальше wrist
        pts[tip] = (0.5, 0.75)       # tip ближе → не вытянут
    pts[4] = (0.55, 0.75)            # thumb тоже не вытянут
    pts[3] = (0.52, 0.6)
    assert classify_gesture(tuple(pts)) == "fist"


def test_classify_palm():
    pts = list(_circle_pts())
    pts[0] = (0.5, 0.95)  # wrist внизу
    # кончики уже перенесены в _circle_pts вверх — все вытянуты
    pts[4] = (0.2, 0.15)  # thumb в сторону-вверх
    pts[3] = (0.3, 0.4)
    assert classify_gesture(tuple(pts)) == "palm"


def test_classify_pinch_by_ratio():
    pts = _circle_pts()
    assert classify_gesture(pts, pinch=0.1) == "pinch"


def test_hand_connections_shape():
    assert len(HAND_CONNECTIONS) >= 20
    assert all(0 <= a < 21 and 0 <= b < 21 for a, b in HAND_CONNECTIONS)


# ---------------------------------------------------------------------------
# AppState / профили / аудит
# ---------------------------------------------------------------------------
@pytest.fixture()
def state(tmp_path, monkeypatch):
    from jarvis.web.state import AppState

    st = AppState(
        AppConfig(), EventBus(), project_root=tmp_path,
    )
    return st


def test_profile_persists_and_switches(state):
    p = state.profiles.set("assist")
    assert p.id == "assist" and p.assist is True
    assert (state.project_root / "models" / "profile.json").exists()
    assert state.profiles.current_id == "assist"
    with pytest.raises(KeyError):
        state.profiles.set("nope")


def test_audit_ring_and_tail(state):
    state.audit.add("web", "test_action", detail="hello")
    state.audit.add("voice", "click", ok=True, detail="клик")
    tail = state.audit.tail(10)
    assert tail[0]["action"] == "click"
    assert tail[-1]["detail"] == "hello"
    assert (state.project_root / "models" / "audit.jsonl").exists()


def test_snapshot_shape(state):
    snap = state.snapshot()
    for key in ("tracker", "pointer", "profile", "profiles", "audit",
                "emergency", "gaze", "hand"):
        assert key in snap
    assert {p["id"] for p in snap["profiles"]} == {"home", "assist", "study"}


def test_snapshot_json_serializable_with_numpy(state):
    """np.bool_/np.float64 из eyetrax не должны ронять WS (json.dumps)."""
    import json

    import numpy as np

    from jarvis.core.events import GazeSample, HandSample
    from jarvis.web.server import _dumps

    # blink как numpy.bool_ — раньше падал: "Object of type bool is not JSON serializable"
    state.bus.publish(GazeSample(
        x=np.float64(100.5), y=np.float64(200.5),
        face=np.True_, blink=np.False_, noise=np.float64(3.2),
    ))
    state.bus.publish(HandSample(
        x=np.float32(0.5), y=np.float32(0.5), pinch=np.float32(0.6),
        confidence=np.float32(1.0), hand_count=1,
        landmarks=((0.1, 0.2), (0.3, 0.4)),
    ))
    text = _dumps({"type": "snapshot", "data": state.snapshot()})
    assert json.loads(text)["type"] == "snapshot"
    # и отдельное gaze-сообщение
    assert json.loads(_dumps({"type": "gaze", "data": state.gaze}))["data"]["blink"] is False


def test_pending_confirm_lifecycle(state):
    cid = state.add_pending({"text": "удали всё", "_exec": lambda: "done"})
    got = state.pop_pending(cid)
    assert got is not None
    assert state.pop_pending(cid) is None  # одноразово


# ---------------------------------------------------------------------------
# Роутер
# ---------------------------------------------------------------------------
@pytest.fixture()
def router(state):
    from jarvis.web.router import CommandRouter

    st = state
    r = CommandRouter(st)
    st.router = r
    return r


def test_router_click_publishes_voice_event(state, router):
    got = []
    state.bus.subscribe(VoiceCommandEvent, got.append)
    res = router.handle("клик", source="test")
    assert res.action == "click"
    assert len(got) == 1 and got[0].action == "click"
    assert state.audit.tail(1)[0]["action"] == "click"


def test_router_profile_switch(state, router):
    res = router.handle("включи режим ассист", source="test")
    assert res.action == "profile"
    assert state.profiles.current_id == "assist"


def test_router_dangerous_needs_confirm(state, router):
    res = router.handle("удали все файлы", source="test")
    assert res.needs_confirm is True
    assert res.confirm_id
    reply = router.run_pending(res.confirm_id)
    assert "Подтверждено" in reply or "истекло" in reply


def test_router_fallback_help(router):
    res = router.handle("помощь", source="test")
    assert res.source == "rules"
    assert "открыть приложение" in res.reply


def test_router_opens_known_app_match(router, monkeypatch):
    # только проверяем классификацию, не реально запуская процесс
    called = {}

    def fake_popen(cmd, **kw):
        called["cmd"] = cmd

        class P:
            pass

        return P()

    import jarvis.web.router as rt

    monkeypatch.setattr(rt.subprocess, "Popen", fake_popen)
    res = router.handle("открой блокнот", source="test")
    assert res.action == "open_app"
    assert called["cmd"] == "notepad"


def test_router_search_uses_full_phrase(router, monkeypatch):
    """«погода в Долгопрудном» → в браузер уходит вся фраза.

    Раньше из regex брался обрывок «в долгопрудном» — браузер открывался
    с мусорным запросом и ответа не было.
    """
    from urllib.parse import unquote_plus

    opened: list[str] = []
    status: list = []
    monkeypatch.setattr(
        "jarvis.web.router.webbrowser.open", lambda url: opened.append(url)
    )
    monkeypatch.setattr(
        router._state.bcast, "push", lambda *a, **k: status.append(a)
    )
    monkeypatch.setattr(router, "_try_web_search", lambda text, source: "")
    res = router.handle("Какая сейчас погода в Долгопрудном", source="test")
    assert res.action == "search"
    assert len(opened) == 1
    query = unquote_plus(opened[0].split("text=", 1)[1])
    assert query == "Какая сейчас погода в Долгопрудном"
    assert any(a and a[0] == "status" and a[1].get("kind") == "browser"
               for a in status)


def test_try_web_search_uses_env_budget(router, monkeypatch):
    """Бюджет веб-поиска берётся из WEB_SEARCH_TIMEOUT, а не 1.8 с."""
    seen: dict = {}

    async def fake_maybe(query, *, timeout=None):
        seen["timeout"] = timeout
        return "ответ"

    monkeypatch.setenv("WEB_SEARCH_TIMEOUT", "9")
    monkeypatch.setattr(
        "jarvis.voice.web_search.maybe_web_search", fake_maybe
    )
    assert router._try_web_search("погода", "test") == "ответ"
    assert seen["timeout"] == 9.0


def test_router_pushes_progress_status(router, monkeypatch):
    """Перед поиском телефон получает событие «Ищу…» (прогресс в UI)."""
    pushed: list = []
    monkeypatch.setattr(
        router._state.bcast, "push", lambda *a, **k: pushed.append(a)
    )

    async def fake_maybe(query, *, timeout=None):
        return "готово"

    monkeypatch.setattr(
        "jarvis.voice.web_search.maybe_web_search", fake_maybe
    )
    assert router._try_web_search("погода в Москве", "test") == "готово"
    status = [a for a in pushed if a and a[0] == "status"]
    assert status and status[0][1].get("kind") == "search"


def test_router_emergency_on_off(state, router):
    res = router.handle("экстренная остановка", source="test")
    assert res.action == "emergency"
    assert state.emergency is True
    res2 = router.handle("продолжай", source="test")
    assert state.emergency is False


def test_router_empty(router):
    res = router.handle("  ", source="test")
    assert "Пустая" in res.reply


def test_voice_partial_plain_no_tasks_event(state):
    from jarvis.core.events import VoicePartialEvent

    pushed = []
    state.bcast.push = lambda *a, **k: pushed.append(a)  # type: ignore[method-assign]
    state.bus.publish(VoicePartialEvent(text="привет", final=True))
    assert not any(msg for msg in pushed if msg and msg[0] == "tasks_open")


def test_voice_partial_task_command_pushes_tasks_open(state):
    from jarvis.core.events import VoicePartialEvent

    pushed = []
    state.bcast.push = lambda *a, **k: pushed.append(a)  # type: ignore[method-assign]
    state.bus.publish(
        VoicePartialEvent(text="добавь задачу купить молоко", final=True)
    )
    assert any(msg and msg[0] == "tasks_open" for msg in pushed)
    # промежуточная часть фразы панель не открывает
    state.bus.publish(
        VoicePartialEvent(text="добавь задачу купить молоко", final=False)
    )
    opens = [m for m in pushed if m and m[0] == "tasks_open"]
    assert len(opens) == 1


# ---------------------------------------------------------------------------
# HTTP API (TestClient)
# ---------------------------------------------------------------------------
@pytest.fixture()
def client(state):
    from jarvis.web import create_app

    app = create_app(state)
    with TestClient(app) as c:
        yield c


def test_index_html(client):
    """Минимальный HUD: орб, кольца активностей, полоса статуса.

    Камера и панели (чат, аудит, ввод) убраны намеренно: страница не должна
    ни содержать их, ни запрашивать поток камеры.
    """
    r = client.get("/")
    assert r.status_code == 200
    assert "orb.js" in r.text
    # обязательное
    assert "ai-orb" in r.text
    assert "act-rings" in r.text
    assert "statusText" in r.text
    # все четыре этапа-индикатора
    for act in ("stt", "search", "llm", "tts"):
        assert f'data-act="{act}"' in r.text
    # вырезанное не должно возвращаться
    assert "cam-dock" not in r.text
    assert "stream.mjpg" not in r.text
    assert "composer" not in r.text
    assert "auditList" not in r.text


def test_static_css_js(client):
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/orb.js").status_code == 200


def test_health_and_status(client):
    assert client.get("/api/health").json()["ok"] is True
    snap = client.get("/api/status").json()
    assert snap["profile"]["current"] == "home"


def test_api_command_click(client, state):
    got = []
    state.bus.subscribe(VoiceCommandEvent, got.append)
    r = client.post("/api/command", json={"text": "клик"})
    body = r.json()
    assert body["ok"] is True and body["action"] == "click"
    assert got and got[0].action == "click"


def test_api_command_empty(client):
    assert client.post("/api/command", json={"text": " " }).status_code == 400


def test_api_profile_switch(client):
    r = client.post("/api/profile", json={"name": "assist"})
    assert r.json()["current"] == "assist"
    assert client.post("/api/profile", json={"name": "nope"}).status_code == 404


def test_api_audit(client, state):
    state.audit.add("web", "smoke")
    entries = client.get("/api/audit").json()["entries"]
    assert entries[0]["action"] == "smoke"


def test_api_confirm_flow(client, router):
    res = router.handle("закрой всё без сохранения", source="web")
    assert res.needs_confirm
    r = client.post("/api/confirm", json={"id": res.confirm_id, "ok": True})
    assert r.json()["ok"] is True
    # повторный confirm — уже пусто
    r2 = client.post("/api/confirm", json={"id": res.confirm_id, "ok": True})
    assert "истекло" in r2.json()["reply"] or "не найдено" in r2.json()["reply"]


def test_api_camera_503_until_frame(client):
    assert client.get("/api/camera.jpg").status_code == 503


def test_api_pointer_and_emergency(client, state):
    # pointer is None в тестовом state — endpoint честно отвечает ok=False
    r = client.post("/api/pointer", json={"enabled": False})
    assert r.status_code == 200
    r2 = client.post("/api/emergency", json={"enabled": True})
    assert r2.json()["active"] is True
    assert state.emergency is True


def test_websocket_snapshot(client):
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "snapshot"
        assert "profile" in msg["data"]


def test_stream_mjpg_route_registered(client):
    # живой поток бесконечен — проверяем регистрацию маршрута, не тело
    paths = [getattr(r, "path", None) for r in client.app.routes]
    assert "/stream.mjpg" in paths
