"""Global LLM-call semaphore (§2 phase 2, day-engine 5.0 parallelism —
rollout step 1, deliberately built and proven at MAX_CONCURRENT=1
before session parallelism itself is turned on).

Session parallelism is parallelism of WAITING (shell, browser, timers,
the owner) — never parallelism of THINKING. Free-tier Groq/OpenRouter
rate limits are shared across the whole agent process; two sessions
calling an LLM at the same instant would just double the 429 rate, not
actually think faster. One global semaphore, FIFO (asyncio.Semaphore's
own waiter order), removes this class of problem outright rather than
trying to be clever about per-provider or per-key throttling.
"""
from __future__ import annotations

import asyncio

llm_semaphore = asyncio.Semaphore(1)
