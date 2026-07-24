"""agent/routing.py -- message routing for waiting_user sessions (§2
phase 2 step 3). Priority by risk (9.2): reply-to-message binding must
be deterministic (never touch the LLM), a lone pending item resolves
without the LLM too, and "1/2"-style disambiguation must never resolve
anything on low confidence -- it asks honestly instead of guessing.
"""
from __future__ import annotations

import asyncio
import json
import time
import types
from unittest.mock import AsyncMock

import memory.db as db
import agent.routing as routing
import agent.approval as approval
import agent.questions as questions
import agent.sessions as sessions


def _groq_json(payload):
    msg = types.SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


def test_nothing_pending_returns_none(tools_ctx):
    assert asyncio.run(routing.resolve("привет")) is None


def test_single_sessionless_approval_resolves_directly(tools_ctx):
    approval.request("spotrent_stop", {}, "spotrent_stop()", task_session_id=None)
    target = asyncio.run(routing.resolve("да"))
    assert target is not None and target["kind"] == "approval"
    assert target["session_id"] is None
    assert target["payload"]["name"] == "spotrent_stop"


def test_single_ask_user_session_resolves_directly(tools_ctx):
    s1 = sessions.start("проверить бэкапы")
    questions.ask(s1["id"], "По бэкапам: какой сервер?", [{"role": "user", "content": "x"}], [], 5)
    target = asyncio.run(routing.resolve("основной"))
    assert target["kind"] == "ask_user" and target["session_id"] == s1["id"]


def test_two_pending_high_confidence_disambiguation(tools_ctx, monkeypatch):
    approval.request("spotrent_stop", {}, "spotrent_stop()", task_session_id=None)
    s2 = sessions.start("найти билеты в Киев")
    questions.ask(
        s2["id"], "Про билеты в Киев: через Варшаву или Франкфурт?",
        [{"role": "user", "content": "x"}], [], 5,
    )
    monkeypatch.setattr(routing, "groq_chat", AsyncMock(return_value=_groq_json({"index": 2})))
    target = asyncio.run(routing.resolve("через Варшаву"))
    assert target["kind"] == "ask_user" and target["session_id"] == s2["id"]


def test_two_pending_low_confidence_asks_honestly_and_resolves_nothing(tools_ctx, monkeypatch):
    approval.request("spotrent_stop", {}, "spotrent_stop()", task_session_id=None)
    s2 = sessions.start("найти билеты в Киев")
    questions.ask(
        s2["id"], "Про билеты в Киев: через Варшаву или Франкфурт?",
        [{"role": "user", "content": "x"}], [], 5,
    )
    monkeypatch.setattr(routing, "groq_chat", AsyncMock(return_value=_groq_json({"index": None})))
    sent = []
    async def send_fn(t):
        sent.append(t)
    result = asyncio.run(routing.resolve("угу", send_fn=send_fn))

    assert result == {"handled": True}
    assert sent and "1 —" in sent[0] and "2 —" in sent[0]
    still_pending = routing.pending_items()
    assert len(still_pending) == 2, "low-confidence disambiguation must not resolve anything"


def test_reply_after_honest_question_reresolves_cleanly(tools_ctx, monkeypatch):
    approval.request("spotrent_stop", {}, "spotrent_stop()", task_session_id=None)
    s2 = sessions.start("найти билеты в Киев")
    questions.ask(
        s2["id"], "Про билеты в Киев: через Варшаву или Франкфурт?",
        [{"role": "user", "content": "x"}], [], 5,
    )
    monkeypatch.setattr(routing, "groq_chat", AsyncMock(return_value=_groq_json({"index": None})))
    asyncio.run(routing.resolve("угу", send_fn=lambda t: asyncio.sleep(0)))

    monkeypatch.setattr(routing, "groq_chat", AsyncMock(return_value=_groq_json({"index": 2})))
    target = asyncio.run(routing.resolve("2"))
    assert target["kind"] == "ask_user" and target["session_id"] == s2["id"]


def test_reply_to_message_binding_is_deterministic_no_llm(tools_ctx, monkeypatch):
    """Deterministic hard-bind must resolve even with 2+ items pending,
    and must NEVER call the LLM to do it."""
    s3 = sessions.create("сессия A", origin="chat")
    questions.ask(s3["id"], "Вопрос A", [{"role": "user", "content": "x"}], [], 5)
    s4 = sessions.create("сессия B", origin="chat")
    questions.ask(s4["id"], "Вопрос B", [{"role": "user", "content": "x"}], [], 5)

    msg_id = int(time.time() * 1000)  # unique per run -- message_bindings.message_id is a real PK
    db.message_binding_create(msg_id, s3["id"])

    def _boom(*a, **kw):
        raise AssertionError("reply-binding must resolve without ever calling the LLM")
    monkeypatch.setattr(routing, "groq_chat", _boom)

    target = asyncio.run(routing.resolve("что угодно", reply_to_message_id=msg_id))
    assert target["session_id"] == s3["id"]
