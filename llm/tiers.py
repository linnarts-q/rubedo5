"""LLM-tier discipline (stage 3): which provider each kind of call goes
through, and what happens when the expected one is fully down.

Two tiers already exist by convention (config.py's own comments call
them out: "Основной мозг" / OpenRouter for user-facing generation and
personality, "Аналитический мозг" / Groq for classification, planning,
summarization, reflection). Every call site already picks the right
one — that part of "discipline" was already being followed by hand.

What was missing: the generation tier (agent/executor.py, the one
that actually produces what the owner reads) had no fallback at all.
If OpenRouter's free-tier keys were all rate-limited, the whole turn
died with "Все API-ключи на лимите" — a complete communication
blackout even though Groq might still have capacity. The analytical
tier doesn't have this problem: agent/classifier.py, agent/planner.py,
and agent/reflect.py each already catch AllKeysExhausted and fall back
to a sane default (route="simple", a single-step plan, a no-retry
verdict) rather than going silent, so they're left as direct
llm.groq.chat callers — nothing to fix there.

`generation_chat` is the fix: try OpenRouter first (as normal), and on
AllKeysExhausted, fall back once to Groq so there's still SOME reply —
accepting a personality/quality hit — rather than total silence. Only
raises AllKeysExhausted itself if both providers are actually down.
"""
from __future__ import annotations

import logging

from llm import openrouter, groq
from llm.exceptions import AllKeysExhausted

log = logging.getLogger("rubedo.llm.tiers")


async def generation_chat(
    messages: list,
    tools: list | None = None,
    temperature: float = 0.7,
    **kwargs,
):
    try:
        return await openrouter.chat(messages, tools=tools, temperature=temperature, **kwargs)
    except AllKeysExhausted as e:
        log.warning(f"OpenRouter (generation tier) exhausted, falling back to Groq: {e}")
        try:
            return await groq.chat(messages, tools=tools, temperature=temperature, **kwargs)
        except AllKeysExhausted:
            raise
