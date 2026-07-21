"""Crash isolation, resume protocol (§2 phase 2, rollout step 4 — "the
last unsolved architectural piece"). A crashed session must not take
its neighbors down with it, and a full-agent crash must not silently
lose Lin's in-flight work or blindly redo a write that may have already
landed.

Three pieces:

1. Checkpointing already lives in agent/executor.py (step_started
   committed right before a tool runs, step_done — the existing
   "tool_call" journal entry — right after). A session's last journal
   entry being "step_started" with nothing after it is exactly how a
   restart tells "this step was still in-flight when the process died"
   from "it finished cleanly" — see `_interrupted_step` below.

2. Restart detection: agent_state's heartbeat_at/clean_shutdown
   (memory/db.py). Whatever process eventually drives day/tick.py
   (not wired to a live one yet, same as the rest of the day-engine)
   should call `agent_heartbeat()` every tick and `mark_clean_shutdown()`
   on graceful exit; `recover_after_crash()` is what it calls once at
   startup, before anything else touches sessions.

3. Resume protocol, split by lane:
   - queue-origin: no negotiation — put the claim straight back to
     'pending' (memory.db.queue_requeue_after_crash), cancel the
     orphaned session. Redoing an autonomous task from scratch is
     cheap; there's nothing in it worth trying to salvage a half-
     finished step for.
   - chat-origin (Lin's own lane — at most one, by construction, so
     there's never more than one to summarize): paused with the
     interrupted step's read/write verdict recorded, one "crash_resume"
     hanging item created, one `important`-severity message returned
     for the caller to send ("перезапустилась, на паузе: ..., продолжить?").
     Her answer is routed the ordinary way (agent/routing.py already
     knows this hanging kind) — no bespoke reply parsing.

Read vs write classification is the one piece that can't be perfectly
mechanical: `_READ_ONLY_TOOLS` is an explicit allowlist, everything else
defaults to "write" — same "over-including is cheap, missing one isn't"
principle this codebase already applies to tool_categories and resource
tags, just pointed at a different question ("could redoing this ever
be wrong?" instead of "does this need a tool?"). A write step is never
silently redone: read -> safe to just let the resumed reasoning
continue naturally (worst case, the model re-issues a harmless lookup);
write -> agent/undo.py's snapshot comparison settles it when the tool
is file_write/file_delete (§15's only snapshotted writes), otherwise
the interrupted-step note simply says so and the resumed reasoning is
explicitly told to check before repeating it, not to redo it blind.
"""
from __future__ import annotations

import json
import logging

from agent import hanging, sessions, undo
from memory.db import (
    agent_heartbeat, agent_mark_clean_shutdown, agent_state_get,
    queue_get_running, queue_requeue_after_crash,
)

log = logging.getLogger("rubedo.agent.crash_recovery")

_READ_ONLY_TOOLS = frozenset({
    "think", "session_plan", "session_report", "session_history",
    "work_mode_get", "memory_search", "file_read", "file_list",
    "system_info", "process_list", "system_uptime", "reminder_list",
    "task_list", "task_details", "wish_list", "memory_history_search",
    "profile_view", "iterations_recent", "weather", "note_list",
    "event_list", "recurring_list", "spotrent_status", "queue_list",
    "pool_list", "calculate", "navigate", "web_search", "web_content",
})


def detect_crash() -> bool:
    """True only if the previous run ended without a clean shutdown
    AND left at least one session genuinely 'active' — matches the
    spec's own rule exactly: no flag alone doesn't mean anything went
    wrong (nothing to recover either way if nothing was running)."""
    state = agent_state_get()
    if not state or not state.get("heartbeat_at"):
        return False
    if state.get("clean_shutdown"):
        return False
    return bool(sessions.list_sessions(status="active", limit=50))


