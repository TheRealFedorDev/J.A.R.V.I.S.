"""Тесты boot-анимации: Sin(x) → логотип."""
import math

import numpy as np
from jarvis.visualize_anim import BootAnimation, blend


def test_boot_animation_has_sin_points():
    anim = BootAnimation(width=640, height=480)
    assert len(anim._sin_points) == 120
    # Sin(x) начинается слева
    assert anim._sin_points[0][0] < anim._sin_points[-1][0]
    # И колеблется по вертикали (sin)
    ys = [p[1] for p in anim._sin_points]
    assert min(ys) < max(ys)  # есть колебания высоты


def test_boot_animation_has_logo_points():
    anim = BootAnimation(width=640, height=480)
    assert len(anim._logo_points) > 10  # текст — много точек


def test_render_returns_none_after_duration():
    anim = BootAnimation(duration=1.0)
    overlay = anim.render(1.5)
    assert overlay is None


def test_render_produces_overlay_during_boot():
    anim = BootAnimation(width=640, height=480, duration=1.6)
    # Фаза 1: Sin(x)
    overlay = anim.render(0.2)
    assert overlay is not None
    assert overlay.shape == (480, 640, 4)
    # Должно быть что-то нарисовано (не все нули)
    assert np.any(overlay[:, :, 3] > 0)

    # Фаза 2: морф
    overlay = anim.render(0.7)
    assert overlay is not None
    assert np.any(overlay[:, :, 3] > 0)

    # Фаза 3: фейд
    overlay = anim.render(1.4)
    assert overlay is not None
    assert np.any(overlay[:, :, 3] > 0)


def test_render_steady_pulse():
    anim = BootAnimation(width=640, height=480)
    overlay = anim.render_steady(0.0)
    assert overlay is not None
    assert np.any(overlay[:, :, 3] > 0)
    # Пульсирует — в разные моменты разная максимальная альфа
    o1 = anim.render_steady(0.0)
    o2 = anim.render_steady(0.4)
    a1 = int(o1[:, :, 3].max())
    a2 = int(o2[:, :, 3].max())
    assert a1 != a2  # пульсация работает


def test_blend_with_none_returns_base():
    base = np.zeros((480, 640, 3), dtype=np.uint8)
    assert np.array_equal(blend(base, None), base)


def test_blend_applies_overlay():
    base = np.zeros((10, 10, 3), dtype=np.uint8)
    overlay = np.zeros((10, 10, 4), dtype=np.uint8)
    overlay[5, 5, :3] = [255, 0, 0]      # красный пиксель
    overlay[5, 5, 3] = 255               # полностью наложить
    result = blend(base, overlay)
    assert tuple(result[5, 5]) == (255, 0, 0)  # BGR
    # фон не меняется (альфа=0)
    assert tuple(result[0, 0]) == (0, 0, 0)
