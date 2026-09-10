"""Математика руки: выделение полезных фич из 21-й точки HandLandmarker.

Всё чистое (numpy-совместимо, без mediapipe) — легко тестируется без камеры.

Используемые лендмарки MediaPipe Hands (справочно):
    0  запястье,  4  кончик большого пальца,  8  кончик указательного,
    9  основание среднего пальца (проксимальная фаланга).
"""

from __future__ import annotations

import numpy as np

INDEX_TIP = 8
THUMB_TIP = 4
WRIST = 0
MIDDLE_MCP = 9


def _point(lms, i: int) -> tuple[float, float]:
    return float(lms[i][0]), float(lms[i][1])


def _dist(a, b) -> float:
    return float(np.hypot(a[0] - b[0], a[1] - b[1]))


def fingers_xy(lms) -> tuple[float, float]:
    """Нормированное положение кончика указательного пальца (0..1), X в обратку."""
    return _point(lms, INDEX_TIP)


def pinch_ratio(lms) -> float:
    """Доля сжатия щипка: dist(палец, большой)/palm_size.

    palm_size = dist(запястье, основание среднего) — масштаб руки в кадре,
    поэтому порог срабатывает при любом расстоянии руки до камеры.
    """
    px, py = _point(lms, INDEX_TIP)
    tx, ty = _point(lms, THUMB_TIP)
    wx, wy = _point(lms, WRIST)
    mx, my = _point(lms, MIDDLE_MCP)
    palm = _dist((wx, wy), (mx, my)) + 1e-6
    return _dist((px, py), (tx, ty)) / palm


def palm_size(lms) -> float:
    """Размер ладони в пикселях кадра (для дебага/пороги)."""
    return _dist(_point(lms, WRIST), _point(lms, MIDDLE_MCP))