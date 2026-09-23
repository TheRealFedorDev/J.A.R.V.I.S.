"""Simple OpenCV-based visualization for J.A.R.V.I.S.

Draws the gaze cursor on screen and allows interaction.
"""

from __future__ import annotations

import logging

import cv2

from jarvis.input.pointer import PointerService
from jarvis.visualize_anim import BootAnimation, blend

log = logging.getLogger("jarvis.visualize")

WINDOW_NAME = "J.A.R.V.I.S - Hand Tracker"
