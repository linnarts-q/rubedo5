"""Stage 9.5: the "skill" route/dispatch concept is fully retired, not
just degraded. rubedo4's separate skills/registry.py was never ported
-- weather/music/news are ordinary tools (§13 categories) reachable
through the normal simple/deep tool-calling path. Priority: classify()
can never produce "skill" anymore (success path, invalid-route
correction, or exception fallback), and a stray "skill" reaching
controller.py degrades to "simple" rather than crashing.
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock

import agent.classifier as classifier


def test_valid_routes_no_longer_include_skill():
    assert "skill" not in classifier._VALID_ROUTES
    assert classifier._VALID_ROUTES == {"simple", "deep", "command"}


def test_classify_exception_fallback_has_no_skill_field(monkeypatch):
    async def _boom(*a, **kw):
        raise RuntimeError("groq down")
    monkeypatch.setattr(classifier, "groq_chat", _boom)

    result = asyncio.run(classifier.classify("погода"))
    assert result["route"] == "simple"
    assert "skill" not in result


def test_classify_corrects_a_stray_skill_route_to_simple(monkeypatch):
    """If the LLM somehow still emits the old "skill" route (stale
    prompt cache, fine-tuned quirk), classify()'s own validation must
    still catch it, same as any other invalid route value."""
    def _fake_response(content):
        msg = types.SimpleNamespace(content=content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    monkeypatch.setattr(classifier, "groq_chat", AsyncMock(return_value=_fake_response(
        '{"route": "skill", "context": "info", "intent": "погода", '
        '"skill": "weather", "missing_info": [], "tool_categories": ["web"]}'
    )))
    result = asyncio.run(classifier.classify("погода"))
    assert result["route"] == "simple"


def test_controller_degrades_a_stray_skill_route_without_dispatch(monkeypatch, tools_ctx):
    """Belt-and-suspenders: even if route_info somehow says "skill",
    controller.py must not try to import skills/registry.py (it
    doesn't exist) -- just fall through as "simple"."""
    import agent.controller as controller
    import agent.executor as executor

    monkeypatch.setattr(controller, "classify", AsyncMock(return_value={
        "route": "skill", "context": "info", "intent": "погода",
        "tool_categories": [], "missing_info": [],
    }))

    def _final_response(text):
        msg = types.SimpleNamespace(content=text, tool_calls=None)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])
    monkeypatch.setattr(executor, "generation_chat", AsyncMock(
        return_value=_final_response("В Дублине сейчас облачно.")
    ))

    sent = []
    async def send_fn(t):
        sent.append(t)
    fake_bus = types.SimpleNamespace(publish=AsyncMock())
    asyncio.run(controller.handle_message(user_id=1, text="погода", bus_client=fake_bus, send_fn=send_fn))

    assert sent and "облачно" in sent[0]
