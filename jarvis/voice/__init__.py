"""Голосовое управление (микрофон + faster-whisper / vosk / pipecat)."""

from jarvis.voice.commands import VoiceCommand, parse_command
from jarvis.voice.service import VoiceService

__all__ = [
    "VoiceCommand",
    "VoiceService",
    "PipecatVoiceService",
    "parse_command",
]


def __getattr__(name: str):
    if name == "PipecatVoiceService":
        from jarvis.voice.pipecat_agent import PipecatVoiceService

        return PipecatVoiceService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")