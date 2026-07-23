"""End-to-end pipeline over transport/local.py's LocalTransport (stage
9.6): message -> session -> execution -> report, message-binding-based
reply routing, yellow-zone approval, and the full crash-restart-resume
cycle -- all through the real transport object, not a bare send_fn
list, so the transport layer itself (feed/next_incoming, message-id
bookkeeping) is exercised, not just the pipeline behind it.

Promoted from the session's own scratchpad smoke_transport_e2e.py,
extended with the yellow-approval leg the audit specifically asked
for (message -> session -> yellow approval -> "да" -> execution ->
report), which the original script didn't cover.
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock

import memory.db as db
from transport.local import LocalTransport
import agent.controller as controller
import agent.sessions as sessions
import agent.executor as executor
import agent.planner as planner
import agent.crash_recovery as cr
import agent.notify as notify
import agent.routing as routing
from bus.client import BusClient


def _final_response(text):
    msg = types.SimpleNamespace(content=text, tool_calls=None)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


def _tool_call_response(name, args="{}"):
    msg = types.SimpleNamespace(
        content=None,
        tool_calls=[types.SimpleNamespace(id="call1", function=types.SimpleNamespace(name=name, arguments=args))],
    )
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


async def _drive_one(transport: LocalTransport, bus: BusClient) -> None:
    """Mimics interface/telegram.py's on_message -- the same shape a
    real Telethon event would be normalized into, minus Telethon."""
    event = await transport.next_incoming()
    if event is None:
        return
    await controller.handle_message(
        user_id=1, text=event["text"], bus_client=bus, send_fn=transport.send,
        reply_to_message_id=event.get("reply_to_message_id"),
    )


def test_ask_user_question_bound_and_reply_resolves_deterministically(tools_ctx, monkeypatch):
    transport = LocalTransport()
    bus = BusClient()

    monkeypatch.setattr(controller, "classify", AsyncMock(return_value={
        "route": "deep", "context": "task", "intent": "разобрать почту",
        "tool_categories": [], "missing_info": [],
    }))
    monkeypatch.setattr(controller, "make_plan", AsyncMock(return_value={"steps": [], "max_iterations": 5}))
    monkeypatch.setattr(executor, "generation_chat", AsyncMock(
        return_value=_tool_call_response("ask_user", '{"question": "По почте: архивировать всё или только спам?"}')
    ))
    asyncio.run(transport.feed("разбери почту"))
    asyncio.run(_drive_one(transport, bus))

    assert len(transport.sent) == 1
    q_message_id = transport.sent[0]["id"]
    assert "архивировать" in transport.sent[0]["text"]

    waiting = sessions.list_sessions(status="waiting_user", limit=10)
    assert len(waiting) == 1
    mail_session_id = waiting[0]["id"]
    assert db.message_binding_get(q_message_id) == mail_session_id

    # A second, unrelated sessionless approval pops up while the first
    # is still waiting -- reply-to-message binding must resolve to the
    # mail session regardless, with zero LLM disambiguation calls.
    from agent import approval
    approval.request("spotrent_stop", {}, "spotrent_stop()", task_session_id=None)

    def _boom(*a, **kw):
        raise AssertionError("reply-to-message binding must resolve without any LLM disambiguation call")
    monkeypatch.setattr(routing, "groq_chat", _boom)
    monkeypatch.setattr(executor, "generation_chat", AsyncMock(return_value=_final_response("Ок, архивирую весь спам.")))

    asyncio.run(transport.feed("только спам", reply_to_message_id=q_message_id))
    asyncio.run(_drive_one(transport, bus))
    assert "архивирую" in transport.sent[-1]["text"]
    assert sessions.get(mail_session_id)["status"] == "done"

    for it in routing.pending_items():
        from agent import hanging
        hanging.resolve(it["id"], "test cleanup")


def test_yellow_approval_full_cycle_over_local_transport(tools_ctx, monkeypatch):
    """message -> session -> yellow approval -> "да" -> execution ->
    report -- the specific leg the audit named, driven entirely
    through LocalTransport rather than a bare send_fn."""
    transport = LocalTransport()
    bus = BusClient()

    call_count = {"n": 0}
    def _fake_shell(command: str) -> str:
        call_count["n"] += 1
        return "готово: диск почищен"

    import agent.tools as tools
    monkeypatch.setitem(tools.TOOLS_MAP, "system_shell", _fake_shell)
    monkeypatch.setattr(controller, "classify", AsyncMock(return_value={
        "route": "deep", "context": "task", "intent": "почистить диск",
        "tool_categories": [], "missing_info": [],
    }))
    monkeypatch.setattr(controller, "make_plan", AsyncMock(return_value={"steps": [], "max_iterations": 5}))
    monkeypatch.setattr(executor, "generation_chat", AsyncMock(
        return_value=_tool_call_response("system_shell", '{"command": "rm -rf /tmp/cache"}')
    ))

    asyncio.run(transport.feed("почисти диск на сервере"))
    asyncio.run(_drive_one(transport, bus))

    assert call_count["n"] == 0, "yellow tool must not execute before confirmation"
    assert "подтверждение" in transport.sent[-1]["text"].lower()

    asyncio.run(transport.feed("да"))
    asyncio.run(_drive_one(transport, bus))

    assert call_count["n"] == 1, "explicit yes over LocalTransport should run the tool exactly once"
    assert "почищен" in transport.sent[-1]["text"]


def test_crash_restart_resume_over_local_transport(tools_ctx):
    transport = LocalTransport()
    bus = BusClient()

    cs = sessions.create("Настроить автобэкап", origin="chat")
    with db.get_conn() as conn:
        conn.execute("UPDATE task_sessions SET status='active' WHERE id=%s", (cs["id"],))
    sessions.log_decision(cs["id"], "step_started", 'call_9 system_shell({"command": "crontab -e"})')
    db.agent_heartbeat()  # simulates the last heartbeat before an unclean exit (kill -9 or otherwise)

    assert cr.detect_crash() is True
    crash_msg = cr.recover_after_crash()
    assert crash_msg and "Настроить автобэкап" in crash_msg

    asyncio.run(notify.deliver("critical", crash_msg, transport.send, source="crash_recovery"))
    assert crash_msg in transport.sent[-1]["text"]

    import unittest.mock as mock
    with mock.patch.object(executor, "generation_chat", AsyncMock(
        return_value=_final_response("Проверила -- crontab не тронут, настраиваю.")
    )):
        asyncio.run(transport.feed("да"))
        asyncio.run(_drive_one(transport, bus))

    assert "Проверила" in transport.sent[-1]["text"]
    assert sessions.get(cs["id"])["status"] == "done"
