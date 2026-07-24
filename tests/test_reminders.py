"""Reminders (stage 9.5) -- ported from rubedo4, previously entirely
inert in rubedo5 (set_reminder existed as a tool, but nothing ever
called get_pending_reminders()). Priority: a due reminder must
actually deliver at "normal" severity (not the spec's literal
"important", which doesn't exist in agent/notify.py), get marked done
exactly once, and remind_at must compare against LOCAL time (a
reminder set for "10 утра" means local wall-clock 10am, not UTC).
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import memory.db as db
import day.tick as tick
from config import now_local


def _fmt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def test_save_reminder_normalizes_iso_t_separator(tools_ctx):
    rid = db.save_reminder("lin", "Позвонить маме", "2026-08-01T10:00:00")
    row = next(r for r in db.get_pending_reminders() if r["id"] == rid)
    assert row["remind_at"] == "2026-08-01 10:00:00"


def test_due_reminder_is_found_by_local_time_not_utc(tools_ctx):
    past_local = _fmt(now_local() - timedelta(minutes=5))
    db.save_reminder("lin", "В прошлом (должно найтись)", past_local)
    future_local = _fmt(now_local() + timedelta(hours=2))
    db.save_reminder("lin", "В будущем (не должно найтись)", future_local)

    due = db.get_due_reminders()
    texts = [r["text"] for r in due]
    assert "В прошлом (должно найтись)" in texts
    assert "В будущем (не должно найтись)" not in texts


def test_tick_delivers_due_reminder_at_normal_severity(tools_ctx, monkeypatch):
    past_local = _fmt(now_local() - timedelta(minutes=1))
    db.save_reminder("lin", "Полить цветы", past_local)

    delivered = {}
    async def _fake_deliver(severity, content, send_fn, source="", **kw):
        delivered["severity"] = severity
        delivered["content"] = content
        delivered["source"] = source
        return None
    import agent.notify as notify
    monkeypatch.setattr(notify, "deliver", _fake_deliver)

    async def send_fn(t):
        pass
    asyncio.run(tick._check_reminders(send_fn))

    assert delivered.get("severity") == "normal"
    assert "Полить цветы" in delivered.get("content", "")
    assert delivered.get("source") == "reminder"


def test_tick_marks_reminder_done_exactly_once(tools_ctx, monkeypatch):
    past_local = _fmt(now_local() - timedelta(minutes=1))
    rid = db.save_reminder("lin", "Разовое напоминание", past_local)

    async def _fake_deliver(severity, content, send_fn, source="", **kw):
        return None
    import agent.notify as notify
    monkeypatch.setattr(notify, "deliver", _fake_deliver)

    async def send_fn(t):
        pass
    asyncio.run(tick._check_reminders(send_fn))
    asyncio.run(tick._check_reminders(send_fn))  # second tick, must not re-fire

    row = next(r for r in db.list_reminders_for_session("lin") if r["id"] == rid)
    assert row["done"] is True
    assert db.get_due_reminders() == []


def test_tick_leaves_future_reminders_untouched(tools_ctx, monkeypatch):
    future_local = _fmt(now_local() + timedelta(hours=1))
    db.save_reminder("lin", "Ещё не время", future_local)

    delivered = []
    async def _fake_deliver(severity, content, send_fn, source="", **kw):
        delivered.append(content)
        return None
    import agent.notify as notify
    monkeypatch.setattr(notify, "deliver", _fake_deliver)

    async def send_fn(t):
        pass
    asyncio.run(tick._check_reminders(send_fn))

    assert delivered == []
    assert len(db.get_pending_reminders()) == 1
