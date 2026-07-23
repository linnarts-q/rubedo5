"""TTS (stage 9.5) -- ported from rubedo4 as-is (mechanism, not
architecture). Priority: registration, and that the tool doesn't block
the event loop or crash when neither edge-tts nor pyttsx3 actually
produce audio (no real speaker in this sandbox) -- the fallback chain
itself is what's under test, not real audio output.
"""
from __future__ import annotations

import asyncio

import agent.tools as tools
import tts.engine as engine


def test_speak_tool_registered():
    assert "speak" in tools.TOOLS_MAP
    schema_names = {f["function"]["name"] for f in tools.TOOLS_SCHEMA}
    assert "speak" in schema_names


def test_speak_async_falls_back_when_edge_tts_missing(monkeypatch):
    """Simulates edge-tts not being installed -- must fall through to
    the pyttsx3 path without raising, matching rubedo4's own
    ImportError handling."""
    import builtins
    real_import = builtins.__import__

    def _fake_import(name, *a, **kw):
        if name == "edge_tts":
            raise ImportError("no edge_tts")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    called = {"n": 0}
    monkeypatch.setattr(engine, "_speak_pyttsx3", lambda text: called.__setitem__("n", called["n"] + 1))

    asyncio.run(engine.speak_async("привет"))
    assert called["n"] == 1


def test_speak_tool_calls_speak_async(monkeypatch):
    called = {}
    async def _fake_speak_async(text):
        called["text"] = text
    monkeypatch.setattr(engine, "speak_async", _fake_speak_async)

    result = asyncio.run(tools.TOOLS_MAP["speak"]("тестовая фраза"))
    assert called.get("text") == "тестовая фраза"
    assert result == "Сказала вслух."
