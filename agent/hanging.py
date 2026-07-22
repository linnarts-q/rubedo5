"""Hanging questions (§5, stage 4) — the real, multi-slot entity behind
what agent/approval.py and agent/questions.py each used to fake with a
single meta-key slot.

A meta key can only ever hold one pending item: while an approval or an
`ask_user` question sat unanswered, anything that triggered a second
one of the same kind silently overwrote the first via save_meta() —
its payload (and the task session waiting on it) just vanished with no
trace. Every call gets its own row in hanging_questions instead, so
nothing gets destroyed.

"Which pending item does this reply answer" still isn't solved by real
NLU here — like a human, the most recently asked one is assumed to be
the one being answered (LIFO), which is exactly what the old
overwrite-based design did in the common single-pending case anyway.
The difference is what happens to anything still unresolved behind
it: instead of disappearing, older pending rows stay queued and get
swept on TTL expiry (still failing their linked task session, same as
before) rather than lost silently.

agent/approval.py and agent/questions.py are thin, kind-specific
wrappers over this module — their public functions (request/pending/
clear, ask/pending/clear) keep the exact same signatures so
agent/executor.py and agent/controller.py needed no changes at their
call sites.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from config import APPROVAL_TTL_HOURS
from memory.db import (
    hanging_create, hanging_list_pending, hanging_resolve,
    hanging_get_pending_for_session,
)

log = logging.getLogger("rubedo.agent.hanging")


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _is_stale(row: dict) -> bool:
    """created_at is written via memory.db._now() — naive UTC text in
    "%Y-%m-%d %H:%M:%S" — so staleness must be judged against naive UTC
    too, not local time, or the TTL drifts by the server's own offset."""
    try:
        created = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return True
    return (_utc_now_naive() - created).total_seconds() > APPROVAL_TTL_HOURS * 3600


def _fail_linked_session(row: dict, reason: str) -> None:
    tsid = row.get("task_session_id")
    if tsid is None:
        return
    try:
        from agent import sessions
        sessions.fail(tsid, reason)
    except Exception as e:
        log.debug(f"orphaned-session fail skipped: {e}")


def _sweep(kind: str) -> list[dict]:
    """Expire every stale pending row of this kind (failing whatever
    task session each was blocking), return the survivors, newest
    first."""
    rows = hanging_list_pending(kind)  # newest first
    survivors = []
    for row in rows:
        if _is_stale(row):
            log.info(f"Hanging question #{row['id']} ({kind}) expired (TTL), clearing")
            hanging_resolve(row["id"], "expired")
            _fail_linked_session(row, "просрочено (TTL), ответа не дождалась")
        else:
            survivors.append(row)
    return survivors


def _sweep_and_get(kind: str) -> dict | None:
    """Expire every stale pending row of this kind, then return the
    most recent still-pending one, if any."""
    survivors = _sweep(kind)
    return survivors[0] if survivors else None


def list_pending(kind: str) -> list[dict]:
    """All still-pending items of this kind, oldest first, after
    sweeping anything past TTL — agent/routing.py (§2 phase 2 step 3)
    needs to see every pending item at once, not just the most recent,
    to route correctly when more than one is genuinely waiting."""
    return list(reversed(_sweep(kind)))


def create(kind: str, payload: dict, task_session_id: int | None = None) -> int:
    return hanging_create(kind, json.dumps(payload, ensure_ascii=False), task_session_id)


def pending(kind: str) -> dict | None:
    """Returns the payload dict of the most recent still-pending item
    of this kind, with its row id folded in under "_hanging_id" (for
    the caller's own clear()/resolve() bookkeeping — callers should pop
    it before handing the dict to anyone outside their own module)."""
    row = _sweep_and_get(kind)
    if not row:
        return None
    try:
        payload = json.loads(row["payload"])
    except Exception:
        hanging_resolve(row["id"], "expired")
        return None
    payload["_hanging_id"] = row["id"]
    return payload


def pending_for_session(session_id: int) -> dict | None:
    """Kind-agnostic counterpart to pending(kind) — a 'waiting_user'
    task session (agent/scheduler.py's status vocabulary) always has
    exactly one pending hanging item, but the caller (agent/routing.py,
    §2 phase 2 step 3) doesn't yet know whether it's an "ask_user"
    question or an "approval" confirmation; this tells it, via
    "_kind", alongside the usual "_hanging_id". Same TTL sweep as
    pending(kind) — a session's answer window can go stale here too."""
    row = hanging_get_pending_for_session(session_id)
    if not row:
        return None
    if _is_stale(row):
        log.info(f"Hanging question #{row['id']} ({row['kind']}) expired (TTL), clearing")
        hanging_resolve(row["id"], "expired")
        _fail_linked_session(row, "просрочено (TTL), ответа не дождалась")
        return None
    try:
        payload = json.loads(row["payload"])
    except Exception:
        hanging_resolve(row["id"], "expired")
        return None
    payload["_hanging_id"] = row["id"]
    payload["_kind"] = row["kind"]
    return payload


def resolve(hq_id: int, status: str = "answered") -> None:
    hanging_resolve(hq_id, status)
