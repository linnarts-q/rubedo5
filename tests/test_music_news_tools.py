"""Music/news tools (§13, stage 9.5) -- ported from rubedo4's
skills/music.py and skills/news.py as plain tools rather than a
separate skill-dispatch route. Priority: registration (categories'
self-check already covers this at import time) plus the deterministic,
no-external-dependency paths -- no mpv/Tavily/Groq needed to verify
these.
"""
from __future__ import annotations

import asyncio

import agent.tools as tools
import agent.tools.music as music
import agent.tools.news as news_mod


def test_music_pause_when_nothing_running():
    music._mpv_proc = None
    assert tools.TOOLS_MAP["music_pause"]() == "Музыка не играет."


def test_music_play_with_no_playlist_configured(monkeypatch):
    monkeypatch.setattr(music, "_load_state", lambda: {})
    import config
    monkeypatch.setattr(config, "MUSIC_PLAYLIST", "", raising=False)
    result = tools.TOOLS_MAP["music_play"]("")
    assert "не задан" in result


def test_music_play_with_url_starts_mpv(monkeypatch):
    monkeypatch.setattr(music, "_load_state", lambda: {})
    saved = {}
    monkeypatch.setattr(music, "_save_state", lambda **kw: saved.update(kw))

    class _FakeProc:
        def poll(self):
            return None

    monkeypatch.setattr(music, "_start_mpv", lambda playlist, start_index=0: _FakeProc())
    result = tools.TOOLS_MAP["music_play"]("https://example.com/playlist")
    assert result == "Включила музыку."
    assert saved.get("playlist") == "https://example.com/playlist"


def test_music_resume_with_no_saved_state(monkeypatch):
    music._mpv_proc = None
    monkeypatch.setattr(music, "_load_state", lambda: {})
    assert tools.TOOLS_MAP["music_resume"]() == "Музыка не играет."


def test_news_without_tavily_key(monkeypatch):
    monkeypatch.setattr(news_mod, "TAVILY_API_KEY", "", raising=False)
    result = asyncio.run(tools.TOOLS_MAP["news"](""))
    assert "TAVILY_API_KEY" in result


def test_all_music_and_news_tools_registered():
    for name in [
        "music_play", "music_pause", "music_resume", "music_stop",
        "music_next", "music_louder", "music_quieter", "news",
    ]:
        assert name in tools.TOOLS_MAP
    schema_names = {f["function"]["name"] for f in tools.TOOLS_SCHEMA}
    for name in [
        "music_play", "music_pause", "music_resume", "music_stop",
        "music_next", "music_louder", "music_quieter", "news",
    ]:
        assert name in schema_names
