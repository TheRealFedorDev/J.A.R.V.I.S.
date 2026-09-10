"""Мини-меню трекинга: предпросмотр камеры и статус системы.

Небольшой всегда-поверху плавающий виджет показывает:
- что видит камера (отзеркаленный предпросмотр);
- статус трекера (READY / CALIBRATING / TRACKING / ERROR);
- FPS, наличие лица, моргание, координаты взгляда;

и даёт управление: повторная **калибровка**, **вкл/выкл курсора**, **выход**.
Панель можно перетаскивать за заголовок.
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPainterPath, QPixmap
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from jarvis.core.config import OverlayConfig
from jarvis.core.events import CameraFrameEvent, GazeSample, TrackerStatusEvent
from jarvis.gaze.screen import primary_screen_size

# Человекочитаемые имена состояний трекера
_STATE_LABELS = {
    "STARTING": "Запуск…",
    "READY": "Готов",
    "CALIBRATING": "Калибровка!",
    "TRACKING": "Трекинг",
    "ERROR": "Ошибка",
    "STOPPED": "Остановлен",
}


class CameraPanel(QWidget):
    """Плавающая мини-панель предпросмотра и статуса Eye-Tracker."""

    def __init__(
        self,
        config: OverlayConfig,
        *,
        on_calibrate,
        on_toggle_cursor,
        on_recenter,
        on_reset_offset,
        on_quit,
    ) -> None:
        super().__init__(None)
        self._config = config
        self._on_calibrate = on_calibrate
        self._on_toggle_cursor = on_toggle_cursor
        self._on_recenter = on_recenter
        self._on_reset_offset = on_reset_offset
        self._on_quit = on_quit

        self._status = "STARTING"
        self._message = "Запуск…"
        self._fps = 0.0
        self._face = False
        self._blink = False
        self._noise: float | None = None
        self._coords = "—"
        self._pixmap: QPixmap | None = None
        self._drag_offset: QPoint | None = None

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowOpacity(self._config.panel_opacity)
        self.setFixedSize(self._config.panel_width, self._config.panel_height)

        self._build_ui()
        self._move_to_configured_position()

    # ------------------------------------------------------------------
    # Построение UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(6)

        # --- Заголовок (за него таскаем панель) ---
        header = QHBoxLayout()
        title = QLabel("J.A.R.V.I.S — Eye Tracker")
        title.setStyleSheet("color: #9fe8ff; font-weight: 600; font-size: 12px;")
        header.addWidget(title)
        header.addStretch(1)
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(20, 20)
        close_btn.setStyleSheet(_BTN_STYLE)
        close_btn.setToolTip("Выход (Esc)")
        close_btn.clicked.connect(self._on_quit)
        header.addWidget(close_btn)
        layout.addLayout(header)

        # --- Предпросмотр камеры ---
        self._video_label = QLabel("ожидание камеры…")
        self._video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._video_label.setMinimumHeight(150)
        self._video_label.setStyleSheet(
            "background: rgba(0,0,0,160); border-radius: 8px; color: #ffffff;"
        )
        layout.addWidget(self._video_label, 1)

        # --- Строка статуса ---
        self._status_label = QLabel("Запуск…")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color: #e8e8e8; font-size: 11px;")
        layout.addWidget(self._status_label)

        # --- Кнопки управления ---
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self._calibrate_btn = QPushButton("Калибровка")
        self._calibrate_btn.setStyleSheet(_BTN_STYLE)
        self._calibrate_btn.clicked.connect(self._on_calibrate)

        self._cursor_btn = QPushButton("Курсор: ВКЛ")
        self._cursor_btn.setStyleSheet(_BTN_STYLE)
        self._cursor_btn.clicked.connect(self._on_toggle_cursor_clicked)

        self._recenter_btn = QPushButton("Выровнять")
        self._recenter_btn.setStyleSheet(_BTN_STYLE)
        self._recenter_btn.setToolTip(
            "Смотрите на центр экрана ~1.5 с — компенсирует сдвиг взгляда"
        )
        self._recenter_btn.clicked.connect(self._on_recenter)

        self._offset_reset_btn = QPushButton("Сброс")
        self._offset_reset_btn.setStyleSheet(_BTN_STYLE)
        self._offset_reset_btn.setToolTip("Сбросить выровненное смещение")
        self._offset_reset_btn.clicked.connect(self._on_reset_offset)

        self._quit_btn = QPushButton("Выход")
        self._quit_btn.setStyleSheet(_BTN_STYLE)
        self._quit_btn.clicked.connect(self._on_quit)

        buttons.addWidget(self._calibrate_btn)
        buttons.addWidget(self._cursor_btn)
        buttons.addWidget(self._recenter_btn)
        buttons.addWidget(self._offset_reset_btn)
        buttons.addWidget(self._quit_btn)
        layout.addLayout(buttons)

    # ------------------------------------------------------------------
    # Слоты моста (вызываются в GUI-потоке)
    # ------------------------------------------------------------------
    def on_camera_frame(self, event: CameraFrameEvent) -> None:
        self._set_frame(event.frame, event.width, event.height)
        self._fps = event.fps

    def on_tracker_status(self, event: TrackerStatusEvent) -> None:
        self._status = event.state
        self._message = event.message
        if event.fps > 0:
            self._fps = event.fps

    def on_gaze_sample(self, sample: GazeSample) -> None:
        self._face = sample.face
        self._blink = sample.blink
        if sample.x is not None:
            self._coords = f"({int(sample.x)}, {int(sample.y)})"
        else:
            self._coords = "—"
        self._noise = sample.noise
        self._update_status_text()

    def on_cursor_toggled(self, enabled: bool) -> None:
        self._cursor_btn.setText("Курсор: ВКЛ" if enabled else "Курсор: ВЫКЛ")

    def on_pointer_message(self, message: str) -> None:
        self._message = message
        self._update_status_text()

    def on_recenter_state(self, active: bool) -> None:
        if active:
            self._saved_message = self._message
            self._message = "Смотрите на ПРИЦЕЛ в центре экрана (1.5 с)…"
        else:
            self._message = getattr(self, "_saved_message", "")
        self._update_status_text()

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------
    def _on_toggle_cursor_clicked(self) -> None:
        self._on_toggle_cursor()

    def _set_frame(self, bgr, width: int, height: int) -> None:
        """Конвертирует BGR (numpy) во фрейм QImage и показывает в label."""
        rgb = bgr[:, :, ::-1].copy()  # BGR -> RGB
        h, w = rgb.shape[:2]
        image = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888)
        self._pixmap = QPixmap.fromImage(image)
        self._video_label.setPixmap(
            self._pixmap.scaled(
                self._video_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self._update_status_text()

    def _update_status_text(self) -> None:
        state_label = _STATE_LABELS.get(self._status, self._status)
        face_txt = "лицо ✓" if self._face else "лицо ✗"
        if self._blink:
            face_txt = "моргание"
        noise_txt = ""
        if self._noise is not None:
            noise_txt = f" | дрожь: {int(self._noise)}px"
            if self._noise > 30 and self._status == "TRACKING":
                noise_txt += " ⚠ перекалибруйся"
        self._status_label.setText(
            f"{state_label}  |  {face_txt}  |  {int(self._fps)} FPS  |  взгляд: {self._coords}{noise_txt}"
            + (f"\n{self._message}" if self._message else "")
        )

    def _move_to_configured_position(self) -> None:
        screen_w, screen_h = primary_screen_size()
        x, y = 10, 10
        if "right" in self._config.panel_position:
            x = screen_w - self.width() - 10
        if "bottom" in self._config.panel_position:
            y = screen_h - self.height() - 10
        self.move(x, y)

    # ------------------------------------------------------------------
    # Перетаскивание за заголовок / выход по Esc
    # ------------------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() < 34:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_offset = None

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self._on_quit()

    # ------------------------------------------------------------------
    # Отображение полупрозрачной панели со скруглёнными углами
    # ------------------------------------------------------------------
    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), 10, 10)
        painter.fillPath(path, QColor(14, 16, 22, 235))


_BTN_STYLE = """
QPushButton {
    background: rgba(30, 90, 120, 200);
    color: #ffffff;
    border: 1px solid rgba(120, 200, 255, 90);
    border-radius: 5px;
    padding: 3px 8px;
    font-size: 11px;
}
QPushButton:hover { background: rgba(40, 120, 160, 230); }
QPushButton:pressed { background: rgba(20, 70, 100, 230); }
"""