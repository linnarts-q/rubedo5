"""Day-task CRUD tools — add / list / details / done / cancel / reschedule.

`_session_id` (needed by add_task's research-scheduler branch) is read
lazily from the parent `agent.tools` package at call time to avoid
circular imports.
"""
from __future__ import annotations

import logging

log = logging.getLogger("rubedo.tools.tasks")


def add_task(
    title: str,
    description: str = "",
    scheduled_at: str = "",
    duration: int = 60,
    type: str = "soft",
) -> str:
    from day.state import add_day_task
    from day.research import needs_research
    from tasks.manager import schedule_rubedo_task
    from agent.tools import _session_id

    tid = add_day_task(
        title=title,
        description=description,
        scheduled_at=scheduled_at.strip() or None,
        duration=duration,
        task_type=type,
    )
    suffix = f" (запланирована на {scheduled_at})" if scheduled_at.strip() else ""

    if needs_research(title, description):
        schedule_rubedo_task(
            "research",
            {"title": title, "description": description, "session_id": _session_id},
        )
        suffix += " — начну собирать материалы"

    return f"Задача добавлена #{tid}: {title}{suffix}"


def list_tasks() -> str:
    from day.state import get_today_tasks
    tasks = get_today_tasks()
    if not tasks:
        return "Задач на сегодня нет."
    lines = []
    for idx, t in enumerate(tasks, start=1):
        line = f"{idx}. {t['title']}"
        if t.get("scheduled_at"):
            line += f" [{t['scheduled_at']}]"
        if t.get("status") not in ("pending", None):
            line += f" ({t['status']})"
        line += f" (id={t['id']})"
        if t.get("status") == "failed":
            line = f"~{line}~"
        lines.append(line)
    return "\n".join(lines)


def get_task_details(task_id: int) -> str:
    import memory.db as db
    with db.get_conn() as conn:
        import sqlite3 as _sqlite3
        conn.row_factory = _sqlite3.Row
        row = conn.execute(
            "SELECT id, title, description, scheduled_at, duration, type, status "
            "FROM day_tasks WHERE id=?",
            (task_id,),
        ).fetchone()
    if not row:
        return f"Задача не найдена."
    parts = [f"«{row['title']}»"]
    if row["scheduled_at"]:
        parts.append(f"Время: {row['scheduled_at']}")
    parts.append(f"Длительность: {row['duration']} мин.")
    parts.append(f"Тип: {row['type']}")
    parts.append(f"Статус: {row['status']}")
    if row["description"]:
        parts.append(f"Описание: {row['description']}")
    return "\n".join(parts)


def _task_title(task_id: int) -> str | None:
    """Look up a task title by id, for use in user-facing tool replies."""
    import memory.db as db
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT title FROM day_tasks WHERE id=?", (task_id,),
        ).fetchone()
    return row[0] if row else None


def mark_task_done(task_id: int) -> str:
    from day.state import update_task_status
    title = _task_title(task_id)
    update_task_status(task_id, "done")
    return (
        f"Задача «{title}» отмечена выполненной." if title
        else "Задача отмечена выполненной."
    )


def mark_task_failed(task_id: int) -> str:
    from day.state import update_task_status
    title = _task_title(task_id)
    update_task_status(task_id, "failed")
    return (
        f"Задача «{title}» отмечена проваленой." if title
        else "Задача отмечена проваленой."
    )


def remove_task(task_id: int) -> str:
    from day.state import update_task_status
    title = _task_title(task_id)
    update_task_status(task_id, "cancelled")
    return (
        f"Задача «{title}» убрана из плана." if title
        else "Задача убрана из плана."
    )


def reschedule_task(task_id: int, scheduled_at: str) -> str:
    from day.state import reschedule_task as _reschedule
    title = _task_title(task_id)
    _reschedule(task_id, scheduled_at.strip() or None)
    suffix = f" на {scheduled_at}" if scheduled_at.strip() else " (время снято)"
    name = f"«{title}»" if title else "Задача"
    return f"{name} перенесена{suffix}."
