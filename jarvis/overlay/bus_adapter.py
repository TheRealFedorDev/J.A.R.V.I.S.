"""Мост «шина событий → Qt-сигналы».

События шины публикуются из потока трекера. Qt-виджеты должны обновляться
только в GUI-потоке, поэтому мост превращает события в pyqtSignal —
слоты автоматически исполняются в потоке приёмника (главном потоке GUI).
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

from jarvis.core.events import (
    CameraFrameEvent,
    GazeSample,
    RecenteringEvent,
    TrackerStatusEvent,
)


class BusToQtBridge(QObject):
    """Подписывается на шину и переизлучает события сигналами Qt."""

    camera_frame = pyqtSignal(object)         # CameraFrameEvent
    tracker_status = pyqtSignal(object)       # TrackerStatusEvent
    recentering = pyqtSignal(object)          # RecenteringEvent
    gaze_sample = pyqtSignal(object)          # GazeSample

    def __init__(self, bus) -> None:
        super().__init__()
        bus.subscribe(CameraFrameEvent, self._on_camera)
        bus.subscribe(TrackerStatusEvent, self._on_status)
        bus.subscribe(RecenteringEvent, self._on_recentering)
        bus.subscribe(GazeSample, self._on_gaze)

    # ------------------------------------------------------------------
    # Слоты шины (вызываются в потоке публикатора → пробрасываем в Qt)
    # ------------------------------------------------------------------
    def _on_camera(self, event: CameraFrameEvent) -> None:
        self.camera_frame.emit(event)

    def _on_status(self, event: TrackerStatusEvent) -> None:
        self.tracker_status.emit(event)

    def _on_recentering(self, event: RecenteringEvent) -> None:
        self.recentering.emit(event)

    def _on_gaze(self, event: GazeSample) -> None:
        self.gaze_sample.emit(event)
