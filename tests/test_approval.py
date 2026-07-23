"""agent/approval.py -- yellow/red-zone confirmation gate (techspec
§1). Priority by risk (9.2): a yellow action must never execute on the
turn it was requested, an explicit "no" must actually cancel it (not
just leave it pending), and a stale request must expire rather than
execute silently later.
"""
from __future__ import annotations

import asyncio
import types
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import memory.db as db
import agent.approval as approval
import agent.sessions as sessions


def _final_response(text):
    msg = types.SimpleNamespace(content=text, tool_calls=None)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


def _tool_call_response(name, args="{}"):
    msg = types.SimpleNamespace(
        content=None,
        tool_calls=[types.SimpleNamespace(
            id="call1", function=types.SimpleNamespace(name=name, arguments=args),
        )],
    )
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


async def _drive(text, send_fn, reply_to_message_id=None):
    import agent.controller as controller
    fake_bus = types.SimpleNamespace(publish=AsyncMock())
    await controller.handle_message(
        user_id=1, text=text, bus_client=fake_bus, send_fn=send_fn,
        reply_to_message_id=reply_to_message_id,
    )


def test_yellow_tool_never_executes_on_first_turn(tools_ctx, monkeypatch):
    """A yellow-zone call halts the turn and asks for confirmation --
    the actual tool function must never run until it's been answered."""
    import agent.controller as controller
    import agent.executor as executor
    import agent.tools as tools

    called = {"ran": False}

    def _fake_shell(command: str) -> str:
        called["ran"] = True
        return "should never run"

    monkeypatch.setitem(tools.TOOLS_MAP, "system_shell", _fake_shell)
    monkeypatch.setattr(controller, "classify", AsyncMock(return_value={
        "route": "deep", "context": "task", "intent": "почистить логи",
        "tool_categories": [], "missing_info": [],
    }))
    monkeypatch.setattr(controller, "make_plan", AsyncMock(return_value={"steps": [], "max_iterations": 5}))
    monkeypatch.setattr(executor, "generation_chat", AsyncMock(
        return_value=_tool_call_response("system_shell", '{"command": "rm -rf /var/log/*"}')
    ))

    sent = []
    async def send_fn(t):
        sent.append(t)

    asyncio.run(_drive("почисти логи на сервере", send_fn))

    assert called["ran"] is False, "yellow tool executed without confirmation!"
    assert sent and "подтверждение" in sent[0].lower() and "да/нет" in sent[0].lower(), sent
    waiting = sessions.list_sessions(status="waiting_user", limit=10)
    assert len(waiting) == 1


def test_explicit_yes_executes_exactly_once(tools_ctx, monkeypatch):
    import agent.controller as controller
    import agent.executor as executor
    import agent.tools as tools

    call_count = {"n": 0}

    def _fake_shell(command: str) -> str:
        call_count["n"] += 1
        return "готово: логи почищены"

    monkeypatch.setitem(tools.TOOLS_MAP, "system_shell", _fake_shell)
    monkeypatch.setattr(controller, "classify", AsyncMock(return_value={
        "route": "deep", "context": "task", "intent": "почистить логи",
        "tool_categories": [], "missing_info": [],
    }))
    monkeypatch.setattr(controller, "make_plan", AsyncMock(return_value={"steps": [], "max_iterations": 5}))
    monkeypatch.setattr(executor, "generation_chat", AsyncMock(
        return_value=_tool_call_response("system_shell", '{"command": "echo cleanup"}')
    ))

    sent = []
    async def send_fn(t):
        sent.append(t)

    asyncio.run(_drive("почисти логи на сервере", send_fn))
    assert call_count["n"] == 0

    asyncio.run(_drive("да", send_fn))
    assert call_count["n"] == 1, "explicit yes should run the tool exactly once"
    assert "почищены" in sent[-1]


def test_explicit_no_cancels_and_never_executes(tools_ctx, monkeypatch):
    import agent.controller as controller
    import agent.executor as executor
    import agent.tools as tools

    called = {"ran": False}

    def _fake_shell(command: str) -> str:
        called["ran"] = True
        return "should never run"

    monkeypatch.setitem(tools.TOOLS_MAP, "system_shell", _fake_shell)
    monkeypatch.setattr(controller, "classify", AsyncMock(return_value={
        "route": "deep", "context": "task", "intent": "почистить логи",
        "tool_categories": [], "missing_info": [],
    }))
    monkeypatch.setattr(controller, "make_plan", AsyncMock(return_value={"steps": [], "max_iterations": 5}))
    monkeypatch.setattr(executor, "generation_chat", AsyncMock(
        return_value=_tool_call_response("system_shell", '{"command": "echo cleanup"}')
    ))

    sent = []
    async def send_fn(t):
        sent.append(t)

    asyncio.run(_drive("почисти логи на сервере", send_fn))
    asyncio.run(_drive("нет", send_fn))

    assert called["ran"] is False, "explicit 'no' must never let the tool execute"
    assert "тмен" in sent[-1].lower()  # "Отменила, не выполняю."
    assert approval.pending() is None


def test_stale_approval_expires_and_fails_linked_session(tools_ctx):
    """Past APPROVAL_TTL_HOURS, a pending approval must not sit there
    forever waiting to be silently confirmed later -- it expires, and
    the session it was blocking is marked failed, not left in limbo."""
    cs = sessions.create("Почистить логи на сервере", origin="chat")
    with db.get_conn() as conn:
        conn.execute("UPDATE task_sessions SET status='active' WHERE id=%s", (cs["id"],))
    sessions.wait_user(cs["id"], reason="approval_pending")
    approval.request("system_shell", {"command": "echo x"}, "system_shell: echo x", task_session_id=cs["id"])

    assert approval.pending() is not None  # fresh -> still pending

    stale_ts = (datetime.utcnow() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE hanging_questions SET created_at=%s WHERE kind='approval' AND status='pending'",
            (stale_ts,),
        )

    assert approval.pending() is None, "stale approval must expire, not stay confirmable"
    assert sessions.get(cs["id"])["status"] == "failed"
