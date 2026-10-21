"""Быстрый веб-поиск для голосового ответа Berta (<~1 с, параллельно).

Источники (без API-ключей):
* open-meteo — текущая погода (геокодинг + forecast);
* open.er-api.com — курс валют (USD/EUR/BTC → RUB);
* DuckDuckGo Instant Answer — факты/определения;
* Wikipedia REST — сводка статьи (policy UA).

`web_search_answer` возвращает готовую короткую фразу для TTS
или "" (тогда фраза уходит в локальную LLM).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

log = logging.getLogger("jarvis.voice.web_search")

# общий бюджет сети на все источники (сек) — гонка, возврат по первому hit.
# Двух последовательных запросов (геокодинг + прогноз) 1.8 с не хватало:
# на первом (холодном) соединении геокодинг съедал весь бюджет, и погода
# возвращала пустоту — ответа не было вовсе.
DEFAULT_TIMEOUT = float(os.environ.get("WEB_SEARCH_TIMEOUT", "6.0"))

_WEATHER_RE = re.compile(
    r"погод|температур|градус|дожд|снегопад|снеж|ветер|шторм|гроз",
    re.IGNORECASE,
)
_CURRENCY_RE = re.compile(
    r"курс\s+(?:валют|доллар|евро|рубл|юан|битк)"
    r"|(?:доллар|евро|юан|битк(?:оин)?)\s+(?:сегодня|к\s+сегодня|к\s+завтра)"
    r"|сколько\s+(?:стоит|стоил)?\s*(?:доллар|евро|битк)",
    re.IGNORECASE,
)
# запросы, которым почти всегда нужен интернет (факты/новости/справочник)
_NEEDS_SEARCH_RE = re.compile(
    r"("
    r"найди|поищи|поиск\w*|загугли|погугли|гугли"
    r"|в\s+интернете|в\s+сети|в\s+сетке|через\s+интернет"
    r"|новост[иия]|что\s+происходит"
    r"|сколько\s+стоит|цен[аы]\s+на"
    r")",
    re.IGNORECASE,
)
# разговорные/командные — не ищем
_SKIP_SEARCH_RE = re.compile(
    r"^(привет|здравств|добрый|доброе|пока|спасибо|шутк|анекдот)",
    re.IGNORECASE,
)

_WEATHER_CODES: dict[int, str] = {
    0: "ясно",
    1: "малооблачно",
    2: "переменная облачность",
    3: "пасмурно",
    45: "туман",
    48: "изморозь",
    51: "морось",
    53: "морось",
    55: "сильная морось",
    61: "дождь",
    63: "дождь",
    65: "сильный дождь",
    71: "снег",
    73: "снег",
    75: "сильный снег",
    80: "ливень",
    81: "ливень",
    82: "сильный ливень",
    95: "гроза",
    96: "гроза с градом",
    99: "сильная гроза",
}


@dataclass(frozen=True)
class WebHit:
    """Один найденный сниппет."""

    text: str
    source: str  # weather | ddg | wiki
    url: str = ""


def needs_web_search(text: str) -> bool:
    """True — фразе нужен быстрый ответ из интернета.

    По умолчанию ход идёт в локальный LLM. Веб включается только когда:
      * пользователь прямо попросил искать («найди», «погугли», «в интернете»);
      * тема требует живых данных (погода, курсы, цены) — 3B-модель их не
        знает и выдумает.

    Вопросительный знак сам по себе НЕ повод: раньше из-за него весь
    разговор уходил в веб мимо LLM.
    """
    t = (text or "").strip()
    if not t or len(t) > 300:
        return False
    if _SKIP_SEARCH_RE.search(t):
        return False
    if _WEATHER_RE.search(t) or _CURRENCY_RE.search(t):
        return True
    return bool(_NEEDS_SEARCH_RE.search(t))


def is_weather_query(text: str) -> bool:
    return bool(_WEATHER_RE.search(text or ""))


def weather_place(text: str) -> str:
    """«погода в Москве» → «Москва»; иначе BERTA_CITY или Москва."""
    t = text or ""
    m = re.search(
        r"погод\w*\s+(?:в|на|для|где)\s+"
        r"([А-Яа-яЁё][\w\-']*(?:\s+[А-Яа-яЁё][\w\-']*){0,2})",
        t,
        re.IGNORECASE,
    )
    if m:
        return _norm_place(m.group(1))
    m = re.search(
        r"в\s+([А-Яа-яЁё][а-яё]+)",
        t,
        re.IGNORECASE,
    )
    if m:
        return _norm_place(m.group(1))
    return _norm_place(os.environ.get("BERTA_CITY") or "Москва")


# частые предложные формы → именительный (для geocoding)
_PLACE_FIX: dict[str, str] = {
    "москве": "Москва",
    "москвы": "Москва",
    "москву": "Москва",
    "санкт-петербурге": "Санкт-Петербург",
    "питере": "Санкт-Петербург",
    "спб": "Санкт-Петербург",
    "казане": "Казань",
    "казаны": "Казань",
    "новосибирске": "Новосибирск",
    "екатеринбурге": "Екатеринбург",
    "воронеже": "Воронеж",
    "сочи": "Сочи",
}

# координаты без RTT на geocoding (частые города)
_PLACE_COORDS: dict[str, tuple[float, float, str]] = {
    "москва": (55.7558, 37.6173, "Москва"),
    "санкт-петербург": (59.9391, 30.3159, "Санкт-Петербург"),
    "казань": (55.7963, 49.1088, "Казань"),
    "новосибирск": (55.0084, 82.9357, "Новосибирск"),
    "екатеринбург": (56.8389, 60.6057, "Екатеринбург"),
    "воронеж": (51.6720, 39.1843, "Воронеж"),
    "сочи": (43.5855, 39.7231, "Сочи"),
}

# кэш geocoding: «москве» → (lat, lon, «Москва»)
_geo_cache: dict[str, tuple[float, float, str]] = {}


def _norm_place(raw: str) -> str:
    s = re.sub(r"[^\w\s\-']+", "", (raw or "")).strip()
    low = s.lower()
    if low in _PLACE_FIX:
        return _PLACE_FIX[low]
    if not s:
        return (os.environ.get("BERTA_CITY") or "Москва").strip() or "Москва"
    # «в Москве» → «Москва» для поиска в _PLACE_COORDS
    if low.endswith("е") and low in _PLACE_FIX:
        return _PLACE_FIX[low]
    return s[0].upper() + s[1:] if s else s


def _clean_snippet(s: str, limit: int = 320) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    s = re.sub(r"\[\d+\]", "", s)
    if len(s) > limit:
        cut = s[:limit]
        # не режем посреди слова
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        s = cut.rstrip(" ,;:-") + "…"
    return s


def compose_answer(query: str, hit: WebHit | None) -> str:
    """Готовая фраза для озвучки (короткая, без markdown)."""
    if hit is None or not hit.text:
        return ""
    body = _clean_snippet(hit.text)
    if not body:
        return ""
    if hit.source == "weather":
        return body
    # лёгкая приставка для фактов
    if hit.source == "wiki":
        return body
    return body


# ---------------------------------------------------------------------------
# HTTP: один переиспользуемый клиент (без TLS-handshake на каждый запрос)
# ---------------------------------------------------------------------------
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
# Wikimedia robot policy: descriptive UA с контактом, иначе 403
_WIKI_UA = (
    "BertaVoiceAssistant/1.0 "
    "(https://github.com/TheRealFedorDev/J.A.R.V.I.S.; local jarvis) "
    "python-httpx/0.28"
)
_client: Any = None  # httpx.AsyncClient | None (лениво на текущем loop)


def _get_client(timeout: float) -> Any:
    global _client
    import httpx

    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            headers={"User-Agent": _UA, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"},
            follow_redirects=True,
            timeout=timeout,
        )
    # httpx: per-request timeout override
    return _client


async def _close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def _get_json(
    url: str, timeout: float, *, headers: dict[str, str] | None = None
) -> dict | list | None:
    try:
        client = _get_client(timeout)
        # connect/read отдельно: TCP-хендшейк не съедал весь бюджет
        import httpx as _httpx

        t = _httpx.Timeout(timeout, connect=min(timeout, 1.2))
        kw: dict[str, Any] = {"timeout": t}
        if headers:
            kw["headers"] = headers
        r = await client.get(url, **kw)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as exc:  # noqa: BLE001 — любая сетевая ошибка → None
        log.debug("web_search GET %s: %s", url[:80], exc)
        return None


async def _get_text(url: str, timeout: float) -> str:
    try:
        client = _get_client(timeout)
        import httpx as _httpx

        t = _httpx.Timeout(timeout, connect=min(timeout, 1.2))
        r = await client.get(
            url,
            timeout=t,
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
        if r.status_code not in (200, 202):
            return ""
        return r.text
    except Exception as exc:  # noqa: BLE001
        log.debug("web_search GET %s: %s", url[:80], exc)
        return ""


async def _geocode(place: str, timeout: float) -> tuple[float, float, str] | None:
    """Координаты города по названию.

    Геокодер open-meteo не понимает косвенные падежи («Долгопрудном»,
    «Королёве» — пустой ответ), поэтому после точной формы пробуем основы
    слова: «Долгопрудн» → «Долгопрудный».
    """
    candidates: list[str] = []
    # сначала основа (обрезаем 1–2 буквы): «Долгопрудном» → «Долгопрудн»,
    # «Королёве» → «Королёв»; затем точная форма
    if len(place) > 5:
        candidates.append(place[:-1])
        if len(place) > 7:
            candidates.append(place[:-2])
    candidates.append(place)
    candidates = list(dict.fromkeys(candidates))

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    for cand in candidates:
        left = deadline - loop.time()
        if left <= 0.2:
            break
        url = (
            "https://geocoding-api.open-meteo.com/v1/search"
            f"?name={quote(cand)}&count=1&language=ru&format=json"
        )
        geo = await _get_json(url, min(left, 2.5))
        results = (geo.get("results") if isinstance(geo, dict) else None) or []
        if results:
            try:
                return (
                    float(results[0]["latitude"]),
                    float(results[0]["longitude"]),
                    str(results[0].get("name") or place),
                )
            except (KeyError, TypeError, ValueError):
                return None
    return None


async def _search_metno(
    lat: float, lon: float, name: str, timeout: float
) -> WebHit | None:
    """Погода через met.no (YR) — без ключа и без блокировок в РФ.

    open-meteo (forecast) местами недоступен (в РФ часто режется по IP),
    поэтому основной источник прогноза — met.no, а open-meteo — запасной.
    """
    url = (
        "https://api.met.no/weatherapi/locationforecast/2.0/compact"
        f"?lat={lat:.4f}&lon={lon:.4f}"
    )
    data = await _get_json(url, timeout, headers={"User-Agent": _WIKI_UA})
    if not isinstance(data, dict):
        return None
    series = (data.get("properties") or {}).get("timeseries") or []
    if not series:
        return None
    first = series[0] or {}
    d = first.get("data") or {}
    details = (d.get("instant") or {}).get("details") or {}
    try:
        temp = int(round(float(details.get("air_temperature"))))
    except (TypeError, ValueError):
        return None
    wind = details.get("wind_speed")
    symbol = str(
        ((d.get("next_1_hours") or {}).get("summary") or {}).get("symbol_code")
        or ""
    )
    base = re.sub(r"_(day|night|polartwilight)$", "", symbol)
    base = re.sub(r"^(light|heavy)", "", base)
    sky = _METNO_SKY.get(base, "облачно")
    text = f"{name}: сейчас {sky}, {temp}°C"
    if wind is not None:
        try:
            text += f", ветер {int(round(float(wind)))} м/с"
        except (TypeError, ValueError):
            pass
    text += "."
    return WebHit(text=text, source="weather")


_METNO_SKY: dict[str, str] = {
    "clearsky": "ясно",
    "fair": "малооблачно",
    "partlycloudy": "переменная облачность",
    "cloudy": "облачно",
    "fog": "туман",
    "rain": "дождь",
    "sleet": "мокрый снег",
    "snow": "снег",
    "rainshowers": "ливень",
    "rainshowersandthunder": "гроза с ливнем",
    "sleetshowers": "мокрый снег",
    "snowshowers": "снегопад",
    "rainandthunder": "гроза с дождём",
    "sleetandthunder": "гроза с мокрым снегом",
    "snowandthunder": "гроза со снегом",
}


async def _search_weather(query: str, timeout: float) -> WebHit | None:
    place = weather_place(query)
    lat = lon = None
    name = place
    key = place.lower()
    loop = asyncio.get_running_loop()
    started = loop.time()
    if key in _PLACE_COORDS:
        lat, lon, name = _PLACE_COORDS[key]
    elif key in _geo_cache:
        lat, lon, name = _geo_cache[key]
    else:
        # геокодингу — не больше половины бюджета (но хотя бы 0.6 с),
        # чтобы прогнозу осталось время на второй запрос
        geo_budget = min(4.0, max(1.5, timeout * 0.6))
        found = await _geocode(place, geo_budget)
        if found is None:
            # город не найден/сеть не ответила — НЕ подменяем Москвой:
            # лучше пустой ответ (уйдём в LLM), чем погода чужого города
            return None
        lat, lon, name = found
        _geo_cache[key] = found

    # основной источник — met.no; open-meteo пробуем, только если он не ответил
    remaining = max(0.5, timeout - (loop.time() - started))
    hit = await _search_metno(lat, lon, name, remaining)
    if hit is not None:
        return hit

    fc_url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
        f"&timezone=auto"
    )
    # координаты уже известны → остаток бюджета на forecast, но не меньше 0.5 с
    fc_budget = max(0.5, timeout - (loop.time() - started))
    fc = await _get_json(fc_url, fc_budget)
    if not isinstance(fc, dict):
        return None
    cur = fc.get("current") or {}
    try:
        temp = int(round(float(cur.get("temperature_2m"))))
        feels = cur.get("apparent_temperature")
        code = int(cur.get("weather_code") or 0)
        wind = cur.get("wind_speed_10m")
    except (TypeError, ValueError):
        return None
    sky = _WEATHER_CODES.get(code, "облачно")
    text = f"{name}: сейчас {sky}, {temp}°C"
    if feels is not None:
        try:
            text += f" (ощущается {int(round(float(feels)))}°C)"
        except (TypeError, ValueError):
            pass
    if wind is not None:
        try:
            text += f", ветер {int(round(float(wind)))} м/с"
        except (TypeError, ValueError):
            pass
    text += "."
    return WebHit(text=text, source="weather")


def _strip_lead(q: str) -> str:
    """«что такое X?» / «кто такой X» → «X» — лучше Instant Answer."""
    return re.sub(
        r"^(что такое|кто такой|что это|кто это|почему|зачем|когда|где)\s+",
        "",
        (q or "").strip(),
        flags=re.IGNORECASE,
    ).strip(" ?!.")


async def _search_ddg(query: str, timeout: float) -> WebHit | None:
    for q in (query, _strip_lead(query)):
        if not q:
            continue
        url = (
            "https://api.duckduckgo.com/"
            f"?q={quote(q)}&format=json&no_html=1&skip_disambig=1"
        )
        data = await _get_json(url, timeout)
        if not isinstance(data, dict):
            continue
        for key in ("Answer", "AbstractText", "Definition"):
            val = data.get(key)
            if isinstance(val, str) and len(val.strip()) > 40:
                return WebHit(text=val, source="ddg")
        for topic in data.get("RelatedTopics") or []:
            if isinstance(topic, dict):
                t = topic.get("Text")
                if isinstance(t, str) and len(t) > 40:
                    return WebHit(text=t, source="ddg")
                for sub in topic.get("Topics") or []:
                    if isinstance(sub, dict):
                        t = sub.get("Text")
                        if isinstance(t, str) and len(t) > 40:
                            return WebHit(text=t, source="ddg")
    return None


async def _search_ddg_html(query: str, timeout: float) -> WebHit | None:
    """HTML-сниппеты DDG; POST preferred (GET часто 202-челлендж)."""
    q = _strip_lead(query) or query
    if not q:
        return None
    html = ""
    try:
        client = _get_client(timeout)
        import httpx as _httpx

        t = _httpx.Timeout(timeout, connect=min(timeout, 1.2))
        r = await client.post(
            "https://html.duckduckgo.com/html/",
            data={"q": q, "b": ""},
            timeout=t,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        if r.status_code in (200, 202):
            html = r.text
    except Exception as exc:  # noqa: BLE001
        log.debug("web_search ddg POST: %s", exc)
    if not html or "result__snippet" not in html and "result-snippet" not in html:
        html = await _get_text(
            f"https://html.duckduckgo.com/html/?q={quote(q)}", timeout
        ) or html
    if not html:
        return None
    patterns = (
        r'class=["\']result__snippet["\'][^>]*>(.*?)</(?:a|td|div|span)>',
        r'class=["\']result-snippet["\'][^>]*>(.*?)</(?:td|div|span)>',
        r'class=["\']result-link["\'][^>]*>(.*?)</a>',
    )
    for pat in patterns:
        parts = re.findall(pat, html, re.IGNORECASE | re.DOTALL)
        for raw in parts:
            text = re.sub(r"<[^>]+>", " ", raw)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > 60 and "Subscribe" not in text and "subscribers" not in text.lower():
                return WebHit(text=text, source="ddg_html")
    return None


async def _search_currency(query: str, timeout: float) -> WebHit | None:
    """open.er-api.com — USD/RUB (и EUR) без ключа, ~0.7 с."""
    q = (query or "").lower()
    base = "USD"
    if "евро" in q:
        base = "EUR"
    elif "битк" in q or "биткоин" in q:
        base = "BTC"
    data = await _get_json(
        f"https://open.er-api.com/v6/latest/{base}",
        timeout,
        headers={"User-Agent": _UA},
    )
    if not isinstance(data, dict) or data.get("result") != "success":
        return None
    rates = data.get("rates") or {}
    try:
        if base == "BTC":
            btc = float(rates.get("RUB") or 0)
            # API: RUB per 1 USD; BTC rates are inverted — use usd/btc if present
            if btc > 0:
                # open.er-api: rates.USD=1, rates.BTC≈1/price_usd
                btc_usd = float(rates.get("BTC") or 0)
                if 0 < btc_usd < 1:
                    price = 1.0 / btc_usd
                    text = f"Биткоин ≈ {price:.0f} USD."
                    return WebHit(text=text, source="currency")
            return None
        rub = float(rates.get("RUB") or 0)
        if rub <= 0:
            return None
        unit = {"USD": "доллар", "EUR": "евро"}[base]
        date = str(data.get("time_last_update_utc") or "")[:16]
        text = f"1 {unit} ≈ {rub:.2f} рубля."
        if date:
            text += f" Курс на {date}."
        return WebHit(text=text, source="currency")
    except (TypeError, ValueError, KeyError):
        return None


async def _search_wikipedia(query: str, timeout: float) -> WebHit | None:
    q = _strip_lead(query)
    if not q:
        return None
    wiki_headers = {"User-Agent": _WIKI_UA}
    title = q.replace(" ", "_")
    url = (
        "https://ru.wikipedia.org/api/rest_v1/page/summary/"
        + quote(title, safe="_")
    )
    data = await _get_json(url, timeout, headers=wiki_headers)
    if not isinstance(data, dict):
        sea_url = (
            "https://ru.wikipedia.org/w/api.php"
            f"?action=opensearch&search={quote(q)}&limit=1&namespace=0&format=json"
        )
        sea = await _get_json(sea_url, timeout, headers=wiki_headers)
        if not (isinstance(sea, list) and len(sea) >= 2 and sea[1]):
            return None
        title2 = sea[1][0].replace(" ", "_")
        data = await _get_json(
            "https://ru.wikipedia.org/api/rest_v1/page/summary/"
            + quote(title2, safe="_"),
            timeout,
            headers=wiki_headers,
        )
        if not isinstance(data, dict):
            return None
    extract = data.get("extract")
    if isinstance(extract, str) and len(extract.strip()) > 30:
        return WebHit(text=extract, source="wiki")
    return None


async def web_search_answer(
    query: str, *, timeout: float | None = None
) -> str:
    """Быстрый ответ для TTS. "" — поиска нет, уходим в LLM.

    Погода/курс: выделенный путь (полный бюджет, без конкурентов).
    Факты: гонка DDG Instant Answer + wiki (+ HTML, если остался бюджет).
    Возврат: первый достойный hit либо лучший из уже готовых по дедлайну.
    """
    q = (query or "").strip()
    if not q:
        return ""
    budget = DEFAULT_TIMEOUT if timeout is None else float(timeout)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget

    # --- погода: только open-meteo (не делим TCP с конкурентами) ---
    if is_weather_query(q):
        try:
            hit = await asyncio.wait_for(
                _search_weather(q, budget), timeout=budget + 0.2
            )
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            hit = None
        if isinstance(hit, WebHit) and hit.text:
            answer = compose_answer(q, hit)
            log.info("web_search: weather → %s | %s", answer[:100], q)
            return answer
        # погода упала — быстрый ответ из wiki/ddg (последняя попытка)
        # без полного бюджета: LLM-фоллбэк важнее долгого простоя

    # --- курс: приоритетный источник, как погода ---
    if _CURRENCY_RE.search(q) and not is_weather_query(q):
        try:
            hit = await asyncio.wait_for(
                _search_currency(q, budget), timeout=budget + 0.1
            )
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            hit = None
        if isinstance(hit, WebHit) and hit.text:
            answer = compose_answer(q, hit)
            log.info("web_search: currency → %s | %s", answer[:100], q)
            return answer

    sources: list[tuple[int, str, Any]] = [
        (1, "ddg", _search_ddg(q, budget)),
        (3, "wiki", _search_wikipedia(q, budget)),
    ]
    # HTML-DDG почти всегда 202-челлендж — только если остался бюджет
    if budget >= 1.5:
        sources.append((2, "ddg_html", _search_ddg_html(q, min(budget, 0.8))))

    pending: dict[asyncio.Task, tuple[int, str]] = {}
    for prio, name, coro in sources:
        pending[asyncio.create_task(coro)] = (prio, name)

    hits: list[tuple[int, WebHit]] = []
    try:
        while pending:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            done, _ = await asyncio.wait(
                pending.keys(),
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                break
            for task in done:
                meta = pending.pop(task, (9, "?"))
                try:
                    hit = task.result()
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(hit, WebHit) and hit.text:
                    hits.append((meta[0], hit))
            # первый hit (ddg=1) + короткая пауза на wiki
            if hits and min(p for p, _ in hits) <= 1:
                # дождаться ещё ≤120 мс лучших источников
                extra = min(0.12, max(0.0, deadline - loop.time()))
                if extra > 0:
                    done2, _ = await asyncio.wait(
                        pending.keys(), timeout=extra,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in done2:
                        meta = pending.pop(task, (9, "?"))
                        try:
                            hit = task.result()
                        except Exception:  # noqa: BLE001
                            continue
                        if isinstance(hit, WebHit) and hit.text:
                            hits.append((meta[0], hit))
                break
    finally:
        for task in list(pending):
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    if not hits:
        log.info("web_search: пусто за %.0f мс | %s", budget * 1000, q)
        return ""
    hits.sort(key=lambda pair: pair[0])
    hit = hits[0][1]
    answer = compose_answer(q, hit)
    log.info("web_search: %s → %s | %s", hit.source, answer[:100], q)
    return answer


async def maybe_web_search(
    query: str, *, timeout: float | None = None
) -> str:
    """Включается только для «знаниевых» фраз (см. needs_web_search)."""
    if not needs_web_search(query):
        return ""
    return await web_search_answer(query, timeout=timeout)


__all__ = [
    "WebHit",
    "compose_answer",
    "is_weather_query",
    "maybe_web_search",
    "needs_web_search",
    "web_search_answer",
    "weather_place",
]
