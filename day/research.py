from __future__ import annotations
import json
import logging
import re
import urllib.parse
import urllib.request
from config import TAVILY_API_KEY

_CITATION_RE = re.compile(r"web_search[†\^]\S*", re.IGNORECASE)

log = logging.getLogger("rubedo.day.research")

_RESEARCH_KEYWORDS = {
    "написать", "напиши", "изучить", "изучи", "подготовить", "подготовь",
    "разобраться", "разберись", "найти", "найди", "исследовать", "исследуй",
    "составить", "составь", "проанализировать", "проанализируй",
    "узнать", "узнай", "собрать", "собери", "сделать обзор",
    "write", "research", "find", "prepare", "analyze", "study",
}

_DEDUP_PREFIX = "research:"


def _network_available() -> bool:
    import socket
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=2)
        return True
    except OSError:
        return False


def needs_research(title: str, description: str = "") -> bool:
    """Heuristic: does this task benefit from web research?"""
    text = (title + " " + description).lower()
    return any(kw in text for kw in _RESEARCH_KEYWORDS) or len(description) > 40


async def research_for_task(
    title: str, description: str = "", session_id: str = "lin"
) -> str:
    """
    Search web for task-relevant info, synthesize with LLM, save to memory.
    Idempotent — skips if research for this task already exists.
    Returns a brief summary of findings.
    """
    from memory.db import search_events, save_event

    dedup_key = f"{_DEDUP_PREFIX}{title[:50]}"
    existing = search_events(dedup_key, min_priority=3, limit=1)
    if existing and dedup_key in existing[0].get("content", ""):
        log.debug(f"Research already done for: {title}")
        return "Материалы по этой задаче уже есть в памяти."

    import asyncio as _aio
    network_ok = await _aio.to_thread(_network_available)
    if not network_ok:
        log.info(f"Research auto-paused (no network): {title[:40]}")
        return ""

    queries = await _build_queries(title, description)
    if not queries:
        return "Не удалось построить поисковые запросы."

    snippets: list[str] = []
    for q in queries[:3]:
        try:
            result = _run_search(q)
            if result and "не нашлось" not in result and "недоступен" not in result:
                snippets.append(f"[{q}]\n{result[:600]}")
        except Exception as e:
            log.warning(f"Research search failed for '{q}': {e}")

    if not snippets:
        return "Не смогла найти релевантных материалов."

    summary = await _synthesize(title, snippets)

    save_event(
        session_id=session_id,
        content=f"{dedup_key}\n{summary}",
        priority=4,
        category="research",
    )
    log.info(f"Research saved for task '{title}'")
    return summary