def _parse_step(content: str) -> tuple[str, dict] | None:
    """Reverses agent/executor.py's "{tc.id} {name}({args_json})"
    step_started format. None on anything unparseable — callers treat
    that the same as an unclassifiable write: safe by construction."""
    try:
        _tc_id, rest = content.split(" ", 1)
        name, args_str = rest.split("(", 1)
        args_str = args_str.rsplit(")", 1)[0]
        args = json.loads(args_str) if args_str.strip() else {}
        return name, args
    except Exception:
        return None


def _interrupted_step(session_id: int) -> dict | None:
    """The session's last journal entry, if it's a "step_started" with
    nothing after it — i.e. the step that was in-flight when the
    process died. None if the session ended cleanly (any other kind
    last) or has no journal at all."""
    journal = sessions.journal(session_id)
    if not journal or journal[-1]["kind"] != "step_started":
        return None
    parsed = _parse_step(journal[-1]["content"])
    if not parsed:
        return {"name": "?", "args": {}, "classification": "write", "verdict": None}
    name, args = parsed
    classification = "read" if name in _READ_ONLY_TOOLS else "write"
    verdict = None
    if classification == "write" and name in ("file_write", "file_delete"):
        hint = str(args.get("filename", ""))
        verdict = undo.verify_last_write(filename_hint=hint)
    return {"name": name, "args": args, "classification": classification, "verdict": verdict}


def _step_note(step: dict | None) -> str:
    if not step:
        return ""
    if step["classification"] == "read":
        return f"(прервано на чтении — {step['name']}, безопасно продолжить)"
    verdict = step.get("verdict") or "нет автоматической проверки — прежде чем повторять, проверь вручную"
    return f"(прервано на записи — {step['name']}, {verdict})"


def _handle_queue_orphan(s: dict) -> None:
    task = queue_get_running()
    if task and task.get("session_id") == s["id"]:
        queue_requeue_after_crash(task["id"])
        log.info(f"Requeued crashed autonomous task #{task['id']} (was session #{s['id']})")
    sessions.cancel(s["id"], reason="агент перезапустился, задача возвращена в очередь")


def _handle_chat_orphan(s: dict) -> tuple[dict, str]:
    step = _interrupted_step(s["id"])
    note = _step_note(step)
    sessions.pause(s["id"], reason="сессия осиротела после сбоя агента" + (f" {note}" if note else ""))
    sessions.log_decision(
        s["id"], "crash_recovery",
        note or "прервано между шагами (последний шаг завершился штатно)",
    )
    brief = s["title"] + (f" — {note}" if note else "")
    return s, brief


def recover_after_crash() -> str | None:
    """Call once at process startup, before anything else touches
    sessions. Returns the one message to send Lin at "critical"
    severity (agent/notify.py — the actual implemented tiers are
    critical/normal/low, not the abstract spec's critical/important/
    routine naming; "the whole agent just crashed and restarted" earns
    the tier that breaks through night/day-off/quiet-mode same as a
    real emergency would, deliberately not "normal"), or None if
    there's nothing for her to decide — only autonomous work was
    orphaned (silently requeued), or there was
    no crash at all."""
    if not detect_crash():
        agent_heartbeat()
        return None

    orphaned = sessions.list_sessions(status="active", limit=50)
    chat_orphans: list[tuple[dict, str]] = []
    for s in orphaned:
        if s.get("origin") == "queue":
            _handle_queue_orphan(s)
        else:
            chat_orphans.append(_handle_chat_orphan(s))

    agent_heartbeat()

    if not chat_orphans:
        return None

    session_ids = [s["id"] for s, _ in chat_orphans]
    briefs = [brief for _, brief in chat_orphans]
    hanging.create(
        "crash_resume",
        {"session_ids": session_ids, "briefs": briefs},
        task_session_id=session_ids[0] if len(session_ids) == 1 else None,
    )
    listing = "\n".join(f"— {b}" for b in briefs)
    return f"Перезапустилась после сбоя. На паузе:\n{listing}\nПродолжить?"


def shutdown_clean() -> None:
    """Call from whatever graceful-exit path eventually exists (signal
    handler, launcher — not wired up yet, same as day/tick.py itself)."""
    agent_mark_clean_shutdown()
