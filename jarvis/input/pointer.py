"""PointerService — подсистема управления физическим курсором.

Подписывается на события ``GazeSample`` (движение курсора взглядом) и
``HandSample`` (воздушная мышь), а также на ``VoiceCommandEvent``
(клик/скролл голосом) и двигает настоящую мышь (через pynput.mouse.Controller).
Работает «по событиям», поэтому собственный цикл держит только поток живым
до остановки.

Обработка движения курсора:
1. скоупинг чувствительностью относительно центра экрана (sensitivity);
2. сглаживание (One Euro Filter) — без дрожания в покое;
3. защита: краткие пропуски (моргание/теря лица) не сбрасывают фильтр;
4. мёртвая зона: убирает «дребезг», движение остаётся непрерывным.
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path

from pynput.mouse import Button, Controller

from jarvis.core.config import HandConfig, PointerConfig
from jarvis.core.events import (
    EventBus,
    GazeSample,
    HandSample,
    PointerStatusEvent,
    RecenteringEvent,
    VoiceCommandEvent,
)
from jarvis.core.screen import primary_screen_size
from jarvis.core.service import BaseService
from jarvis.hand.pointer import HandPointerState
from jarvis.input.smoothing import Smoother, create_smoother

log = logging.getLogger("jarvis.pointer")


class PointerService(BaseService):
    """Превращает события взгляда в движение курсора мыши.

    Когда в кадре есть РУКА — курсор ведёт она (точность несколько px),
    взгляд игнорируется. Рука ушла дольше `HAND_DROPOUT` — управление
    возвращается взгляду.
    """

    def __init__(
        self,
        config: PointerConfig,
        bus: EventBus,
        hand_config: HandConfig | None = None,
    ) -> None:
        super().__init__(name="pointer")
        self._config = config
        self._bus = bus
        self._mouse = Controller()
        self._hc = hand_config
        self._smoother: Smoother = create_smoother(
            config.smoothing,
            ema_alpha=config.ema_alpha,
            one_euro_min_cutoff=config.one_euro_min_cutoff,
            one_euro_beta=config.one_euro_beta,
        )
        self._hand_state = HandPointerState(
            sensitivity=hand_config.sensitivity if hand_config else 1.6,
            mirror_x=hand_config.mirror_x if hand_config else True,
            pinch_close=hand_config.pinch_close if hand_config else 0.35,
            pinch_open=hand_config.pinch_open if hand_config else 0.55,
            startup_frames=hand_config.startup_frames if hand_config else 3,
            ema=hand_config.ema if hand_config else 0.40,
            dead_zone=hand_config.dead_zone if hand_config else 1.5,
            max_move=hand_config.max_move if hand_config else 1400.0,
        )
        self._hand_active = False       # ведёт ли сейчас курсор рука
        self._hand_dropout_at: float | None = None
        self._enabled = config.enabled
        self._last_applied: tuple[float, float] | None = None
        self._last_time: float | None = None
        self._offset = self._load_offset(config.offset_path)  # компенсационное смещение (px)
        self._offset_path = config.offset_path
        self._recenter_active = False      # идёт ли сбор точек для выравнивания
        self._recenter_start = 0.0
        self._recenter_buf: list[tuple[float, float]] = []
        self._gaze_lost_at: float | None = None  # когда взгляда не было (dropout)
        self._screen_w, self._screen_h = primary_screen_size()
        self._click_active = False         # курсор зафиксирован при клике
        bus.subscribe(VoiceCommandEvent, self._on_voice_command)
        bus.subscribe(GazeSample, self._on_gaze_sample)
        if self._hc is not None and self._hc.enabled:
            bus.subscribe(HandSample, self._on_hand_sample)
        log.info(
            "курсор: enabled=%s smoothing=%s sensitivity=%.2f рука=%s",
            self._enabled, config.smoothing, config.sensitivity,
            bool(self._hc and self._hc.enabled),
        )

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------
    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if not enabled and self._hand_state.pinch_pressed:
            self._mouse.release(Button.left)
        self._hand_state.reset()
        self._bus.publish(PointerStatusEvent(enabled=self._enabled))
        log.info("управление курсором: %s", "ВКЛ" if enabled else "ВЫКЛ")

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def offset(self) -> tuple[float, float]:
        """Текущее компенсационное смещение курсора (dx, dy)."""
        return self._offset

    def start_recenter(self) -> None:
        """Начать выравнивание: смотрите на центр экрана ~1.5 с.

        Соберёт сглаженные точки и выставит смещение так, чтобы курсор
        встал точно в центр видения пользователя. Гасит системный сдвиг
        калибровки/позы головы.
        """
        self._recenter_active = True
        self._recenter_start = time.monotonic()
        self._recenter_buf.clear()
        self._smoother.reset()
        self._bus.publish(RecenteringEvent(active=True))
        log.info("выравнивание взгляда: смотрите на центр экрана")

    def reset_offset(self) -> None:
        """Сбросить компенсацию смещения (курсор снова «как есть»)."""
        self._offset = (0.0, 0.0)
        try:
            self._offset_path.unlink(missing_ok=True)
        except OSError:
            pass
        self._bus.publish(PointerStatusEvent(
            enabled=self._enabled, message="Смещение сброшено",
        ))
        log.info("смещение взгляда сброшено")

    @staticmethod
    def _load_offset(path: Path) -> tuple[float, float]:
        """Читает сохранённое смещение из JSON. При ошибке/отсутствии — (0, 0)."""
        try:
            if not path.exists():
                return (0.0, 0.0)
            data = json.loads(path.read_text(encoding="utf-8"))
            dx, dy = float(data.get("dx", 0.0)), float(data.get("dy", 0.0))
            if not (math.isfinite(dx) and math.isfinite(dy)):
                return (0.0, 0.0)
            if abs(dx) < 1e-6 and abs(dy) < 1e-6:
                return (0.0, 0.0)
            log.info("смещение загружено из %s: (%+.0f, %+.0f)", path, dx, dy)
            return (dx, dy)
        except Exception:
            log.warning("не удалось прочитать %s, смещение = (0, 0)", path, exc_info=True)
            return (0.0, 0.0)

    def _save_offset(self) -> None:
        try:
            self._offset_path.parent.mkdir(parents=True, exist_ok=True)
            self._offset_path.write_text(
                json.dumps({"dx": self._offset[0], "dy": self._offset[1]}),
                encoding="utf-8",
            )
        except OSError:
            log.warning("не удалось сохранить смещение в %s", self._offset_path)

    # ------------------------------------------------------------------
    # Обработчик голосовых команд
    # ------------------------------------------------------------------
    def _on_voice_command(self, ev: VoiceCommandEvent) -> None:
        a = ev.action
        if a is None:
            return
        if a == "enable":
            self.set_enabled(True)
            return
        if a == "disable":
            self.set_enabled(False)
            return
        if not self._enabled:
            return
        if a == "click":
            self._mouse.click(Button.left, 1)
        elif a == "double_click":
            self._mouse.click(Button.left, 2)
        elif a == "right_click":
            self._mouse.click(Button.right, 1)
        elif a == "scroll":
            amount = int(ev.args.get("amount", 3))
            direction = ev.args.get("direction", "down")
            self._mouse.scroll(0, -amount if direction == "down" else amount)
        log.info("голос: %s выполнен", a)

    # ------------------------------------------------------------------
    # Обработчик события взгляда (основной канал курсора)
    # ------------------------------------------------------------------
    def _on_gaze_sample(self, sample: GazeSample) -> None:
        # Взгляда нет (лицо потеряно / моргание): курсор замирает, фильтр
        # держим живым dropout секунд — короткие пропуски не сбрасывают.
        if sample.x is None or sample.y is None:
            if self._gaze_lost_at is None:
                self._gaze_lost_at = time.monotonic()
            elif time.monotonic() - self._gaze_lost_at >= self._config.dropout:
                self._smoother.reset()
                self._last_applied = None
                self._gaze_lost_at = time.monotonic()  # не дрейфуем
            return
        self._gaze_lost_at = None

        # Выравнивание: собираем сырые точки, пока смотрите в центр ~1.5 с.
        if self._recenter_active:
            self._recenter_buf.append((sample.x, sample.y))
            if time.monotonic() - self._recenter_start >= 1.5:
                self._finish_recenter()
            return

        if not self._enabled or self._hand_active:
            return  # рука ведёт курсор; взгляд в это время игнорируем

        # калибровочное/выровненное смещение → чувствительность → фильтр
        x = sample.x + self._offset[0]
        y = sample.y + self._offset[1]
        x, y = self._scale(x, y)
        sx, sy = self._smoother.step(x, y)

        # мёртвая зона против «дребезга» в покое (сравнение с применённой точкой)
        if self._last_applied is not None:
            if math.hypot(sx - self._last_applied[0], sy - self._last_applied[1]) < (
                self._config.dead_zone
            ):
                return
        self._last_applied = (sx, sy)
        self._last_time = time.monotonic()
        nx = min(max(sx, 0.0), float(self._screen_w - 1))
        ny = min(max(sy, 0.0), float(self._screen_h - 1))
        self._mouse.position = (int(nx), int(ny))

    # ------------------------------------------------------------------
    # Обработчик событий руки (воздушная мышь)
    # ------------------------------------------------------------------
    def _on_hand_sample(self, sample: HandSample) -> None:
        if not self._enabled:
            return
        if sample.x is None or sample.y is None:
            # рука пропала из кадра: после dropout возврат к взгляду
            if self._hand_dropout_at is None:
                self._hand_dropout_at = time.monotonic()
            if self._hand_dropout_at is not None and \
                    time.monotonic() - self._hand_dropout_at >= (
                        self._hc.dropout if self._hc else 0.8
                    ):
                if self._hand_state.pinch_pressed:
                    self._mouse.release(Button.left)
                self._hand_active = False
                self._hand_state.reset()
                self._smoother.reset()
            return

        self._hand_dropout_at = None
        width, height = primary_screen_size()
        cmd = self._hand_state.step((sample.x, sample.y), sample.pinch,
                                    width, height)
        if cmd.dx is None:
            return
        self._hand_active = True
        self._last_time = time.monotonic()

        px, py = self._mouse.position
        nx = min(max(px + cmd.dx, 0.0), float(width - 1))
        ny = min(max(py + cmd.dy, 0.0), float(height - 1))
        self._mouse.position = (int(nx), int(ny))

        if cmd.button_down:
            self._mouse.press(Button.left)
            log.debug("щипок: кнопка нажата")
        if cmd.button_up:
            self._mouse.release(Button.left)
            log.debug("щипок: кнопка отпущена")

    # ------------------------------------------------------------------
    def _finish_recenter(self) -> None:
        """Завершить выравнивание: центр экрана минус медиана собранных точек."""
        self._recenter_active = False
        if len(self._recenter_buf) < 5:
            self._bus.publish(RecenteringEvent(active=False))
            log.info("выравнивание отменено: мало точек (следите за центром)")
            return
        width, height = primary_screen_size()
        cx, cy = width / 2.0, height / 2.0
        pts = self._recenter_buf
        med_x = sorted(p[0] for p in pts)[len(pts) // 2]
        med_y = sorted(p[1] for p in pts)[len(pts) // 2]
        self._offset = (cx - med_x, cy - med_y)
        self._save_offset()
        self._bus.publish(RecenteringEvent(active=False))
        self._bus.publish(PointerStatusEvent(
            enabled=self._enabled,
            message=f"Взгляд выровнен: Δ({int(self._offset[0])}, {int(self._offset[1])})",
        ))
        log.info(
            "выравнивание завершено: смещение=(%+.0f, %+.0f) px по %d точкам",
            self._offset[0], self._offset[1], len(pts),
        )

    def _scale(self, x: float, y: float) -> tuple[float, float]:
        """Масштабирует координаты относительно центра экрана (sensitivity)."""
        sensitivity = self._config.sensitivity
        if abs(sensitivity - 1.0) < 1e-6:
            return x, y
        width, height = primary_screen_size()
        cx, cy = width / 2.0, height / 2.0
        return cx + (x - cx) * sensitivity, cy + (y - cy) * sensitivity

    # ------------------------------------------------------------------
    # Цикл: сервис событийный, поток просто дежурит до остановки
    # ------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop_event.wait(timeout=1.0):
            pass