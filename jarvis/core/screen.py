"""Геометрия основного монитора (перенос из jarvis/gaze/screen)."""

from __future__ import annotations

from functools import lru_cache

from screeninfo import get_monitors


@lru_cache(maxsize=1)
def primary_screen_size() -> tuple[int, int]:
    """Возвращает (width, height) главного монитора в пикселях."""
    m = get_monitors()[0]
    return m.width, m.height