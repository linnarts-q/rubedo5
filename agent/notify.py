"""Notification severity + delivery policy (§7, day-engine 5.0
responsibility 3).

The day engine (day/phase.py) never sends messages itself. Every
outgoing proactive message — pool-nudge today, day-task reminders and
a digest once those pieces exist — is a notification with its own
severity, gated through should_deliver()/notify_or_bundle() here. This
module only tracks which severities are deliverable right now; it has
no opinion on what gets sent or when a briefing actually flushes a
bundle — that content-generation piece is separate, later work.

Severities:
    critical — always deliverable, any phase, any override, any window.
    normal   — deliverable unless the current policy is restricted.
    low      — same restriction as normal today; kept distinct since a
               future policy may treat it differently (e.g. a channel-
               specific narrower window) without call sites changing.

The current policy is "critical-only" whenever ANY of:
    - day phase (day/phase.py) is "night"
    - today is a day-off (day_state.is_dayoff) — day-off is a PARALLEL
      preset, not a phase, so it overrides regardless of which phase
      is actually active right now
    - the owner has manually set checkin_mode to "quiet" — an explicit
      do-not-disturb override that coexists with the automatic phase
      machine (e.g. "in a meeting" during an otherwise normal day)
Otherwise every severity is deliverable — "день = все уровни".

This replaces what day/pool.py used to do by hand: check is_dayoff
itself and filter by priority threshold — exactly the "binary day-off
suppression" the day-engine plan calls out for deletion as its own
class of special-cased logic. day/pool.py now just calls
should_deliver("low", ...) like any other channel would; day-off isn't
special-cased in pool-specific code anywhere anymore, it's one more
input to one shared policy function.

`should_deliver`/`notify_or_bundle` additionally accept an optional
narrower time window (`quiet_start`/`quiet_end`) for non-critical
severities — this is a separate, finer-grained concern from the phase
machine (a "day" phase still shouldn't necessarily nudge the owner at
09:05, right as he's waking up) and defaults to the day's own
WAKE_TIME..SLEEP_TIME window when the caller doesn't supply one.
"""
from __future__ import annotations

from datetime import datetime

from config import WAKE_TIME, SLEEP_TIME

SEVERITIES = ("critical", "normal", "low")
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


def get_current_policy() -> dict:
    """Returns {"restricted": bool, "deliver": [...], "bundle": [...]}."""
    import day.phase as phase
    from day.state import get_today_state

    restricted = phase.current() == "night"
    state = get_today_state() or {}
    if state.get("is_dayoff"):
        restricted = True
    if get_mode() == "quiet":
        restricted = True

    if restricted:
        return {"restricted": True, "deliver": ["critical"], "bundle": ["normal", "low"]}
    return {"restricted": False, "deliver": list(SEVERITIES), "bundle": []}


def should_deliver(severity: str, quiet_start: str | None = None, quiet_end: str | None = None) -> bool:
    """The gate. `quiet_start`/`quiet_end` let a specific channel apply
    its own narrower window on top of the phase-driven policy for
    non-critical severities — omit them to just use the day's window."""
    if severity == "critical":
        return True
    if severity not in get_current_policy()["deliver"]:
        return False
    start = quiet_start or WAKE_TIME
    end = quiet_end or SLEEP_TIME
    return _within_window(start, end)


def notify_or_bundle(
    severity: str, content: str, source: str = "",
    quiet_start: str | None = None, quiet_end: str | None = None,
) -> bool:
    """The actual call a channel makes: True means "send it now",
    False means it was queued into notification_bundle instead (for a
    briefing, once that piece exists, to flush) — nothing is silently
    dropped just because the policy said "not now"."""
    if should_deliver(severity, quiet_start, quiet_end):
        return True
    from memory.db import save_bundled_notification
    save_bundled_notification(severity, content, source)
    return False
