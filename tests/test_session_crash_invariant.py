"""Session kill-9 invariant (§18, techspec §2 phase 2). Priority by
risk (9.2): after an unclean restart, no chat-origin task from Лин is
ever silently lost, no write-level step is ever blindly re-executed
without a verified verdict, and each of the three non-terminal
statuses (paused / waiting_user / waiting_dependency) only wakes via
its own designated waker.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import types
from pathlib import Path
from unittest.mock import AsyncMock

import memory.db as db
import agent.crash_recovery as cr
import agent.sessions as sessions
import agent.undo as undo
import agent.routing as routing
import agent.scheduler as scheduler


def _simulate_unclean_exit():
    """agent_heartbeat() flips clean_shutdown=False as a side effect --
    that alone models "the process just stopped", no separate dirty
    flag needed."""
    db.agent_heartbeat()


def _final_response(text):
    msg = types.SimpleNamespace(content=text, tool_calls=None)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


def test_no_crash_on_clean_state(tools_ctx):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM agent_state")
    assert cr.detect_crash() is False
    db.agent_mark_clean_shutdown()
    assert cr.detect_crash() is False


def test_no_crash_when_nothing_was_running(tools_ctx):
    _simulate_unclean_exit()
    assert cr.detect_crash() is False


def test_queue_origin_orphan_requeued_silently(tools_ctx):
    """A crash mid-autonomous-task never loses the task -- it goes back
    to 'pending' in the queue -- but Лин never gets bothered about
    something she never asked about directly."""
    task_id = db.queue_add("Автономная задача, которую прервал сбой")
    qs = sessions.create("Автономная задача, которую прервал сбой", origin="queue")
    with db.get_conn() as conn:
        conn.execute("UPDATE task_sessions SET status='active' WHERE id=%s", (qs["id"],))
    db.queue_mark_running(task_id, session_id=qs["id"])
    _simulate_unclean_exit()

    assert cr.detect_crash() is True
    msg = cr.recover_after_crash()
    assert msg is None, "a queue-only orphan should requeue silently, no message for Лин"
    requeued = next(t for t in db.queue_list() if t["id"] == task_id)
    assert requeued["status"] == "pending" and requeued["session_id"] is None
    assert sessions.get(qs["id"])["status"] == "cancelled"


def test_chat_origin_read_step_resumes_as_safe(tools_ctx):
    """Interrupted on a read-only step -> nothing to lose, no task from
    Лин is lost, and the verdict is honestly 'safe to continue'."""
    cs = sessions.create("Найти билеты в Киев", origin="chat")
    with db.get_conn() as conn:
        conn.execute("UPDATE task_sessions SET status='active' WHERE id=%s", (cs["id"],))
    sessions.log_decision(cs["id"], "step_started", 'call_1 web_search({"query": "билеты Киев"})')
    _simulate_unclean_exit()

    msg = cr.recover_after_crash()
    assert msg and "Найти билеты в Киев" in msg and "Продолжить?" in msg
    assert sessions.get(cs["id"])["status"] == "paused"
    journal = sessions.journal(cs["id"])
    crash_entry = next(e for e in journal if e["kind"] == "crash_recovery")
    assert "безопасно продолжить" in crash_entry["content"]

    pending = [it for it in routing.pending_items() if it["kind"] == "crash_resume"]
    assert len(pending) == 1 and pending[0]["task_session_id"] == cs["id"]


def test_chat_origin_write_step_verified_against_undo_snapshot_unchanged(tools_ctx):
    """Interrupted on a write step where the undo snapshot shows the
    file never actually changed -- the verdict must say so honestly,
    not guess "probably fine"."""
    tmpdir = tempfile.mkdtemp()
    target = Path(tmpdir) / "report.txt"
    target.write_text("original content")
    undo.snapshot_before_write(target)

    cs = sessions.create("Переписать report.txt", origin="chat")
    with db.get_conn() as conn:
        conn.execute("UPDATE task_sessions SET status='active' WHERE id=%s", (cs["id"],))
    sessions.log_decision(
        cs["id"], "step_started",
        f'call_2 file_write({json.dumps({"filename": str(target)})})',
    )
    _simulate_unclean_exit()
    msg = cr.recover_after_crash()
    assert msg and "Переписать report.txt" in msg

    journal = sessions.journal(cs["id"])
    crash_entry = next(e for e in journal if e["kind"] == "crash_recovery")
    assert "НЕ выполнилась" in crash_entry["content"]


def test_chat_origin_write_step_verified_against_undo_snapshot_changed(tools_ctx):
    """Same shape, but the file DID change after the snapshot -- the
    verdict must say it landed, never silently re-run the write."""
    tmpdir = tempfile.mkdtemp()
    target = Path(tmpdir) / "report.txt"
    target.write_text("changed by the write")
    undo.snapshot_before_write(target)
    target.write_text("changed AGAIN -- this is what actually landed")

    cs = sessions.create("Переписать report.txt снова", origin="chat")
    with db.get_conn() as conn:
        conn.execute("UPDATE task_sessions SET status='active' WHERE id=%s", (cs["id"],))
    sessions.log_decision(
        cs["id"], "step_started",
        f'call_3 file_write({json.dumps({"filename": str(target)})})',
    )
    _simulate_unclean_exit()
    cr.recover_after_crash()
    journal = sessions.journal(cs["id"])
    crash_entry = next(e for e in journal if e["kind"] == "crash_recovery")
    assert "изменилось" in crash_entry["content"] and "НЕ" not in crash_entry["content"]


def test_write_step_with_no_undo_snapshot_stays_honest(tools_ctx):
    """A write-classified tool with no undo snapshot at all (e.g.
    system_shell) never gets treated as verified-safe by default --
    the honest 'no automatic check' note, not a guess."""
    cs = sessions.create("Настроить автобэкап", origin="chat")
    with db.get_conn() as conn:
        conn.execute("UPDATE task_sessions SET status='active' WHERE id=%s", (cs["id"],))
    sessions.log_decision(cs["id"], "step_started", 'call_4 system_shell({"command": "crontab -e"})')
    _simulate_unclean_exit()
    cr.recover_after_crash()
    journal = sessions.journal(cs["id"])
    crash_entry = next(e for e in journal if e["kind"] == "crash_recovery")
    assert "нет автоматической проверки" in crash_entry["content"]


def test_resume_never_blindly_replays_the_interrupted_tool_call(tools_ctx, monkeypatch):
    """The actual invariant: 'да' resumes into fresh LLM reasoning with
    the crash context as a note, never by re-invoking the parsed tool
    call directly against TOOLS_MAP."""
    import agent.controller as controller
    import agent.executor as executor

    cs = sessions.create("Настроить автобэкап", origin="chat")
    with db.get_conn() as conn:
        conn.execute("UPDATE task_sessions SET status='active' WHERE id=%s", (cs["id"],))
    sessions.log_decision(cs["id"], "step_started", 'call_4 system_shell({"command": "crontab -e"})')
    _simulate_unclean_exit()
    cr.recover_after_crash()

    monkeypatch.setattr(executor, "generation_chat", AsyncMock(
        return_value=_final_response("Проверила -- crontab не тронут, настраиваю заново.")
    ))
    sent = []
    async def send_fn(t):
        sent.append(t)
    fake_bus = types.SimpleNamespace(publish=AsyncMock())
    asyncio.run(controller.handle_message(user_id=1, text="да", bus_client=fake_bus, send_fn=send_fn))

    assert sent and "Проверила" in sent[0]
    assert sessions.get(cs["id"])["status"] == "done"
    assert all(it["kind"] != "crash_resume" for it in routing.pending_items())


# ── waiting_dependency wakes only via its own resource-conflict waker ──

def test_waiting_dependency_stays_blocked_while_conflict_persists(tools_ctx):
    chat = scheduler.start_session("Задача Лин на сервере", origin="chat", tags=["server"])
    assert chat["status"] == "active"

    queued = scheduler.start_session("Автономная проверка сервера", origin="queue", tags=["server"])
    assert queued["status"] == "waiting_dependency", "conflicting resource tag must block it, not run it"

    resumed = scheduler.resume_waiting()
    assert queued["id"] not in [s["id"] for s in resumed]
    assert sessions.get(queued["id"])["status"] == "waiting_dependency"


def test_waiting_dependency_wakes_once_conflict_clears(tools_ctx):
    chat = scheduler.start_session("Задача Лин на сервере", origin="chat", tags=["server"])
    queued = scheduler.start_session("Автономная проверка сервера", origin="queue", tags=["server"])
    assert queued["status"] == "waiting_dependency"

    sessions.complete(chat["id"], result="готово")
    resumed = scheduler.resume_waiting()
    assert queued["id"] in [s["id"] for s in resumed]
    assert sessions.get(queued["id"])["status"] == "active"


def test_waiting_user_invisible_to_resume_waiting(tools_ctx):
    """resume_waiting() only ever looks at queue-origin
    'waiting_dependency' rows -- a waiting_user session must be
    structurally invisible to it; it wakes only through
    agent/controller.py's message routing."""
    cs = sessions.create("Ждёт ответа Лин", origin="chat")
    with db.get_conn() as conn:
        conn.execute("UPDATE task_sessions SET status='active' WHERE id=%s", (cs["id"],))
    sessions.wait_user(cs["id"], reason="уточняющий вопрос")

    scheduler.resume_waiting()
    assert sessions.get(cs["id"])["status"] == "waiting_user", (
        "resume_waiting() must never touch a waiting_user session"
    )
