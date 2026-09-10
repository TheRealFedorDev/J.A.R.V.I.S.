"""Лаунчер J.A.R.V.I.S.

Собирает приложение из частей:
конфиг → шина событий → сервисы (трекер взгляда, курсор) → оверлей GUI.
"""

from __future__ import annotations

from pathlib import Path

import logging

from jarvis.core.config import AppConfig
from jarvis.core.events import EventBus, VoiceCommandEvent
from jarvis.core.logging import setup_logging
from jarvis.gaze.tracker import GazeTracker
from jarvis.hand.driver import HandLandmarkerDriver, resolve_hand_landmarker_task
from jarvis.input.pointer import PointerService
from jarvis.overlay.app import OverlayApp
from jarvis.voice.service import VoiceService

log = logging.getLogger("jarvis.launcher")
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def build_app() -> OverlayApp:
    """Сборка всех подсистем и оверлея."""

    config = AppConfig.load(env_file=PROJECT_ROOT / ".env")
    setup_logging(config.log_level)

    # Пути к моделям относительно корня проекта (если заданы относительными)
    if not config.gaze.model_path.is_absolute():
        config.gaze.model_path = PROJECT_ROOT / config.gaze.model_path
    if config.gaze.face_landmarker_path and not Path(
        config.gaze.face_landmarker_path
    ).is_absolute():
        config.gaze.face_landmarker_path = str(
            PROJECT_ROOT / config.gaze.face_landmarker_path
        )

    bus = EventBus()

    # «Воздушная мышь»: общий с взглядом поток камеры, отдельный лендмаркер.
    hand_driver = None
    if config.hand.enabled:
        try:
            hand_driver = HandLandmarkerDriver(
                model_path=config.hand.landmarker_path,
            )
        except Exception as exc:
            log.warning("рука-мышь недоступна, работаем только по взгляду: %s", exc)

    tracker = GazeTracker(config.gaze, bus, hand_driver=hand_driver)
    pointer = PointerService(config.pointer, bus, hand_config=config.hand)

    # Голос: «клик», «двойной», «право», «скролл», «калибровка», «включи/выключи».
    voice = None
    if config.voice.enabled:
        voice = VoiceService(config.voice, bus)
        if not Path(config.voice.model_path).exists():
            log.warning(
                "модель vosk не найдена (%s) — голос отключён. Скачайте из "
                "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip",
                config.voice.model_path,
            )
            voice = None
    if voice is not None:
        bus.subscribe(
            VoiceCommandEvent,
            lambda ev: tracker.request_calibration()
            if ev.action == "calibrate" else None,
        )

    overlay = OverlayApp(
        overlay_config=config.overlay,
        pointer_config=config.pointer,
        bus=bus,
        tracker=tracker,
        pointer=pointer,
    )

    # Фоновые сервисы стартуют до GUI
    tracker.start()
    pointer.start()
    if voice is not None:
        voice.start()

    return overlay


def main() -> int:
    app = build_app()
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())