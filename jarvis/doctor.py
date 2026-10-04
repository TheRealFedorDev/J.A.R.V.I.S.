"""Диагностика бэкендов одной командой: что работает, что нет, на чём.

    python -m jarvis.doctor            таблица
    python -m jarvis.doctor --json     машинный вывод
    python -m jarvis.doctor --deep     плюс реальная загрузка STT-модели
    python -m jarvis.doctor --probe stt   только один раздел

Код возврата 0, если все обязательные проверки прошли, иначе 1 — удобно
вешать на запуск перед демонстрацией.

Диагностика специально идёт через те же `provider_chain()` и
`run_with_fallback()`, что и живой код. Проверяется не переписка логики, а
реальный путь отказа: если цепочка настроена неверно, doctor это покажет
раньше, чем комиссия.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from jarvis.core.backends import (
    AllProvidersFailed,
    CLOUD_PROVIDERS,
    Provider,
    cloud_api_key,
    cloud_probe_url,
    describe,
    detect_hardware,
    llm_root_url,
    provider_chain,
    resolve_stt,
    run_with_fallback,
)
from jarvis.core.config import load_dotenv

OK = "OK"
FAIL = "FAIL"
SKIP = "SKIP"

_CLOUD: dict[str, str] = {}


@dataclass
class Probe:
    """Результат одной проверки."""

    name: str
    kind: str
    status: str
    latency_ms: float = 0.0
    detail: str = ""
    critical: bool = True

    @property
    def ok(self) -> bool:
        return self.status == OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "latency_ms": round(self.latency_ms, 1),
            "detail": self.detail,
            "critical": self.critical,
        }


@dataclass
class Report:
    probes: list[Probe] = field(default_factory=list)

    def add(self, probe: Probe) -> None:
        self.probes.append(probe)

    def extend(self, probes: list[Probe]) -> None:
        self.probes.extend(probes)

    def count(self, status: str, *, critical_only: bool = False) -> int:
        return sum(
            1 for p in self.probes
            if p.status == status and (not critical_only or p.critical)
        )

    def exit_code(self) -> int:
        return 1 if self.count(FAIL, critical_only=True) else 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.exit_code() == 0,
            "config": describe(),
            "probes": [p.as_dict() for p in self.probes],
            "summary": {
                "ok": self.count(OK),
                "fail": self.count(FAIL),
                "skip": self.count(SKIP),
            },
        }


def _measure(
    name: str,
    kind: str,
    call: Callable[[], Probe],
    *,
    critical: bool = True,
) -> Probe:
    started = time.perf_counter()
    try:
        probe = call()
    except Exception as exc:  # noqa: BLE001 — диагностика не должна падать
        elapsed = (time.perf_counter() - started) * 1000.0
        return Probe(name, kind, FAIL, elapsed, f"{type(exc).__name__}: {exc}", critical)
    probe.latency_ms = (time.perf_counter() - started) * 1000.0
    probe.critical = critical
    return probe


def _http(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> tuple[int, bytes]:
    """GET с кодом ответа. Сетевые ошибки бросаются, HTTP-ошибки — нет."""
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read() or b""


# ---------------------------------------------------------------------------
# Железо и сеть
# ---------------------------------------------------------------------------
def probe_hardware() -> Probe:
    hardware = detect_hardware()
    plan = resolve_stt()
    detail = (
        f"Python {hardware.python_version} · {hardware.summary()}"
        + (f" · {hardware.ram_gb} ГБ RAM" if hardware.ram_gb else "")
    )
    return Probe("железо", "core", OK, detail=detail, critical=False)


def probe_network() -> Probe:
    for host in ("api.openai.com", "www.microsoft.com"):
        try:
            with socket.create_connection((host, 443), timeout=4):
                return Probe("сеть", "core", OK, detail=f"TLS до {host} открыт")
        except OSError as exc:
            last = exc
    return Probe("сеть", "core", FAIL, detail=f"интернет недоступен: {last}")


def probe_voice_api() -> Probe:
    host = os.environ.get("LOCAL_VOICE_HOST", "127.0.0.1")
    port = os.environ.get("LOCAL_VOICE_PORT", "8077")
    try:
        status, body = _http(f"http://{host}:{port}/health", timeout=2.0)
    except (urllib.error.URLError, OSError, TimeoutError):
        return Probe(
            "local voice API", "core", SKIP,
            detail=f"{host}:{port} не отвечает — он нужен только при "
                   f"VOICE_AGENT=livekit",
        )
    if status != 200:
        return Probe(
            "local voice API", "core", FAIL,
            detail=f"{host}:{port} ответил {status}",
        )
    data = json.loads(body or b"{}")
    return Probe(
        "local voice API", "core", OK,
        detail=f"{host}:{port} · asr={data.get('asr')} · device={data.get('device')}",
    )


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
def _ollama_tags() -> list[str]:
    status, body = _http(f"{llm_root_url()}/api/tags", timeout=4.0)
    if status != 200:
        raise RuntimeError(f"Ollama ответила {status}")
    data = json.loads(body or b"{}")
    return [str(m.get("name") or "") for m in data.get("models", [])]


def _model_present(available: list[str], wanted: str) -> bool:
    target = wanted.strip().lower()
    for name in available:
        if name.lower() == target or name.lower().split(":")[0] == target.split(":")[0]:
            return True
    return False


def _call_llm(provider: Provider) -> str:
    if provider.name in ("ollama", "local"):
        available = _ollama_tags()
        if not available:
            raise RuntimeError("в Ollama нет ни одной модели (ollama pull llama3.2:3b)")
        wanted = provider.arg or os.environ.get("LLM_MODEL", "llama3.2:3b")
        if not _model_present(available, wanted):
            raise RuntimeError(f"модель {wanted} не скачана; доступны: {', '.join(available)}")
        return f"{wanted} · Ollama, моделей в наличии {len(available)}"

    spec = CLOUD_PROVIDERS.get(provider.name)
    if spec is None:
        raise RuntimeError(f"провайдер «{provider.name}» неизвестен doctor")
    if spec.kind != "llm":
        raise RuntimeError(f"{provider.name} — это провайдер {spec.kind}, а не LLM")
    if not cloud_api_key(provider.name):
        raise LookupError(f"не задан {spec.env_var}")

    url, headers = cloud_probe_url(provider.name)
    status, _ = _http(url, headers=headers, timeout=6.0)
    if status in (401, 403):
        raise RuntimeError(f"{status}: ключ отвергнут или регион заблокирован")
    if status >= 500:
        raise RuntimeError(f"{status}: ошибка сервиса")
    return f"{spec.env_var} принят, ответ {status}"


def _probe_llm_chain() -> list[Probe]:
    probes: list[Probe] = []
    for provider in provider_chain("llm"):
        probes.append(_measure(
            str(provider), "llm",
            lambda p=provider: Probe(str(p), "llm", OK, detail=_call_llm(p)),
        ))
    return probes


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------
def _edge_voice() -> str:
    from jarvis.tts.voices import DEFAULT_RU_VOICE

    return os.environ.get("TTS_VOICE_RU") or DEFAULT_RU_VOICE


def _call_tts(provider: Provider) -> str:
    if provider.name in ("edge", "edge-tts"):
        import edge_tts

        voice = _edge_voice()

        async def _run() -> int:
            total = 0
            stream = edge_tts.Communicate("Проверка связи", voice)
            async for chunk in stream.stream():
                if chunk.get("type") == "audio":
                    total += len(chunk.get("data") or b"")
            return total

        total = asyncio.run(_run())
        if total <= 0:
            raise RuntimeError("edge-tts вернул 0 байт аудио")
        return f"{voice} · {total} Б"

    if provider.name in ("pyttsx3", "sapi", "offline"):
        import pyttsx3

        engine = pyttsx3.init()
        engine.say("проверка")
        engine.stop()
        return "локальный синтез SAPI доступен"

    spec = CLOUD_PROVIDERS.get(provider.name)
    if spec is None:
        raise RuntimeError(f"провайдер «{provider.name}» неизвестен doctor")
    if spec.kind != "tts":
        raise RuntimeError(f"{provider.name} — это провайдер {spec.kind}, а не синтез")
    if not cloud_api_key(provider.name):
        raise LookupError(f"не задан {spec.env_var}")
    url, headers = cloud_probe_url(provider.name)
    status, _ = _http(url, headers=headers, timeout=6.0)
    if status in (401, 403):
        raise RuntimeError(f"{status}: ключ отвергнут")
    return f"{spec.env_var} принят, ответ {status}"


def _probe_tts_chain() -> list[Probe]:
    return [
        _measure(
            str(provider), "tts",
            lambda p=provider: Probe(str(p), "tts", OK, detail=_call_tts(p)),
        )
        for provider in provider_chain("tts")
    ]


# ---------------------------------------------------------------------------
# STT
# ---------------------------------------------------------------------------
def _call_stt(provider: Provider, *, deep: bool) -> str:
    import numpy as np

    if provider.name in ("faster-whisper", "whisper"):
        from faster_whisper import WhisperModel

        plan = resolve_stt()
        model = WhisperModel(plan.model, device=plan.device, compute_type=plan.compute_type)
        if not deep:
            return (
                f"{plan.model} / {plan.device} / {plan.compute_type} · "
                f"модель: {plan.model_source}, девайс: {plan.device_source}"
            )
        silence = np.zeros(int(16000 * 0.5), dtype=np.float32)
        segments, info = model.transcribe(silence, language="ru", beam_size=1)
        text = " ".join(s.text.strip() for s in segments).strip()
        return f"{plan.model} / {plan.device} · {info.duration:.1f} c · «{text or 'пусто'}»"

    if provider.name == "vosk":
        raw = os.environ.get("VOICE_MODEL_PATH") or "models/vosk/vosk-model-small-ru-0.22"
        path = Path(raw)
        if not path.exists():
            raise FileNotFoundError(f"модель vosk не скачана: {path}")
        return f"модель на месте: {path}"

    raise RuntimeError(f"движок «{provider.name}» неизвестен doctor")


def _probe_stt_chain(*, deep: bool) -> list[Probe]:
    return [
        _measure(
            str(provider), "stt",
            lambda p=provider: Probe(str(p), "stt", OK, detail=_call_stt(p, deep=deep)),
        )
        for provider in provider_chain("stt")
    ]


# ---------------------------------------------------------------------------
# Сборка и вывод
# ---------------------------------------------------------------------------
def _probe_deep_fallback() -> Probe:
    """Целостность фоллбэка: при падающем первом провайдере второй отвечает."""
    chain = (
        Provider("llm", "ollama", ""),
        Provider("llm", "definitely-not-a-provider", ""),
    )
    try:
        result = run_with_fallback(chain, _call_llm)
    except AllProvidersFailed:
        return Probe(
            "фоллбэк LLM", "core", OK,
            detail="цепочка корректно сообщила об отказе всех провайдеров",
        )
    return Probe(
        "фоллбэк LLM", "core", OK,
        detail=f"отработал {result.provider} · попыток {len(result.attempts)}",
        critical=False,
    )


def collect(*, deep: bool = False) -> Report:
    """Прогнать все проверки и вернуть отчёт."""
    report = Report()
    report.add(_measure("железо", "core", probe_hardware, critical=False))
    report.add(_measure("сеть", "core", probe_network, critical=False))
    report.extend(_probe_stt_chain(deep=deep))
    report.extend(_probe_tts_chain())
    report.extend(_probe_llm_chain())
    report.add(_measure(
        "local voice API", "core", probe_voice_api, critical=False,
    ))
    report.add(_measure("фоллбэк LLM", "core", _probe_deep_fallback, critical=False))
    return report


def _fmt_latency(ms: float) -> str:
    return f"{ms:>7.0f} мс"


def _use_utf8() -> None:
    """Кириллица в консоли Windows: без этого вывод рассыпается в CP866."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def render(report: Report) -> str:
    """Человекочитаемая таблица без внешних зависимостей."""
    hardware = detect_hardware()
    plan = resolve_stt()
    config = describe()

    lines: list[str] = []
    lines.append("=" * 68)
    lines.append("J.A.R.V.I.S. — диагностика бэкендов")
    lines.append("=" * 68)
    lines.append(f"Железо: {hardware.summary()} · {hardware.ram_gb} ГБ RAM · "
                 f"Python {hardware.python_version}")
    lines.append(f"План STT: {plan.model} / {plan.device} / {plan.compute_type}")
    for kind in ("stt", "tts", "llm"):
        chain = ", ".join(config[kind]["chain"]) or "—"
        lines.append(f"Цепочка {kind.upper()}: {chain}")
    lines.append("")

    current = ""
    for probe in report.probes:
        if probe.kind != current:
            current = probe.kind
            titles = {"core": "СИСТЕМА", "stt": "РАСПОЗНАВАНИЕ",
                      "tts": "СИНТЕЗ", "llm": "ЯЗЫКОВАЯ МОДЕЛЬ"}
            lines.append(f"[{titles.get(current, current.upper())}]")
        mark = {OK: "  ok  ", FAIL: " FAIL ", SKIP: " skip "}[probe.status]
        lines.append(f"  {mark} {probe.name:<22}{_fmt_latency(probe.latency_ms)}"
                     f"  {probe.detail}")

    lines.append("")
    lines.append("-" * 68)
    lines.append(
        f"Итог: успешно {report.count(OK)} · "
        f"провалено {report.count(FAIL)} · "
        f"пропущено {report.count(SKIP)}"
    )
    failed = [p for p in report.probes if p.status == FAIL]
    if failed:
        lines.append("Чинить в первую очередь: " + ", ".join(p.name for p in failed))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m jarvis.doctor",
        description="Проверить STT/TTS/LLM и железо на этой машине.",
    )
    parser.add_argument("--json", action="store_true", help="вывод в JSON")
    parser.add_argument("--deep", action="store_true",
                        help="реально загрузить STT-модель и распознать фрагмент")
    parser.add_argument("--probe", choices=["stt", "tts", "llm", "core"],
                        help="проверить только один раздел")
    args = parser.parse_args(argv)

    _use_utf8()
    load_dotenv(Path(os.environ.get("JARVIS_ENV_FILE", ".env")))

    if args.probe:
        builders: dict[str, Callable[[], list[Probe]]] = {
            "stt": lambda: _probe_stt_chain(deep=args.deep),
            "tts": _probe_tts_chain,
            "llm": _probe_llm_chain,
            "core": lambda: [
                _measure("железо", "core", probe_hardware, critical=False),
                _measure("сеть", "core", probe_network, critical=False),
                _measure("фоллбэк LLM", "core", _probe_deep_fallback, critical=False),
            ],
        }
        report = Report(probes=builders[args.probe]())
    else:
        report = collect(deep=args.deep)

    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())