async def _build_queries(title: str, description: str) -> list[str]:
    from datetime import datetime
    from llm.groq import chat as groq_chat
    from agent.prompts import build_analytics_system

    dt = datetime.now().strftime("%d.%m.%Y %H:%M")
    prompt = (
        f"Задача: «{title}»\n"
        f"Описание: {description or 'нет'}\n\n"
        "Составь 2-3 конкретных поисковых запроса для сбора полезной информации "
        "по этой задаче. Верни JSON-массив строк. Только JSON, без markdown.\n"
        'Пример: ["запрос 1", "запрос 2"]'
    )
    try:
        resp = await groq_chat(
            [
                {"role": "system", "content": build_analytics_system(dt)},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        result = json.loads(raw)
        return [q for q in result if isinstance(q, str) and q.strip()][:3]
    except Exception as e:
        log.warning(f"Query building failed: {e}")
        return [title]


def _run_search(query: str) -> str:
    if TAVILY_API_KEY:
        try:
            from tavily import TavilyClient
            client = TavilyClient(api_key=TAVILY_API_KEY)
            result = client.search(query, max_results=3)
            parts = []
            for r in result.get("results", []):
                title = r.get("title", "")
                content = r.get("content", "")[:300]
                parts.append(f"{title}: {content}")
            return "\n".join(parts) if parts else "Результатов не нашлось."
        except Exception as e:
            log.warning(f"Tavily failed: {e}")
    try:
        encoded = urllib.parse.quote(query)
        url = (
            f"https://api.duckduckgo.com/?q={encoded}"
            f"&format=json&no_html=1&skip_disambig=1&no_redirect=1"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        parts = []
        if data.get("AbstractText"):
            parts.append(data["AbstractText"][:400])
        for topic in data.get("RelatedTopics", [])[:2]:
            if isinstance(topic, dict) and topic.get("Text"):
                parts.append(topic["Text"][:200])
        return "\n".join(parts) if parts else "Результатов не нашлось."
    except Exception as e:
        return f"Поиск недоступен: {e}"


async def _synthesize(title: str, snippets: list[str]) -> str:
    from datetime import datetime
    from llm.groq import chat as groq_chat
    from agent.prompts import build_analytics_system

    dt = datetime.now().strftime("%d.%m.%Y %H:%M")
    combined = "\n\n".join(snippets)
    prompt = (
        f"Задача: «{title}»\n\n"
        f"Найденные материалы:\n{combined[:2000]}\n\n"
        "Выдели самое важное и полезное для выполнения этой задачи. "
        "3-5 предложений на русском. Конкретно, без воды."
    )
    try:
        resp = await groq_chat(
            [
                {"role": "system", "content": build_analytics_system(dt)},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
        )
        result = resp.choices[0].message.content.strip()
        result = _CITATION_RE.sub("", result).strip()
        return result
    except Exception as e:
        log.warning(f"Synthesis failed: {e}")
        return snippets[0][:500] if snippets else ""


async def _reflect_and_question(title: str, snippets: list[str]) -> list[str]:
    """Identify research gaps in collected snippets, return follow-up search queries."""
    from datetime import datetime
    from llm.groq import chat as groq_chat
    from agent.prompts import build_analytics_system

    dt = datetime.now().strftime("%d.%m.%Y %H:%M")
    combined = "\n\n".join(snippets)[:2500]
    prompt = (
        f"Тема для исследования: «{title}»\n\n"
        f"Уже найдено:\n{combined}\n\n"
        "Что важного по этой теме не раскрыто или неясно из найденных материалов? "
        "Составь 2-3 конкретных поисковых запроса для восполнения пробелов. "
        "Верни ТОЛЬКО JSON-массив строк. Только JSON, без markdown.\n"
        'Пример: ["запрос 1", "запрос 2"]'
    )
    try:
        resp = await groq_chat(
            [{"role": "system", "content": build_analytics_system(dt)},
             {"role": "user", "content": prompt}],
            temperature=0.3,
        )
        raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        result = json.loads(raw)
        return [q for q in result if isinstance(q, str) and q.strip()][:3]
    except Exception as e:
        log.warning(f"Research reflection failed: {e}")
        return []


async def _deep_synthesize(title: str, description: str, snippets: list[str]) -> str:
    """Synthesize all collected material into a structured actionable summary."""
    from datetime import datetime
    from llm.groq import chat as groq_chat
    from agent.prompts import build_analytics_system

    dt = datetime.now().strftime("%d.%m.%Y %H:%M")
    combined = "\n\n".join(snippets)[:4000]
    prompt = (
        f"Задача из бэклога: «{title}»\n"
        + (f"Контекст: {description}\n" if description else "")
        + f"\nСобранные материалы:\n{combined}\n\n"
        "Составь структурированный обзор, который поможет выполнить эту задачу. "
        "Включи:\n"
        "— ключевые факты и выводы\n"
        "— конкретные шаги или рекомендации\n"
        "— важные нюансы или риски\n\n"
        "На русском. Конкретно, без воды. 6-10 предложений."
    )
    try:
        resp = await groq_chat(
            [{"role": "system", "content": build_analytics_system(dt)},
             {"role": "user", "content": prompt}],
            temperature=0.3,
        )
        result = resp.choices[0].message.content.strip()
        result = _CITATION_RE.sub("", result).strip()
        return result
    except Exception as e:
        log.warning(f"Deep synthesis failed: {e}")
        return ""


async def deep_research_for_task(
    title: str, description: str = "", session_id: str = "lin"
) -> str:
    """Multi-round deep research: search → reflect on gaps → follow-up search → synthesize.

    Unlike research_for_task(), this always refreshes (no dedup skip) and
    produces a richer structured output. Intended for periodic pool-task research.
    """
    import asyncio as _aio
    from memory.db import save_event

    if not await _aio.to_thread(_network_available):
        log.info(f"Deep research auto-paused (no network): {title[:40]}")
        return ""

    log.info(f"Deep research starting: '{title[:50]}'")

    # Phase 1: initial queries + searches
    queries = await _build_queries(title, description)
    snippets: list[str] = []
    for q in queries[:3]:
        try:
            result = _run_search(q)
            if result and "не нашлось" not in result and "недоступен" not in result:
                snippets.append(f"[{q}]\n{result[:600]}")
        except Exception as e:
            log.warning(f"Deep research p1 search failed '{q}': {e}")

    if not snippets:
        log.info(f"Deep research: no initial results for '{title[:40]}'")
        return ""

    # Phase 2: reflection — what gaps remain?
    followup_queries = await _reflect_and_question(title, snippets)

    # Phase 3: follow-up searches for identified gaps
    for q in followup_queries:
        try:
            result = _run_search(q)
            if result and "не нашлось" not in result and "недоступен" not in result:
                snippets.append(f"[{q}]\n{result[:500]}")
        except Exception as e:
            log.warning(f"Deep research p3 search failed '{q}': {e}")

    # Phase 4: deep synthesis of all collected material
    summary = await _deep_synthesize(title, description, snippets)
    if not summary:
        return ""

    dedup_key = f"{_DEDUP_PREFIX}{title[:50]}"
    save_event(
        session_id=session_id,
        content=f"{dedup_key}\n{summary}",
        priority=5,
        category="research",
    )
    log.info(f"Deep research saved for '{title[:50]}'")
    return summary
