"""Консольная анимация старта J.A.R.VИС.

Отображает Sin(x), которая постепенно трансформируется в логотип,
затем пульсирующее «сердце». Работает в терминале — не требует GUI.

Используется пока слушатель ждёт триггер (щелчок / «старт»), чтобы дать
понять, что система живая и готова к работе.
"""

from __future__ import annotations

import math
import shutil
import sys
import time
from typing import Optional


# Параметры консольной анимации (совпадают с визуальной в visualize_anim)
WIDTH = 60
HEIGHT = 7
DURATION = 1.6
SIN_PHASE = 0.6
MORPH_PHASE = 0.3
LOGO_TEXT = "JARVIS"
N_POINTS = 60


def _sin_points(phase: float) -> list[tuple[int, int]]:
    """Точки Sin(x) в символьном поле WIDTH x HEIGHT."""
    pts = []
    span = WIDTH - 4
    amp = 1.5
    for i in range(N_POINTS):
        t = i / max(1, N_POINTS - 1)
        x = int(2 + t * span)
        y = int(3.5 + amp * math.sin(t * math.pi * 2.0))
        pts.append((x, y))
    return pts


def _render_frame(t: float) -> str:
    """Отрендерить кадр консольной анимации."""
    grid = [[" " for _ in range(WIDTH)] for _ in range(HEIGHT)]
    if t < SIN_PHASE:
        # Фаза 1: рисуем Sin(x) точкой-за-точкой
        progress = t / SIN_PHASE
        pts = _sin_points(t)
        n = int(progress * len(pts))
        for i in range(n):
            x, y = pts[i]
            if 0 <= x < WIDTH and 0 <= y < HEIGHT:
                grid[y][x] = "●"
        # Пульсирующая точка в конце
        if n > 0:
            lx, ly = pts[min(n - 1, len(pts) - 1)]
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx * dx + dy * dy <= 1:
                        nx, ny = lx + dx, ly + dy
                        if 0 <= nx < WIDTH and 0 <= ny < HEIGHT:
                            grid[ny][nx] = "◉"
    elif t < SIN_PHASE + MORPH_PHASE:
        # Фаза 2: морфинг Sin → лого
        mt = (t - SIN_PHASE) / MORPH_PHASE
        pts = _sin_points(t)
        n_sin = max(0, int((1.0 - mt) * len(pts)))
        for i in range(n_sin):
            x, y = pts[i]
            if 0 <= x < WIDTH and 0 <= y < HEIGHT:
                grid[y][x] = "·"
        # Логотип выделяется
        for i, ch in enumerate(LOGO_TEXT):
            col = int(8 + i * (WIDTH - 8 - len(LOGO_TEXT)) / max(1, len(LOGO_TEXT) - 1))
            row = 3 + int(mt * 2)  # выходит по центру
            if mt > 0.2:
                grid[row][col] = ch
    else:
        # Фаза 3: логотип + пульс
        pulse = 1 if math.sin(time.time() * math.pi / 0.6) > 0 else 0
        for i, ch in enumerate(LOGO_TEXT):
            col = int(8 + i * (WIDTH - 8 - len(LOGO_TEXT)) / max(1, len(LOGO_TEXT) - 1))
            grid[3][col] = ch.upper() if pulse or i == 2 else ch.lower()

    return "\n".join("".join(row) for row in grid)


def run_boot_animation(duration: float = DURATION, interval: float = 1 / 30) -> None:
    """Запустить консольную boot-анимацию."""
    start = time.time()
    try:
        while True:
            elapsed = time.time() - start
            if elapsed >= duration + 0.3:  # немного постоим на пульсирующем лого
                break
            frame = _render_frame(elapsed)
            sys.stdout.write("\r" + frame)
            sys.stdout.flush()
            time.sleep(interval)
    finally:
        sys.stdout.write("\r" + " " * (WIDTH * HEIGHT) + "\r")
        print("J.A.R.VИС — готов к работе")
        print("Щёлкните пальцами или скажите «старт» для запуска")


def boot_message() -> None:
    """Краткое приветственное сообщение без анимации (для логов)."""
    width = shutil.get_terminal_size().columns
    line = "━" * max(20, width // 2)
    print(line)
    print("          J.A.R.VИС — готов к работе")
    print("Щёлкните пальцами или скажите «старт» для запуска")
    print(line)
