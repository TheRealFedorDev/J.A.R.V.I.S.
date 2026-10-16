import asyncio
import json
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse
from pydantic import BaseModel


def _get_phone_key() -> str:
    key = os.environ.get("PHONE_API_KEY") or ""
    if not key:
        raise HTTPException(status_code=503, detail="phone_api_not_configured")
    return key


async def require_phone_key(
    x_phone_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
) -> None:
    expected = _get_phone_key()
    provided = x_phone_key or (authorization.split(" ", 1)[1] if authorization and authorization.startswith("Bearer ") else None)
    if not provided or provided != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


router = APIRouter(dependencies=[Depends(require_phone_key)])


class TaskIn(BaseModel):
    text: str


class CmdIn(BaseModel):
    text: str


class BoolIn(BaseModel):
    enabled: bool


class TelemetryIn(BaseModel):
    battery_pct: Optional[float] = None
    is_charging: Optional[bool] = None
    wifi_ssid: Optional[str] = None
    wifi_rssi: Optional[int] = None
    network_type: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    ts: Optional[float] = None


@router.get("/health")
async def health() -> dict[str, Any]:
    import time

    return {"ok": True, "ts": time.time()}


@router.get("/state")
async def state() -> dict[str, Any]:
    from fastapi import Request

    # will be wired from server; placeholder for now
    return {"ok": True, "phone_api": True}


@router.get("/tasks")
async def list_tasks() -> dict[str, Any]:
    return {"tasks": []}


@router.post("/tasks")
async def add_task(body: TaskIn) -> dict[str, Any]:
    return {"ok": True, "task": {"id": 1, "text": body.text}}


@router.post("/cmd")
async def cmd(body: CmdIn) -> dict[str, Any]:
    return {"matched": False, "answer": "cmd stub"}


@router.post("/voice")
async def voice(file: UploadFile = File(...)) -> dict[str, Any]:
    _ = await file.read()
    return {"transcript": "", "matched": False, "answer": ""}


@router.post("/telemetry")
async def telemetry(body: TelemetryIn) -> dict[str, Any]:
    return {"ok": True}


@router.patch("/tasks/{task_id}")
async def patch_task(task_id: int, body: TaskIn) -> dict[str, Any]:
    return {"ok": True, "task": {"id": task_id, "text": body.text}}


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: int) -> dict[str, Any]:
    return {"ok": True}


@router.post("/mic")
async def mic(body: BoolIn) -> dict[str, Any]:
    return {"ok": True, "muted": not body.enabled}


@router.post("/tts")
async def tts(body: BoolIn) -> dict[str, Any]:
    return {"ok": True, "enabled": body.enabled}
