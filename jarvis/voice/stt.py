"""Чистые хелперы стримингового STT (без модели/микрофона)."""

from __future__ import annotations

import numpy as np


def to_float32(data: bytes) -> np.ndarray:
    """PCM int16 → float32 в диапазоне [-1, 1] (формат faster-whisper)."""
    pcm = np.frombuffer(data, dtype=np.int16)
    return pcm.astype(np.float32) / 32768.0


def is_voiced(pcm: np.ndarray, threshold: float) -> bool:
    """Энергетический VAD: RMS блока выше порога → есть речь."""
    if pcm.size == 0:
        return False
    rms = float(np.sqrt(np.mean(np.square(pcm, dtype=np.float64)) + 1e-12))
    return rms > threshold


def join_segments(segments) -> str:
    """Склеить сегменты whisper (объекты с .text) в одну фразу."""
    parts: list[str] = []
    for seg in segments:
        text = (getattr(seg, "text", None) or "").strip()
        if text:
            parts.append(text)
    return " ".join(parts).strip()


def touch_ratio(dist: float, r_sum: float) -> float:
    """dist / (r1+r2): ≤1 — кружочки касаются/перекрываются."""
    return dist / max(r_sum, 1e-9)
