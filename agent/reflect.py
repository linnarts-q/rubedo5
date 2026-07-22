"""Reflective cycle (§3, stage 3): when a task session ends in failure,
don't just report "something went wrong" — look at what was actually
tried (the session's decision journal) and decide, once, whether a
different approach would fix it right now, or whether the honest
answer is a specific diagnosis instead of a generic error string. This
is the concrete mechanism behind the goal's "доводит задачи до конца
через анализ собственных ошибок": one corrective retry informed by
what just happened, not silent looping and not a shrug.

Phase 1 scope: exactly one reflection per session, on natural failures
only (loop detected, max-iterations exhausted, an uncaught exception,
LLM exhaustion) — not on owner-cancelled approvals or a stale TTL,
which aren't "the task went wrong", they're "the task never got a
chance". agent/controller.py is the caller; it also guards against
re-reflecting on an already-reflected session (a session is only ever
created fresh per "deep" turn today, so this is a defensive backstop,
not something that fires in practice yet).
"""
from __future__ import annotations

import json
import logging

from llm.groq import chat as groq_chat
from agent.prompts import build_analytics_system
from config import now_local

log = logging.getLogger("rubedo.agent.reflect")

_REFLECT_PROMPT = """Задача провалилась. Журнал решений по ходу выполнения:
{journal}

Причина провала: {error}

Разберись честно: это можно исправить другим подходом прямо сейчас, или дело \
в чём-то, что не зависит от повторной попытки (не хватает доступа, данных, \
прав, или сама задача невыполнима как поставлена)?

Отвечай строго JSON, без markdown:
{{"retry": true/false, "corrected_approach": "конкретно что сделать иначе, если retry=true, иначе null", "diagnosis": "короткий честный итог для хозяина, 1-2 предложения, без вежливых оговорок"}}
"""


async def reflect_on_failure(journal_entries: list[dict], error: str) -> dict:
    """Returns {"retry": bool, "corrected_approach": str|None, "diagnosis": str}.
    Falls back to a no-retry verdict carrying the raw error as the
    diagnosis if the reflection call itself fails — reflection is a
    best-effort improvement on the failure report, never a new way for
    a turn to blow up."""
    journal_text = "\n".join(
        f"[{e['kind']}] {e['content'][:200]}" for e in journal_entries
    ) or "(пусто)"
    dt = now_local().strftime("%d.%m.%Y %H:%M")
    prompt = _REFLECT_PROMPT.format(journal=journal_text, error=error)
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
        return {
            "retry": bool(result.get("retry", False)),
            "corrected_approach": result.get("corrected_approach") or None,
            "diagnosis": result.get("diagnosis") or error or "Не получилось, причина неясна.",
        }
    except Exception as e:
        log.warning(f"Reflection failed: {e}")
        return {"retry": False, "corrected_approach": None, "diagnosis": error or "Не получилось."}
