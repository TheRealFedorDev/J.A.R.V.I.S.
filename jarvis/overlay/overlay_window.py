"""Полотно взгляда: полупрозрачное окно на весь экран с точкой взгляда.

Окно «прозрачно для мыши» (WA_TransparentForMouseEvents), поэтому клики
пользователя проходят сквозь него к настоящему приложению под ним.
Настоящий курсор двигается PointerService, а эта точка показывает,
«куда система смотрит» в данный момент.
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QWidget

from jarvis.core.config import OverlayConfig
from jarvis.core.events import GazeSample


class CursorOverlay(QWidget):
    """Полупрозрачное полотно с точкой взгляда на весь основной экран."""

    def __init__(self, config: OverlayConfig) -> None:
        super().__init__(None)
        self._config = config
        self._gaze: GazeSample | None = None
        self._recenter = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    def on_gaze_sample(self, sample: GazeSample) -> None:
        """Слот моста: обновляет положение точки и перерисовывает окно."""
        self._gaze = sample
        self.update()

    def on_recentering(self, active: bool) -> None:
        """Включить/выключить прицел в центре (во время выравнивания)."""
        self._recenter = active
        self.update()

    def _paint_recenter_target(self, painter: QPainter) -> None:
        """Прицел в центре экрана, по которому надо совместить взгляд."""
        cx, cy = self.width() / 2.0, self.height() / 2.0
        color = QColor(self._config.cursor_color)
        halo = QColor(color)
        halo.setAlpha(50)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(halo)
        painter.drawEllipse(QPointF(cx, cy), 46.0, 46.0)

        ring = QColor(color)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(ring, 3))
        painter.drawEllipse(QPointF(cx, cy), 34.0, 34.0)

        cross = QColor("#ffffff")
        pen = QPen(cross, 2)
        painter.setPen(pen)
        for dx in (-16, 16):
            painter.drawLine(int(cx + dx), int(cy - 6), int(cx + dx), int(cy + 6))
        for dy in (-16, 16):
            painter.drawLine(int(cx - 6), int(cy + dy), int(cx + 6), int(cy + dy))
        painter.setBrush(cross)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(cx, cy), 3.0, 3.0)

    def _paint_glow_dot(self, painter: QPainter, cx: float, cy: float) -> None:
        """Рисует «светящийся» маркер взгляда: центр + внешнее кольцо."""
        color = QColor(self._config.cursor_color)
        r = self._config.cursor_size

        # внешняя мягкая аура
        halo = QColor(color)
        halo.setAlpha(46)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(halo)
        painter.drawEllipse(QPointF(cx, cy), r * 1.7, r * 1.7)

        # кольцо
        ring = QColor(color)
        ring.setAlpha(210)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(ring, 2))
        painter.drawEllipse(QPointF(cx, cy), r, r)

        # центральная точка
        core = QColor(color)
        painter.setBrush(core)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(cx, cy), 3.0, 3.0)

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt-имя метода)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._recenter:
            self._paint_recenter_target(painter)
        if self._gaze is None or self._gaze.x is None or self._gaze.y is None:
            return  # лицо не найдено / моргание — точку не рисуем
        self._paint_glow_dot(painter, self._gaze.x, self._gaze.y)