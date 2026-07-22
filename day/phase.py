"""Day-phase state machine (§16, day-engine 5.0).

The day isn't a timeline, it's four phases — night, morning, day,
evening. day-off (day_state.is_dayoff, per-date) is a PARALLEL preset,
not a fifth phase — it rides alongside whatever phase is active rather
than replacing it; agent/notify.py's delivery policy reads both.

Transitions are triggered by events; the clock is never the only
trigger:

    night   -> morning : confirmed wake-up (alarm fired + a reaction)
    morning -> day      : first substantive message, or the first task
                          session of the day starting
    day     -> evening  : a confirmed wrapup
    evening -> night     : an explicit "спокойной ночи", or long silence
                          past a configured hour count (check_silence)

Deliberately no LLM here, and deliberately NOT keyed by calendar date
the way day_state is — a per-date row would silently reset at
midnight regardless of what actually happened (a wrapup confirmed at
23:58 has no business being undone by a date rollover two minutes
later). Content generation (briefing/wrapup text) is a separate,
later concern layered on top of a phase once it's entered; this module
only ever decides WHICH phase we're in.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from memory.db import get_day_phase, init_day_phase, set_day_phase

log = logging.getLogger("rubedo.day.phase")

PHASES = ("night", "morning", "day", "evening")


def _now_iso() -> str:
    return datetime.now().isoformat()


def _ensure_initialized() -> dict:
    row = get_day_phase()
    if row is None:
        init_day_phase("night", _now_iso())
        row = get_day_phase()
    return row


def current() -> str:
    """The active phase. A fresh install starts at "night" — presumed
    asleep until a real wake-up event fires, never auto-assumed
    awake just because the process started."""
    return _ensure_initialized()["phase"]


def entered_at() -> datetime:
    row = _ensure_initialized()
    return datetime.fromisoformat(row["entered_at"])


def _transition(expected_from: str, to: str) -> str | None:
    """Only moves if currently in `expected_from` — every transition is
    a no-op (returns None) from any other phase, so calling the wrong
    event handler at the wrong time can't corrupt state."""
    row = _ensure_initialized()
    if row["phase"] != expected_from:
        return None
    set_day_phase(to, _now_iso())
    log.info(f"Day phase: {expected_from} -> {to}")
    return to


def on_wake_confirmed() -> str | None:
    """night -> morning."""
    return _transition("night", "morning")


def on_first_activity() -> str | None:
    """morning -> day, on the first substantive message or task session."""
    return _transition("morning", "day")


def on_wrapup_confirmed() -> str | None:
    """day -> evening."""
    return _transition("day", "evening")


def on_goodnight() -> str | None:
    """evening -> night, explicit."""
    return _transition("evening", "night")


def check_silence(hours: float, last_message_at: datetime | None) -> str | None:
    """evening -> night after long silence past `last_message_at`.
    Only fires from "evening" — silence during "night" or "day" isn't
    this transition's concern, and silence isn't itself a trigger for
    any other phase (no clock-only path out of morning/day)."""
    if last_message_at is None:
        return None
    if _ensure_initialized()["phase"] != "evening":
        return None
    if datetime.now() - last_message_at >= timedelta(hours=hours):
        return _transition("evening", "night")
    return None
