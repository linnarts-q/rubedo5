"""Idle-agenda (§6) — stage 9.4. Generator only, never an executor:
decides what's worth doing when there's truly nothing else, and writes
it into rubedo_queue via queue_add(). agent/queue_runner.py is the only
thing that ever picks a queue task up and runs it, through the exact
same session -> zones -> reflection -> journal path as anything else.
No second, "side" execution route for idle-initiated work (confirmed
directly with Лин, 9.4) — the boundary is generator vs executor, not
which module happened to create the row.

Trigger (day/tick.py, checked once per tick): no active sessions AND
rubedo_queue is empty AND day phase isn't night. Naturally
self-throttling once something IS queued — the queue stops being
empty until agent/queue_runner.py actually finishes that task, so this
can't pile up duplicate suggestions on back-to-back ticks by itself.
IDLE_AGENDA_COOLDOWN_HOURS additionally guards the case where nothing
gets queued at all (no pattern found, no active wishes) — without it,
every idle tick would re-scan for nothing, forever, while truly idle.

Each idea it queues is Rubedo's own initiative — agent/queue_runner.py
already logs an "initiative" decision-journal entry (§14) the moment
it picks the task up (agent/queue_runner.py:162 and friends); this
module's job is just to make sure the "why" is legible there too, by
writing it straight into the queued task's own description rather
than inventing a second, parallel journal mechanism.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from config import (
    IDLE_AGENDA_COOLDOWN_HOURS,
    IDLE_AGENDA_QUESTION_COOLDOWN_HOURS,
    IDLE_AGENDA_PATTERN_LOOKBACK_DAYS,
    IDLE_AGENDA_PATTERN_MIN_COUNT,
)

log = logging.getLogger("rubedo.day.agenda")

_META_LAST_RUN = "idle_agenda_last_run"
_META_LAST_QUESTION = "idle_agenda_last_question"

_WEEKDAY_NAMES = [
    "понедельникам", "вторникам", "средам", "четвергам",
    "пятницам", "субботам", "воскресеньям",
]


def _hours_since(meta_key: str) -> float:
    from memory.db import load_meta
    raw = load_meta(meta_key)
    if not raw:
        return float("inf")
    try:
        last = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return float("inf")
    return (datetime.now(timezone.utc).replace(tzinfo=None) - last).total_seconds() / 3600


def _mark(meta_key: str) -> None:
    from memory.db import save_meta
    save_meta(meta_key, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))


def _mine_weekday_pattern() -> dict | None:
    """A day_task title completed on the same weekday at least
    IDLE_AGENDA_PATTERN_MIN_COUNT times in the lookback window, never
    linked to a recurring task — an implicit regularity worth
    surfacing (С1's own example: "что бывает по вторникам")."""
    from memory.db import get_conn
    since = (datetime.now().date() - timedelta(days=IDLE_AGENDA_PATTERN_LOOKBACK_DAYS)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT title, EXTRACT(DOW FROM date::date) AS dow, COUNT(DISTINCT date) AS cnt "
            "FROM day_tasks WHERE recurring_id IS NULL AND status='done' AND date >= %s "
            "GROUP BY title, EXTRACT(DOW FROM date::date) "
            "HAVING COUNT(DISTINCT date) >= %s ORDER BY cnt DESC LIMIT 1",
            (since, IDLE_AGENDA_PATTERN_MIN_COUNT),
        ).fetchall()
    if not rows:
        return None
    row = rows[0]
    return {
        "title": row["title"],
        "dow_name": _WEEKDAY_NAMES[int(row["dow"])],
        "count": row["cnt"],
    }


def _already_queued(needle: str) -> bool:
    from memory.db import queue_list
    needle_low = needle.lower()
    return any(needle_low in (t.get("title") or "").lower() for t in queue_list())


def _queue_pattern_task() -> bool:
    pattern = _mine_weekday_pattern()
    if not pattern or _already_queued(pattern["title"]):
        return False
    from memory.db import queue_add
    queue_add(
        title=f"Разобраться с закономерностью: «{pattern['title']}»",
        description=(
            f"Инициатива idle-агенды (§6): «{pattern['title']}» встречалось "
            f"{pattern['count']}+ раз по {pattern['dow_name']} за последние "
            f"{IDLE_AGENDA_PATTERN_LOOKBACK_DAYS} дней, но не привязано ни к "
            "одной повторяющейся задаче. Стоит проверить, не совпадение ли "
            "это, и предложить Лин сделать регулярной."
        ),
        priority=2,
    )
    log.info(f"idle-agenda: queued weekday-pattern task for {pattern['title']!r}")
    return True


def _queue_wish_research() -> bool:
    from memory.db import get_active_wishes, queue_add
    wishes = get_active_wishes()
    if not wishes:
        return False
    wish = wishes[0]
    if _already_queued(wish["content"][:40]):
        return False
    queue_add(
        title=f"Разобраться с желанием: «{wish['content']}»",
        description=(
            f"Инициатива idle-агенды (§6): в списке wishes лежит "
            f"«{wish['content']}» без движения. Простой — время изучить и "
            "рассказать, что узнала."
        ),
        priority=2,
    )
    log.info(f"idle-agenda: queued wish-research task for wish #{wish['id']}")
    return True


async def _ask_have_tasks(send_fn) -> bool:
    if _hours_since(_META_LAST_QUESTION) < IDLE_AGENDA_QUESTION_COOLDOWN_HOURS:
        return False
    from agent import notify
    await notify.deliver(
        "normal", "Всё свободно, а идей больше нет — есть какие-то задачи для меня?",
        send_fn, source="idle_agenda",
    )
    _mark(_META_LAST_QUESTION)
    return True


async def run(send_fn) -> None:
    """Called from day/tick.py once its own trigger conditions hold
    (no active sessions, empty queue, not night). Tries each idea in
    order, stops at the first one that actually produces something —
    one idle tick, one new idea, never a burst of several at once."""
    if _hours_since(_META_LAST_RUN) < IDLE_AGENDA_COOLDOWN_HOURS:
        return
    _mark(_META_LAST_RUN)

    if _queue_pattern_task():
        return
    if _queue_wish_research():
        return
    await _ask_have_tasks(send_fn)
