"""Web-flavored tools — search, weather, navigation, math.

Pure functions: no agent.tools module-level state (`_session_id`,
`_send_*_fn`, etc.) referenced here. Safe to extract.
"""
from __future__ import annotations

import logging
import urllib.parse
import urllib.request

log = logging.getLogger("rubedo.tools.web")


def _duckduckgo_search(query: str) -> str:
    try:
        encoded = urllib.parse.quote(query)
        url = (
            f"https://api.duckduckgo.com/?q={encoded}"
            f"&format=json&no_html=1&skip_disambig=1&no_redirect=1"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            import json as _j
            data = _j.loads(r.read())
        parts = []
        if data.get("AbstractText"):
            parts.append(data["AbstractText"][:500])
        for topic in data.get("RelatedTopics", [])[:3]:
            if isinstance(topic, dict) and topic.get("Text"):
                parts.append(topic["Text"][:200])
        return "\n\n".join(parts) if parts else "Результатов не нашлось."
    except Exception as e:
        return f"Поиск недоступен: {e}"


def web_search(query: str) -> str:
    from config import TAVILY_API_KEY
    if TAVILY_API_KEY:
        try:
            from tavily import TavilyClient
            client = TavilyClient(api_key=TAVILY_API_KEY)
            result = client.search(query, max_results=5)
            parts = []
            for r in result.get("results", []):
                title = r.get("title", "")
                content = r.get("content", "")[:400]
                parts.append(f"**{title}**\n{content}")
            return "\n\n".join(parts) if parts else "Результатов не нашлось."
        except Exception as e:
            log.warning(f"[tavily] {e}, fallback to DuckDuckGo")
    return _duckduckgo_search(query)


def navigate(destination: str, origin: str = "") -> str:
    from config import HOME_ADDRESS, DEFAULT_CITY
    origin = origin.strip() or HOME_ADDRESS or DEFAULT_CITY
    maps_url = (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={urllib.parse.quote(origin)}"
        f"&destination={urllib.parse.quote(destination)}"
        "&travelmode=transit"
    )
    search_result = web_search(f"маршрут из {origin} до {destination} {DEFAULT_CITY} транспорт время")
    return f"Маршрут: {origin} → {destination}\n{search_result}\n\nGoogle Maps: {maps_url}"


def calculate(expression: str) -> str:
    """Safe math eval. Falls back to web_search on unknown identifiers
    or on currency-related queries."""
    import ast
    import math
    import operator as op

    _safe_ops = {
        ast.Add: op.add, ast.Sub: op.sub, ast.Mul: op.mul,
        ast.Div: op.truediv, ast.Pow: op.pow, ast.USub: op.neg,
        ast.UAdd: op.pos, ast.Mod: op.mod,
    }
    _safe_names = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}

    def _eval(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BinOp):
            return _safe_ops[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp):
            return _safe_ops[type(node.op)](_eval(node.operand))
        if isinstance(node, ast.Name) and node.id in _safe_names:
            return _safe_names[node.id]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _safe_names:
            return _safe_names[node.func.id](*[_eval(a) for a in node.args])
        raise ValueError(f"Unsupported: {ast.dump(node)}")

    currency_keywords = ["usd", "eur", "gbp", "uah", "pln", "курс", "валют", "convert"]
    if any(kw in expression.lower() for kw in currency_keywords):
        return web_search(expression)
    try:
        result = _eval(ast.parse(expression, mode="eval").body)
        return f"{expression} = {result}"
    except Exception:
        return web_search(expression)


_WMO_CODES = {
    0: "Ясно", 1: "Преим. ясно", 2: "Перем. облачность", 3: "Пасмурно",
    45: "Туман", 48: "Изморозь",
    51: "Лёгкая морось", 53: "Морось", 55: "Густая морось",
    61: "Слабый дождь", 63: "Дождь", 65: "Сильный дождь",
    71: "Слабый снег", 73: "Снег", 75: "Сильный снег",
    77: "Снежная крупа",
    80: "Ливень", 81: "Ливни", 82: "Сильный ливень",
    85: "Снегопад", 86: "Сильный снегопад",
    95: "Гроза", 96: "Гроза с градом", 99: "Гроза, крупный град",
}
_WDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
_GEO_CACHE: dict[str, tuple[float, float]] = {}

