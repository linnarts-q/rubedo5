"""News tool (§13, stage 9.5) — ported from rubedo4's skills/news.py as
a plain tool instead of a skill-registry entry, same reasoning as
agent/tools/music.py. Fetches via Tavily, picks/summarizes the most
interesting items via the fast tier (Groq).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from config import TAVILY_API_KEY, NEWS_LOCATION, NEWS_COUNT

log = logging.getLogger("rubedo.tools.news")

_FETCH_COUNT = 10


async def _fetch_news(location: str | None = None, count: int = _FETCH_COUNT) -> list[dict]:
    loc = location or NEWS_LOCATION
    if not TAVILY_API_KEY:
        return []
    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=TAVILY_API_KEY)
        result = await asyncio.to_thread(client.search, f"latest news {loc}", max_results=count)
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", "")[:400],
            }
            for r in result.get("results", [])
        ]
    except Exception as e:
        log.warning(f"News fetch failed: {e}")
        return []


async def _pick_best(items: list[dict], count: int = 3) -> list[dict]:
    """Groq (fast tier) selects the most interesting items and writes
    a one-sentence Russian summary for each."""
    try:
        from llm.groq import chat as groq_chat
        numbered = "\n\n".join(
            f"{i}. {it['title']}\n{it['snippet']}"
            for i, it in enumerate(items, 1)
        )
        prompt = (
            f"You are given {len(items)} news items. "
            f"Select the {count} most interesting and significant ones. "
            "For each selected item write a single sentence summary in Russian (1 sentence, no fluff). "
            "Reply with a JSON array of objects: [{\"index\": <1-based>, \"summary\": \"...\"}]. "
            "No markdown, no extra text.\n\n"
            + numbered
        )
        resp = await groq_chat(
            messages=[
                {"role": "system", "content": "You are a news editor. Output only valid JSON."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"^```[a-z]*\n?|```$", "", raw, flags=re.MULTILINE).strip()
        picks = json.loads(raw)
        result = []
        for p in picks[:count]:
            idx = p.get("index", 0) - 1
            if 0 <= idx < len(items):
                result.append({**items[idx], "summary": p.get("summary", items[idx]["snippet"])})
        return result
    except Exception as e:
        log.warning(f"News pick failed: {e}, falling back to first {count}")
        return items[:count]


async def news(location: str = "") -> str:
    """Latest news, optionally for a specific location (defaults to
    config.NEWS_LOCATION)."""
    items = await _fetch_news(location=location or None, count=_FETCH_COUNT)
    if not items:
        return "Не удалось загрузить новости. Проверь TAVILY_API_KEY."

    display_count = NEWS_COUNT if NEWS_COUNT > 0 else 3
    picked = await _pick_best(items, count=display_count)

    lines = ["Последние новости:"]
    for i, item in enumerate(picked, 1):
        summary = item.get("summary") or item.get("snippet", "")
        lines.append(f"{i}. {item['title']}")
        if summary:
            lines.append(f"   {summary}")
    return "\n".join(lines)
