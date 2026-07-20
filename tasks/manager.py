from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from memory.db import get_conn

log = logging.getLogger("rubedo.tasks")


def _now() -> str:
    """Naive-UTC text timestamp — same convention as memory.db._now()."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def schedule_rubedo_task(type: str, payload: dict, trigger_at: str | None = None) -> int:
    """Add a proactive task to Rubedo's own queue."""
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO rubedo_tasks (type, payload, trigger_at, created_at) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (type, json.dumps(payload, ensure_ascii=False), trigger_at, _now()),
        ).fetchone()
        return row["id"]


def get_pending_rubedo_tasks() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, type, payload, trigger_at FROM rubedo_tasks "
            "WHERE status='pending' "
            "AND (trigger_at IS NULL OR trigger_at <= %s) "
            "ORDER BY created_at ASC",
            (_now(),),
        ).fetchall()
    return [
        {"id": r["id"], "type": r["type"], "payload": json.loads(r["payload"] or "{}"), "trigger_at": r["trigger_at"]}
        for r in rows
    ]


def complete_rubedo_task(task_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE rubedo_tasks SET status='done' WHERE id=%s", (task_id,))


def cancel_rubedo_task(task_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE rubedo_tasks SET status='cancelled' WHERE id=%s", (task_id,))
