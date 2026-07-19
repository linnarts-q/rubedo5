from __future__ import annotations
import json
import logging
from memory.db import get_conn

log = logging.getLogger("rubedo.tasks")


def schedule_rubedo_task(type: str, payload: dict, trigger_at: str | None = None) -> int:
    """Add a proactive task to Rubedo's own queue."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO rubedo_tasks (type, payload, trigger_at) VALUES (?, ?, ?)",
            (type, json.dumps(payload, ensure_ascii=False), trigger_at),
        )
        return cur.lastrowid


def get_pending_rubedo_tasks() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, type, payload, trigger_at FROM rubedo_tasks "
            "WHERE status='pending' "
            "AND (trigger_at IS NULL OR trigger_at <= datetime('now', 'localtime')) "
            "ORDER BY created_at ASC"
        ).fetchall()
    return [
        {"id": r[0], "type": r[1], "payload": json.loads(r[2] or "{}"), "trigger_at": r[3]}
        for r in rows
    ]


def complete_rubedo_task(task_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE rubedo_tasks SET status='done' WHERE id=?", (task_id,))


def cancel_rubedo_task(task_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE rubedo_tasks SET status='cancelled' WHERE id=?", (task_id,))
