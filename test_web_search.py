"""Тесты быстрого веб-поиска (чистая логика, без сети)."""

from __future__ import annotations

from jarvis.voice.web_search import (
    WebHit,
    compose_answer,
    is_weather_query,
    maybe_web_search,
    needs_web_search,
    weather_place,
)


# ---------------------------------------------------------------------------
# needs_web_search / is_weather_query / weather_place
# ---------------------------------------------------------------------------
def test_needs_search_weather_question():
    assert needs_web_search("Какая сейчас погода?")
    assert needs_web_search("погода в Санкт-Петербурге")
    assert needs_web_search("какая температура в Москве")


def test_needs_search_facts_and_lookup():
    """Веб — только по явной просьбе или там, где модели нужны живые данные."""
    assert needs_web_search("найди мне погоду на завтра")
    assert needs_web_search("курс доллара сегодня")
    assert needs_web_search("погугли что нового")
    assert needs_web_search("в интернете что нового")
    assert needs_web_search("сколько стоит айфон")


def test_question_mark_alone_does_not_trigger_search():
    """Регрессия: вопросительный знак гонял весь разговор в веб мимо LLM."""
    assert not needs_web_search("почему небо голубое?")
    assert not needs_web_search("что такое квантовая запутанность?")
    assert not needs_web_search("кто такой Пушкин")
    assert not needs_web_search("Ты работаешь?")
    assert not needs_web_search("когда будет весна")


def test_skips_smalltalk_and_empty():
    assert not needs_web_search("привет")
    assert not needs_web_search("доброе утро")
    assert not needs_web_search("расскажи шутку")
    assert not needs_web_search("")
    assert not needs_web_search("   ")
    assert not needs_web_search("а" * 400)


def test_is_weather_query():
    assert is_weather_query("погода завтра")
    assert is_weather_query("сколько градусов?")
    assert not is_weather_query("кто такой Эйнштейн")


def test_weather_place_extraction():
    assert weather_place("погода в Тбилиси") == "Тбилиси"
    assert weather_place("какая погода в Санкт-Петербурге") in (
        "Санкт-Петербурге",
        "Санкт-Петербурге".lower(),
    ) or "Петербург" in weather_place("погода в Санкт-Петербурге")
    # без города — дефолт из env/Москва
    place = weather_place("какая сейчас погода?")
    assert place  # непустая строка


def test_weather_place_env_default(monkeypatch):
    monkeypatch.setenv("BERTA_CITY", "Казань")
    assert weather_place("погода?") == "Казань"


# ---------------------------------------------------------------------------
# compose_answer
# ---------------------------------------------------------------------------
def test_compose_answer_truncates_and_strips():
    long = "Слово " * 200
    out = compose_answer("x", WebHit(text=long, source="ddg"))
    assert len(out) <= 340
    assert out.endswith("…")
    assert "  " not in out


def test_compose_answer_empty_hit():
    assert compose_answer("x", None) == ""
    assert compose_answer("x", WebHit(text="", source="ddg")) == ""


def test_compose_weather_passthrough():
    text = "В Москве сейчас ясно, 12°C."
    assert compose_answer("погода", WebHit(text=text, source="weather")) == text


# ---------------------------------------------------------------------------
# maybe_web_search: не трогает сеть для разговорных фраз
# ---------------------------------------------------------------------------
def test_maybe_web_search_skips_smalltalk():
    import asyncio

    out = asyncio.run(maybe_web_search("привет"))
    assert out == ""


def test_maybe_web_search_empty():
    import asyncio

    assert asyncio.run(maybe_web_search("")) == ""


# ---------------------------------------------------------------------------
# Геокодинг и погода: косвенные падежи, met.no
# ---------------------------------------------------------------------------
def test_geocode_retries_inflected_forms(monkeypatch):
    """«Долгопрудном» точно не находится — пробуем основу слова."""
    import asyncio
    from urllib.parse import quote

    from jarvis.voice import web_search

    calls: list[str] = []

    async def fake_get_json(url, timeout, *, headers=None):
        calls.append(url)
        if quote("Долгопрудн") in url:
            return {"results": [{
                "latitude": 55.9496, "longitude": 37.5018,
                "name": "Долгопрудный",
            }]}
        return {"generationtime_ms": 0.1}

    monkeypatch.setattr(web_search, "_get_json", fake_get_json)
    got = asyncio.run(web_search._geocode("Долгопрудном", 2.0))
    assert got == (55.9496, 37.5018, "Долгопрудный")
    # основа ищется первой — точная косвенная форма не нужна
    from urllib.parse import unquote_plus

    assert len(calls) == 1
    assert "Долгопрудн" in unquote_plus(calls[0])


def test_metno_parses_compact(monkeypatch):
    import asyncio

    from jarvis.voice import web_search

    async def fake_get_json(url, timeout, *, headers=None):
        return {"properties": {"timeseries": [{
            "data": {
                "instant": {"details": {
                    "air_temperature": 4.4, "wind_speed": 2.4,
                }},
                "next_1_hours": {"summary": {"symbol_code": "clearsky_day"}},
            },
        }]}}

    monkeypatch.setattr(web_search, "_get_json", fake_get_json)
    hit = asyncio.run(
        web_search._search_metno(55.9, 37.5, "Долгопрудный", 1.0)
    )
    assert hit is not None and hit.source == "weather"
    assert "Долгопрудный" in hit.text
    assert "ясно" in hit.text and "4°C" in hit.text
