"""Единая настройка логирования J.A.R.V.I.S."""

from __future__ import annotations

import logging
import sys


def _force_utf8_stream() -> None:
    """Перевести stdout/stderr на UTF-8: иначе cp1251 роняет лог на «→» и теряет строки."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATEFMT = "%H:%M:%S"


def setup_logging(level: str = "INFO") -> None:
    _force_utf8_stream()
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=_FORMAT,
        datefmt=_DATEFMT,
        stream=sys.stdout,
    )