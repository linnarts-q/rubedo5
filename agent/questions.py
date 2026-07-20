"""Session questions — the model's `ask_user` tool pausing a task
session mid-reasoning to ask the owner something before continuing
(techspec §2 phase 1).

Precursor to the full "hanging question" entity (§5), the same way
agent/approval.py is a precursor to it for yellow/red tool calls — this
covers exactly one case (a task session waiting on a free-text answer)
and is TTL-armed like every other meta-based intercept in
agent/controller.py, so a reply hours later doesn't silently resume a
dead task with stale context.

The stored payload carries the in-flight message history (including
the placeholder tool-response agent/executor.py appends for the
ask_user call itself) plus the tool-category list that was loaded for
that run — enough to reconstruct tools_schema/tools_map and hand the
whole thing back to executor.run() once the owner answers, continuing
the same reasoning rather than starting over.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from config import APPROVAL_TTL_HOURS

log = logging.getLogger("rubedo.agent.questions")

_META_PENDING = "pending_session_question"
_META_ARMED_AT = "pending_session_question_armed_at"


def ask(
    session_id: int, question: str, history: list[dict],
    tool_categories: list[str], max_iter: int,
) -> None:
    from memory.db import save_meta
    save_meta(_META_PENDING, json.dumps({
        "session_id": session_id,
        "question": question,
        "history": history,
        "tool_categories": tool_categories,
        "max_iter": max_iter,
    }))
    save_meta(_META_ARMED_AT, datetime.now().isoformat())


def pending() -> dict | None:
    """Return the pending question payload, or None if there isn't one
    or it went stale past APPROVAL_TTL_HOURS (same TTL knob approval.py
    uses — no separate config knob for this yet)."""
    from memory.db import load_meta

    raw = load_meta(_META_PENDING)
    if not raw:
        return None
    armed = load_meta(_META_ARMED_AT) or ""
    try:
        armed_dt = datetime.fromisoformat(armed)
    except ValueError:
        clear()
        return None
    if (datetime.now() - armed_dt).total_seconds() > APPROVAL_TTL_HOURS * 3600:
        log.info("Pending session question expired (TTL), clearing")
        _fail_orphaned_session(raw)
        clear()
        return None
    try:
        return json.loads(raw)
    except Exception:
        clear()
        return None


def _fail_orphaned_session(raw: str) -> None:
    """A paused task session whose ask_user question went unanswered
    past the TTL has nothing left to wait for — mark it failed rather
    than leaving it 'paused' forever. Best-effort, same as approval.py's
    twin of this helper."""
    try:
        sid = json.loads(raw).get("session_id")
        if sid is not None:
            from agent import sessions
            sessions.fail(sid, "вопрос остался без ответа (TTL)")
    except Exception as e:
        log.debug(f"orphaned-session fail skipped: {e}")


def clear() -> None:
    from memory.db import save_meta
    save_meta(_META_PENDING, "")
    save_meta(_META_ARMED_AT, "")
