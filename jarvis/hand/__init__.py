"""Подсистема «воздушная мышь»: рука в кадре → точный курсор.

В отличие от взгляда (абсолютный маппинг, потолок ~150px из-за шума камеры),
рука даёт **относительный** контроль с точностью в несколько px на удалении
и без утомления — рука опирается на локоть, двигаются только пальцы.

Компоненты:
- `features` — чистая математика (кончик пальца, щипок);
- `driver`   — MediaPipe HandLandmarker (общая с взглядом камера);
- `pointer`  — состояние «палец→курсор + щипок→кнопка».
"""

from jarvis.hand.driver import HandLandmarkerDriver, resolve_hand_landmarker_task
from jarvis.hand.pointer import HandCommand, HandPointerState

__all__ = [
    "HandCommand",
    "HandLandmarkerDriver",
    "HandPointerState",
    "resolve_hand_landmarker_task",
]