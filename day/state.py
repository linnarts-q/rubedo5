from __future__ import annotations
import json
import logging
from datetime import date, datetime
from memory.db import get_conn as _conn

log = logging.getLogger("rubedo.day.state")


def _notify_plan_changed() -> None:
    try:
        from bus.client import sync_publisher
        from bus.events import DayPlanUpdated
        sync_publisher.publish(DayPlanUpdated(date=date.today().isoformat()))
    except Exception as e:
        log.debug(f"plan-changed notify skipped: {e}")


def _notify_task_completed(task_id: int) -> None:
    try:
        from bus.client import sync_publisher
        from bus.events import TaskCompleted
        sync_publisher.publish(TaskCompleted(task_id=task_id))
    except Exception as e:
        log.debug(f"task-completed notify skipped: {e}")


def get_today_state() -> dict | None:
    today = date.today().isoformat()
    with _conn() as conn:
        row = conn.execute("SELECT * FROM day_state WHERE date=%s", (today,)).fetchone()
    return dict(row) if row else None


def ensure_today() -> dict:
    """Ensure today's state row exists, return it."""
    today = date.today().isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO day_state(date) VALUES(%s) ON CONFLICT (date) DO NOTHING",
            (today,),
        )
        row = conn.execute("SELECT * FROM day_state WHERE date=%s", (today,)).fetchone()
    return dict(row)


def hydrate_recurring() -> int:
    """Instantiate active recurring tasks into today's day_tasks if not already present.

    days field (JSON) supports: "daily", "weekday", "weekend", day names
    ("mon".."sun"), or weekday integers (0=Mon).
    Returns number of tasks added.
    """
    today = date.today()
    today_str = today.isoformat()
    weekday = today.weekday()
    day_name = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][weekday]
    added = 0

    with _conn() as conn:
        recurring = conn.execute("SELECT * FROM recurring_tasks WHERE active=1").fetchall()
        for task in recurring:
            row = dict(task)
            existing = conn.execute(
                "SELECT id FROM day_tasks WHERE date=%s AND recurring_id=%s",
                (today_str, row["id"]),
            ).fetchone()
            if existing:
                continue

            try:
                days = json.loads(row.get("days") or '["daily"]')
            except Exception:
                days = ["daily"]

            applies = (
                "daily" in days
                or ("weekday" in days and weekday < 5)
                or ("weekend" in days and weekday >= 5)
                or day_name in days
                or weekday in days
            )

            if applies:
                conn.execute(
                    "INSERT INTO day_tasks(date, title, description, scheduled_at, "
                    "duration, type, position, recurring_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        today_str, row["title"], row.get("description", ""),
                        row.get("time"), row.get("duration", 60),
                        row["type"], 999, row["id"],
                    ),
                )
                added += 1
    if added:
        _notify_plan_changed()
    return added


def set_briefing_done(value: bool = True) -> None:
    today = date.today().isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO day_state(date, briefing_done) VALUES(%s,%s) "
            "ON CONFLICT (date) DO UPDATE SET briefing_done=excluded.briefing_done",
            (today, int(value)),
        )


def set_wrapup_done(value: bool = True) -> None:
    today = date.today().isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO day_state(date, wrapup_done) VALUES(%s,%s) "
            "ON CONFLICT (date) DO UPDATE SET wrapup_done=excluded.wrapup_done",
            (today, int(value)),
        )


def set_day_off(value: bool = True) -> None:
    today = date.today().isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO day_state(date, is_dayoff) VALUES(%s,%s) "
            "ON CONFLICT (date) DO UPDATE SET is_dayoff=excluded.is_dayoff",
            (today, int(value)),
        )


def set_checkin_mode(mode: str) -> None:
    today = date.today().isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO day_state(date, checkin_mode) VALUES(%s,%s) "
            "ON CONFLICT (date) DO UPDATE SET checkin_mode=excluded.checkin_mode",
            (today, mode),
        )


def append_day_notes(text: str) -> None:
    today = date.today().isoformat()
    with _conn() as conn:
        existing = conn.execute("SELECT notes FROM day_state WHERE date=%s", (today,)).fetchone()
        old = existing["notes"] if existing else ""
        new_notes = (old + "\n" + text).strip()
        conn.execute(
            "INSERT INTO day_state(date, notes) VALUES(%s,%s) "
            "ON CONFLICT (date) DO UPDATE SET notes=excluded.notes",
            (today, new_notes),
        )


def get_today_tasks() -> list[dict]:
    today = date.today().isoformat()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM day_tasks WHERE date=%s AND status!='cancelled' "
            "ORDER BY position, scheduled_at",
            (today,),
        ).fetchall()
    return [dict(r) for r in rows]


def add_day_task(
    title: str,
    description: str = "",
    scheduled_at: str | None = None,
    duration: int = 60,
    task_type: str = "soft",
    position: int = 999,
    recurring_id: int | None = None,
    for_date: str | None = None,
) -> int:
    target_date = for_date or date.today().isoformat()
    with _conn() as conn:
        row = conn.execute(
            "INSERT INTO day_tasks(date, title, description, scheduled_at, duration, type, position, recurring_id) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (target_date, title, description, scheduled_at, duration, task_type, position, recurring_id),
        ).fetchone()
        new_id = row["id"]
    _notify_plan_changed()
    return new_id


