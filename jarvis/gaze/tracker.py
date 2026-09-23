"""GazeTracker — трекер взгляда в общем потоке камеры.

Конвейер кадра:
    VideoCapture → eyetrax GazeEstimator (FaceLandmarker, 478 лендмарков
    + регрессионная модель) → GazeSample(x, y — экран) → шина
    + копия кадра (downscale + mirror) → CameraFrameEvent (превью панели)
    + HandLandmarkerDriver (рука-мышь, если подключён) → HandSample

Калибровка (`request_calibration`) разрывает поток камеры, запускает
полноэкранную процедуру eyetrax (5 или 9 точек), сохраняет обученную
модель в `GazeConfig.model_path` и возвращает камеру в цикл.

Без обученной модельно приложение стартует: статус READY с подсказкой
«нажмите Калибровка», рука/голос работают, взгляд пока не двигает курсор.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from pathlib import Path

import numpy as np

from jarvis.core.config import GazeConfig
from jarvis.core.events import (
    EventBus,
    CameraFrameEvent,
    GazeSample,
    HandSample,
    TrackerStatusEvent,
)
from jarvis.core.service import BaseService

log = logging.getLogger("jarvis.gaze")

_PREVIEW_MAX_WIDTH = 640      # ширина превью (px; веб-панели нужно чётче)
_PREVIEW_INTERVAL = 1.0 / 12  # ~12 FPS превью (GUI не грузим)
_STATUS_INTERVAL = 0.5        # период публикации статуса/fps (с)
_NOISE_WINDOW = 30            # сколько сэмплов для оценки дрожи
_FPS_ALPHA = 0.15             # сглаживание FPS


class GazeTracker(BaseService):
    """Камера → взгляда/рука → шина событий."""

    def __init__(
        self,
        config: GazeConfig,
        bus: EventBus,
        hand_driver=None,  # HandLandmarkerDriver | None
    ) -> None:
        super().__init__(name="gaze")
        self._cfg = config
        self._bus = bus
        self._hand = hand_driver
        self._calib_requested = False
        self._estimator = None          # eyetrax.GazeEstimator
        self._model_ready = False       # обучена ли регрессионная модель
        self._cam = None

    # ------------------------------------------------------------------
    # Публичный API (вызывается из GUI/голоса)
    # ------------------------------------------------------------------
    def request_calibration(self) -> None:
        """Запросить полную калибровку (флаг; подхватит цикл камеры)."""
        self._calib_requested = True
        log.info("калибровка запрошена")

    # ------------------------------------------------------------------
    def _fail(self, message: str) -> None:
        log.error("gaze: %s", message)
        self._bus.publish(TrackerStatusEvent(state="ERROR", message=message))

    def _open_camera(self):
        import cv2

        idx = int(self._cfg.camera_index)
        cap = cv2.VideoCapture(idx)
        if not cap.isOpened() and idx == 0:
            cap.release()
            cap = cv2.VideoCapture(1)  # компат-фоллбэк eyetrax
        if not cap.isOpened():
            cap.release()
            return None
        return cap

    def _face_model_arg(self) -> str | None:
        # None → eyetrax сам возьмёт ~/.cache/eyetrax/mediapipe/face_landmarker.task
        return self._cfg.face_landmarker_path or None

    def _loop(self) -> None:
        if not self._cfg.enabled:
            log.info("gaze: выключен конфигом")
            self._bus.publish(TrackerStatusEvent(
                state="STOPPED", message="Трекер взгляда отключён (GAZE_ENABLED=false)"
            ))
            while not self._stop_event.wait(timeout=1.0):
                pass
            return

        self._bus.publish(TrackerStatusEvent(state="STARTING", message="Запуск…"))

        # --- eyetrax: FaceLandmarker + регрессионная модель ---
        try:
            from eyetrax.gaze import GazeEstimator
        except Exception as exc:
            self._fail(f"eyetrax недоступен: {exc}")
            while not self._stop_event.wait(timeout=1.0):
                pass
            return
        try:
            self._estimator = GazeEstimator(face_landmarker_model=self._face_model_arg())
        except Exception as exc:
            self._fail(f"FaceLandmarker не загрузился: {exc}")
            while not self._stop_event.wait(timeout=1.0):
                pass
            return

        # --- обученная модель (pickle) ---
        model_path: Path = Path(self._cfg.model_path)
        if model_path.exists():
            try:
                self._estimator.load_model(model_path)
                self._model_ready = hasattr(self._estimator.model.scaler, "mean_")
                log.info("gaze: модель загружена из %s", model_path)
            except Exception as exc:
                log.warning("gaze: не удалось загрузить %s: %s", model_path, exc)
                self._model_ready = False
        message = (
            "Следу за взглядом"
            if self._model_ready
            else "Модель не обучена — нажмите «Калибровка»"
        )

        # --- камера (общий с рукой поток) ---
        cap = self._open_camera()
        if cap is None:
            self._fail(
                "камера не найдена (проверьте индекс GAZE_CAMERA / доступ к устройству)"
            )
            self._estimator.close()
            while not self._stop_event.wait(timeout=1.0):
                pass
            return
        self._cam = cap

        self._bus.publish(TrackerStatusEvent(state="READY", message=message))
        log.info("gaze: камера открыта (модель=%s)", "да" if self._model_ready else "нет")

        import cv2

        fps = 0.0
        prev_t = time.perf_counter()
        last_preview = 0.0
        last_status = 0.0
        noise_buf: deque[tuple[float, float]] = deque(maxlen=_NOISE_WINDOW)
        state = "TRACKING" if self._model_ready else "READY"
        last_message = message

        try:
            while not self._stop_event.is_set():
                # Калибровка? Разрываем камеру, полный экран eyetrax, потом обратно.
                if self._calib_requested:
                    self._calib_requested = False
                    cap = self._calibrate(cap)
                    if cap is None:
                        self._fail("камера не вернулась после калибровки")
                        break
                    self._cam = cap
                    state = "TRACKING" if self._model_ready else "READY"
                    last_message = (
                        "Калибровка сохранена — слежу за взглядом"
                        if self._model_ready
                        else "Калибровка отменена"
                    )
                    self._bus.publish(TrackerStatusEvent(state=state, message=last_message))
                    prev_t = time.perf_counter()
                    continue

                ok, frame = cap.read()
                if not ok or frame is None:
                    time.sleep(0.05)
                    continue

                now = time.perf_counter()
                dt = now - prev_t
                prev_t = now
                if dt > 1e-4:
                    inst = 1.0 / dt
                    fps = inst if fps == 0.0 else fps + (inst - fps) * _FPS_ALPHA

                # ---- взгляд ----
                face = False
                blink = False
                x = y = None
                noise = None
                try:
                    feats, blink = self._estimator.extract_features(frame)
                except Exception:
                    feats, blink = None, False
                face = feats is not None
                if face and not blink and self._model_ready:
                    try:
                        pt = self._estimator.predict(np.asarray([feats]))[0]
                        x, y = float(pt[0]), float(pt[1])
                        noise_buf.append((x, y))
                        if len(noise_buf) >= 15:
                            xs = [p[0] for p in noise_buf]
                            ys = [p[1] for p in noise_buf]
                            noise = float(np.hypot(np.std(xs), np.std(ys)))
                    except Exception as exc:
                        log.debug("gaze: predict failed: %s", exc)
                if not face:
                    noise_buf.clear()
                self._bus.publish(GazeSample(
                    x=x, y=y, face=face, blink=blink, noise=noise
                ))

                # ---- рука (общий кадр) ----
                if self._hand is not None:
                    try:
                        got = self._hand.extract(frame)
                    except Exception as exc:
                        log.debug("hand: extract failed: %s", exc)
                        got = None
                    if got is None:
                        self._bus.publish(HandSample(
                            x=None, y=None, pinch=None, confidence=0.0, hand_count=0
                        ))
                    else:
                        nx, ny, pinch, landmarks = got
                        self._bus.publish(HandSample(
                            x=nx, y=ny, pinch=pinch, confidence=1.0, hand_count=1,
                            landmarks=landmarks,
                        ))

                # ---- превью для мини-панели (downscale + mirror) ----
                if now - last_preview >= _PREVIEW_INTERVAL:
                    last_preview = now
                    h, w = frame.shape[:2]
                    scale = _PREVIEW_MAX_WIDTH / max(w, 1)
                    small = cv2.resize(
                        frame, (_PREVIEW_MAX_WIDTH, max(1, int(h * scale)))
                    )
                    small = np.ascontiguousarray(small[:, ::-1])  # BGR, зеркально
                    self._bus.publish(CameraFrameEvent(
                        frame=small,
                        width=small.shape[1],
                        height=small.shape[0],
                        fps=fps,
                    ))

                # ---- статус + fps ----
                if now - last_status >= _STATUS_INTERVAL:
                    last_status = now
                    state = "TRACKING" if self._model_ready else "READY"
                    self._bus.publish(TrackerStatusEvent(
                        state=state, message=last_message, fps=fps
                    ))
        finally:
            try:
                cap.release()
            except Exception:
                pass
            self._cam = None
            if self._estimator is not None:
                try:
                    self._estimator.close()
                except Exception:
                    pass
            self._bus.publish(TrackerStatusEvent(state="STOPPED", message="Остановлен"))
            log.info("gaze: остановлен")

    # ------------------------------------------------------------------
    def _calibrate(self, cap):
        """Полноэкранная калибровка eyetrax. Возвращает новую камеру или None."""
        import cv2
        from eyetrax.calibration import run_5_point_calibration, run_9_point_calibration
        from eyetrax.gaze import GazeEstimator

        log.info("gaze: старт калибровки (%s)", self._cfg.calibration)
        self._bus.publish(TrackerStatusEvent(
            state="CALIBRATING",
            message="Смотрите на зелёные точки… (Esc — отмена)",
        ))
        try:
            cap.release()
        except Exception:
            pass
        try:
            cv2.destroyAllWindows()
        except Exception:
            pass

        est = None
        trained = False
        try:
            # свежий estimator: чистая история морганий + свой FaceLandmarker
            est = GazeEstimator(face_landmarker_model=self._face_model_arg())
            if str(self._cfg.calibration).lower() == "9p":
                run_9_point_calibration(est, camera_index=int(self._cfg.camera_index))
            else:
                run_5_point_calibration(est, camera_index=int(self._cfg.camera_index))
            trained = hasattr(est.model.scaler, "mean_")
            if trained:
                model_path = Path(self._cfg.model_path)
                model_path.parent.mkdir(parents=True, exist_ok=True)
                est.save_model(model_path)
                log.info("gaze: модель обучена и сохранена в %s", model_path)
                # подменяем estimator (старый закрываем)
                if self._estimator is not None:
                    try:
                        self._estimator.close()
                    except Exception:
                        pass
                self._estimator = est
                self._model_ready = True
                est = None  # не закрывать в finally — он теперь основной
            else:
                log.info("gaze: калибровка прервана/без данных")
        except Exception as exc:
            log.exception("gaze: калибровка упала: %s", exc)
            self._bus.publish(TrackerStatusEvent(
                state="READY", message=f"Калибровка не удалась: {exc}"
            ))
        finally:
            if est is not None:
                try:
                    est.close()
                except Exception:
                    pass

        try:
            cv2.destroyAllWindows()
        except Exception:
            pass
        return self._open_camera()
