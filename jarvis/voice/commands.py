"""Голосовые команды: разбор распознанного текста в действия.

Чистая логика без аудио/vosk — легко тестируется.
Порядок проверок важен («двойной клик» не должен попасть в «клик»).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class VoiceCommand:
    action: str
    args: dict = field(default_factory=dict)


def parse_command(text: str) -> VoiceCommand | None:
    """Переводит распознанную фразу в действие или None (не команда)."""
    if not text or not text.strip():
        return None
    s = " " + text.strip().lower() + " "

    # длинные/конкретные фразы раньше коротких
    if "двойн" in s or "дважды" in s or "два" in s:
        return VoiceCommand("double_click")
    if "правый" in s or "правая" in s or "право" in s:
        return VoiceCommand("right_click")

    if "вниз" in s:
        return VoiceCommand("scroll", {"direction": "down", "amount": 3})
    if "вверх" in s:
        return VoiceCommand("scroll", {"direction": "up", "amount": 3})

    if "клик" in s or "нажми" in s:
        return VoiceCommand("click")

    if "выключ" in s or "стоп" in s or "пауза" in s:
        return VoiceCommand("disable")
    if "включ" in s:
        return VoiceCommand("enable")

    if "калибр" in s:
        return VoiceCommand("calibrate")

    return None