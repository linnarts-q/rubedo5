"""Post-verification (§4, stage 4): before trusting a session's own
"done" self-report, cross-check it against what its tool calls
actually returned.

A model that calls file_write, gets "Ошибка: ..." back, but still
tells the owner "Готово, файл записан" is exactly the kind of silent
prod-breaker the goal statement rules out ("физически не способна
молча сломать прод"). This catches the mismatch mechanically — no
extra LLM call needed, just reading the same decision journal the
reflective cycle (agent/reflect.py) already reads for actual failures.

Deliberately conservative: it never overturns the session's own status
(a session that failed a step and then genuinely recovered via a later
retry would otherwise get falsely downgraded) — it only makes sure the
owner isn't told "готово" when a step the model never acknowledged
actually broke, by appending a caveat rather than hiding it.
"""
from __future__ import annotations

import re

_FAILURE_MARKERS = re.compile(
    r"ошибк|не удалось|не получилось|failed|denied|not found|не найден",
    re.IGNORECASE,
)


def find_unacknowledged_failures(journal_entries: list[dict], final_reply: str) -> list[str]:
    """Return the tool-call journal lines that look like a failure,
    unless `final_reply` itself already reads as acknowledging trouble
    (a plain keyword check — enough to avoid double-flagging an honest
    reply, without a second LLM call to judge tone)."""
    if _FAILURE_MARKERS.search(final_reply or ""):
        return []
    return [
        e["content"] for e in journal_entries
        if e.get("kind") == "tool_call" and _FAILURE_MARKERS.search(e.get("content") or "")
    ]
