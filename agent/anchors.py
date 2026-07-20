"""Negotiated anchor times (§2/§5, day-engine 5.0 responsibility 2).

Wake-up, briefing, and wrapup times aren't fixed constants (rubedo4's
_CADENCE). Each evening, Rubedo proposes tomorrow's times — defaulting
to yesterday's actual values, or config's WAKE_TIME/WORK_START/
WRAPUP_TIME on day one — the owner confirms or corrects with a single
reply, and silence past the TTL falls back to yesterday's values.
There's no hard auto-wake (§16): the cost of guessing wrong here is
high enough that "ask, then default gently" beats "assume and alarm."

Uses the same hanging-question mechanism (agent/hanging.py) as
approval gates and ask_user — a distinct kind ("anchor_negotiation")
rather than inventing another bespoke pending-slot.

This module doesn't decide WHEN to negotiate or when a day actually
starts — that's the day-engine tick this is one slice of, not built
yet. propose_tonight() is a plain function a future evening trigger
calls once; resolve_reply() is what agent/controller.py's matching
intercept calls with the owner's answer; ensure_resolved() is what a
future morning trigger (or briefing content generation) calls to
guarantee SOME resolved time exists even if the negotiation was never
answered.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta

from config import WAKE_TIME, WORK_START, WRAPUP_TIME
from agent import hanging
from day.state import get_anchor_times, set_anchor_times

log = logging.getLogger("rubedo.agent.anchors")

_KIND = "anchor_negotiation"
_ANCHOR_KEYS = ("wake_time", "briefing_time", "wrapup_time")
_DEFAULTS = {"wake_time": WAKE_TIME, "briefing_time": WORK_START, "wrapup_time": WRAPUP_TIME}

_ANCHOR_LABELS = {
    "wake_time": ("подъём", "будильник"),
    "briefing_time": ("брифинг",),
    "wrapup_time": ("врапап", "итоги"),
}

_CONFIRM_WORDS = {
    "да", "ок", "окей", "ok", "подходит", "го", "давай", "согласен", "согласна", "норм",
}


def _yesterday_or_default(for_date: str) -> dict:
    """Yesterday's actual values for each anchor, falling back to
    config defaults for whichever weren't set (e.g. the very first
    negotiation ever, or a gap in history)."""
    y = (date.fromisoformat(for_date) - timedelta(days=1)).isoformat()
    yv = get_anchor_times(y)
    return {k: (yv.get(k) or _DEFAULTS[k]) for k in _ANCHOR_KEYS}


def _format_proposal(target_date: str, proposed: dict) -> str:
    return (
        f"На завтра ({target_date}) предлагаю: подъём в {proposed['wake_time']}, "
        f"брифинг в {proposed['briefing_time']}, врапап в {proposed['wrapup_time']}. "
        "Го, или поправь."
    )


def propose_tonight(for_date: str | None = None) -> str:
    """Propose tomorrow's anchor times (or `for_date`, mainly for
    tests). Opens a hanging question and returns the proposal text —
    the caller is responsible for actually sending it."""
    target = for_date or (date.today() + timedelta(days=1)).isoformat()
    proposed = _yesterday_or_default(target)
    hanging.create(_KIND, {"date": target, "proposed": proposed})
    return _format_proposal(target, proposed)


def _extract_corrections(text: str) -> dict:
    """Small, deliberately non-clever parser: an "HH:MM" pattern found
    shortly after an anchor's Russian label. Anything not mentioned is
    left out — the caller merges with the proposal (already defaulted
    from yesterday), not with a second fallback, so a correction only
    touches what it actually corrects."""
    corrections: dict[str, str] = {}
    lower = text.lower()
    time_re = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
    for col, labels in _ANCHOR_LABELS.items():
        for label in labels:
            idx = lower.find(label)
            if idx == -1:
                continue
            window = lower[idx:idx + 40]
            m = time_re.search(window)
            if m:
                corrections[col] = f"{int(m.group(1)):02d}:{m.group(2)}"
            break
    return corrections


def pending() -> dict | None:
    return hanging.pending(_KIND)


def resolve_reply(text: str) -> str:
    """Apply the owner's reply to the pending negotiation. Returns a
    short confirmation string. If nothing is pending, says so plainly
    rather than silently doing nothing."""
    p = pending()
    if not p:
        return "Не жду сейчас подтверждения по времени якорей."
    hq_id = p.pop("_hanging_id")
    target = p["date"]
    proposed = p["proposed"]

    stripped = text.strip().lower().rstrip("!.,")
    if stripped in _CONFIRM_WORDS:
        final = dict(proposed)
    else:
        final = {**proposed, **_extract_corrections(text)}

    set_anchor_times(target, **final)
    hanging.resolve(hq_id, "answered")
    return (
        f"Записала на {target}: подъём {final['wake_time']}, "
        f"брифинг {final['briefing_time']}, врапап {final['wrapup_time']}."
    )


def ensure_resolved(for_date: str) -> dict:
    """Guarantee `for_date` has resolved anchor times even if the
    negotiation was never answered — "default on silence: yesterday's
    value" (§16, no hard auto-wake). Triggers the hanging-question TTL
    sweep as a side effect (same as calling pending() would), then
    fills in whichever anchors are still unset from yesterday/config
    defaults."""
    hanging.pending(_KIND)
    existing = get_anchor_times(for_date)
    if all(existing.get(k) for k in _ANCHOR_KEYS):
        return existing
    fallback = _yesterday_or_default(for_date)
    to_fill = {k: v for k, v in fallback.items() if not existing.get(k)}
    if to_fill:
        set_anchor_times(for_date, **to_fill)
    return get_anchor_times(for_date)
