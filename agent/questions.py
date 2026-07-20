"""Session questions — the model's `ask_user` tool pausing a task
session mid-reasoning to ask the owner something before continuing
(techspec §2 phase 1).

The stored payload carries the in-flight message history (including
the placeholder tool-response agent/executor.py appends for the
ask_user call itself) plus the tool-category list that was loaded for
that run — enough to reconstruct tools_schema/tools_map and hand the
whole thing back to executor.run() once the owner answers, continuing
the same reasoning rather than starting over.

Storage, TTL, and multi-slot handling all live in agent/hanging.py
(§5, stage 4) — this module is just the "ask_user"-kind wrapper over
it, keeping the exact ask/pending/clear signatures this file has
always had.
"""
from __future__ import annotations

import logging

from agent import hanging

log = logging.getLogger("rubedo.agent.questions")

_KIND = "ask_user"
_current_id: int | None = None


def ask(
    session_id: int, question: str, history: list[dict],
    tool_categories: list[str], max_iter: int,
) -> None:
    hanging.create(
        _KIND,
        {
            "session_id": session_id,
            "question": question,
            "history": history,
            "tool_categories": tool_categories,
            "max_iter": max_iter,
        },
        task_session_id=session_id,
    )


def pending() -> dict | None:
    """Return the pending question payload, or None if there isn't one
    or it went stale past APPROVAL_TTL_HOURS (same TTL knob approval.py
    uses — no separate config knob for this yet)."""
    global _current_id
    p = hanging.pending(_KIND)
    _current_id = p.pop("_hanging_id") if p else None
    return p


def clear() -> None:
    global _current_id
    if _current_id is not None:
        hanging.resolve(_current_id, "answered")
        _current_id = None
