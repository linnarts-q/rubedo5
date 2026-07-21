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

Every write to the decision journal or the experience table goes
through memory.writer's single-writer lock (§2 phase 2 parallelism,
rollout step 1) rather than calling memory.db directly — proven at
MAX_CONCURRENT=1, before session parallelism itself exists, so a
future second concurrent session can't race on these writes by
construction.
"""
from __future__ import annotations

import contextvars
import logging

from memory.db import (
    session_create, session_get, session_get_active, session_set_status,
    session_list as _session_list, session_log, session_journal as _session_journal,
    save_experience,
)
from memory.writer import write as _writer_write

log = logging.getLogger("rubedo.agent.sessions")

_TERMINAL = {"done", "failed", "cancelled"}

# Which task session the *currently executing tool call* belongs to
# (§2 phase 2 parallelism). agent/executor.py sets this for the
# duration of one run() call; asyncio Tasks each get their own copy of
# a contextvar, so two genuinely concurrent sessions never see each
# other's value — unlike a plain module-level global, which would be
# shared mutable state two interleaved coroutines could clobber.
# asyncio.to_thread also propagates the current context into the
# thread it spawns, so this is safe for the sync tool functions in
# agent/tools/*.py too (they run via asyncio.to_thread).
_current_session_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_task_session_id", default=None
)


def set_current(session_id: int | None) -> contextvars.Token:
    return _current_session_id.set(session_id)


def reset_current(token: contextvars.Token) -> None:
    _current_session_id.reset(token)


def _log(session_id: int, kind: str, content: str) -> None:
    _writer_write(session_log, session_id, kind, content)


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
        _writer_write(save_experience, s["title"], chain, outcome, success=success)
    except Exception as e:
        log.debug(f"experience revival skipped for session #{session_id}: {e}")


def start(title: str, origin: str = "chat") -> dict:
    """Start a new task session. If one is already active, pause it
    first — phase 1 is sequential, only one session runs at a time.

    Superseded by agent.scheduler.start_session() (§2 phase 2) at both
    real call sites (agent/controller.py, agent/queue_runner.py) now
    that two sessions can be genuinely concurrent; kept here as the
    plain sequential primitive it always was, in case something needs
    exactly that later."""
    current = session_get_active()
    if current:
        log.info(f"Pausing session #{current['id']} ({current['title']!r}) to start {title!r}")
        pause(current["id"], reason=f"вытеснена новой задачей: {title}")
    sid = session_create(title, origin=origin)
    _log(sid, "start", title)
    return session_get(sid)


def create(title: str, origin: str, tags: list[str] | None = None, status: str = "active") -> dict:
    """Low-level session creation with no pausing/displacement logic of
    its own — agent/scheduler.py is what decides whether anything needs
    pausing and what `status` a new session should start at ('active',
    or 'waiting_dependency' for a queue session blocked before it ever
    ran). Plain start() stays the all-in-one sequential convenience."""
    sid = session_create(title, origin=origin, status=status, resource_tags=tags)
    _log(sid, "start" if status == "active" else "queued_waiting", title)
    return session_get(sid)


def activate_waiting(session_id: int | None) -> dict | None:
    """Promote a session out of 'waiting_dependency' once its blocker
    (a resource-tag conflict or a full concurrency slot, agent/
    scheduler.py) has cleared. Distinct from resume(): that un-pauses a
    session that already ran and was displaced; this is for a session
    that never started running at all."""
    if session_id is None:
        return None
    s = session_get(session_id)
    if not s or s["status"] != "waiting_dependency":
        return None
    session_set_status(session_id, "active")
    _log(session_id, "unblocked", "разблокирована планировщиком")
    return session_get(session_id)


def list_active() -> list[dict]:
    return _session_list("active", limit=50)


def pause(session_id: int | None, reason: str = "") -> None:
    if session_id is None:
        return
    session_set_status(session_id, "paused")
    _log(session_id, "pause", reason or "приостановлена")


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
    _log(session_id, "resume", "возобновлена")
    return session_get(session_id)


def complete(session_id: int | None, result: str = "") -> None:
    if session_id is None:
        return
    session_set_status(session_id, "done", result=result)
    _log(session_id, "complete", result or "готово")
    _revive_experience(session_id, success=True)


def fail(session_id: int | None, error: str) -> None:
    if session_id is None:
        return
    session_set_status(session_id, "failed", error=error)
    _log(session_id, "fail", error)
    _revive_experience(session_id, success=False)


def cancel(session_id: int | None, reason: str = "") -> None:
    if session_id is None:
        return
    session_set_status(session_id, "cancelled", result=reason)
    _log(session_id, "cancel", reason or "отменена")


def log_decision(session_id: int | None, kind: str, content: str) -> None:
    """No-op if session_id is None, so call sites don't need to guard
    every call with `if session:` — most tool-call logging happens
    unconditionally in agent/executor.py regardless of whether a
    session is active for this turn."""
    if session_id is None:
        return
    try:
        _log(session_id, kind, content)
    except Exception as e:
        log.debug(f"log_decision skipped: {e}")


def active() -> dict | None:
    """The task session for the current tool-call context if one's set
    (agent/executor.py, for the duration of one run() call), otherwise
    the single most-recently-active row in the DB — that fallback is
    only correct when at most one session is active system-wide, so
    callers outside an executor.run() context (queue_runner's tick-level
    scheduling decisions) must not rely on it once two sessions can be
    active at once; they should query memory.db.session_list directly."""
    sid = _current_session_id.get()
    if sid is not None:
        return session_get(sid)
    return session_get_active()


def get(session_id: int) -> dict | None:
    return session_get(session_id)


def journal(session_id: int) -> list[dict]:
    return _session_journal(session_id)


def list_sessions(status: str | None = None, limit: int = 20) -> list[dict]:
    return _session_list(status, limit)
