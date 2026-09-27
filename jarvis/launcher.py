"""Лаунчер J.A.R.V.I.S / Berta.

Собирает приложение из частей:
конфиг → шина событий → сервисы (трекер, курсор, голос, TTS)
     → web-панель (по умолчанию) или PyQt-оверлей (--overlay).
"""

from __future__ import annotations

import sys
from pathlib import Path

import logging

from jarvis.core.config import AppConfig
from jarvis.core.events import EventBus, VoiceCommandEvent
from jarvis.core.logging import setup_logging
from jarvis.gaze.tracker import GazeTracker
from jarvis.hand.driver import HandLandmarkerDriver
from jarvis.input.pointer import PointerService
from jarvis.voice.service import VoiceService

log = logging.getLogger("jarvis.launcher")
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def build_core():
    """Конфиг + шина + сервисы (без GUI). Общая сборка web и overlay."""
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
        asr = (config.voice.asr or "faster-whisper").lower()
        if asr == "vosk" and not Path(config.voice.model_path).exists():
            log.warning(
                "модель vosk не найдена (%s) — голос отключён. Скачайте из "
                "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip",
                config.voice.model_path,
            )
        else:
            agent = (config.voice.agent or "legacy").lower()
            if agent == "livekit":
                try:
                    from jarvis.voice.livekit_service import LiveKitVoiceService

                    voice = LiveKitVoiceService(config.voice, bus)
                    log.info("голос: LiveKit-агент (STT→LLM→TTS, barge-in)")
                except Exception as exc:
                    log.warning(
                        "LiveKit-агент недоступен (%s) — fallback на VoiceService", exc
                    )
                    voice = VoiceService(config.voice, bus)
            elif agent == "pipecat":
                try:
                    from jarvis.voice.pipecat_agent import PipecatVoiceService

                    voice = PipecatVoiceService(config.voice, bus)
                    log.info("голос: pipecat-агент (STT→LLM→TTS)")
                except Exception as exc:
                    log.warning(
                        "pipecat недоступен (%s) — fallback на VoiceService", exc
                    )
                    voice = VoiceService(config.voice, bus)
            else:
                voice = VoiceService(config.voice, bus)
    if voice is not None:
        bus.subscribe(
            VoiceCommandEvent,
            lambda ev: tracker.request_calibration()
            if ev.action == "calibrate" else None,
        )

    # TTS (женские нейроголоса, стрим) — нужен web-роутеру и озвучке ответов
    tts = None
    try:
        from jarvis.tts import TextToSpeech, TtsConfig

        tts = TextToSpeech(TtsConfig.from_env())
    except Exception as exc:
        log.warning("TTS недоступен: %s", exc)

    return config, bus, tracker, pointer, voice, tts


def start_core(tracker, pointer, voice) -> None:
    tracker.start()
    pointer.start()
    if voice is not None:
        voice.start()


def stop_core(tracker, pointer, voice, tts) -> None:
    for svc in (voice, pointer, tracker):
        if svc is not None:
            try:
                svc.stop()
            except Exception:
                log.exception("оставка сервиса %r", svc)
    if tts is not None:
        try:
            tts.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# PyQt-оверлей (классический режим, --overlay)
# ---------------------------------------------------------------------------
def build_app():
    """Сборка всех подсистем и Qt-оверлея."""
    from jarvis.overlay.app import OverlayApp

    config, bus, tracker, pointer, voice, tts = build_core()

    overlay = OverlayApp(
        overlay_config=config.overlay,
        pointer_config=config.pointer,
        bus=bus,
        tracker=tracker,
        pointer=pointer,
    )

    # Голос тоже надо гасить при выходе из GUI (трекер/курсор гасит OverlayApp)
    if voice is not None:
        overlay._qapp.aboutToQuit.connect(voice.stop)

    start_core(tracker, pointer, voice)

    return overlay


# ---------------------------------------------------------------------------
# Web-панель Berta (по умолчанию — «запускается через сайт»)
# ---------------------------------------------------------------------------
def run_web(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> int:
    """Стартует сервисы + uvicorn с web-панелью. Блокирует до остановки."""
    import threading
    import webbrowser

    import uvicorn

    from jarvis.web import AppState, create_app

    config, bus, tracker, pointer, voice, tts = build_core()
    state = AppState(
        config, bus,
        tracker=tracker, pointer=pointer, voice=voice, tts=tts,
        project_root=PROJECT_ROOT,
    )
    app = create_app(state)

    start_core(tracker, pointer, voice)
    log.info("Berta web-панель: http://%s:%d", host, port)

    if open_browser:
        def _open() -> None:
            import time as _t

            _t.sleep(1.2)
            try:
                webbrowser.open(f"http://{host}:{port}")
            except Exception:
                pass

        threading.Thread(target=_open, name="open-browser", daemon=True).start()

    config_uv = uvicorn.Config(
        app, host=host, port=port, log_level="warning", access_log=False,
    )
    server = uvicorn.Server(config_uv)
    try:
        server.run()
    finally:
        stop_core(tracker, pointer, voice, tts)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--overlay" in argv or "--qt" in argv:
        overlay = build_app()
        return overlay.run()

    host = "127.0.0.1"
    port = 8765
    open_browser = "--no-browser" not in argv
    for arg in argv:
        if arg.startswith("--port="):
            try:
                port = int(arg.split("=", 1)[1])
            except ValueError:
                pass
        elif arg.startswith("--host="):
            host = arg.split("=", 1)[1]
    if "--web" in argv or "--no-browser" in argv:
        pass  # уже web-режим
    return run_web(host=host, port=port, open_browser=open_browser)


if __name__ == "__main__":
    raise SystemExit(main())
