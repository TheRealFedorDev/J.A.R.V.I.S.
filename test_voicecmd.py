"""Endpoint /voicecmd: livekit-агент спрашивает основной процесс про задачи.

Импорт local_voice_api тянет AppConfig.load(.env), который setdefault-ит
переменные окружения (например VOICE_AGENT=livekit) для всего pytest-процесса.
Поэтому модуль импортируем лениво в фикстуре и восстанавливаем os.environ
после каждого теста — иначе ломаются конфиг-тесты pipecat.
"""

from __future__ import annotations

import os


def _env_snapshot() -> dict[str, str]:
    return dict(os.environ)


def _restore_env(saved: dict[str, str]) -> None:
    os.environ.clear()
    os.environ.update(saved)


def _client():
    saved = _env_snapshot()
    try:
        from starlette.testclient import TestClient

        from jarvis.voice.local_voice_api import app

        with TestClient(app) as client:
            yield client
    finally:
        _restore_env(saved)


def test_voicecmd_plain_phrase_not_matched() -> None:
    for client in _client():
        response = client.post("/voicecmd", json={"text": "привет"})
    assert response.status_code == 200
    assert response.json() == {"matched": False, "answer": ""}


def test_voicecmd_task_phrase_matched() -> None:
    for client in _client():
        response = client.post(
            "/voicecmd", json={"text": "добавь задачу купить молоко"}
        )
    data = response.json()
    assert response.status_code == 200
    assert data["matched"] is True
    assert isinstance(data["answer"], str) and data["answer"]


def test_voicecmd_empty_text_not_matched() -> None:
    for client in _client():
        response = client.post("/voicecmd", json={"text": "   "})
    assert response.status_code == 200
    assert response.json() == {"matched": False, "answer": ""}


# ---------------------------------------------------------------------------
# Фильтр фантомной речи (шуму не место в LLM)
# ---------------------------------------------------------------------------
class _Seg:
    def __init__(self, text: str, no_speech_prob: float = 0.0) -> None:
        self.text = text
        self.no_speech_prob = no_speech_prob


_filter_fn = None


def _filter(segments):
    global _filter_fn
    if _filter_fn is None:
        saved = _env_snapshot()
        try:
            from jarvis.voice.local_voice_api import _filter_and_join_segments

            _filter_fn = _filter_and_join_segments
        finally:
            _restore_env(saved)
    return _filter_fn(iter(segments))


def test_filter_keeps_live_speech() -> None:
    assert _filter([_Seg("добавь задачу купить молоко", 0.05)]) == (
        "добавь задачу купить молоко"
    )


def test_filter_drops_noise_phantom() -> None:
    # «Берта.» и мусор на тишине — no_speech_prob высокий, такая речь уходит
    assert _filter([_Seg("Берта.", 0.9), _Seg("купить", 0.05)]) == "купить"


def test_filter_drops_short_noise() -> None:
    assert _filter([_Seg("ы", 0.3), _Seg("а", 0.3)]) == ""


def test_filter_threshold_boundary() -> None:
    assert _filter([_Seg("да", 0.6)]) == ""          # >= порог — фантом
    assert _filter([_Seg("да", 0.59)]) == "да"        # ниже порога — речь