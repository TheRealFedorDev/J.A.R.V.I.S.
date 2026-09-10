"""Чистая логика «воздушной мыши»: палец → курсор, щипок → кнопка.

Без side-эффектов: принимает нормированные координаты пальца и долю сжатия
щипка, возвращает приращение курсора в px и события кнопки. Всё, что связано
с камерой/mediapipe/мышью, остаётся в вызывающем коде.

Механика:
- относительный режим: курсор движется ПРИРАЩЕНИЯМИ от положения, где палец
  показался в кадре (рука опирается на локоть, двигаются только пальцы);
- зеркалирование X (как у трекпада/вебки-превью), если mirror_x=True;
- первые startup_frames кадров после появления руки — «зажим», курсор не
  движется, но база запоминается (не телепорт);
- щипок с гистерезисом: закрыли ниже pinch_close → кнопка нажата,
  раскрыли выше pinch_open → отпущена. Быстрое нажатие = клик (само по себе),
  удержание с движением = drag.
"""

from __future__ import annotations

from dataclasses import dataclass


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
    """Гистерезис щипка + относительный маппинг палец→курсор (стабилен)."""

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
    ) -> None:
        self._sens = sensitivity
        self._mirror_x = mirror_x
        self._close = pinch_close
        self._open = pinch_open
        self._startup = startup_frames
        self._ema = ema
        self._dead_zone = dead_zone
        self._max_move = max_move

        self._prev: tuple[float, float] | None = None  # прошлый палец (норм.)
        self._frames = 0                                # кадров с рукой подряд
        self._pinch = False                             # кнопка сейчас нажата
        self._sdx = 0.0                                 # сглаженное приращение
        self._sdy = 0.0

    def reset(self) -> None:
        """Рука пропала: сбрасываем базу, кнопку и сглаживание."""
        self._prev = None
        self._frames = 0
        self._pinch = False
        self._sdx = self._sdy = 0.0

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
            self._prev = (nx, ny)   # первый кадр — только захват базы
            self._frames = 1

        dx = dy = 0.0
        if self._frames >= self._startup:
            dx = (nx - self._prev[0]) * screen_w * self._sens
            dy = (ny - self._prev[1]) * screen_h * self._sens
        self._prev = (nx, ny)

        # лёгкое сглаживание приращений (EMA) + мёртвая зона + потолок скорости
        self._sdx = self._ema * dx + (1.0 - self._ema) * self._sdx
        self._sdy = self._ema * dy + (1.0 - self._ema) * self._sdy
        out_dx, out_dy = self._sdx, self._sdy
        if (out_dx ** 2 + out_dy ** 2) < self._dead_zone ** 2:
            out_dx = out_dy = 0.0
        m = float((self._max_move ** 2) / (out_dx ** 2 + out_dy ** 2 + 1e-9))
        if m < 1.0:
            out_dx *= m
            out_dy *= m

        # гистерезис щипка
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