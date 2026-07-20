"""Task sessions (techspec §2, phase 1: pause/sequential + decision journal).

A task session ties together the arc of one piece of ongoing work — a
"deep", multi-step request today; an autonomous queue task once that
runner exists — across possibly more than one agent/executor.py run.
It exists for two reasons:

1. Pause/resume. A task can be interrupted mid-thought — an approval
   gate waiting on the owner's yes/no (agent/approval.py), or the model
   itself asking a clarifying question via the `ask_user` tool
   (agent/tools/sessions.py) — without losing where it was. Phase 1 is
   deliberately *sequential*, not parallel (real concurrency across
   sessions is stage 7's job): at most one session is 'active'
   system-wide. Starting a new one pauses whatever was active rather
   than interleaving silently.

2. The decision journal (session_decisions in memory/db.py) is the
   retrievable trail of what happened and why during a session — tool
   calls, the model's own `plan`/`report` notes, pauses, resumes.

3. Experience revival (§9, stage 3): every session that reaches a real
   verdict — done or failed, not merely cancelled — condenses into one
   row in the long-dead `experience` table (ported from rubedo4,
   unused until now): what was attempted, which tools were used, in
   what order, and how it turned out. agent/controller.py looks this
   up (memory.db.search_experience, pg_trgm similarity) before a new
   "deep" task starts and surfaces close matches as context — "how did
   something like this go last time" — which is the concrete substrate
   the goal's "analyzes its own mistakes" needs, and the retrieval half
   of memory layer 4 (§11): real search over structured past attempts,
   not fuzzy title-matching against ephemeral chat history.
"""
from __future__ import annotations

import logging

from memory.db import (
    session_create, session_get, session_get_active, session_set_status,
    session_list as _session_list, session_log, session_journal as _session_journal,
    save_experience,
)

log = logging.getLogger("rubedo.agent.sessions")

_TERMINAL = {"done", "failed", "cancelled"}


def _revive_experience(session_id: int, success: bool) -> None:
    """Condense a finished session's journal into one `experience` row.
    Best-effort: a save failure here must never break the session
    lifecycle transition that triggered it."""
    try:
        s = session_get(session_id)
        if not s:
            return
        entries = _session_journal(session_id)
        chain = " → ".join(
            e["content"].split(" -> ", 1)[0] for e in entries if e["kind"] == "tool_call"
        )
        outcome = (s.get("result") or s.get("error") or "")[:300]
        save_experience(s["title"], chain, outcome, success=success)
    except Exception as e:
        log.debug(f"experience revival skipped for session #{session_id}: {e}")


def start(title: str, origin: str = "chat") -> dict:
    """Start a new task session. If one is already active, pause it
    first — phase 1 is sequential, only one session runs at a time."""
    current = session_get_active()
    if current:
        log.info(f"Pausing session #{current['id']} ({current['title']!r}) to start {title!r}")
        pause(current["id"], reason=f"вытеснена новой задачей: {title}")
    sid = session_create(title, origin=origin)
    session_log(sid, "start", title)
    return session_get(sid)


def pause(session_id: int | None, reason: str = "") -> None:
    if session_id is None:
        return
    session_set_status(session_id, "paused")
    session_log(session_id, "pause", reason or "приостановлена")


def resume(session_id: int | None) -> dict | None:
    """Resume a paused session. No-op (returns None) if there's no
    session, or it's not actually paused — already-terminal or
    already-active sessions have nothing to resume."""
    if session_id is None:
        return None
    s = session_get(session_id)
    if not s or s["status"] != "paused":
        return None
    session_set_status(session_id, "active")
    session_log(session_id, "resume", "возобновлена")
    return session_get(session_id)


def complete(session_id: int | None, result: str = "") -> None:
    if session_id is None:
        return
    session_set_status(session_id, "done", result=result)
    session_log(session_id, "complete", result or "готово")
    _revive_experience(session_id, success=True)


def fail(session_id: int | None, error: str) -> None:
    if session_id is None:
        return
    session_set_status(session_id, "failed", error=error)
    session_log(session_id, "fail", error)
    _revive_experience(session_id, success=False)


def cancel(session_id: int | None, reason: str = "") -> None:
    if session_id is None:
        return
    session_set_status(session_id, "cancelled", result=reason)
    session_log(session_id, "cancel", reason or "отменена")


def log_decision(session_id: int | None, kind: str, content: str) -> None:
    """No-op if session_id is None, so call sites don't need to guard
    every call with `if session:` — most tool-call logging happens
    unconditionally in agent/executor.py regardless of whether a
    session is active for this turn."""
    if session_id is None:
        return
    try:
        session_log(session_id, kind, content)
    except Exception as e:
        log.debug(f"log_decision skipped: {e}")


def active() -> dict | None:
    return session_get_active()


def get(session_id: int) -> dict | None:
    return session_get(session_id)


def journal(session_id: int) -> list[dict]:
    return _session_journal(session_id)


def list_sessions(status: str | None = None, limit: int = 20) -> list[dict]:
    return _session_list(status, limit)
