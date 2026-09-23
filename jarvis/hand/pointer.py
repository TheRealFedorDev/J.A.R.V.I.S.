"""Чистая логика «воздушной мыши»: палец → курсор, жесты → кнопки.

Без side-эффектов: принимает нормированные координаты пальца, возвращает
приращение курсора в px и события кнопки.

Антидрожание:
- One Euro Filter по координате палеца (покой → сильно гасит шум MediaPipe,
  быстрое движение → почти без задержки);
- отсечение одиночных «скачков» лендмарка (outlier reject);
- EMA по приращениям + мёртвая зона — финальный слой стабильности.
"""

from __future__ import annotations

from dataclasses import dataclass

from jarvis.input.smoothing import OneEuroSmoother


@dataclass(frozen=True)
class HandCommand:
    """Один кадр управления.

    dx, dy — приращение курсора в **экранных пикселях** (может быть 0).
    button_down / button_up — события нажатия/отпускания кнопки мыши.
    """

    dx: float | None = None      # None — рука не видна (курсор держим)
    dy: float | None = None
    button_down: bool = False
    button_up: bool = False


class HandPointerState:
    """Сглаженный относительный маппинг палец→курсор + гистерезис кнопок."""

    def __init__(
        self,
        *,
        sensitivity: float = 1.6,
        mirror_x: bool = True,
        pinch_close: float = 0.35,
        pinch_open: float = 0.55,
        startup_frames: int = 3,
        ema: float = 0.40,
        dead_zone: float = 1.5,
        max_move: float = 1400.0,
        one_euro_min_cutoff: float = 0.8,
        one_euro_beta: float = 0.12,
        spike_threshold: float = 0.08,   # норм. единиц: резкий скачок → пропуск
    ) -> None:
        self._sens = sensitivity
        self._mirror_x = mirror_x
        self._close = pinch_close
        self._open = pinch_open
        self._startup = startup_frames
        self._ema = max(0.05, min(0.95, ema))
        self._dead_zone = dead_zone
        self._max_move = max_move
        self._spike = spike_threshold

        # сглаживание СЫРОЙ координаты пальца (антидрожание)
        self._pos_filter = OneEuroSmoother(
            min_cutoff=one_euro_min_cutoff,
            beta=one_euro_beta,
        )
        self._prev: tuple[float, float] | None = None  # прошлый (сглаженный) палец
        self._frames = 0
        self._pinch = False
        self._sdx = 0.0
        self._sdy = 0.0

    def reset(self) -> None:
        """Рука пропала: сбрасываем базу, кнопку и сглаживание."""
        self._prev = None
        self._frames = 0
        self._pinch = False
        self._sdx = self._sdy = 0.0
        self._pos_filter.reset()

    @property
    def pinch_pressed(self) -> bool:
        return self._pinch

    def step(
        self,
        finger_xy: tuple[float, float] | None,
        pinch: float | None,
        screen_w: int,
        screen_h: int,
    ) -> HandCommand:
        """Обработка одного кадра руки."""
        if finger_xy is None:
            self.reset()
            return HandCommand()

        nx, ny = finger_xy
        if self._mirror_x:
            nx = 1.0 - nx

        self._frames += 1
        if self._prev is None:
            # первый кадр: прогреваем фильтр, только захват базы
            sx, sy = self._pos_filter.step(nx, ny)
            self._prev = (sx, sy)
            self._frames = 1
            return HandCommand(dx=0.0, dy=0.0)

        # 1) One Euro по сырой координате — гасит дрожание в покое
        sx, sy = self._pos_filter.step(nx, ny)

        # 2) отсечение одиночного скачка лендмарка (не двигаем курсор)
        jump = ((sx - self._prev[0]) ** 2 + (sy - self._prev[1]) ** 2) ** 0.5
        if jump > self._spike:
            # базу не двигаем — «замораживаем» позицию на этот кадр
            sx, sy = self._prev

        dx = dy = 0.0
        if self._frames >= self._startup:
            dx = (sx - self._prev[0]) * screen_w * self._sens
            dy = (sy - self._prev[1]) * screen_h * self._sens
        self._prev = (sx, sy)

        # 3) EMA по приращениям + мёртвая зона + потолок скорости
        self._sdx = self._ema * dx + (1.0 - self._ema) * self._sdx
        self._sdy = self._ema * dy + (1.0 - self._ema) * self._sdy
        out_dx, out_dy = self._sdx, self._sdy
        if (out_dx ** 2 + out_dy ** 2) < self._dead_zone ** 2:
            out_dx = out_dy = 0.0
            # в покое затухаем, чтобы EMA не «копила» хвост
            self._sdx *= 0.85
            self._sdy *= 0.85
        m = float((self._max_move ** 2) / (out_dx ** 2 + out_dy ** 2 + 1e-9))
        if m < 1.0:
            out_dx *= m
            out_dy *= m

        # гистерезис щипка (fallback, основной путь — circle gestures)
        down = up = False
        if pinch is not None:
            if not self._pinch and pinch < self._close:
                self._pinch = True
                down = True
            elif self._pinch and pinch > self._open:
                self._pinch = False
                up = True

        return HandCommand(
            dx=out_dx, dy=out_dy, button_down=down, button_up=up
        )
