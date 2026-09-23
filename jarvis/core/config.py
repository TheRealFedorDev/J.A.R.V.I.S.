"""Конфигурация J.A.R.V.I.S.

Читает `.env` (если есть) и наслаивает значения по умолчанию.
Каждый подсистеме — свой dataclass-блок конфига.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Служебные помощники чтения переменных окружения
# ---------------------------------------------------------------------------
_TRUE_VALUES = {"1", "true", "yes", "on", "y"}


def _env(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, str(default)).lower() in _TRUE_VALUES


def load_dotenv(path: Path) -> None:
    """Минимальный загрузчик .env без сторонних зависимостей."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


# ---------------------------------------------------------------------------
# Блоки конфигурации подсистем
# ---------------------------------------------------------------------------
@dataclass
class PointerConfig:
    """Параметры управления физическим курсором (подсистема ввода)."""

    enabled: bool = True                     # управлять ли настоящей мышью
    smoothing: str = "one_euro"              # none | ema | kalman | one_euro
    ema_alpha: float = 0.35                  # сила EMA-сглаживания (0..1)
    one_euro_min_cutoff: float = 0.6         # One Euro: сила фильтра в покое (меньше = плавнее, больше = отзывчивее)
    one_euro_beta: float = 0.3               # One Euro: реакция на скорость (больше = меньше лаг/«липкость»)
    sensitivity: float = 1.0                 # множитель амплитуды движения
    dead_zone: float = 2.0                   # мёртвая зона на сглаженном (px): гасит «дребезг»
    dropout: float = 0.4                     # сек: кратковременная пропажа лица/моргание не сбрасывают фильтр
    max_move: float = 600.0                  # макс. перемещение курсора за кадр (px): гасит выбросы модели
    offset_path: Path = field(default_factory=lambda: Path("models/gaze_offset.json"))
    clamp_to_screen: bool = True             # не выходить за границы экрана

    @classmethod
    def from_env(cls) -> "PointerConfig":
        return cls(
            enabled=_env_bool("POINTER_ENABLED", True),
            smoothing=_env("POINTER_SMOOTHING", "one_euro"),
            ema_alpha=_env_float("POINTER_EMA_ALPHA", 0.35),
            one_euro_min_cutoff=_env_float("POINTER_ONE_EURO_MIN_CUTOFF", 0.6),
            one_euro_beta=_env_float("POINTER_ONE_EURO_BETA", 0.3),
            sensitivity=_env_float("POINTER_SENSITIVITY", 1.0),
            dead_zone=_env_float("POINTER_DEAD_ZONE", 2.0),
            dropout=_env_float("POINTER_DROPOUT", 0.4),
            max_move=_env_float("POINTER_MAX_MOVE", 600.0),
            offset_path=Path(_env("GAZE_OFFSET_PATH", "models/gaze_offset.json")),
            clamp_to_screen=_env_bool("POINTER_CLAMP", True),
        )


@dataclass
class OverlayConfig:
    """Параметры визуального оверлея PyQt6."""

    show_gaze_cursor: bool = True            # точка взгляда на экране
    cursor_color: str = "#00e5ff"            # цвет курсора взгляда
    cursor_size: int = 22                    # радиус точки взгляда (px)
    show_camera_panel: bool = True           # мини-меню предпросмотра камеры
    panel_width: int = 340                   # ширина мини-панели
    panel_height: int = 240                  # высота мини-панели
    panel_position: str = "top-left"         # top-left | top-right | bottom-left | bottom-right
    panel_opacity: float = 0.88              # прозрачность панели (0..1)

    @classmethod
    def from_env(cls) -> "OverlayConfig":
        return cls(
            show_gaze_cursor=_env_bool("OVERLAY_GAZE_CURSOR", True),
            cursor_color=_env("OVERLAY_CURSOR_COLOR", "#00e5ff"),
            cursor_size=_env_int("OVERLAY_CURSOR_SIZE", 22),
            show_camera_panel=_env_bool("OVERLAY_CAMERA_PANEL", True),
            panel_width=_env_int("OVERLAY_PANEL_WIDTH", 340),
            panel_height=_env_int("OVERLAY_PANEL_HEIGHT", 240),
            panel_position=_env("OVERLAY_PANEL_POSITION", "top-left"),
            panel_opacity=_env_float("OVERLAY_PANEL_OPACITY", 0.88),
        )


@dataclass
class HandConfig:
    """Параметры «воздушной мыши» (подсистема руки)."""

    enabled: bool = True                 # включать ли руку-мышь
    landmarker_path: str | None = None   # путь к hand_landmarker.task
    mirror_x: bool = True                # зеркалить X (как трекпад/превью)
    sensitivity: float = 1.6             # усиление движения палец→курсор
    pinch_close: float = 0.35            # ниже этой доли — щипок зажат
    pinch_open: float = 0.55             # выше этой — отпущен (гистерезис)
    startup_frames: int = 3              # кадров «зажима» после появления руки
    ema: float = 0.40                    # сглаживание приращений (0..1)
    dead_zone: float = 1.5               # мёртвая зона движения (px/кадр)
    max_move: float = 1400.0             # потолок перемещения за кадр (px)
    dropout: float = 0.8                 # сек до возврата к управлению взглядом

    @classmethod
    def from_env(cls) -> "HandConfig":
        return cls(
            enabled=_env_bool("HAND_ENABLED", True),
            landmarker_path=os.environ.get("HAND_LANDMARKER_PATH") or None,
            mirror_x=_env_bool("HAND_MIRROR_X", True),
            sensitivity=_env_float("HAND_SENSITIVITY", 1.6),
            pinch_close=_env_float("HAND_PINCH_CLOSE", 0.35),
            pinch_open=_env_float("HAND_PINCH_OPEN", 0.55),
            startup_frames=_env_int("HAND_STARTUP_FRAMES", 3),
            ema=_env_float("HAND_EMA", 0.40),
            dead_zone=_env_float("HAND_DEAD_ZONE", 1.5),
            max_move=_env_float("HAND_MAX_MOVE", 1400.0),
            dropout=_env_float("HAND_DROPOUT", 0.8),
        )


