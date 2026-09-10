"""Мост «шина событий → Qt-сигналы».

События шины публикуются из потока трекера. Qt-виджеты должны обновляться
только в GUI-потоке, поэтому мост превращает события в pyqtSignal —
слоты автоматически исполняются в потоке приёмника (главном потоке GUI).
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

from jarvis.core.events import (
    CameraFrameEvent,
    RecenteringEvent,
    TrackerStatusEvent,
)


class BusToQtBridge(QObject):
    """Подписывается на шину и переизлучает события сигналами Qt."""

    camera_frame = pyqtSignal(object)         # CameraFrameEvent
    tracker_status = pyqtSignal(object)       # TrackerStatusEvent
    recentering = pyqtSignal(object)          # RecenteringEvent

    def __init__(self, bus) -> None:
        super().__init__()
        bus.subscribe(CameraFrameEvent, self._on_camera)
        bus.subscribe(TrackerStatusEvent, self._on_status)
        bus.subscribe(RecenteringEvent, self._on_recentering)