"""Autonomous queue runner (§2 infrastructure) — executes tasks already
sitting in rubedo_queue. Runner ≠ idle-agenda: this doesn't decide
what goes into the queue (pattern-mining, health-sweep, wishes — §6,
not built here); it only surfaced now because day/tick.py finally
gives it something to attach to.

At most one autonomous (queue-origin) session active at a time (§2
phase 2) — agent.scheduler enforces that, plus resource-tag conflicts
against whatever chat session Lin has running. Starting a new queue
task no longer needs special-casing "the owner's message pre-empts the
runner": agent.scheduler.start_session(origin="chat") is what pauses a
conflicting/blocking autonomous session, from the chat side, exactly
the same way agent.sessions.start() always pre-empted it under phase 1
— except now that can happen while this session's own executor_run()
is genuinely still live (two sessions truly concurrent), so pausing it
alone isn't enough to stop it; see agent/executor.py's own
`resumable_on_pause` docstring for how a live run notices and halts.

A queue task claimed but blocked before it can run (tag conflict at
claim time) sits as its session's 'waiting_dependency' with nothing to
preserve; one displaced mid-run sits 'paused' (slot-count) or
'waiting_dependency' (tag conflict — same status a never-ran session
gets, but this one has a history stash to go with it, agent/hanging.py
kind "session_displaced"). rubedo_queue.session_id links the claim to
either, so a later tick's resume_waiting()/resume_displaced() can find
and finish it without creating a second claim for the same task.

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
    queue_get_next_idle, queue_get_running, queue_mark_running, queue_mark_done,
    queue_mark_failed, queue_depends_satisfied, load_meta,
)
from agent import sessions, stopword, notify, scheduler, resources
from agent.classifier import classify
from agent.executor import run as executor_run
from agent.reflect import reflect_on_failure
from agent.tools import TOOLS_SCHEMA, TOOLS_MAP
from bus.client import BusClient

log = logging.getLogger("rubedo.agent.queue_runner")


async def _execute(
    task: dict, s: dict, messages: list, send_fn, max_iterations: int = 15,
) -> None:
    """Run one claimed, active queue session to completion (or failure,
    or a fresh mid-run displacement), and report the outcome. Shared by
    the fresh-pickup path and both resumption paths — always full tool
    access (queue tasks never had a classifier-restricted set; the
    resource tags used for conflict detection are a separate concern,
    agent/resources.py), just a different starting `messages`.

    `send_fn` is the transport's plain (text) -> message_id | None
    callable (transport/base.py) — threaded through from run_queue_tick
    so notify.deliver() below can actually reach the owner instead of
    only deciding "should this be sent" and stopping there."""
    try:
        reply, _ = await executor_run(
            messages=messages,
            tools_schema=TOOLS_SCHEMA, tools_map=TOOLS_MAP,
            session_id="lin", bus_client=BusClient(), max_iterations=max_iterations,
            task_session_id=s["id"], tool_categories=[],
            full_tools_schema=TOOLS_SCHEMA, full_tools_map=TOOLS_MAP,
            resumable_on_pause=True,
        )
    except Exception as e:
        reply = f"Ошибка: {e}"
        sessions.fail(s["id"], str(e))

    final = sessions.get(s["id"])
    if final["status"] == "failed":
        should_retry = queue_mark_failed(task["id"], final.get("error") or "")
        if not should_retry:
            verdict = await reflect_on_failure(sessions.journal(s["id"]), final.get("error") or "")
            await notify.deliver(
                "normal", f"Задача из очереди не получилась: {verdict['diagnosis']}", send_fn, source="queue",
            )
    elif final["status"] == "done":
        queue_mark_done(task["id"], result=reply[:300])
        await notify.deliver("low", f"Сделала из очереди: {task['title']}", send_fn, source="queue")
    # else: status is "paused" or "waiting_dependency" — displaced
    # mid-run by Lin's task again (agent/executor.py noticed and
    # re-stashed) — leave rubedo_queue 'running', claim intact; nothing
    # to report yet, a later tick picks it back up. Same for a
    # not-yet-resolved "waiting_dependency" that never got this far.


async def run_queue_tick(send_fn=None) -> None:
    if stopword.is_frozen() or load_meta("queue_paused") == "1":
        return

    # Tick-driven resumption (§2 phase 2), two distinct cases:
    # (1) a session mid-run when displaced — resume with its exact
    #     stashed history, don't redo work it already did.
    displaced = scheduler.resume_displaced()
    if displaced:
        resumed_sess, hist, _cats, max_iter = displaced
        task = queue_get_running()
        if task and task.get("session_id") == resumed_sess["id"]:
            sessions.log_decision(
                resumed_sess["id"], "initiative", f"продолжает из очереди (после вытеснения): {task['title']}",
            )
            await _execute(task, resumed_sess, hist, send_fn, max_iter)
        return  # at most one autonomous session — handled either way

    # (2) a session that never got to run at all (tag conflict at claim
    #     time) — starts fresh, it has no history to preserve.
    for resumed in scheduler.resume_waiting():
        if resumed.get("origin") != "queue":
            continue
        task = queue_get_running()
        if task and task.get("session_id") == resumed["id"]:
            sessions.log_decision(resumed["id"], "initiative", f"продолжает из очереди: {task['title']}")
            await _execute(
                task, resumed, [{"role": "user", "content": task["description"] or task["title"]}], send_fn,
            )
        return

    claimed = queue_get_running()
    if claimed:
        _sess = sessions.get(claimed["session_id"]) if claimed.get("session_id") else None
        if _sess and _sess["status"] == "failed":
            # Reconciliation: the session died out-of-band, not through
            # _execute()'s own post-run bookkeeping — e.g. agent/
            # hanging.py's TTL sweep expiring a stashed displacement
            # nobody came back to unblock in time. Without this the
            # rubedo_queue row would sit at 'running' forever.
            should_retry = queue_mark_failed(claimed["id"], _sess.get("error") or "истёк срок ожидания")
            if not should_retry:
                await notify.deliver(
                    "normal", f"Задача из очереди не получилась (истёк срок ожидания): {claimed['title']}",
                    send_fn, source="queue",
                )
        return  # already claimed: executing in another concurrent call, still waiting_dependency, or just reconciled

    task = queue_get_next_idle()
    if not task or not queue_depends_satisfied(task):
        return

    # Resource tags (agent/resources.py), derived the same way a chat
    # "deep" task's are (agent/controller.py) — via the classifier's
    # own tool_categories pick, not a second independent guess.
    route_info = await classify(task["description"] or task["title"])
    tags = resources.tags_for_categories(route_info.get("tool_categories", []))

    s = scheduler.start_session(task["title"], origin="queue", tags=tags)
    queue_mark_running(task["id"], session_id=s["id"])
    if s["status"] != "active":
        return  # waiting_dependency — claimed, a later tick will resume it

    _why = f" — {task['description']}" if task.get("description") else ""
    sessions.log_decision(s["id"], "initiative", f"взяла из очереди: {task['title']}{_why}")
    await _execute(task, s, [{"role": "user", "content": task["description"] or task["title"]}], send_fn)