_FALLBACK_COORDS: dict[str, tuple[float, float]] = {
    "dublin": (53.3498, -6.2603),
    "london": (51.5074, -0.1278),
    "kyiv": (50.4501, 30.5234),
    "kiev": (50.4501, 30.5234),
    "paris": (48.8566, 2.3522),
    "berlin": (52.5200, 13.4050),
    "moscow": (55.7558, 37.6173),
    "new york": (40.7128, -74.0060),
    "amsterdam": (52.3676, 4.9041),
    "warsaw": (52.2297, 21.0122),
}


def _geocode(city: str) -> tuple[float, float] | None:
    import json as _j
    if city in _GEO_CACHE:
        return _GEO_CACHE[city]

    city_key = city.lower().strip()

    # Persistent DB cache (survives restarts)
    try:
        from memory.db import load_meta
        cached = load_meta(f"geocode_{city_key}")
        if cached:
            lat, lon = map(float, cached.split(","))
            _GEO_CACHE[city] = (lat, lon)
            return lat, lon
    except Exception:
        pass

    try:
        q = urllib.parse.quote(city)
        url = f"https://geocoding-api.open-meteo.com/v1/search?name={q}&count=1&language=en&format=json"
        req = urllib.request.Request(url, headers={"User-Agent": "rubedo/5"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = _j.loads(r.read())
        results = data.get("results") or []
        if not results:
            return _FALLBACK_COORDS.get(city_key)
        lat = results[0]["latitude"]
        lon = results[0]["longitude"]
        _GEO_CACHE[city] = (lat, lon)
        try:
            from memory.db import save_meta
            save_meta(f"geocode_{city_key}", f"{lat},{lon}")
        except Exception:
            pass
        return lat, lon
    except Exception as e:
        log.warning(f"geocode failed for {city!r}: {type(e).__name__}: {e}")
        return _FALLBACK_COORDS.get(city_key)


def _fmt_day(date_str: str, tmin: float, tmax: float, wmo: int, precip: float) -> str:
    from datetime import datetime as _dt
    d = _dt.strptime(date_str, "%Y-%m-%d")
    wd = _WDAYS[d.weekday()]
    desc = _WMO_CODES.get(wmo, f"код {wmo}")
    s = f"+{int(tmax)}" if tmax >= 0 else str(int(tmax))
    n = f"+{int(tmin)}" if tmin >= 0 else str(int(tmin))
    line = f"{wd} {d.day:02d}.{d.month:02d}: {n}…{s}°C, {desc}"
    if precip >= 1.0:
        line += f", {precip:.1f} мм"
    return line


def get_weather(city: str = "", days: int = 3, date_ref: str = "") -> str:
    """Fetch weather for any date range.

    date_ref examples: "today", "yesterday", "2 days ago",
    "2026-05-20", "last 5 days", "next 7 days".
    Omit date_ref for a forecast starting today.
    """
    import json as _j
    from datetime import date as _date, timedelta as _td
    from config import DEFAULT_CITY

    city = (city or "").strip() or DEFAULT_CITY
    today = _date.today()

    # Parse date_ref into (start, end) dates
    date_ref = (date_ref or "").strip().lower()
    try:
        days_n = max(1, min(16, int(days)))
    except (TypeError, ValueError):
        days_n = 3

    if not date_ref or date_ref in ("today", "сегодня", "forecast"):
        start = today
        end = today + _td(days=days_n - 1)
    elif date_ref in ("yesterday", "вчера"):
        start = end = today - _td(days=1)
    elif "days ago" in date_ref or "дней назад" in date_ref or "день назад" in date_ref:
        import re as _re
        m = _re.search(r"(\d+)", date_ref)
        n = int(m.group(1)) if m else 1
        start = end = today - _td(days=n)
    elif "last" in date_ref or "последн" in date_ref:
        import re as _re
        m = _re.search(r"(\d+)", date_ref)
        n = int(m.group(1)) if m else days_n
        start = today - _td(days=n - 1)
        end = today
    elif "next" in date_ref or "следующ" in date_ref:
        import re as _re
        m = _re.search(r"(\d+)", date_ref)
        n = int(m.group(1)) if m else days_n
        start = today + _td(days=1)
        end = today + _td(days=n)
    else:
        # Try ISO date
        try:
            from datetime import datetime as _dt
            d = _dt.strptime(date_ref[:10], "%Y-%m-%d").date()
            start = end = d
        except Exception:
            start = today
            end = today + _td(days=days_n - 1)

    lat_lon = _geocode(city)
    if not lat_lon:
        return f"Не смогла определить координаты для «{city}» — возможно нет сети или город написан неверно."
    lat, lon = lat_lon

    start_str = start.isoformat()
    end_str = end.isoformat()

    # Use historical API if start is in the past, forecast if future
    if start <= today:
        # Historical (or today included in forecast)
        if end <= today:
            # Pure historical
            url = (
                f"https://archive-api.open-meteo.com/v1/archive"
                f"?latitude={lat}&longitude={lon}"
                f"&start_date={start_str}&end_date={end_str}"
                f"&daily=temperature_2m_max,temperature_2m_min,weathercode,precipitation_sum"
                f"&timezone=auto"
            )
        else:
            # Spans today: use forecast API (covers -2 days to +16)
            url = (
                f"https://api.open-meteo.com/v1/forecast"
                f"?latitude={lat}&longitude={lon}"
                f"&start_date={start_str}&end_date={end_str}"
                f"&daily=temperature_2m_max,temperature_2m_min,weathercode,precipitation_sum"
                f"&timezone=auto"
            )
    else:
        # Pure future forecast
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            f"&start_date={start_str}&end_date={end_str}"
            f"&daily=temperature_2m_max,temperature_2m_min,weathercode,precipitation_sum"
            f"&timezone=auto"
        )

    import socket as _socket
    import time as _time
    _last_exc = None
    data = None
    for _attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "rubedo/5"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = _j.loads(r.read())
            break
        except Exception as e:
            _last_exc = e
            if _attempt == 0:
                log.warning(f"Weather API attempt 1 failed ({type(e).__name__}), retrying in 3s")
                _time.sleep(3)
    if data is None:
        e = _last_exc
        err_type = type(e).__name__
        err_msg = str(e).strip() or "(нет деталей)"
        if isinstance(e, TimeoutError) or "timed out" in err_msg.lower():
            hint = "таймаут — сервер не ответил вовремя"
        elif isinstance(e, (OSError, _socket.gaierror)) or "name or service not known" in err_msg.lower() or "network" in err_msg.lower():
            hint = "нет сети или DNS не резолвится"
        elif "HTTP Error" in err_type or "HTTPError" in err_type:
            hint = "HTTP ошибка от сервера"
        else:
            hint = "неизвестная ошибка"
        return f"Погода недоступна ({hint}): {err_type}: {err_msg}"

    try:
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        tmax_list = daily.get("temperature_2m_max", [])
        tmin_list = daily.get("temperature_2m_min", [])
        wmo_list = daily.get("weathercode", [])
        precip_list = daily.get("precipitation_sum", [])

        if not dates:
            return "Нет данных по погоде за этот период."

        header = f"Погода — {city}"
        if start == end:
            from datetime import datetime as _dt
            d = _dt.strptime(dates[0], "%Y-%m-%d")
            header += f", {d.day:02d}.{d.month:02d}.{d.year}"
        else:
            header += f", {start_str} — {end_str}"
        lines = [header + ":"]
        for i, ds in enumerate(dates):
            tmax = tmax_list[i] if i < len(tmax_list) else 0
            tmin = tmin_list[i] if i < len(tmin_list) else 0
            wmo = int(wmo_list[i]) if i < len(wmo_list) else 0
            precip = float(precip_list[i]) if i < len(precip_list) and precip_list[i] is not None else 0.0
            lines.append(_fmt_day(ds, tmin, tmax, wmo, precip))
        return "\n".join(lines)
    except Exception as e:
        return f"Не смогла разобрать ответ погоды ({type(e).__name__}): {e}"
