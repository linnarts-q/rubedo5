"""Session scheduler (§2 phase 2, day-engine 5.0 parallelism — rollout
step 2). Sits between agent/controller.py + agent/queue_runner.py and
agent/sessions.py: decides *whether* a new session can go active right
now, not how a session's lifecycle works once it's running.

Two lanes only, by construction:
  - chat  — Lin's own conversation. Starting a new chat task always
    pauses whatever chat task was running (agent.sessions.start()'s
    original invariant, unchanged) — Lin only ever has one thread of
    conversation in front of her.
  - queue — autonomous work (agent/queue_runner.py). At most one
    autonomous session may be active at a time; it never outranks
    Lin's lane.

Since a new chat task always displaces the old chat task, and at most
one queue session is ever active, total active sessions is always
`(0 or 1 chat) + (0 or 1 queue)` — never more than MAX_CONCURRENT_SESSIONS
at its default of 2. The slot-count check below still exists in full
generality (not hardcoded to "2 lanes fit"), so it does the right thing
if MAX_CONCURRENT_SESSIONS is ever tuned down for testing or up after
the spec's own week-of-stability gate.

Resource tags are coarse and deterministic (agent/resources.py) — no
file locks on the fly. Lin's task always wins a tag conflict (the
queue session pauses); a new queue task blocked by conflict or a full
slot is created directly as 'waiting_dependency' rather than 'active',
and picked back up by resume_waiting() on a later tick — same
tick-driven philosophy as the rest of the day-engine, not event-driven.
"""
from __future__ import annotations

import json
import logging

import config
from agent import sessions

log = logging.getLogger("rubedo.agent.scheduler")


def _tags_of(session: dict) -> set[str]:
    raw = session.get("resource_tags") or "[]"
    try:
        return set(json.loads(raw))
    except (TypeError, ValueError):
        return set()


def _split_lanes(actives: list[dict]) -> tuple[list[dict], list[dict]]:
    chat_actives = [s for s in actives if s.get("origin") != "queue"]
    queue_actives = [s for s in actives if s.get("origin") == "queue"]
    return chat_actives, queue_actives


def start_session(title: str, origin: str = "chat", tags: list[str] | None = None) -> dict:
    """Decide whether the new session goes active immediately, and what
    (if anything) needs to be paused to make room for it. Mirrors
    agent.sessions.start()'s return shape — callers get back the new
    session's row either way, just check `status` to see whether it's
    running yet."""
    tag_set = set(tags or [])
    chat_actives, queue_actives = _split_lanes(sessions.list_active())

    if origin == "chat":
        for s in chat_actives:
            log.info(f"Pausing chat session #{s['id']} ({s['title']!r}) to start {title!r}")
            sessions.pause(s["id"], reason=f"вытеснена новой задачей: {title}")
        remaining_queue = list(queue_actives)
        for s in queue_actives:
            if tag_set & _tags_of(s):
                log.info(
                    f"Pausing queue session #{s['id']} ({s['title']!r}) — "
                    f"resource conflict with Lin's task {title!r}"
                )
                sessions.pause(s["id"], reason=f"вытеснена задачей от Лин: {title}")
                remaining_queue.remove(s)
        # Slot-count fallback ("Нет свободного слота → автономная в
        # paused"). With the real MAX_CONCURRENT_SESSIONS=2 and exactly
        # two lanes this never fires — 1 chat + 1 queue always fits —
        # but it's what makes chat still correctly displace a
        # non-conflicting autonomous session if MAX_CONCURRENT_SESSIONS
        # is ever run at 1 (rollout step 1's infra-only config).
        if 1 + len(remaining_queue) > config.MAX_CONCURRENT_SESSIONS:
            for s in remaining_queue:
                log.info(
                    f"Pausing queue session #{s['id']} ({s['title']!r}) — "
                    f"no free slot for Lin's task {title!r}"
                )
                sessions.pause(s["id"], reason=f"вытеснена задачей от Лин (нет свободного слота): {title}")
        return sessions.create(title, origin="chat", tags=sorted(tag_set), status="active")

    # origin == "queue": at most one autonomous slot, never outranks chat.
    blocked = (
        bool(queue_actives)
        or len(chat_actives) + len(queue_actives) >= config.MAX_CONCURRENT_SESSIONS
        or any(tag_set & _tags_of(s) for s in chat_actives)
    )
    status = "waiting_dependency" if blocked else "active"
    return sessions.create(title, origin="queue", tags=sorted(tag_set), status=status)


def resume_waiting() -> list[dict]:
    """Tick-driven resumption of queue sessions created as
    'waiting_dependency'. Re-runs exactly the checks start_session()
    itself used, oldest-blocked-first, so this can never activate a
    session start_session() would have blocked — call this at the top
    of agent/queue_runner.py's run_queue_tick(), before it looks for a
    new task to pick up."""
    waiting = [
        s for s in sessions.list_sessions(status="waiting_dependency", limit=50)
        if s.get("origin") == "queue"
    ]
    waiting.sort(key=lambda s: s["id"])

    resumed = []
    for s in waiting:
        chat_actives, queue_actives = _split_lanes(sessions.list_active())
        if queue_actives:
            continue
        if len(chat_actives) + len(queue_actives) >= config.MAX_CONCURRENT_SESSIONS:
            continue
        if any(_tags_of(s) & _tags_of(a) for a in chat_actives):
            continue
        activated = sessions.activate_waiting(s["id"])
        if activated:
            log.info(f"Unblocked queue session #{s['id']} ({s['title']!r})")
            resumed.append(activated)
    return resumed


def resume_displaced() -> tuple[dict, list, list, int] | None:
    """Tick-driven resumption of a queue session that was genuinely
    mid-run when a chat task displaced it (agent/executor.py's
    `resumable_on_pause` stash, kind "session_displaced" in agent/
    hanging.py) — distinct from resume_waiting(): this session already
    ran some tool calls, so it resumes with that exact history instead
    of starting the task over. Returns (session, history,
    tool_categories, max_iterations) if something was unblocked and
    resumed, else None. At most one such stash can exist at a time (at
    most one autonomous session ever active), so there's nothing to
    loop over here unlike resume_waiting()."""
    from agent import hanging

    payload = hanging.pending("session_displaced")
    if not payload:
        return None
    hq_id = payload.pop("_hanging_id")
    sid = payload.get("task_session_id")
    s = sessions.get(sid) if sid is not None else None
    if not s or s["status"] != "paused":
        # Resolved some other way already (failed/cancelled) — stash is stale.
        hanging.resolve(hq_id, "stale")
        return None

    chat_actives, queue_actives = _split_lanes(sessions.list_active())
    if any(_tags_of(s) & _tags_of(a) for a in chat_actives):
        return None  # still blocked — leave the stash, try again next tick
    if len(chat_actives) + len(queue_actives) >= config.MAX_CONCURRENT_SESSIONS:
        return None

    resumed = sessions.resume(sid)
    if not resumed:
        hanging.resolve(hq_id, "stale")
        return None
    hanging.resolve(hq_id, "resumed")
    log.info(f"Resumed displaced queue session #{sid} ({resumed['title']!r}) with its stashed history")
    return resumed, payload["history"], payload["tool_categories"], payload["max_iterations"]
