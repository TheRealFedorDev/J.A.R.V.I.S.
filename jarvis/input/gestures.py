"""Статичные жесты руки по 21 лендмарку MediaPipe (ТЗ §1: P0).

Классификатор чистый (numpy-free, только math) — тестируется без камеры.
Порядок лендмарков MediaPipe Hand:
    0 — запястье; 4/8/12/16/20 — кончики; 6/10/14/18 —PIP; 5/9/13/17 — MCP.
Палец считается вытянутым, если кончик дальше от запястья, чем PIP-сустав.
"""

from __future__ import annotations

import math

# кончики,PIP, MCP для [index, middle, ring, pinky]
_FINGERS = ((8, 6, 5), (12, 10, 9), (16, 14, 13), (20, 18, 17))
_THUMB_TIP, _THUMB_IP, _THUMB_MCP = 4, 3, 2

# связи скелета для отрисовки (MediaPipe HAND_CONNECTIONS, упрощённо)
HAND_CONNECTIONS: tuple[tuple[int, int], ...] = (
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20),
    (0, 17),
)


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def classify_gesture(
    landmarks: tuple[tuple[float, float], ...] | None,
    pinch: float | None = None,
) -> str | None:
    """Определяет статичный жест по лендмаркам.

    Возвращает одно из: fist | palm | thumbs_up | thumbs_down | point |
    pinch | None (руки нет / не распознано).
    """
    if not landmarks or len(landmarks) < 21:
        return None

    wrist = landmarks[0]
    thumb_tip = landmarks[_THUMB_TIP]

    extended: list[bool] = []
    for tip_i, pip_i, _mcp_i in _FINGERS:
        tip_d = _dist(landmarks[tip_i], wrist)
        pip_d = _dist(landmarks[pip_i], wrist)
        extended.append(tip_d > pip_d * 1.12)

    thumb_d_tip = _dist(thumb_tip, wrist)
    thumb_d_ip = _dist(landmarks[_THUMB_IP], wrist)
    thumb_extended = thumb_d_tip > thumb_d_ip * 1.05

    # щипок: низкий pinch_ratio от драйвера или очень близкие кончики
    # (порог маленький: у кулака кончики тоже рядом, но не настолько)
    if pinch is not None and pinch < 0.35:
        return "pinch"
    if _dist(thumb_tip, landmarks[8]) < 0.04:
        return "pinch"

    idx, mid, ring, pinky = extended

    if not any(extended) and not thumb_extended:
        return "fist"
    if idx and mid and ring and pinky and thumb_extended:
        return "palm"
    if idx and not mid and not ring and not pinky:
        # большой вытянут вверх/вниз — thumbs up/down
        if thumb_extended and thumb_tip[1] < wrist[1] - 0.05:
            return "thumbs_up"
        if thumb_extended and thumb_tip[1] > wrist[1] + 0.05:
            return "thumbs_down"
        return "point"
    if thumb_extended and not any(extended):
        if thumb_tip[1] < wrist[1]:
            return "thumbs_up"
        return "thumbs_down"
    return None


GESTURE_LABELS: dict[str, str] = {
    "fist": "Кулак",
    "palm": "Ладонь",
    "thumbs_up": "👍 вверх",
    "thumbs_down": "👎 вниз",
    "point": "Указатель",
    "pinch": "Щипок",
}
