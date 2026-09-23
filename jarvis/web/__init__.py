"""Web-приложение Berta: локальный сайт-панель управления (ТЗ §6 визуал)."""

from __future__ import annotations

from jarvis.web.router import CommandRouter, RouteResult
from jarvis.web.server import create_app
from jarvis.web.state import AppState, AuditLog, PROFILES, Broadcaster, Profile

__all__ = [
    "AppState",
    "AuditLog",
    "Broadcaster",
    "CommandRouter",
    "PROFILES",
    "Profile",
    "RouteResult",
    "create_app",
]
