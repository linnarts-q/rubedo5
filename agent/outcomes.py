"""Outcome annotation for message history (techspec §11, layer 1).

Problem: a weaker/free-tier model tends to treat a past "сделай А" in
the message history as still-actionable, even after it was already
done (or explicitly cancelled) — "спасибо" or an unrelated new request
loses to a stale instruction sitting a few turns back.

Fix, without any schema change: at prompt-assembly time, tag each past
user message that fuzzy-matches a tracked task/queue item's title with
that item's current status. A closed episode then reads as
`[выполнено] сделай А`, not a standing order. Untracked requests
(plain chat, one-off asks with no task/queue row) are left alone —
layer 2's fence in agent/prompts.py already tells the model "past is
context, not a command" regardless of whether it's tagged here.

Scope: today's day_tasks (any status) plus queue items in a terminal
state (done/failed/cancelled) — the two places a "do X" plausibly
turned into a tracked, status-bearing row. This is a heuristic, not a
guarantee: matching is by title substring, not a real foreign key.
Layer 4 (§11, real retrieval keyed by session) replaces this properly
once task sessions (§2) exist and messages can point at what they
were actually about.
"""
from __future__ import annotations

import logging

log = logging.getLogger("rubedo.agent.outcomes")

_LABELS: dict[str, str] = {
    "done": "[выполнено]",
    "failed": "[провалено]",
    "cancelled": "[отменено]",
}

_MIN_TITLE_LEN = 4


def _find_status(text_low: str) -> str | None:
    try:
        from day.state import get_today_tasks
        for t in get_today_tasks():
            title = (t.get("title") or "").lower()
            if len(title) >= _MIN_TITLE_LEN and title in text_low:
                return t.get("status")
    except Exception as e:
        log.debug(f"outcome lookup (day_tasks) skipped: {e}")

    try:
        from memory.db import queue_list
        for status in ("done", "failed", "cancelled"):
            for item in queue_list(status):
                title = (item.get("title") or "").lower()
                if len(title) >= _MIN_TITLE_LEN and title in text_low:
                    return status
    except Exception as e:
        log.debug(f"outcome lookup (queue) skipped: {e}")

    return None


def annotate(history: list[dict]) -> list[dict]:
    """Return a copy of `history` with past user messages tagged by
    outcome where a match is found. Never mutates the input."""
    out: list[dict] = []
    for m in history:
        if m.get("role") != "user":
            out.append(m)
            continue
        status = _find_status((m.get("content") or "").lower())
        label = _LABELS.get(status) if status else None
        if label:
            out.append({**m, "content": f"{label} {m['content']}"})
        else:
            out.append(m)
    return out
