"""Шина событий J.A.R.V.I.S.

Архитектурный принцип: компоненты **не знают друг о друге напрямую**,
а взаимодействуют через события. Подсистема-издатель вызывает `publish`,
заинтересованные подсистемы **подписываются** (`subscribe`) и получают
события в своём потоке.

Это позволяет, например, отключить управление мышью (PointerService),
не трогая код трекера взгляда, а позже добавлять голосовой ввод/LLM,
просто публикуя свои события в ту же шину.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Callable

log = logging.getLogger("jarvis.events")


# ---------------------------------------------------------------------------
# Типы событий (снимки состояния подсистем)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CameraFrameEvent:
    """Уменьшенный кадр с камеры для мини-панели предпросмотра."""

    frame: Any                    # np.ndarray (BGR, отзеркален для предпросмотра)
    width: int
    height: int
    fps: float


@dataclass(frozen=True)
class TrackerStatusEvent:
    """Текущее состояние трекера взгляда."""

    state: str                     # STARTING|CALIBRATING|READY|TRACKING|ERROR|STOPPED
    message: str = ""
    fps: float = 0.0


@dataclass(frozen=True)
class PointerStatusEvent:
    """Состояние подсистемы управления курсором."""

    enabled: bool
    message: str = ""


@dataclass(frozen=True)
class RecenteringEvent:
    """Идёт ли выравнивание взгляда (панель/оверлей рисуют мишень)."""

    active: bool = True


@dataclass(frozen=True)
class HandSample:
    """Снимок руки за кадр из лендмаркера.

    x, y — нормированное положение кончика указательного пальца в КАМЕРНЫХ
    координатах (0..1, X не зеркалится — зеркалит подсистема курsort); None,
    если рука не видна. pinch — доля сжатия щипка (больше = открыт).
    """

    x: float | None
    y: float | None
    pinch: float | None = None
    confidence: float = 0.0
    hand_count: int = 0
    landmarks: tuple[tuple[float, float], ...] | None = None  # 21 точка (x, y) 0..1 — скелет для UI
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class GazeSample:
    """Один сэмпл взгляда (трекер → шина): экранные координаты + статус лица.

    x, y — положение точки взгляда в ЭКРАННЫХ пикселях; None — лицо
    потеряно или идёт моргание (точку не рисуем, курсор не двигаем).
    noise — оценка дрожи (std последних сэмплов, px); >30 ≈ «перекалибруйся».
    """

    x: float | None = None
    y: float | None = None
    face: bool = False
    blink: bool = False
    noise: float | None = None
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class VoicePartialEvent:
    """Частично распознанная речь — стриминг, пока человек ещё говорит.

    Публикуется на лету (rolling partial faster-whisper / vosk), чтобы UI
    показывал текст до завершения фразы. `final=True` — фраза завершена
    (дублирует текст финального результата, затем публикуется VoiceCommandEvent).
    """

    text: str = ""
    final: bool = False
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class VoiceCommandEvent:
    """Распознанная голосовая команда (аудио-loop → STT → шина)."""

    action: str | None          # click | double_click | right_click | scroll | enable | disable | calibrate | None
    args: dict = field(default_factory=dict)
    text: str = ""              # распознанная фраза (для статуса/оверлея)
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Шина
# ---------------------------------------------------------------------------
class EventBus:
    """Потокобезопасная шина публикации/подписки на события."""

    def __init__(self) -> None:
        self._subscribers: dict[type, list[Callable[[Any], None]]] = defaultdict(list)
        self._lock = Lock()

    def subscribe(self, event_type: type, handler: Callable[[Any], None]) -> None:
        """Подписать обработчик на события указанного типа."""
        with self._lock:
            self._subscribers[event_type].append(handler)

    def publish(self, event: Any) -> None:
        """Разослать событие всем подписчикам его типа."""
        handlers = list(self._subscribers.get(type(event), ()))
        for handler in handlers:
            try:
                handler(event)
            except Exception:  # сбой одного подписчика не убивает остальных
                log.exception("Подписчик %r не смог обработать событие %r", handler, event)