"""Анимация загрузки: Sin(x) → логотип → пульсирующее «сердце» J.A.R.VИС.

Анимация состоит из трёх фаз:
  1. Точка рисует кривую Sin(x) слева направо (0.6 с).
  2. Кривая «сворачивается» в буквы J.A.R.ВИС (0.3 с) — точки раскидываются
     по контуру текста.
  3. Логотип пульсирует (мигает) — визуальный индикатор «живого» приложения.

Не зависит от внешних ресурсов (только numpy + opencv), полностью тестируется
на координатах точек без GUI.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

# Цвета (BGR)
WHITE = (255, 255, 255)
CYAN = (0, 220, 255)
CYAN_DIM = (30, 90, 130)
ORANGE = (0, 140, 255)

FONT = cv2.FONT_HERSHEY_SIMPLEX


def _put_text_alpha(overlay: np.ndarray, text: str, org,
                    font_face, font_scale, color, thickness, line_type, alpha: int) -> None:
    """Рисует текст на RGBA-оверлее с заданной альфа-прозрачностью.

    cv2.putText не пишет альфа из 4-го элемента цвета — поэтому рисуем текст
    в временный BGR-буфер, берём маску и масштабируем альфа.
    """
    h, w = overlay.shape[:2]
    buf = np.zeros((h, w, 3), dtype=np.uint8)
    cv2.putText(buf, text, org, font_face, font_scale, color[:3], thickness, line_type)
    if alpha > 0:
        mask = (buf.sum(axis=2) > 0).astype(np.uint8)
        for c in range(3):
            overlay[:, :, c] = np.maximum(overlay[:, :, c], buf[:, :, c])
        overlay[:, :, 3] = np.maximum(overlay[:, :, 3], mask * alpha)


def _circle_alpha(overlay: np.ndarray, center, radius, color, alpha: int) -> None:
    """Рисует заполненный круг с альфа."""
    if alpha <= 0:
        return
    cv2.circle(overlay, center, radius, (*color[:3], 255), -1)
    h, w = overlay.shape[:2]
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.circle(mask, center, radius, 255, -1)
    overlay[:, :, 3] = np.maximum(overlay[:, :, 3], mask.astype(np.uint8) * alpha // 255 * alpha // 255)


@dataclass
class BootAnimation:
    """Самодостаточный boot-аниматор. Формирует «кадры» для отрисовки.

    Использование:
        anim = BootAnimation(width=640, height=480)
        for frame_t in np.arange(0, 2.0, dt):
            overlay = anim.render(frame_t)      # RGBA-наложение
            # blend overlay onto base frame
    """

    width: int = 640
    height: int = 480
    duration: float = 1.6        # общая длительность анимации
    sin_phase: float = 0.6       # фаза рисования Sin(x)
    morph_phase: float = 0.3     # фаза morph в лого
    n_points: int = 120          # точек на кривой Sin(x)
    logo_text: str = "J.A.R.ВИС"
    _sin_points: list = field(default_factory=list)
    _logo_points: list = field(default_factory=list)
    _font_scale: float = 0.7
    _thickness: int = 2

    def __post_init__(self) -> None:
        self._compute_sin_points()
        self._compute_logo_points()

    # ------------------------------------------------------------------
    def _compute_sin_points(self) -> None:
        """Предрасчёт точек Sin(x) в пикселях окна (левый край → правый)."""
        pts = []
        margin_x, margin_y = self.width * 0.1, self.height * 0.35
        span = self.width - 2 * margin_x
        amp = self.height * 0.12
        for i in range(self.n_points):
            t = i / max(1, self.n_points - 1)
            x = int(margin_x + t * span)
            y = int(margin_y + amp * math.sin(t * math.pi * 2.0) + self.height * 0.15)
            pts.append((x, y))
        self._sin_points = pts

    def _compute_logo_points(self) -> None:
        """Контур текста логотипа в пикселях (точки для morph-анимации)."""
        img = np.zeros((self.height, self.width, 1), dtype=np.uint8)
        size = cv2.getTextSize(self.logo_text, FONT, self._font_scale, self._thickness)
        tx = (self.width - size[0][0]) // 2
        ty = (self.height + size[0][1]) // 2
        cv2.putText(img, self.logo_text, (tx, ty), FONT,
                    self._font_scale, 255, self._thickness, cv2.LINE_AA)
        ys, xs = np.where(img[:, :, 0] > 0)
        self._logo_points = [(int(x), int(y)) for x, y in zip(xs, ys)]
        if not self._logo_points:
            self._logo_points = [(self.width // 2, self.height // 2)]

    # ------------------------------------------------------------------
    def render(self, t: float) -> np.ndarray | None:
        """Вернуть RGBA-оверлей для момента t (сек), или None — если анимация пройдена."""
        if t >= self.duration:
            return None
        overlay = np.zeros((self.height, self.width, 4), dtype=np.uint8)

        if t < self.sin_phase:
            # Фаза 1: точка рисует Sin(x)
            progress = t / self.sin_phase
            n = int(progress * len(self._sin_points))
            for i in range(1, n):
                cv2.line(overlay, self._sin_points[i - 1], self._sin_points[i],
                         (*CYAN[:3], 220), 2)
            # «пульсирующая точка» в конце
            if n > 0:
                pulse = 0.6 + 0.4 * math.sin(t * 8.0)
                r = max(2, int(pulse * 5))
                cv2.circle(overlay, self._sin_points[min(n - 1, len(self._sin_points) - 1)],
                           r, (*WHITE[:3], 200), -1)
        elif t < self.sin_phase + self.morph_phase:
            # Фаза 2: морфинг Sin -> лого
            mt = (t - self.sin_phase) / self.morph_phase
            n_sin = max(0, int((1.0 - mt) * len(self._sin_points)))
            for i in range(1, n_sin):
                cv2.line(overlay, self._sin_points[i - 1], self._sin_points[i],
                         (*CYAN_DIM[:3], 120), 1)
            # Логотип «выделяется» из оставшихся точек
            if self._logo_points:
                sample_n = int(mt * len(self._logo_points))
                for (lx, ly) in self._logo_points[:sample_n:3]:
                    cv2.circle(overlay, (lx, ly), 1, (*CYAN[:3], 220), -1)
                size = cv2.getTextSize(self.logo_text, FONT, self._font_scale, self._thickness)
                tx = (self.width - size[0][0]) // 2
                ty = (self.height + size[0][1]) // 2
                alpha = int(mt * 255)
                if alpha > 10:
                    _put_text_alpha(overlay, self.logo_text, (tx, ty), FONT,
                                    self._font_scale, ORANGE, self._thickness,
                                    cv2.LINE_AA, alpha)
        else:
            # Фаза 3: чистый логотип с первой пульсацией (переход к steady-state)
            size = cv2.getTextSize(self.logo_text, FONT, self._font_scale, self._thickness)
            tx = (self.width - size[0][0]) // 2
            ty = (self.height + size[0][1]) // 2
            fade = (t - self.sin_phase - self.morph_phase) / max(1e-6,
                  self.duration - self.sin_phase - self.morph_phase)
            alpha = int(min(1.0, fade * 1.5) * 200)
            if alpha > 5:
                _put_text_alpha(overlay, self.logo_text, (tx, ty), FONT,
                                self._font_scale, CYAN, self._thickness,
                                cv2.LINE_AA, alpha)
        return overlay

    def render_steady(self, t: float) -> np.ndarray:
        """Постоянный (post-boot) overlay: пульсирующее лого в углу.

        t — абсолютное время. Пульсация ~ 0.8 с. Используется после boot-duration,
        чтобы дать понять, что J.A.RВИС «жив», пока система готовится.
        """
        overlay = np.zeros((self.height, self.width, 4), dtype=np.uint8)
        scale = 0.4
        size = cv2.getTextSize(self.logo_text, FONT, scale, 1)
        # уголок внизу-справа
        tx = self.width - size[0][0] - 12
        ty = self.height - 12
        pulse = 0.5 + 0.5 * math.sin(t * math.pi / 0.8)
        alpha = int(60 + pulse * 80)
        if alpha > 0:
            _put_text_alpha(overlay, self.logo_text, (tx, ty), FONT, scale,
                            ORANGE, 1, cv2.LINE_AA, alpha)
        return overlay


def blend(base: np.ndarray, overlay: Any | None, alpha_mul: float = 1.0) -> np.ndarray:
    """Альфа-наслоение overlay (H, W, 4) на base (H, W, 3 BGR)."""
    if overlay is None:
        return base
    a = (overlay[:, :, 3].astype(float) / 255.0) * alpha_mul
    a = np.clip(a, 0.0, 1.0)
    rgb = overlay[:, :, :3].astype(float)
    bgr = base.astype(float)
    masked = (rgb * a[..., None]) + (bgr * (1 - a)[..., None])
    return masked.astype(np.uint8)
