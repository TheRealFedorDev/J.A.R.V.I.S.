"""Сборка GUI-части приложения: QApplication + мост + окна.

Порядок важен: QApplication создаётся первым и обязательно в главном потоке
(это делает лаунчер, вызывая OverlayApp из main).
"""

from __future__ import annotations

import logging
import sys

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from jarvis.core.config import OverlayConfig, PointerConfig
from jarvis.core.events import EventBus, PointerStatusEvent
from jarvis.gaze.tracker import GazeTracker
from jarvis.input.pointer import PointerService
from jarvis.overlay.bus_adapter import BusToQtBridge
from jarvis.overlay.camera_panel import CameraPanel
from jarvis.overlay.overlay_window import CursorOverlay

log = logging.getLogger("jarvis.overlay")


class OverlayApp:
    """Связывает шину событий, сервисы и Qt-окна."""

    def __init__(
        self,
        overlay_config: OverlayConfig,
        pointer_config: PointerConfig,
        bus: EventBus,
        tracker: GazeTracker,
        pointer: PointerService,
    ) -> None:
        self._overlay_config = overlay_config
        self._tracker = tracker
        self._pointer = pointer

        # QApplication д.б. создан до любых QWidget
        self._qapp = QApplication(sys.argv)
        self._qapp.setQuitOnLastWindowClosed(False)
        self._qapp.aboutToQuit.connect(self._on_about_to_quit)

        # Мост шины → Qt-сигналы
        self._bridge = BusToQtBridge(bus)

        # Окно курсора взгляда (на весь экран, клики сквозь него)
        self._cursor_overlay = CursorOverlay(overlay_config)
        self._bridge.gaze_sample.connect(self._cursor_overlay.on_gaze_sample)
        self._bridge.recentering.connect(
            lambda e: self._cursor_overlay.on_recentering(e.active)
        )
        if overlay_config.show_gaze_cursor:
            self._cursor_overlay.showFullScreen()

        # Мини-панель трекинга
        self._panel = CameraPanel(
            overlay_config,
            on_calibrate=self._on_calibrate,
            on_toggle_cursor=self._on_toggle_cursor,
            on_recenter=self._on_recenter,
            on_reset_offset=self._on_reset_offset,
            on_quit=self._quit,
        )
        self._bridge.camera_frame.connect(self._panel.on_camera_frame)
        self._bridge.tracker_status.connect(self._panel.on_tracker_status)
        self._bridge.gaze_sample.connect(self._panel.on_gaze_sample)
        self._bridge.recentering.connect(
            lambda e: self._panel.on_recenter_state(e.active)
        )
        if overlay_config.show_camera_panel:
            self._panel.show()
            self._panel.raise_()

        bus.subscribe(PointerStatusEvent, self._on_pointer_status)

        # Панель выходит поверх курсора взгляда
        QTimer.singleShot(150, lambda: self._panel.raise_())

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------
    def run(self) -> int:
        """Запуск события GUI; блокирует до выхода. Возвращает код завершения."""
        log.info("GUI оверлей запущен")
        return self._qapp.exec()

    # ------------------------------------------------------------------
    # Обработчики кнопок панели
    # ------------------------------------------------------------------
    def _on_calibrate(self) -> None:
        log.info("запрос калибровки от пользователя")
        self._tracker.request_calibration()

    def _on_recenter(self) -> None:
        log.info("запрос выравнивания взгляда")
        self._pointer.start_recenter()

    def _on_reset_offset(self) -> None:
        log.info("запрос сброса смещения")
        self._pointer.reset_offset()

    def _on_toggle_cursor(self) -> None:
        self._pointer.set_enabled(not self._pointer.enabled)

    def _on_pointer_status(self, event: PointerStatusEvent) -> None:
        self._panel.on_cursor_toggled(event.enabled)
        if event.message:
            self._panel.on_pointer_message(event.message)

    def _quit(self) -> None:
        log.info("пользователь завершил работу")
        self._qapp.quit()

    def _on_about_to_quit(self) -> None:
        # останавливаем фоновые сервисы перед выходом
        try:
            self._tracker.stop()
        finally:
            self._pointer.stop()