def update_task_status(task_id: int, status: str, verified_by: str | None = None) -> None:
    now_iso = datetime.now().isoformat()
    with _conn() as conn:
        if status == "done":
            conn.execute(
                "UPDATE day_tasks SET status=%s, verified_by=%s, completed_at=%s WHERE id=%s",
                (status, verified_by, now_iso, task_id),
            )
        else:
            conn.execute(
                "UPDATE day_tasks SET status=%s WHERE id=%s", (status, task_id)
            )
    _notify_plan_changed()
    if status == "done":
        _notify_task_completed(task_id)


def reschedule_task(task_id: int, scheduled_at: str | None) -> None:
    """Reschedule a task. Clears nudges_fired so the cycle restarts from the new time."""
    with _conn() as conn:
        conn.execute(
            "UPDATE day_tasks SET scheduled_at=%s, nudges_fired='{}' WHERE id=%s",
            (scheduled_at, task_id),
        )
    _notify_plan_changed()


def get_nudges_fired(task_id: int) -> dict:
    """Return {point: ISO timestamp} for nudges already fired on this task."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT nudges_fired FROM day_tasks WHERE id=%s", (task_id,)
        ).fetchone()
    if not row or not row["nudges_fired"]:
        return {}
    try:
        return json.loads(row["nudges_fired"])
    except Exception:
        return {}


def mark_nudge_fired(task_id: int, point: str) -> None:
    """Append point→now to nudges_fired JSON for the task."""
    fired = get_nudges_fired(task_id)
    fired[point] = datetime.now().isoformat()
    with _conn() as conn:
        conn.execute(
            "UPDATE day_tasks SET nudges_fired=%s WHERE id=%s",
            (json.dumps(fired), task_id),
        )


def get_today_timed_tasks() -> list[dict]:
    """Today's pending/in_progress tasks that have a scheduled time."""
    today = date.today().isoformat()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM day_tasks WHERE date=%s AND scheduled_at IS NOT NULL "
            "AND status IN ('pending','in_progress') ORDER BY scheduled_at",
            (today,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_recently_completed(since_iso: str) -> list[dict]:
    """Today's tasks that became done after the given ISO timestamp."""
    today = date.today().isoformat()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM day_tasks WHERE date=%s AND status='done' "
            "AND completed_at IS NOT NULL AND completed_at > %s "
            "ORDER BY completed_at",
            (today, since_iso),
        ).fetchall()
    return [dict(r) for r in rows]


def get_unverified_today() -> list[dict]:
    """Today's done tasks where verified_by is auto or unknown — need wrapup confirmation."""
    today = date.today().isoformat()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM day_tasks WHERE date=%s AND status='done' "
            "AND verified_by IN ('auto','unknown') ORDER BY scheduled_at",
            (today,),
        ).fetchall()
    return [dict(r) for r in rows]


def increment_nudge(task_id: int) -> None:
    now = datetime.now().isoformat()
    with _conn() as conn:
        conn.execute(
            "UPDATE day_tasks SET nudge_count=nudge_count+1, last_nudge=%s WHERE id=%s",
            (now, task_id),
        )


def get_overdue_tasks() -> list[dict]:
    today = date.today().isoformat()
    now_str = datetime.now().strftime("%H:%M")
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM day_tasks WHERE date=%s AND status='pending' "
            "AND scheduled_at IS NOT NULL AND scheduled_at < %s "
            "ORDER BY scheduled_at",
            (today, now_str),
        ).fetchall()
    return [dict(r) for r in rows]


def get_active_recurring() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM recurring_tasks WHERE active=1").fetchall()
    return [dict(r) for r in rows]


_RECURRING_VALID_DAYS = {
    "daily", "weekday", "weekend",
    "mon", "tue", "wed", "thu", "fri", "sat", "sun",
}


def add_recurring(
    title: str,
    days: list[str],
    time: str | None = None,
    description: str = "",
    duration: int = 60,
    task_type: str = "soft",
) -> int:
    """Insert a recurring task. `days` accepts items from _RECURRING_VALID_DAYS;
    invalid items are silently dropped, falling back to 'daily' if the list
    becomes empty. Returns the new id."""
    cleaned = [d.lower().strip() for d in days if isinstance(d, str)]
    cleaned = [d for d in cleaned if d in _RECURRING_VALID_DAYS]
    if not cleaned:
        cleaned = ["daily"]
    with _conn() as conn:
        row = conn.execute(
            "INSERT INTO recurring_tasks(title, description, type, days, time, duration, active) "
            "VALUES(%s,%s,%s,%s,%s,%s,1) RETURNING id",
            (title, description, task_type, json.dumps(cleaned), time, duration),
        ).fetchone()
        return row["id"]


def delete_recurring(rid: int) -> bool:
    """Soft-delete: set active=0. Already-materialized day_tasks for today are
    not touched — caller can remove via task_remove if needed."""
    with _conn() as conn:
        cur = conn.execute("UPDATE recurring_tasks SET active=0 WHERE id=%s", (rid,))
        return cur.rowcount > 0


# ─ Tick timing helpers ─────────────────────────────────────────────────────

def get_last_tick_minutes(key: str) -> float:
    """Minutes since key was last marked. Returns large value if never recorded."""
    from memory.db import load_meta
    val = load_meta(key)
    if not val:
        return 9999.0
    try:
        last = datetime.fromisoformat(val)
        return (datetime.now() - last).total_seconds() / 60.0
    except Exception:
        return 9999.0


def mark_tick(key: str) -> None:
    """Record current timestamp for the given tick key."""
    from memory.db import save_meta
    save_meta(key, datetime.now().isoformat())
