"""Голосовое управление (микрофон + vosk)."""

from jarvis.voice.commands import VoiceCommand, parse_command
from jarvis.voice.service import VoiceService

__all__ = ["VoiceCommand", "VoiceService", "parse_command"]