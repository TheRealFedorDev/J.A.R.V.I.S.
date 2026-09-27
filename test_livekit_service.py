"""Тесты обвязки LiveKit-агента: конфиг, выбор в лаунчере, петля порта."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from jarvis.core.config import VoiceConfig
from jarvis.voice.livekit_service import (
    AGENT_PY,
    AGENT_SCRIPT,
    HOST,
    MAIN_PY,
    PORT,
    LiveKitVoiceService,
)


class _Bus:
    """Заглушка EventBus: сервис им не пользуется, но конструктор его ждёт."""


def _service() -> LiveKitVoiceService:
    return LiveKitVoiceService(VoiceConfig(), _Bus())  # type: ignore[arg-type]


def test_config_accepts_livekit_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICE_AGENT", "livekit")
    assert VoiceConfig.from_env().agent == "livekit"


def test_config_rejects_unknown_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICE_AGENT", "pipecat-9000")
    assert VoiceConfig.from_env().agent == "legacy"


def test_launcher_routes_livekit_to_new_service() -> None:
    source = (Path(__file__).resolve().parents[0] / "jarvis" / "launcher.py").read_text(
        encoding="utf-8"
    )
    assert 'agent == "livekit"' in source
    assert "LiveKitVoiceService" in source


def test_agent_and_venvs_present() -> None:
    assert AGENT_SCRIPT.exists(), "нет jarvis/voice/livekit_agent.py"
    assert MAIN_PY.exists(), "нет основного venv"
    assert AGENT_PY.exists(), "нет .venv-livekit"


def test_agent_does_not_import_jarvis() -> None:
    """Агент запускается в чужом venv — импорты jarvis.* сломали бы его."""
    source = AGENT_SCRIPT.read_text(encoding="utf-8")
    assert "from jarvis" not in source
    assert "import jarvis" not in source


def test_port_busy_detects_listener() -> None:
    import jarvis.voice.livekit_service as module

    original = module.PORT
    try:
        # 1) свободный порт: поднимаем на 0, узнаём номер, закрываем
        with socket.socket() as probe:
            probe.bind((HOST, 0))
            free_port = probe.getsockname()[1]
        module.PORT = free_port
        assert LiveKitVoiceService._port_busy() is False

        # 2) занятый порт: на нём реально слушает сокет
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((HOST, 0))
            listener.listen(1)
            module.PORT = listener.getsockname()[1]
            assert LiveKitVoiceService._port_busy() is True
    finally:
        module.PORT = original


def test_service_starts_nothing_on_construction() -> None:
    service = _service()
    assert service._shim is None
    assert service._agent is None
