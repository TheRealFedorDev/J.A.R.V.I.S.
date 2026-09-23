"""Подсистема трекера взгляда: камера → eyetrax → экранные координаты.

Компоненты:
- `tracker` — `GazeTracker`: общий с рукой поток камеры, FaceLandmarker,
  обучение модели (5-/9-точечная калибровка eyetrax), публикация
  `GazeSample` / `CameraFrameEvent` / `TrackerStatusEvent` / `HandSample`;
- `screen` — геометрия основного монитора (совместимый алиас core.screen).
"""

from jarvis.gaze.tracker import GazeTracker

__all__ = ["GazeTracker"]