@dataclass
class GazeConfig:
    """Параметры трекера взгляда (eyetrax + MediaPipe FaceLandmarker)."""

    enabled: bool = True                     # включать ли подсистему взгляда
    camera_index: int = 0                    # индекс веб-камеры (0 → фоллбэк 1)
    model_path: Path = field(
        default_factory=lambda: Path("models/gaze/model.pkl")
    )                                        # обученная модель eyetrax (pickle)
    face_landmarker_path: str | None = None  # путь к face_landmarker.task (None → кэш)
    calibration: str = "5p"                  # 5p (быстро) | 9p (точно)

    @classmethod
    def from_env(cls) -> "GazeConfig":
        raw_model = os.environ.get("GAZE_MODEL_PATH")
        raw_face = os.environ.get("GAZE_FACE_LANDMARKER_PATH")
        return cls(
            enabled=_env_bool("GAZE_ENABLED", True),
            camera_index=_env_int("GAZE_CAMERA", 0),
            model_path=Path(raw_model) if raw_model else Path("models/gaze/model.pkl"),
            face_landmarker_path=raw_face or None,
            calibration=_env("GAZE_CALIBRATION", "5p"),
        )


@dataclass
class VoiceConfig:
    """Параметры голосового управления (faster-whisper / vosk, оффлайн)."""

    enabled: bool = True                     # включать ли голос
    asr: str = "faster-whisper"              # faster-whisper | vosk
    model_path: Path = field(
        default_factory=lambda: Path("models/vosk/vosk-model-small-ru-0.22")
    )                                        # путь vosk (фоллбэк ASR)
    whisper_model: str = "large-v3"          # размер/имя модели faster-whisper
    whisper_device: str = "auto"             # auto | cpu | cuda
    whisper_compute_type: str = "default"    # default | int8 | float16 | ...
    whisper_language: str = "ru"             # язык распознавания
    vad_threshold: float = 0.012             # порог RMS для «говорит»
    silence_end: float = 0.55                # сек тишины → конец фразы
    partial_seconds: float = 0.85            # интервал partial-перетранскрибции
    device: int | None = None                # индекс микрофона sounddevice
    samplerate: int = 16000                  # 16 кГц (whisper/vosk)
    block_seconds: float = 0.25              # длительность блока захвата (сек)

    @classmethod
    def from_env(cls) -> "VoiceConfig":
        raw = os.environ.get("VOICE_MODEL_PATH")
        raw_dev = os.environ.get("VOICE_DEVICE")
        asr = (os.environ.get("VOICE_ASR") or "faster-whisper").strip().lower()
        if asr not in ("faster-whisper", "vosk"):
            asr = "faster-whisper"
        return cls(
            enabled=_env_bool("VOICE_ENABLED", True),
            asr=asr,
            model_path=Path(raw) if raw else Path("models/vosk/vosk-model-small-ru-0.22"),
            whisper_model=os.environ.get("WHISPER_MODEL") or "large-v3",
            whisper_device=os.environ.get("WHISPER_DEVICE") or "auto",
            whisper_compute_type=os.environ.get("WHISPER_COMPUTE_TYPE") or "default",
            whisper_language=os.environ.get("WHISPER_LANGUAGE") or "ru",
            vad_threshold=_env_float("VOICE_VAD_THRESHOLD", 0.012),
            silence_end=_env_float("VOICE_SILENCE_END", 0.55),
            partial_seconds=_env_float("VOICE_PARTIAL_SECONDS", 0.85),
            device=_env_int("VOICE_DEVICE", 0) if raw_dev else None,
            samplerate=_env_int("VOICE_SAMPLERATE", 16000),
            block_seconds=_env_float("VOICE_BLOCK_SECONDS", 0.25),
        )


@dataclass
class AppConfig:
    """Сводный конфиг всего приложения."""

    data_dir: Path = field(default_factory=Path)
    log_level: str = "INFO"
    pointer: PointerConfig = field(default_factory=PointerConfig.from_env)
    hand: HandConfig = field(default_factory=HandConfig.from_env)
    gaze: GazeConfig = field(default_factory=GazeConfig.from_env)
    voice: VoiceConfig = field(default_factory=VoiceConfig.from_env)
    overlay: OverlayConfig = field(default_factory=OverlayConfig.from_env)

    @classmethod
    def load(cls, env_file: Path | None = None) -> "AppConfig":
        if env_file is not None:
            load_dotenv(env_file)
        return cls(
            data_dir=Path(_env("JARVIS_DATA_DIR", "models")),
            log_level=_env("JARVIS_LOG_LEVEL", "INFO"),
            pointer=PointerConfig.from_env(),
            hand=HandConfig.from_env(),
            gaze=GazeConfig.from_env(),
            voice=VoiceConfig.from_env(),
            overlay=OverlayConfig.from_env(),
        )