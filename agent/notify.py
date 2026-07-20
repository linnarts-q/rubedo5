"""Notification-level gate + work modes (stage 5, part 1 — the owner
is coming back separately for the fuller day-engine rework this feeds
into; this is the first real slice, not a placeholder).

Every proactive message Rubedo might send — pool-nudge today, day-task
reminders and an evening digest once that engine exists — should pass
through one policy point instead of each channel inventing its own
quiet-hours logic by hand. day/pool.py's own `_within_quiet_window`
was exactly that: a bespoke, disconnected copy of what this module now
centralizes.

Levels:
    urgent — always allowed, any mode, any time of day.
    normal — allowed only in mode "normal", within the day's wake/sleep
             window (WAKE_TIME..SLEEP_TIME).
    low    — like normal, but the caller may pass a narrower window
             instead (pool-nudges use their own gentler
             POOL_QUIET_START/END — a low-priority nudge landing right
             after waking up or right before sleep is worse than one
             landing mid-afternoon).

Modes (day_state.checkin_mode — ported from rubedo4's schema but never
wired to anything until now):
    normal — default; gate behaves as described above.
    quiet  — do-not-disturb: only "urgent" gets through, regardless of
             time. Suppressed messages are simply not sent yet, not
             queued for later — batching into a digest needs the
             fuller day-engine rework this is a first slice of.
"""
from __future__ import annotations

from datetime import datetime

from config import WAKE_TIME, SLEEP_TIME

_VALID_MODES = {"normal", "quiet"}


def _now_hm() -> str:
    return datetime.now().strftime("%H:%M")


def _within_window(start: str, end: str) -> bool:
    now = _now_hm()
    if start <= end:
        return start <= now < end
    return now >= start or now < end  # window crosses midnight


def get_mode() -> str:
    from day.state import get_today_state
    state = get_today_state() or {}
    mode = state.get("checkin_mode") or "normal"
    return mode if mode in _VALID_MODES else "normal"


def set_mode(mode: str) -> None:
    from day.state import set_checkin_mode
    set_checkin_mode(mode)


def should_notify(level: str, quiet_start: str | None = None, quiet_end: str | None = None) -> bool:
    """The gate. `quiet_start`/`quiet_end` let a specific channel use
    its own gentler window instead of the default WAKE_TIME..SLEEP_TIME
    — omit them to just use the day's window."""
    if level == "urgent":
        return True
    if get_mode() != "normal":
        return False
    start = quiet_start or WAKE_TIME
    end = quiet_end or SLEEP_TIME
    return _within_window(start, end)
