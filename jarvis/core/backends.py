"""Единая конфигурация бэкендов STT/TTS/LLM и подбор параметров под железо.

Раньше каждая подсистема читала `os.environ` самостоятельно, а имена моделей
были захардкожены в пяти местах. Из-за этого переезд на другой ноутбук — в
лабораторию с RTX 4060 — требовал правки кода, а не конфига.

Модуль даёт четыре вещи:

    detect_hardware()     что реально есть на машине
    resolve_stt()         модель / девайс / compute_type под это железо
    provider_chain()      упорядоченный список провайдеров из .env
    run_with_fallback()   первый доступный провайдер + отчёт, кто ответил

Диагностика всего сразу: `python -m jarvis.doctor`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "AllProvidersFailed",
    "Attempt",
    "CLOUD_PROVIDERS",
    "CloudProvider",
    "FallbackResult",
    "Hardware",
    "Provider",
    "SttPlan",
    "cloud_api_key",
    "cloud_auth_headers",
    "cloud_probe_url",
    "describe",
    "detect_hardware",
    "llm_root_url",
    "provider_chain",
    "resolve_stt",
    "run_with_fallback",
]


# ---------------------------------------------------------------------------
# Железо
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Hardware:
    """Сводка по машине. Пустые поля означают «не смогли определить»."""

    cuda_devices: int = 0
    gpu_name: str = ""
    vram_mb: int = 0
    cpu_count: int = 1
    ram_gb: float = 0.0
    python_version: str = ""

    @property
    def has_cuda(self) -> bool:
        return self.cuda_devices > 0

    @property
    def tier(self) -> str:
        """cpu | gpu-tiny | gpu-small | gpu-large — по VRAM, а не по названию."""
        if not self.has_cuda:
            return "cpu"
        if self.vram_mb >= 7000:
            return "gpu-large"
        if self.vram_mb >= 4000:
            return "gpu-small"
        return "gpu-tiny"

    def summary(self) -> str:
        if self.has_cuda:
            gpu = self.gpu_name or "CUDA"
            if self.vram_mb:
                gpu += f" {self.vram_mb // 1024}ГБ"
            return f"{gpu} / CPU {self.cpu_count} ядер"
        return f"CPU {self.cpu_count} ядер (без CUDA)"


def _cuda_device_count() -> int:
    """Число видимых ctranslate2 CUDA-устройств.

    Именно ctranslate2, а не nvidia-smi: faster-whisper считает через него, и
    установленный, но не найденный им CUDA — это ровно тот случай, где
    nvidia-smi врёт.
    """
    try:
        import ctranslate2

        return int(ctranslate2.get_cuda_device_count())
    except Exception:
        return 0


def _nvidia_smi() -> tuple[str, int]:
    """Имя GPU и VRAM в МБ через nvidia-smi. Пусто, если утилиты нет."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return "", 0
    try:
        proc = subprocess.run(  # noqa: S603
            [exe, "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "", 0
    if proc.returncode != 0 or not proc.stdout.strip():
        return "", 0
    name, _, memory = proc.stdout.strip().splitlines()[0].partition(",")
    try:
        vram = int(memory.strip())
    except ValueError:
        vram = 0
    return name.strip(), vram


def _ram_gb() -> float:
    """Физическая память в ГБ. На Windows — напрямую из kernel32."""
    try:
        import ctypes

        class _MemStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemStatus()
        status.dwLength = ctypes.sizeof(_MemStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return round(status.ullTotalPhys / 1024**3, 1)
    except Exception:
        pass
    return 0.0


_hardware_cache: Hardware | None = None


def detect_hardware(*, refresh: bool = False) -> Hardware:
    """Определить железо. Результат кэшируется: nvidia-smi не бесплатен."""
    global _hardware_cache
    if _hardware_cache is not None and not refresh:
        return _hardware_cache

    devices = _cuda_device_count()
    name, vram = _nvidia_smi() if devices else ("", 0)
    import sys

    _hardware_cache = Hardware(
        cuda_devices=devices,
        gpu_name=name,
        vram_mb=vram,
        cpu_count=os.cpu_count() or 1,
        ram_gb=_ram_gb(),
        python_version=".".join(str(p) for p in sys.version_info[:3]),
    )
    return _hardware_cache


# ---------------------------------------------------------------------------
# План STT
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SttPlan:
    """Эффективные параметры faster-whisper и откуда они взялись."""

    model: str
    device: str
    compute_type: str
    model_source: str = "env"
    device_source: str = "env"
    compute_source: str = "env"
    note: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "model": self.model,
            "device": self.device,
            "compute_type": self.compute_type,
            "model_source": self.model_source,
            "device_source": self.device_source,
            "compute_source": self.compute_source,
            "note": self.note,
        }


def _recommended_model(hardware: Hardware, device: str) -> tuple[str, str]:
    """Модель по классу машины. Возвращает (модель, пояснение)."""
    if device != "cuda":
        return "base", "CPU: base держит реальное время, large-v3 — нет"
    match hardware.tier:
        case "gpu-large":
            return "large-v3", ">=7ГБ VRAM: large-v3 укладывается в VRAM"
        case "gpu-small":
            return "medium", "4-6ГБ VRAM: medium — лучший размен качество/VRAM"
        case _:
            return "small", "<4ГБ VRAM: small — единственное, что влезает"


def _resolve_device(raw: str, hardware: Hardware) -> tuple[str, str]:
    """'auto' → cuda при наличии CUDA. Возвращает (девайс, источник)."""
    value = (raw or "").strip().lower()
    if value in ("cuda", "gpu") and hardware.has_cuda:
        return "cuda", "env"
    if value == "cpu":
        return "cpu", "env"
    if value in ("cuda", "gpu") and not hardware.has_cuda:
        return "cpu", "auto(просили cuda, её нет)"
    return ("cuda", "auto") if hardware.has_cuda else ("cpu", "auto")


def _resolve_compute(raw: str, device: str) -> tuple[str, str]:
    value = (raw or "").strip()
    if value and value != "auto":
        return value, "env"
    if device == "cuda":
        return "float16", "auto(cuda)"
    return "int8", "auto(cpu)"


def resolve_stt(env: Mapping[str, str] | None = None) -> SttPlan:
    """Собрать эффективный SttPlan из окружения и железа.

    Пустое значение или `auto` означает «выбери сам». Это сделано специально:
    раньше `.env.example` содержал `WHISPER_DEVICE=cpu`, и копирование файла
    на ноутбук с RTX 4060 молча оставляло Whisper на CPU.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    hardware = detect_hardware()

    device, device_source = _resolve_device(
        source.get("WHISPER_DEVICE", "auto"), hardware
    )

    raw_model = (source.get("WHISPER_MODEL") or "").strip()
    if not raw_model or raw_model == "auto":
        model, note = _recommended_model(hardware, device)
        model_source = "auto"
    else:
        model, model_source, note = raw_model, "env", ""

    compute, compute_source = _resolve_compute(
        source.get("WHISPER_COMPUTE_TYPE", "auto"), device
    )

    return SttPlan(
        model=model,
        device=device,
        compute_type=compute,
        model_source=model_source,
        device_source=device_source,
        compute_source=compute_source,
        note=note,
    )


# ---------------------------------------------------------------------------
# Провайдеры
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Provider:
    """Запись цепочки: kind (stt|tts|llm), имя и необязательный аргумент.

    Аргумент — это после двоеточия: `openai:gpt-4o-mini`, `ollama:qwen2.5:7b`.
    """

    kind: str
    name: str
    arg: str = ""

    def __str__(self) -> str:
        return f"{self.name}:{self.arg}" if self.arg else self.name


_DEFAULT_CHAINS: dict[str, tuple[str, ...]] = {
    "stt": ("faster-whisper",),
    "tts": ("edge",),
    "llm": ("ollama",),
}


def provider_chain(
    kind: str, env: Mapping[str, str] | None = None, **_: Any
) -> tuple[Provider, ...]:
    """Разобрать `STT_CHAIN` / `TTS_CHAIN` / `LLM_CHAIN` из окружения."""
    source: Mapping[str, str] = os.environ if env is None else env
    raw = source.get(f"{kind.upper()}_CHAIN", "")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        parts = list(_DEFAULT_CHAINS.get(kind, ()))

    providers: list[Provider] = []
    for part in parts:
        name, _, arg = part.partition(":")
        name = name.strip().lower()
        if name:
            providers.append(Provider(kind=kind, name=name, arg=arg.strip()))
    return tuple(providers)


@dataclass
class Attempt:
    """Результат одной попытки в цепочке."""

    provider: Provider
    ok: bool
    latency_ms: float
    error: str = ""


@dataclass
class FallbackResult:
    """Что ответило и кто tried раньше."""

    value: Any
    provider: Provider | None
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        """True, если отработал не первый провайдер — система выжила на фоллбэке."""
        return self.provider is not None and self.provider != self.attempts[0].provider

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": str(self.provider) if self.provider else None,
            "degraded": self.degraded,
            "attempts": [
                {
                    "provider": str(a.provider),
                    "ok": a.ok,
                    "latency_ms": round(a.latency_ms, 1),
                    "error": a.error,
                }
                for a in self.attempts
            ],
        }


class AllProvidersFailed(RuntimeError):
    """Ни один провайдер в цепочке не ответил."""

    def __init__(self, attempts: Sequence[Attempt]) -> None:
        self.attempts = list(attempts)
        detail = "; ".join(
            f"{a.provider}: {a.error}" for a in self.attempts
        ) or "цепочка пуста"
        super().__init__(f"все провайдеры недоступны — {detail}")


def run_with_fallback(
    chain: Sequence[Provider],
    call: Callable[[Provider], Any],
    *,
    retry_sleep: float = 0.0,
) -> FallbackResult:
    """Вызвать `call` на первом провайдере, который не упадёт.

    Исключение от любого провайдера считается отказом: следующий в цепочке.
    Исключение из цепочки бросается наружу как AllProvidersFailed с историей.
    """
    attempts: list[Attempt] = []
    last_index = len(chain) - 1

    for index, provider in enumerate(chain):
        started = time.perf_counter()
        try:
            value = call(provider)
        except Exception as exc:  # noqa: BLE001 — это и есть отказ провайдера
            elapsed = (time.perf_counter() - started) * 1000.0
            attempts.append(Attempt(
                provider=provider,
                ok=False,
                latency_ms=elapsed,
                error=f"{type(exc).__name__}: {exc}",
            ))
            if retry_sleep and index < last_index:
                time.sleep(retry_sleep)
            continue

        elapsed = (time.perf_counter() - started) * 1000.0
        attempts.append(Attempt(provider=provider, ok=True, latency_ms=elapsed))
        return FallbackResult(value=value, provider=provider, attempts=attempts)

    raise AllProvidersFailed(attempts)


@dataclass(frozen=True)
class CloudProvider:
    """Описание облачного провайдера: где ключ, куда стучаться, как авторизоваться."""

    name: str
    kind: str                  # llm | stt | tts
    env_var: str
    base_url: str
    auth_style: str = "bearer"  # bearer | token | query | xi-api-key
    probe_url: str = ""
    openai_compatible: bool = True


CLOUD_PROVIDERS: dict[str, CloudProvider] = {
    spec.name: spec
    for spec in (
        CloudProvider("openai", "llm", "OPENAI_API_KEY",
                      "https://api.openai.com/v1",
                      probe_url="https://api.openai.com/v1/models"),
        CloudProvider("openrouter", "llm", "OPENROUTER_API_KEY",
                      "https://openrouter.ai/api/v1",
                      probe_url="https://openrouter.ai/api/v1/models"),
        CloudProvider("groq", "llm", "GROQ_API_KEY",
                      "https://api.groq.com/openai/v1",
                      probe_url="https://api.groq.com/openai/v1/models"),
        CloudProvider("mistral", "llm", "MISTRAL_API_KEY",
                      "https://api.mistral.ai/v1",
                      probe_url="https://api.mistral.ai/v1/models"),
        CloudProvider("cerebras", "llm", "CEREBRAS_API_KEY",
                      "https://api.cerebras.ai/v1",
                      probe_url="https://api.cerebras.ai/v1/models"),
        CloudProvider("xai", "llm", "XAI_API_KEY",
                      "https://api.x.ai/v1",
                      probe_url="https://api.x.ai/v1/models"),
        CloudProvider("github", "llm", "GITHUB_TOKEN",
                      "https://models.github.ai/inference",
                      probe_url="https://models.github.ai/inference/models"),
        CloudProvider("hf", "llm", "HF_TOKEN",
                      "https://router.huggingface.co/v1",
                      probe_url="https://router.huggingface.co/v1/models"),
        CloudProvider("deepgram", "stt", "DEEPGRAM_API_KEY",
                      "https://api.deepgram.com/v1", auth_style="token",
                      probe_url="https://api.deepgram.com/v1/projects"),
        CloudProvider("elevenlabs", "tts", "ELEVENLABS_API_KEY",
                      "https://api.elevenlabs.io/v1", auth_style="xi-api-key",
                      probe_url="https://api.elevenlabs.io/v1/voices"),
    )
}


def cloud_api_key(name: str, env: Mapping[str, str] | None = None) -> str:
    """Ключ провайдера из окружения. Пустая строка = не задан."""
    source: Mapping[str, str] = os.environ if env is None else env
    spec = CLOUD_PROVIDERS.get(name)
    if spec is None:
        return ""
    return (source.get(spec.env_var) or "").strip()


def cloud_auth_headers(
    name: str, env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Заголовки авторизации для облачного провайдера."""
    spec = CLOUD_PROVIDERS.get(name)
    if spec is None:
        return {}
    key = cloud_api_key(name, env)
    if not key:
        return {}
    if spec.auth_style == "bearer":
        return {"Authorization": f"Bearer {key}"}
    if spec.auth_style == "token":
        return {"Authorization": f"Token {key}"}
    if spec.auth_style == "xi-api-key":
        return {"xi-api-key": key}
    return {}


def cloud_probe_url(
    name: str, env: Mapping[str, str] | None = None
) -> tuple[str, dict[str, str]]:
    """URL и заголовки для проверки доступности провайдера."""
    spec = CLOUD_PROVIDERS.get(name)
    if spec is None:
        raise KeyError(f"неизвестный облачный провайдер: {name}")
    url = spec.probe_url or spec.base_url
    if spec.auth_style == "query":
        url = f"{url}?key={cloud_api_key(name, env)}"
    return url, {"Accept": "application/json", **cloud_auth_headers(name, env)}


def llm_root_url(env: Mapping[str, str] | None = None) -> str:
    """Корень Ollama без `/v1` — для нативных эндпоинтов `/api/*`."""
    source: Mapping[str, str] = os.environ if env is None else env
    url = (source.get("LLM_BASE_URL") or "http://localhost:11434/v1").strip()
    url = url.rstrip("/")
    for suffix in ("/v1", "/api"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url


def _default_tts_voices() -> tuple[str, str]:
    """Голоса по умолчанию из jarvis.tts.voices. Пусто, если импорт не удался."""
    try:
        from jarvis.tts.voices import DEFAULT_EN_VOICE, DEFAULT_RU_VOICE

        return DEFAULT_RU_VOICE, DEFAULT_EN_VOICE
    except Exception:
        return "", ""


def describe(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Сводка эффективной конфигурации — для doctor, /api/backends и логов."""
    source: Mapping[str, str] = os.environ if env is None else env
    hardware = detect_hardware()
    stt = resolve_stt(source)
    default_ru, default_en = _default_tts_voices()
    return {
        "hardware": {
            "tier": hardware.tier,
            "summary": hardware.summary(),
            "cuda_devices": hardware.cuda_devices,
            "vram_mb": hardware.vram_mb,
            "cpu_count": hardware.cpu_count,
            "ram_gb": hardware.ram_gb,
            "python": hardware.python_version,
        },
        "stt": {**stt.as_dict(), "chain": [str(p) for p in provider_chain("stt", source)]},
        "tts": {
            "chain": [str(p) for p in provider_chain("tts", source)],
            "voice_ru": source.get("TTS_VOICE_RU") or default_ru,
            "voice_en": source.get("TTS_VOICE") or default_en,
        },
        "llm": {
            "chain": [str(p) for p in provider_chain("llm", source)],
            "model": source.get("LLM_MODEL", "llama3.2:3b"),
            "base_url": source.get("LLM_BASE_URL", ""),
            "keep_alive": source.get("OLLAMA_KEEP_ALIVE", ""),
        },
    }