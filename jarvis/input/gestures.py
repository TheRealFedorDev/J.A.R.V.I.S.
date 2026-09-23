"""Статичные жесты руки по 21 лендмарку MediaPipe (ТЗ §1: P0).

Классификатор чистый (numpy-free, только math) — тестируется без камеры.
Порядок лендмарков MediaPipe Hand:
    0 — запястье; 4/8/12/16/20 — кончики; 6/10/14/18 — PIP; 5/9/13/17 — MCP.
Палец считается вытянутым, если кончик дальше от запястья, чем PIP-сустав.

Управление мышью (кружочки на кончиках, диаметр ≈ толщина пальца):
    • есть кружок указательного (палец вытянут) → курсор;
    • кружки указательного и большого коснулись → ЛКМ;
    • кружки среднего и большого коснулись → drag&drop.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# кончики,PIP, MCP для [index, middle, ring, pinky]
_FINGERS = ((8, 6, 5), (12, 10, 9), (16, 14, 13), (20, 18, 17))
_THUMB_TIP, _THUMB_IP, _THUMB_MCP = 4, 3, 2

# диаметр кружка = толщина пальца ≈ 0.55 длины фаланги tip–PIP
_DIAM_FACTOR = 0.55

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


def finger_radius(
    landmarks: tuple[tuple[float, float], ...],
    tip: int,
    joint: int,
) -> float:
    """Радиус кружка кончика: диаметр ≈ толщина пальца."""
    return 0.5 * _DIAM_FACTOR * _dist(landmarks[tip], landmarks[joint])


@dataclass(frozen=True)
class FingerControls:
    """Сырые кружочки кончиков для управления мышью.

    mouse_xy — положение кружка указательного, если палец вытянут (иначе None).
    index_thumb_ratio / middle_thumb_ratio — dist/(r1+r2); ≤1.0 — касание.
    """

    mouse_xy: tuple[float, float] | None = None
    index_thumb_ratio: float | None = None
    middle_thumb_ratio: float | None = None
    index_thumb_touch: bool = False
    middle_thumb_touch: bool = False


def finger_controls(landmarks: tuple[tuple[float, float], ...] | None) -> FingerControls | None:
    """Кружочки кончиков → управление (мышь / ЛКМ / drag). None — руки нет."""
    if not landmarks or len(landmarks) < 21:
        return None

    wrist = landmarks[0]
    index_tip = landmarks[8]
    middle_tip = landmarks[12]
    thumb_tip = landmarks[4]

    # кружок указательного: палец вытянут (кончик дальше от запястья, чем PIP)
    def _ext(tip_i: int, pip_i: int) -> bool:
        return _dist(landmarks[tip_i], wrist) > _dist(landmarks[pip_i], wrist) * 1.05

    index_extended = _ext(8, 6)
    mouse_xy = index_tip if index_extended else None

    r_thumb = finger_radius(landmarks, _THUMB_TIP, _THUMB_IP)
    r_index = finger_radius(landmarks, 8, 6)
    r_middle = finger_radius(landmarks, 12, 10)

    d_it = _dist(thumb_tip, index_tip)
    d_mt = _dist(thumb_tip, middle_tip)
    rsum_it = r_thumb + r_index
    rsum_mt = r_thumb + r_middle

    ratio_it = d_it / max(rsum_it, 1e-9)
    ratio_mt = d_mt / max(rsum_mt, 1e-9)

    # кулак: все пальцы свернулись — касания кончиков не интерпретируем
    n_ext = sum(
        1 for tip_i, pip_i, _ in _FINGERS if _ext(tip_i, pip_i)
    )
    is_fist = n_ext == 0

    return FingerControls(
        mouse_xy=mouse_xy,
        # в кулаке ratio гасим → гистерезис размыкает click/drag
        index_thumb_ratio=None if is_fist else ratio_it,
        middle_thumb_ratio=None if is_fist else ratio_mt,
        index_thumb_touch=(not is_fist) and ratio_it <= 1.0,
        middle_thumb_touch=(not is_fist) and ratio_mt <= 1.0,
    )


def touch_hysteresis(prev: bool, ratio: float | None, *, open_at: float = 1.2) -> bool:
    """Касание с гистерезисом: замкнуть ≤1, разомкнуть ≤open_at."""
    if ratio is None:
        return False
    if prev:
        return ratio <= open_at
    return ratio <= 1.0


def classify_gesture(
    landmarks: tuple[tuple[float, float], ...] | None,
    pinch: float | None = None,
) -> str | None:
    """Определяет статичный жест по лендмаркам.

    Возвращает одно из: fist | drag | click | palm | thumbs_up | thumbs_down |
    point | pinch | None (руки нет / не распознано).
    drag — средний+большой коснулись; click — указательный+большой.
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

    # кулак — до касаний (свернутые кончики не дают «ложный» click/drag)
    if not any(extended) and not thumb_extended:
        return "fist"

    fc = finger_controls(landmarks)
    if fc is not None and fc.middle_thumb_touch and not fc.index_thumb_touch:
        return "drag"
    if fc is not None and fc.index_thumb_touch:
        return "click"

    # щипок: низкий pinch_ratio от драйвера (fallback без полных лендмарков)
    if pinch is not None and pinch < 0.35:
        return "pinch"
    if _dist(thumb_tip, landmarks[8]) < 0.04:
        return "pinch"

    idx, mid, ring, pinky = extended

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
    "click": "🖱 ЛКМ",
    "drag": "↔ Drag",
}
