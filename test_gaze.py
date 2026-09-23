"""Тесты подсистемы взгляда: GazeSample, GazeConfig, трекер, pointer, мост."""

from __future__ import annotations

from jarvis.core.config import GazeConfig, PointerConfig
from jarvis.core.events import (
    EventBus,
    GazeSample,
    PointerStatusEvent,
    RecenteringEvent,
)


# ---------------------------------------------------------------------------
# Событие
# ---------------------------------------------------------------------------
def test_gaze_sample_defaults():
    s = GazeSample()
    assert s.x is None and s.y is None
    assert s.face is False and s.blink is False and s.noise is None
    s2 = GazeSample(x=10.0, y=20.0, face=True, blink=False, noise=5.0)
    assert (s2.x, s2.y, s2.face, s2.noise) == (10.0, 20.0, True, 5.0)


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------
def test_gaze_config_defaults():
    cfg = GazeConfig()
    assert cfg.enabled is True
    assert cfg.camera_index == 0
    assert str(cfg.model_path).endswith("model.pkl")
    assert cfg.face_landmarker_path is None
    assert cfg.calibration == "5p"


def test_gaze_config_from_env(monkeypatch):
    monkeypatch.setenv("GAZE_ENABLED", "false")
    monkeypatch.setenv("GAZE_CAMERA", "2")
    monkeypatch.setenv("GAZE_MODEL_PATH", "custom/model.pkl")
    monkeypatch.setenv("GAZE_FACE_LANDMARKER_PATH", "C:/models/face.task")
    monkeypatch.setenv("GAZE_CALIBRATION", "9p")
    cfg = GazeConfig.from_env()
    assert cfg.enabled is False
    assert cfg.camera_index == 2
    assert str(cfg.model_path).replace("\\", "/") == "custom/model.pkl"
    assert cfg.face_landmarker_path == "C:/models/face.task"
    assert cfg.calibration == "9p"


def test_app_config_has_gaze():
    from jarvis.core.config import AppConfig

    cfg = AppConfig()
    assert isinstance(cfg.gaze, GazeConfig)


# ---------------------------------------------------------------------------
# Пакет jarvis.gaze
# ---------------------------------------------------------------------------
def test_gaze_package_imports():
    from jarvis.gaze import GazeTracker

    bus = EventBus()
    tracker = GazeTracker(GazeConfig(), bus)
    assert tracker._calib_requested is False
    tracker.request_calibration()
    assert tracker._calib_requested is True


def test_gaze_screen_alias():
    from jarvis.core.screen import primary_screen_size as core_fn
    from jarvis.gaze.screen import primary_screen_size as gaze_fn

    w1, h1 = core_fn()
    w2, h2 = gaze_fn()
    assert (w1, h1) == (w2, h2)
    assert w1 > 0 and h1 > 0


# ---------------------------------------------------------------------------
# Мост шины → Qt (восстановленные слоты + gaze_sample)
# ---------------------------------------------------------------------------
def test_bridge_reemits_gaze_sample():
    from jarvis.overlay.bus_adapter import BusToQtBridge

    bus = EventBus()
    bridge = BusToQtBridge(bus)
    got = []
    bridge.gaze_sample.connect(lambda e: got.append(e))
    bus.publish(GazeSample(x=1.0, y=2.0, face=True))
    assert len(got) == 1
    assert got[0].x == 1.0 and got[0].face is True


def test_bridge_reemits_status_and_recentering():
    from jarvis.core.events import TrackerStatusEvent
    from jarvis.overlay.bus_adapter import BusToQtBridge

    bus = EventBus()
    bridge = BusToQtBridge(bus)
    statuses, rec = [], []
    bridge.tracker_status.connect(lambda e: statuses.append(e))
    bridge.recentering.connect(lambda e: rec.append(e))
    bus.publish(TrackerStatusEvent(state="READY", message="ok"))
    bus.publish(RecenteringEvent(active=True))
    assert statuses[0].state == "READY"
    assert rec[0].active is True


# ---------------------------------------------------------------------------
# PointerService: обработка взгляда (без движения настоящей мыши)
# ---------------------------------------------------------------------------
def test_pointer_subscribes_to_gaze_and_respects_enabled():
    bus = EventBus()
    from jarvis.input.pointer import PointerService

    ptr = PointerService(PointerConfig(enabled=False), bus, hand_config=None)
    bus.publish(GazeSample(x=100.0, y=100.0, face=True))
    # выключен → точка не применялась
    assert ptr._last_applied is None


def test_pointer_gaze_lost_keeps_state_within_dropout(monkeypatch):
    """Короткая пропажа взгляда не сбрасывает фильтр; длинная — сбрасывает."""
    import jarvis.input.pointer as pmod

    class FakeTime:
        def __init__(self):
            self.t = 50.0

        def monotonic(self):
            return self.t

    fake = FakeTime()
    monkeypatch.setattr(pmod, "time", fake)

    bus = EventBus()
    from jarvis.input.pointer import PointerService

    ptr = PointerService(PointerConfig(enabled=True), bus, hand_config=None)
    bus.publish(GazeSample(x=100.0, y=100.0, face=True))
    assert ptr._last_applied is not None
    applied = ptr._last_applied

    # пропажа в пределах dropout — состояние живо
    fake.t += PointerConfig().dropout * 0.5
    bus.publish(GazeSample(x=None, y=None, face=False))
    assert ptr._last_applied == applied
    assert ptr._gaze_lost_at is not None

    # пропажа дольше dropout — фильтр сброшен
    fake.t += PointerConfig().dropout + 0.1
    bus.publish(GazeSample(x=None, y=None, face=False))
    assert ptr._last_applied is None


def test_pointer_recenter_computes_offset(monkeypatch, tmp_path):
    """Выравнивание: 1.5 с точек в (100,200) → offset = центр − (100,200)."""
    import jarvis.input.pointer as pmod

    class FakeTime:
        def __init__(self):
            self.t = 10.0

        def monotonic(self):
            return self.t

    fake = FakeTime()
    monkeypatch.setattr(pmod, "time", fake)

    bus = EventBus()
    from jarvis.core.screen import primary_screen_size
    from jarvis.input.pointer import PointerService

    offset_path = tmp_path / "gaze_offset.json"
    ptr = PointerService(
        PointerConfig(offset_path=offset_path), bus, hand_config=None
    )
    statuses, rec = [], []
    bus.subscribe(PointerStatusEvent, statuses.append)
    bus.subscribe(RecenteringEvent, rec.append)

    ptr.start_recenter()
    assert rec and rec[0].active is True

    # сырые точки (100, 200), время плывёт вперёд
    for _ in range(6):
        bus.publish(GazeSample(x=100.0, y=200.0, face=True))
        fake.t += 0.3

    assert rec[-1].active is False
    sw, sh = primary_screen_size()
    assert ptr.offset == (sw / 2.0 - 100.0, sh / 2.0 - 200.0)
    assert any("выровнен" in (s.message or "") for s in statuses)
    assert offset_path.exists()  # смещение сохранено в tmp
