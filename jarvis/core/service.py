"""Базовый класс сервиса.

Каждый сервис (трекер взгляда, управление курсором, ...) живёт в своём
потоке и имеет единый жизненный цикл: `start()` / `stop()`.
Остановка — кооперативная: цикл проверяет флаг `_stop_event`.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from threading import Event, Thread

log = logging.getLogger("jarvis.service")


class BaseService(ABC):
    """Сервис с собственным потоком выполнения и флагом остановки."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._stop_event = Event()
        self._thread: Thread | None = None

    @abstractmethod
    def _loop(self) -> None:
        """Основной цикл сервиса (исполняется в отдельном потоке)."""

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = Thread(target=self._safe_loop, name=self.name, daemon=True)
        self._thread.start()
        log.info("[%s] запущен", self.name)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        log.info("[%s] остановлен", self.name)

    def _safe_loop(self) -> None:
        try:
            self._loop()
        except Exception:
            log.exception("[%s] необработанная ошибка цикла", self.name)
            self._stop_event.set()