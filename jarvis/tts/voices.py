"""Голоса edge-tts и выбор голоса по языку текста.

Лучшие доступные женские нейроголоса (проверено по списку edge-tts 7.x,
322 голоса; ru-RU-DariaNeural и en-GB-EmmaNeural Microsoft удалил):
  * EN — en-GB-SoniaNeural: элегантный британский женский (Friendly, Positive);
  * RU — ru-RU-SvetlanaNeural: единственный русский женский (Friendly, Positive).

Для смешанного текста каждый фрагмент читается «своим» голосом: language-
детект по кириллице (см. jarvis.tts.normalize.is_russian).
"""

from __future__ import annotations

from jarvis.tts.normalize import is_russian

# Женские голоса по умолчанию (проверены на существование в edge-tts 7.2.8).
DEFAULT_EN_VOICE = "en-GB-SoniaNeural"
DEFAULT_RU_VOICE = "ru-RU-SvetlanaNeural"

__all__ = ["DEFAULT_EN_VOICE", "DEFAULT_RU_VOICE", "is_russian", "select_voice"]


def select_voice(text: str, *, en_voice: str = DEFAULT_EN_VOICE, ru_voice: str = DEFAULT_RU_VOICE) -> str:
    """Голос для текста: русский → ru_voice, иначе → en_voice."""
    return ru_voice if is_russian(text) else en_voice
