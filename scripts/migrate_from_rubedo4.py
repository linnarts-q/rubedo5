"""One-time data migration: rubedo4 (SQLite) -> rubedo5 (Postgres).

Run once, on the machine where rubedo4's SQLite file lives (the
mini-PC), against an already-initialized rubedo5 Postgres database
(memory.db.init_db() must have run first). Schema names/columns line
up almost 1:1 by design (stage 1.5's Postgres migration kept them
deliberately parallel) -- this is a straight table-by-table copy, not
a transformation.

Scope (Лин's call): curated memory in full -- facts, summaries,
events, experience, internal_notes, wishes, day_tasks,
recurring_tasks, week_events, rubedo_tasks, insights, pool_tasks,
rubedo_queue(+recurring), profiles -- plus only the most recent slice
of raw `messages` (--messages-limit, default 20 rows ~ 10 exchanges),
not the whole chat log. Every migrated user-role message gets tagged
`[закрыто]` (stage 9.6) -- §11 layer 1's outcome-annotation only ever
looks at TODAY's day_tasks, so it can never retroactively mark an old
instruction among these as done/cancelled; left untagged, a weak model
could read a months-old "сделай Х" as still standing the moment it
lands in fresh context.

Deliberately NOT migrated:
  - working_memory: dead table in rubedo5 -- no read/write function
    anywhere references it. Transient per-session scratch state that
    never outlived a single old conversation anyway.
  - day_state / day_phase_state: daily operational bookkeeping, not
    memory -- a leftover briefing_done flag from three months ago has
    no value, and day_phase_state didn't exist in rubedo4 at all.
  - task_sessions, session_decisions, hanging_questions,
    message_bindings, notification_bundle, agent_state, credentials:
    all new-in-5 concepts (§2, §7, §14, §15, stage 7.5) that simply
    didn't exist in rubedo4 -- nothing to map them from.

Idempotent: every insert preserves the source row's original id and
uses `ON CONFLICT DO NOTHING` (bare -- covers any unique constraint on
the table, not just the PK), so re-running after a partial or repeated
run only ever fills gaps, never duplicates. Each table's id sequence
is bumped past the highest migrated id afterward so rubedo5's own new
rows don't collide with it later.

Usage:
    python scripts/migrate_from_rubedo4.py /path/to/rubedo4/data/rubedo.db
    python scripts/migrate_from_rubedo4.py /path/to/rubedo.db --apply
    python scripts/migrate_from_rubedo4.py /path/to/rubedo.db --apply --messages-limit 10

Without --apply this only counts and prints what WOULD move, touching
neither database.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.db import get_conn  # noqa: E402


def _reset_running_queue_task(row: dict) -> dict:
    """A 'running' queue task's process died with the old bot -- there's
    nothing in memory to resume, it just gets a fresh shot as 'pending'."""
    if row.get("status") == "running":
        row = dict(row, status="pending", started_at=None)
    return row


_TABLES: list[dict] = [
    {"name": "facts", "cols": ["content", "owner", "interest", "created_at"]},
    {"name": "summaries", "cols": ["session_id", "content", "created_at"]},
    {"name": "internal_notes", "cols": ["content", "tags", "created_at"]},
    {"name": "events", "cols": [
        "session_id", "content", "priority", "tags", "category",
        "last_accessed", "access_count", "archived", "created_at", "updated_at",
    ]},
    {"name": "experience", "cols": [
        "task_description", "date", "tool_chain", "result", "success", "created_at",
    ]},
    {"name": "reminders", "cols": ["session_id", "text", "remind_at", "created_at", "done"]},
    {"name": "wishes", "cols": ["content", "done", "created_at"]},
    {"name": "day_tasks", "cols": [
        "date", "title", "description", "type", "scheduled_at", "duration", "status",
        "nudge_count", "last_nudge", "position", "recurring_id", "verified_by",
        "nudges_fired", "completed_at",
    ]},
    {"name": "recurring_tasks", "cols": [
        "title", "description", "type", "days", "time", "duration", "active",
    ]},
    {"name": "week_events", "cols": [
        "title", "description", "event_date", "event_time", "week_of", "status",
        "remind_days", "created_at",
    ]},
    {"name": "rubedo_tasks", "cols": ["type", "payload", "trigger_at", "status", "created_at"]},
    {"name": "insights", "cols": ["key", "value", "updated_at"]},
    {"name": "pool_tasks", "cols": [
        "title", "description", "priority", "created_at", "last_nudged_at",
        "completed_at", "nudge_count", "snoozed_until",
    ]},
    {"name": "rubedo_queue", "cols": [
        "title", "description", "status", "priority", "scheduled_at", "depends_on",
        "max_retries", "retry_count", "result", "error", "created_at", "started_at",
        "completed_at",
    ], "transform": _reset_running_queue_task},
    {"name": "rubedo_queue_recurring", "cols": [
        "title", "description", "priority", "recurrence", "next_run_at",
        "created_at", "enabled",
    ]},
    {"name": "profiles", "cols": ["entity", "key", "value", "updated_at"]},
]


def _fetch_all(sqlite_conn: sqlite3.Connection, table: str, cols: list[str]) -> list[dict]:
    col_sql = ", ".join(["id"] + cols)
    rows = sqlite_conn.execute(f"SELECT {col_sql} FROM {table} ORDER BY id ASC").fetchall()
    keys = ["id"] + cols
    return [dict(zip(keys, r)) for r in rows]


def _fetch_recent_messages(sqlite_conn: sqlite3.Connection, limit: int) -> list[dict]:
    """Tags every migrated user-role message with [закрыто] -- critical
    per §11 layer 1 (agent/outcomes.py): that mechanism only annotates
    outcomes by matching TODAY's day_tasks and terminal-state queue
    items, so it can never retroactively catch an old "сделай Х" among
    these migrated rows. Left unannotated, a weak model could read one
    as a still-standing order the moment it lands in fresh context --
    the exact bug §11 exists to prevent, just via a different route
    than the one it was built for. A permanent, generic tag (not one of
    outcomes.py's specific done/failed/cancelled labels, since the true
    outcome of a months-old instruction usually isn't known at
    migration time) baked directly into the stored content, so no
    runtime lookup is needed for these rows ever again."""
    cols = ["session_id", "role", "content", "created_at"]
    col_sql = ", ".join(["id"] + cols)
    rows = sqlite_conn.execute(
        f"SELECT {col_sql} FROM messages ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    keys = ["id"] + cols
    result = [dict(zip(keys, r)) for r in reversed(rows)]
    for row in result:
        if row["role"] == "user" and not row["content"].startswith("[закрыто]"):
            row["content"] = f"[закрыто] {row['content']}"
    return result


def _insert_rows(pg_conn, table: str, cols: list[str], rows: list[dict]) -> int:
    if not rows:
        return 0
    all_cols = ["id"] + cols
    placeholders = ", ".join(["%s"] * len(all_cols))
    col_sql = ", ".join(all_cols)
    sql = f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
    inserted = 0
    for row in rows:
        cur = pg_conn.execute(sql, [row[c] for c in all_cols])
        inserted += cur.rowcount
    pg_conn.execute(
        f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
        f"COALESCE((SELECT MAX(id) FROM {table}), 1))"
    )
    return inserted


def _build_plan(sqlite_conn: sqlite3.Connection, messages_limit: int) -> list[tuple[str, list[str], list[dict]]]:
    plan = []
    for spec in _TABLES:
        rows = _fetch_all(sqlite_conn, spec["name"], spec["cols"])
        if "transform" in spec:
            rows = [spec["transform"](r) for r in rows]
        plan.append((spec["name"], spec["cols"], rows))
    msg_cols = ["session_id", "role", "content", "created_at"]
    plan.append(("messages", msg_cols, _fetch_recent_messages(sqlite_conn, messages_limit)))
    return plan


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("sqlite_path", help="Путь к rubedo4 data/rubedo.db")
    ap.add_argument("--apply", action="store_true",
                     help="Реально записать в Postgres (по умолчанию — только подсчёт, ничего не трогает)")
    ap.add_argument("--messages-limit", type=int, default=20,
                     help="Сколько последних строк messages перенести (по умолчанию 20 ~ 10 обменов)")
    args = ap.parse_args()

    src_path = Path(args.sqlite_path)
    if not src_path.exists():
        print(f"Не найден файл: {src_path}", file=sys.stderr)
        sys.exit(1)

    sqlite_conn = sqlite3.connect(str(src_path))
    plan = _build_plan(sqlite_conn, args.messages_limit)
    sqlite_conn.close()

    print("ПЕРЕНОС" if args.apply else "ПРЕДПРОСМОТР (ничего не пишет — добавьте --apply, чтобы применить)")
    total = 0
    for name, _cols, rows in plan:
        label = f"{name} (последние {args.messages_limit})" if name == "messages" else name
        print(f"  {label}: {len(rows)}")
        total += len(rows)
    print(f"  ИТОГО: {total}")

    if not args.apply:
        return

    with get_conn() as pg_conn:
        for name, cols, rows in plan:
            n = _insert_rows(pg_conn, name, cols, rows)
            print(f"  {name}: вставлено {n} (из {len(rows)}; остальное уже было — конфликт пропущен)")

    print("Готово.")


if __name__ == "__main__":
    main()
