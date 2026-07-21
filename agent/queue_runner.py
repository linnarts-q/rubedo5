"""Autonomous queue runner (§2 infrastructure) — executes tasks already
sitting in rubedo_queue. Runner ≠ idle-agenda: this doesn't decide
what goes into the queue (pattern-mining, health-sweep, wishes — §6,
not built here); it only surfaced now because day/tick.py finally
gives it something to attach to.

One task at a time (§2 phase 1 — sequential). agent.sessions.start()
already pauses whatever the owner's own conversation had active the
same way a "deep" chat task would, so "the owner's message pre-empts
the runner" needs no special-casing here.

Runs in any day phase, including night/day-off — agent.notify gates
only the result message, never the work itself. Stop-phrase (§15) and
the existing queue_paused flag (agent/tools/__init__.py's queue_pause/
resume tools) both freeze it — no new task starts while either is set.
A task that exhausts its retries (memory.db.queue_mark_failed's own
3-strikes accounting) goes through the same reflective cycle
(agent.reflect) as any other failed session before its report is sent.
"""
from __future__ import annotations

import logging

from memory.db import (
    queue_get_next_idle, queue_mark_running, queue_mark_done,
    queue_mark_failed, queue_depends_satisfied, load_meta,
)
from agent import sessions, stopword, notify
from agent.executor import run as executor_run
from agent.reflect import reflect_on_failure
from agent.tools import TOOLS_SCHEMA, TOOLS_MAP
from bus.client import BusClient

log = logging.getLogger("rubedo.agent.queue_runner")


async def run_queue_tick() -> None:
    if stopword.is_frozen() or load_meta("queue_paused") == "1":
        return
    if sessions.active():
        return  # one running session at a time (§2 phase 1)

    task = queue_get_next_idle()
    if not task or not queue_depends_satisfied(task):
        return

    queue_mark_running(task["id"])
    s = sessions.start(task["title"], origin="queue")
    sessions.log_decision(s["id"], "initiative", f"взяла из очереди: {task['title']}")

    try:
        reply, _ = await executor_run(
            messages=[{"role": "user", "content": task["description"] or task["title"]}],
            tools_schema=TOOLS_SCHEMA, tools_map=TOOLS_MAP,
            session_id="lin", bus_client=BusClient(), max_iterations=15,
            task_session_id=s["id"], tool_categories=[],
            full_tools_schema=TOOLS_SCHEMA, full_tools_map=TOOLS_MAP,
        )
    except Exception as e:
        reply = f"Ошибка: {e}"
        sessions.fail(s["id"], str(e))

    final = sessions.get(s["id"])
    if final["status"] == "failed":
        should_retry = queue_mark_failed(task["id"], final.get("error") or "")
        if not should_retry:
            verdict = await reflect_on_failure(sessions.journal(s["id"]), final.get("error") or "")
            notify.notify_or_bundle(
                "normal", f"Задача из очереди не получилась: {verdict['diagnosis']}", source="queue",
            )
    else:
        queue_mark_done(task["id"], result=reply[:300])
        notify.notify_or_bundle("low", f"Сделала из очереди: {task['title']}", source="queue")
