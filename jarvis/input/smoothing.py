"""Сглаживание траектории взгляда перед движением курсора.

«Сырые» предсказания регрессии дрожат от кадра к кадру. Фильтры:
- `none` — без сглаживания (максимальная отзывчивость, но «дребезг»);
- `ema` — экспоненциальное скользящее среднее (просто и быстро);
- `kalman` — фильтр Кальмана (плавно, но чувствителен к настройкам);
- `one_euro` — One Euro Filter (рекомендуется): сила фильтрации зависит
  от скорости движения. Стоишь — дрожание убирается, поехал глазами —
  задержка минимальна, курсор «поспевает».
"""

from __future__ import annotations

import time
from typing import Protocol

import cv2
import numpy as np


class Smoother(Protocol):
    def step(self, x: float, y: float) -> tuple[float, float]: ...
    def reset(self) -> None: ...


class NoSmoother:
    """Пропускает точку без изменений."""

    def step(self, x: float, y: float) -> tuple[float, float]:
        return x, y

    def reset(self) -> None:
        pass


class EMASmoother:
    """Экспоненциальное скользящее среднее. alpha — сила сглаживания (0..1)."""

    def __init__(self, alpha: float = 0.35) -> None:
        self._alpha = alpha
        self._last: tuple[float, float] | None = None

    def step(self, x: float, y: float) -> tuple[float, float]:
        if self._last is None:
            self._last = (x, y)
        else:
            lx, ly = self._last
            self._last = (
                self._alpha * x + (1 - self._alpha) * lx,
                self._alpha * y + (1 - self._alpha) * ly,
            )
        return self._last

    def reset(self) -> None:
        self._last = None


class KalmanSmoother:
    """Фильтр Кальмана (постоянная скорость) поверх координат взгляда."""

    def __init__(self, process_var: float = 50.0, measurement_var: float = 0.2) -> None:
        self._process_var = process_var
        self._measurement_var = measurement_var
        self._init_filter()

    def _init_filter(self) -> None:
        dt = 1.0
        self._kf = cv2.KalmanFilter(4, 2)
        self._kf.transitionMatrix = np.array(
            [[1, 0, dt, 0],
             [0, 1, 0, dt],
             [0, 0, 1, 0],
             [0, 0, 0, 1]], dtype=np.float32
        )
        self._kf.measurementMatrix = np.array(
            [[1, 0, 0, 0], [0, 1, 0, 0]], dtype=np.float32
        )
        self._kf.processNoiseCov = np.eye(4, dtype=np.float32) * self._process_var
        self._kf.measurementNoiseCov = np.eye(2, dtype=np.float32) * self._measurement_var
        self._kf.errorCovPost = np.eye(4, dtype=np.float32)
        self._kf.statePre = np.zeros((4, 1), np.float32)
        self._kf.statePost = np.zeros((4, 1), np.float32)

    def step(self, x: float, y: float) -> tuple[float, float]:
        measurement = np.array([[x], [y]], dtype=np.float32)
        # Первый шаг: инициализируем состояние измерением
        if not np.any(self._kf.statePost):
            self._kf.statePre[:2] = measurement
            self._kf.statePost[:2] = measurement
        pred = self._kf.predict()
        self._kf.correct(measurement)
        return float(pred[0, 0]), float(pred[1, 0])

    def reset(self) -> None:
        self._init_filter()


class OneEuroSmoother:
    """One Euro Filter — адаптивное сглаживание для указателя взгляда.

    Сила фильтра зависит от текущей скорости сигнала:
      фиксация взгляда  -> низкая скорость -> сильная фильтрация (без дрожи);
      быстрый скачок     -> высокая скорость -> слабая фильтрация (без запаздывания).
    """

    def __init__(
        self,
        min_cutoff: float = 1.2,
        beta: float = 0.08,
        d_cutoff: float = 1.0,
    ) -> None:
        self._fcmin = min_cutoff   # мин. частота среза (сила фильтра в покое)
        self._beta = beta          # чувствительность к скорости (0 — почти ema)
        self._dcutoff = d_cutoff   # частота среза сглаживания скорости
        self._x: float | None = None
        self._y: float | None = None
        self._dx = 0.0             # сглаженная скорость (px/с)
        self._last_t: float | None = None

    def _alpha(self, cutoff: float, dt: float) -> float:
        tau = 1.0 / (2.0 * np.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def step(self, x: float, y: float) -> tuple[float, float]:
        now = time.perf_counter()
        if self._last_t is None or self._x is None:
            self._x, self._y = x, y
            self._last_t = now
            return x, y

        dt = max(now - self._last_t, 1e-4)
        self._last_t = now

        # мгновенная скорость движения взгляда
        speed = np.hypot(x - self._x, y - self._y) / dt
        # сглаживаем скорость (низкочастотный фильтр по производной)
        alpha_d = self._alpha(self._dcutoff, dt)
        self._dx = alpha_d * speed + (1.0 - alpha_d) * self._dx

        # адаптивная частота среза: чем быстрее движение, тем меньше фильтрация
        cutoff = self._fcmin + self._beta * abs(self._dx)
        alpha = self._alpha(cutoff, dt)

        self._x = alpha * x + (1.0 - alpha) * self._x
        self._y = alpha * y + (1.0 - alpha) * self._y
        return self._x, self._y

    def reset(self) -> None:
        self._x = None
        self._y = None
        self._dx = 0.0
        self._last_t = None


def create_smoother(
    mode: str,
    ema_alpha: float = 0.35,
    one_euro_min_cutoff: float = 1.2,
    one_euro_beta: float = 0.08,
) -> Smoother:
    """Фабрика фильтров по имени режима: none | ema | kalman | one_euro."""
    if mode == "ema":
        return EMASmoother(alpha=ema_alpha)
    if mode == "kalman":
        return KalmanSmoother()
    if mode == "one_euro":
        return OneEuroSmoother(
            min_cutoff=one_euro_min_cutoff, beta=one_euro_beta
        )
    return NoSmoother()