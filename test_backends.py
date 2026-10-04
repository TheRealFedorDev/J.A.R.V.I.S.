"""Конфигурация бэкендов: авто-подбор под железо и цепочки с фоллбэком.

Живые баги, которые здесь закрыты:
* `WHISPER_DEVICE=auto` молча превращался в `cpu`, поэтому ноутбук в
  лаборатории с RTX 4060 продолжал считать на процессоре, не замечая
  видеокарты;
* legacy- и pipecat-контуры передавали `auto` прямо в faster-whisper,
  который такое значение отвергает;
* копирование `.env.example` на другую машину pins'ило cpu/int8/base,
  и переезд требовал правки кода вместо правки конфига.

Проверяем:
* auto действительно выбирает cuda/float16/large-v3 при наличии VRAM;
* явные значения в .env всегда выигрывают у авто;
* парсинг цепочек `kind:arg`;
* фоллбэк: первый упал — отвечает второй, и видно, кто именно ответил.
"""

from __future__ import annotations

import pytest

from jarvis.core.backends import (
    AllProvidersFailed,
    CLOUD_PROVIDERS,
    Hardware,
    Provider,
    cloud_api_key,
    cloud_auth_headers,
    describe,
    llm_root_url,
    provider_chain,
    resolve_stt,
    run_with_fallback,
)


# ---------------------------------------------------------------------------
# Авто-подбор под железо
# ---------------------------------------------------------------------------
def test_cpu_without_cuda_picks_base_int8(monkeypatch):
    monkeypatch.setattr(
        "jarvis.core.backends.detect_hardware",
        lambda **_: Hardware(cuda_devices=0, cpu_count=12, ram_gb=15.4),
    )
    plan = resolve_stt({})
    assert plan.device == "cpu"
    assert plan.compute_type == "int8"
    assert plan.model == "base"
    assert plan.device_source == "auto"
    assert plan.model_source == "auto"


@pytest.mark.parametrize(
    "vram_mb, expected_model",
    [
        (8192, "large-v3"),
        (6144, "medium"),
        (3072, "small"),
    ],
)
def test_cuda_scales_model_with_vram(monkeypatch, vram_mb, expected_model):
    monkeypatch.setattr(
        "jarvis.core.backends.detect_hardware",
        lambda **_: Hardware(cuda_devices=1, vram_mb=vram_mb, gpu_name="RTX 4060"),
    )
    plan = resolve_stt({})
    assert plan.device == "cuda"
    assert plan.compute_type == "float16"
    assert plan.model == expected_model


def test_explicit_env_beats_auto(monkeypatch):
    monkeypatch.setattr(
        "jarvis.core.backends.detect_hardware",
        lambda **_: Hardware(cuda_devices=1, vram_mb=8192),
    )
    plan = resolve_stt({
        "WHISPER_MODEL": "small",
        "WHISPER_DEVICE": "cpu",
        "WHISPER_COMPUTE_TYPE": "int8",
    })
    assert (plan.model, plan.device, plan.compute_type) == ("small", "cpu", "int8")
    assert plan.model_source == "env"
    assert plan.device_source == "env"


def test_cuda_requested_but_absent_degrades_to_cpu(monkeypatch):
    monkeypatch.setattr(
        "jarvis.core.backends.detect_hardware",
        lambda **_: Hardware(cuda_devices=0),
    )
    plan = resolve_stt({"WHISPER_DEVICE": "cuda"})
    assert plan.device == "cpu"
    assert "cuda" in plan.device_source


def test_unknown_device_string_falls_back_to_auto(monkeypatch):
    monkeypatch.setattr(
        "jarvis.core.backends.detect_hardware",
        lambda **_: Hardware(cuda_devices=0),
    )
    assert resolve_stt({"WHISPER_DEVICE": "tpu"}).device == "cpu"


# ---------------------------------------------------------------------------
# Цепочки провайдеров
# ---------------------------------------------------------------------------
def test_chain_defaults_when_env_empty():
    assert [str(p) for p in provider_chain("llm", {})] == ["ollama"]
    assert [str(p) for p in provider_chain("tts", {})] == ["edge"]
    assert [str(p) for p in provider_chain("stt", {})] == ["faster-whisper"]


