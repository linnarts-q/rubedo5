"""agent/stopword.py -- deterministic stop-phrase (techspec §15).
Priority by risk (9.2): the stop/resume path must fire as a plain
string comparison, before any LLM call of any kind -- verified here by
making every LLM entry point raise if called at all, not just by
mocking them to succeed.
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock

import agent.controller as controller
import agent.stopword as stopword
from memory.db import save_meta


def _boom(*a, **kw):
    raise AssertionError("stop-phrase path must never reach an LLM call")


def _patch_all_llm_entry_points(monkeypatch):
    """Every place agent/controller.py could plausibly reach an LLM --
    if the deterministic stop-phrase check didn't short-circuit first,
    one of these would raise instead of silently succeeding."""
    monkeypatch.setattr(controller, "classify", _boom)
    monkeypatch.setattr(controller, "make_plan", _boom)
    import agent.executor as executor
    monkeypatch.setattr(executor, "generation_chat", _boom)
    import agent.routing as routing
    monkeypatch.setattr(routing, "groq_chat", _boom)


def test_stop_phrase_freezes_without_touching_any_llm(monkeypatch):
    monkeypatch.setattr(stopword, "STOP_PHRASE", "стоп машина")
    monkeypatch.setattr(stopword, "RESUME_PHRASE", "поехали")
    save_meta("autonomy_frozen", "0")
    _patch_all_llm_entry_points(monkeypatch)

    sent = []
    async def send_fn(t):
        sent.append(t)
    fake_bus = types.SimpleNamespace(publish=AsyncMock())

    asyncio.run(controller.handle_message(
        user_id=1, text="стоп машина", bus_client=fake_bus, send_fn=send_fn,
    ))

    assert stopword.is_frozen() is True
    assert sent and "становил" in sent[0].lower()


def test_resume_phrase_unfreezes_without_touching_any_llm(monkeypatch):
    monkeypatch.setattr(stopword, "STOP_PHRASE", "стоп машина")
    monkeypatch.setattr(stopword, "RESUME_PHRASE", "поехали")
    save_meta("autonomy_frozen", "1")
    _patch_all_llm_entry_points(monkeypatch)

    sent = []
    async def send_fn(t):
        sent.append(t)
    fake_bus = types.SimpleNamespace(publish=AsyncMock())

    asyncio.run(controller.handle_message(
        user_id=1, text="поехали", bus_client=fake_bus, send_fn=send_fn,
    ))

    assert stopword.is_frozen() is False
    assert sent and "озобновля" in sent[0].lower()


def test_empty_phrase_config_never_matches(monkeypatch):
    """Empty STOP_PHRASE/RESUME_PHRASE (the default, feature inert) must
    never accidentally match an empty or whitespace-only message."""
    monkeypatch.setattr(stopword, "STOP_PHRASE", "")
    monkeypatch.setattr(stopword, "RESUME_PHRASE", "")
    assert stopword.is_stop_phrase("") is False
    assert stopword.is_stop_phrase("   ") is False
    assert stopword.is_resume_phrase("") is False


def test_frozen_state_strips_all_tools_even_if_llm_requests_one(monkeypatch):
    """While frozen, a route that would normally load tools gets none
    at all -- the executor physically cannot fire a tool call, it's
    not a prompt-level request to behave."""
    monkeypatch.setattr(stopword, "STOP_PHRASE", "стоп машина")
    monkeypatch.setattr(stopword, "RESUME_PHRASE", "поехали")
    save_meta("autonomy_frozen", "1")

    monkeypatch.setattr(controller, "classify", AsyncMock(return_value={
        "route": "deep", "context": "task", "intent": "почисти логи",
        "tool_categories": ["system"], "missing_info": [],
    }))
    monkeypatch.setattr(controller, "make_plan", AsyncMock(return_value={"steps": [], "max_iterations": 5}))

    import agent.executor as executor
    captured = {}
    _orig = executor.run

    async def _spy_run(messages, tools_schema, tools_map, *a, **kw):
        captured["tools_schema"] = tools_schema
        captured["tools_map"] = tools_map
        msg = types.SimpleNamespace(content="Ничего не делаю, заморожена.", tool_calls=None)
        return "Ничего не делаю, заморожена.", messages

    monkeypatch.setattr(controller, "executor_run", _spy_run)

    sent = []
    async def send_fn(t):
        sent.append(t)
    fake_bus = types.SimpleNamespace(publish=AsyncMock())

    asyncio.run(controller.handle_message(
        user_id=1, text="почисти логи на сервере", bus_client=fake_bus, send_fn=send_fn,
    ))

    assert captured.get("tools_schema") == []
    assert captured.get("tools_map") == {}
