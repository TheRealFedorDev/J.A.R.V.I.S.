"""Драйвер MediaPipe HandLandmarker: кодирует один кадр → фичи руки."""

from __future__ import annotations

import logging
import os
import time

import cv2
import numpy as np

from jarvis.hand import features as H

log = logging.getLogger("jarvis.hand")


def resolve_hand_landmarker_task(explicit: str | None) -> str:
    """Путь к hand_landmarker.task: env → кэш → ошибка (как для face_)."""
    if explicit:
        return explicit
    env = os.environ.get("HAND_LANDMARKER_PATH")
    if env:
        return env
    cache = os.path.join(
        os.path.expanduser("~"), ".cache", "eyetrax", "mediapipe", "hand_landmarker.task"
    )
    if os.path.exists(cache):
        return cache
    raise FileNotFoundError(
        "hand_landmarker.task не найден. Скачайте его и укажите "
        "HAND_LANDMARKER_PATH или положите в "
        "%USERPROFILE%\\.cache\\eyetrax\\mediapipe\\."
    )


class HandLandmarkerDriver:
    """Обёртка над mediapipe HandLandmarker (video mode).

    `extract(frame)` возвращает либо None (руки нет/недостаточно уверенно),
    либо (nx, ny, pinch_ratio): нормированное положение кончика указательного
    пальца (0..1 от камеры, X в сторону кадра) и долю сжатия щипка.
    """

    def __init__(
        self,
        model_path: str | None = None,
        min_detection_confidence: float = 0.5,
        min_tracking_confidence: float = 0.5,
        max_num_hands: int = 1,
    ) -> None:
        import mediapipe as mp
        from mediapipe.tasks.python import vision
        from mediapipe.tasks.python.core.base_options import BaseOptions

        self._mp = mp
        options = vision.HandLandmarkerOptions(
            base_options=BaseOptions(
                model_asset_path=resolve_hand_landmarker_task(model_path)
            ),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=max_num_hands,
            min_hand_detection_confidence=min_detection_confidence,
            min_hand_presence_confidence=min_tracking_confidence,
            min_tracking_confidence=min_tracking_confidence,
        )
        self._landmarker = vision.HandLandmarker.create_from_options(options)
        self._last_ts_ms = 0
        log.info("HandLandmarker готов")

    # ------------------------------------------------------------------
    def extract(self, frame):
        """Возвращает (nx, ny, pinch_ratio) или None, если руки не видно."""
        rgb = np.ascontiguousarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        ts = int(time.time() * 1000)
        if ts <= self._last_ts_ms:
            ts = self._last_ts_ms + 1
        self._last_ts_ms = ts
        res = self._landmarker.detect_for_video(
            self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb),
            ts,
        )
        if not res.hand_landmarks:
            return None
        # точки: (x, y, z) нормализованные
        lms = np.array(
            [(lm.x, lm.y, lm.z) for lm in res.hand_landmarks[0]],
            dtype=np.float64,
        )
        nx, ny = H.fingers_xy(lms)
        pinch = H.pinch_ratio(lms)
        return (nx, ny, pinch)