def test_chain_parses_order_and_arguments():
    chain = provider_chain("llm", {"LLM_CHAIN": "ollama, openai:gpt-4o-mini ,github"})
    assert [str(p) for p in chain] == ["ollama", "openai:gpt-4o-mini", "github"]
    assert chain[1].arg == "gpt-4o-mini"
    assert chain[0].arg == ""


def test_chain_drops_empty_items():
    chain = provider_chain("tts", {"TTS_CHAIN": "edge,,  ,pyttsx3"})
    assert [p.name for p in chain] == ["edge", "pyttsx3"]


def test_cloud_specs_have_env_var_and_probe():
    for name, spec in CLOUD_PROVIDERS.items():
        assert spec.env_var, f"{name}: нет переменной с ключом"
        assert spec.probe_url, f"{name}: нет probe_url"
        assert spec.kind in ("llm", "stt", "tts")


def test_cloud_auth_styles():
    assert cloud_auth_headers("openai", {"OPENAI_API_KEY": "sk-1"}) == {
        "Authorization": "Bearer sk-1"
    }
    assert cloud_auth_headers("deepgram", {"DEEPGRAM_API_KEY": "dg-1"}) == {
        "Authorization": "Token dg-1"
    }
    assert cloud_auth_headers("elevenlabs", {"ELEVENLABS_API_KEY": "el-1"}) == {
        "xi-api-key": "el-1"
    }
    assert cloud_auth_headers("openai", {}) == {}


def test_cloud_api_key_missing_is_empty():
    assert cloud_api_key("groq", {}) == ""
    assert cloud_api_key("no-such-provider", {}) == ""


def test_llm_root_url_strips_openai_suffix():
    assert llm_root_url({"LLM_BASE_URL": "http://localhost:11434/v1"}) == (
        "http://localhost:11434"
    )
    assert llm_root_url({"LLM_BASE_URL": "http://localhost:11434/v1/"}) == (
        "http://localhost:11434"
    )


def test_describe_reports_all_three_chains():
    payload = describe({
        "LLM_CHAIN": "ollama",
        "STT_CHAIN": "faster-whisper",
        "TTS_CHAIN": "edge,pyttsx3",
    })
    assert payload["stt"]["chain"] == ["faster-whisper"]
    assert payload["tts"]["chain"] == ["edge", "pyttsx3"]
    assert payload["llm"]["chain"] == ["ollama"]
    assert "model" in payload["stt"]


# ---------------------------------------------------------------------------
# Фоллбэк
# ---------------------------------------------------------------------------
def test_first_provider_wins():
    chain = (
        Provider("llm", "first"),
        Provider("llm", "second"),
    )
    result = run_with_fallback(chain, lambda p: f"ответ:{p.name}")
    assert result.provider is not None and result.provider.name == "first"
    assert result.value == "ответ:first"
    assert result.degraded is False
    assert len(result.attempts) == 1


def test_falls_through_to_second_and_reports_degraded():
    chain = (
        Provider("llm", "битый"),
        Provider("llm", "живой"),
    )

    def call(provider: Provider) -> str:
        if provider.name == "битый":
            raise ConnectionError("соединение отклонено")
        return "ответ"

    result = run_with_fallback(chain, call)
    assert result.value == "ответ"
    assert result.provider is not None and result.provider.name == "живой"
    assert result.degraded is True
    assert [a.ok for a in result.attempts] == [False, True]
    assert "отклонено" in result.attempts[0].error


def test_all_providers_failed_carries_history():
    chain = (Provider("llm", "a"), Provider("llm", "b"))

    def call(provider: Provider) -> str:
        raise TimeoutError(f"{provider.name} молчит")

    with pytest.raises(AllProvidersFailed) as excinfo:
        run_with_fallback(chain, call)
    assert len(excinfo.value.attempts) == 2
    assert "молчит" in str(excinfo.value)


def test_empty_chain_raises_rather_than_returning_none():
    with pytest.raises(AllProvidersFailed):
        run_with_fallback((), lambda p: "x")


def test_fallback_result_serializes_for_audit():
    chain = (Provider("llm", "битый"), Provider("llm", "живой"))

    def call(provider: Provider) -> str:
        if provider.name == "битый":
            raise OSError("нет сети")
        return "ок"

    data = run_with_fallback(chain, call).as_dict()
    assert data["provider"] == "живой"
    assert data["degraded"] is True
    assert data["attempts"][1]["ok"] is True
    assert data["attempts"][0]["error"]