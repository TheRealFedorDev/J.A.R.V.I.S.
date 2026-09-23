"""Тесты STT-хелперов и жестов по кружочкам кончиков."""

from __future__ import annotations

import math

import numpy as np

from jarvis.core.config import VoiceConfig
from jarvis.input.gestures import (
    FingerControls,
    classify_gesture,
    finger_controls,
    finger_radius,
    touch_hysteresis,
)
from jarvis.voice.stt import is_voiced, join_segments, to_float32


# ---------------------------------------------------------------------------
# STT helpers
# ---------------------------------------------------------------------------
def test_to_float32_range():
    pcm = to_float32(np.array([0, 16384, -32768], dtype=np.int16).tobytes())
    assert pcm.dtype == np.float32
    assert abs(float(pcm[0])) < 1e-6
    assert 0.4 < float(pcm[1]) < 0.6
    assert abs(float(pcm[2]) + 1.0) < 1e-6


def test_is_voiced_threshold():
    quiet = np.zeros(400, dtype=np.float32)
    loud = np.full(400, 0.2, dtype=np.float32)
    assert is_voiced(quiet, 0.012) is False
    assert is_voiced(loud, 0.012) is True
    assert is_voiced(np.array([], dtype=np.float32), 0.012) is False


def test_join_segments():
    class S:
        def __init__(self, text):
            self.text = text

    assert join_segments([S(" Привет "), S(""), S("мир")]) == "Привет мир"
    assert join_segments([]) == ""


def test_voice_config_asr_defaults():
    cfg = VoiceConfig.from_env()
    assert cfg.asr == "faster-whisper"
    assert cfg.whisper_model == "medium"
    assert cfg.whisper_language == "ru"


def test_voice_config_asr_env(monkeypatch):
    monkeypatch.setenv("VOICE_ASR", "vosk")
    monkeypatch.setenv("WHISPER_MODEL", "small")
    monkeypatch.setenv("VOICE_PARTIAL_SECONDS", "1.5")
    cfg = VoiceConfig.from_env()
    assert cfg.asr == "vosk"
    assert cfg.whisper_model == "small"
    assert cfg.partial_seconds == 1.5


# ---------------------------------------------------------------------------
# Кружочки кончиков
# ---------------------------------------------------------------------------
def _hand(
    *,
    index_ext=True,
    thumb_at_index=False,
    thumb_at_middle=False,
):
    """21 лендмарк: базовая «вытянутая» рука + опциональные касания."""
    pts = [(0.5, 0.9)]  # wrist
    for i in range(1, 21):
        pts.append((0.5, 0.5))
    # пальцы вытянуты вверх
    for tip, pip, mcp in ((8, 6, 5), (12, 10, 9), (16, 14, 13), (20, 18, 17)):
        pts[mcp] = (0.3 + (tip - 8) * 0.05, 0.6)
        pts[pip] = (0.3 + (tip - 8) * 0.05, 0.4)
        pts[tip] = (0.3 + (tip - 8) * 0.05, 0.2)
    # большой
    pts[1] = (0.35, 0.7)
    pts[2] = (0.3, 0.55)
    pts[3] = (0.28, 0.4)
    pts[4] = (0.25, 0.3)

    if not index_ext:
        # указатель свёрнут: tip ближе к wrist, чем pip
        pts[8] = (0.4, 0.7)
        pts[6] = (0.4, 0.5)

    if thumb_at_index:
        pts[4] = pts[8]
    if thumb_at_middle:
        pts[4] = pts[12]
    return tuple(pts)


def test_finger_radius_positive():
    lms = _hand()
    r = finger_radius(lms, 8, 6)
    assert r > 0
    # диаметр ≈ толщина пальца: 2r = 0.55 * dist(tip, pip)
    expected = 0.5 * 0.55 * math.hypot(
        lms[8][0] - lms[6][0], lms[8][1] - lms[6][1]
    )
    assert abs(r - expected) < 1e-9


def test_finger_controls_none():
    assert finger_controls(None) is None
    assert finger_controls(((0.0, 0.0),)) is None


def test_finger_controls_mouse_when_index_extended():
    fc = finger_controls(_hand(index_ext=True))
    assert isinstance(fc, FingerControls)
    assert fc.mouse_xy is not None
    assert fc.index_thumb_touch is False


def test_finger_controls_no_mouse_when_index_curled():
    fc = finger_controls(_hand(index_ext=False))
    assert fc.mouse_xy is None


def test_finger_controls_click_index_thumb():
    fc = finger_controls(_hand(thumb_at_index=True))
    assert fc.index_thumb_touch is True
    assert fc.middle_thumb_touch is False


def test_finger_controls_drag_middle_thumb():
    fc = finger_controls(_hand(thumb_at_middle=True, index_ext=True))
    assert fc.middle_thumb_touch is True
    assert fc.index_thumb_touch is False


def test_fist_does_not_click():
    # кулак: кончики сбились, пальцы не вытянуты
    pts = [(0.5, 0.9)] * 21
    pts[0] = (0.5, 0.9)
    for tip, pip in ((8, 6), (12, 10), (16, 14), (20, 18)):
        pts[pip] = (0.5, 0.55)
        pts[tip] = (0.5, 0.75)
    pts[4] = (0.55, 0.75)
    pts[3] = (0.52, 0.6)
    fc = finger_controls(tuple(pts))
    assert fc.index_thumb_touch is False
    assert fc.middle_thumb_touch is False
    assert fc.index_thumb_ratio is None  # гистерезис разомкнётся
    assert classify_gesture(tuple(pts)) == "fist"


def test_classify_click_and_drag():
    assert classify_gesture(_hand(thumb_at_index=True)) == "click"
    assert classify_gesture(_hand(thumb_at_middle=True, index_ext=True)) == "drag"


def test_touch_hysteresis():
    assert touch_hysteresis(False, 0.9) is True
    assert touch_hysteresis(False, 1.1) is False
    assert touch_hysteresis(True, 1.1) is True   # ещё замкнуто (open_at=1.2)
    assert touch_hysteresis(True, 1.3) is False  # разомкнулось
    assert touch_hysteresis(True, None) is False


def test_gesture_labels_include_controls():
    from jarvis.input.gestures import GESTURE_LABELS

    assert "click" in GESTURE_LABELS
    assert "drag" in GESTURE_LABELS
