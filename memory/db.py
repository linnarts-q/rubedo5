from __future__ import annotations
import logging
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from config import DATABASE_URL

log = logging.getLogger("rubedo.db")

_RU_STOPWORDS = frozenset({
    'это', 'что', 'как', 'для', 'или', 'при', 'на', 'в', 'и', 'с', 'по', 'но', 'он',
    'она', 'они', 'мне', 'ты', 'я', 'не', 'то', 'из', 'от', 'до', 'за', 'под',
    'над', 'об', 'же', 'ли', 'бы', 'так', 'вот', 'уже', 'ещё', 'только', 'тоже',
    'был', 'есть', 'было', 'быть', 'the', 'is', 'are', 'was', 'for', 'and', 'not',
})


def _now() -> str:
    """UTC 'now' as text, matching what SQLite's plain `datetime('now')`
    used to produce for created_at/updated_at columns. Postgres has no
    equivalent implicit-UTC default that isn't also tangled up with the
    server's own timezone setting, so this is supplied from Python
    instead, same convention config.now_local() already uses for
    local-time values elsewhere."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _normalize_tag(word: str) -> str:
    if len(word) > 4 and word.isascii() and word.endswith('s'):
        return word[:-1]
    return word


def _extract_tags(text: str) -> str:
    words = re.findall(r'\b[а-яёa-zA-Z]{4,}\b', text.lower())
    seen, result = set(), []
    for w in words:
        normalized = _normalize_tag(w)
        if normalized not in _RU_STOPWORDS and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
        if len(result) >= 7:
            break
    return ' '.join(result)


# ─ Connection pool ──────────────────────────────────────────────────────────
# One shared pool for the whole process — memory/db.py, day/state.py,
# day/pool.py, agent/credentials.py, tasks/manager.py all go through
# get_conn() below rather than opening their own connections, unlike
# the SQLite version where each of those files had its own raw
# sqlite3.connect(). Postgres handles real concurrent connections
# natively, so the old single global threading.Lock serializing every
# access is gone too — that was a SQLite-specific workaround.

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ConnectionPool(
                    DATABASE_URL,
                    min_size=1,
                    max_size=8,
                    kwargs={"row_factory": dict_row},
                    open=True,
                )
    return _pool


@contextmanager
def get_conn():
    """Yields a pooled connection. Commits on a clean exit, rolls back
    on exception, returns the connection to the pool either way — same
    contract the old per-call sqlite3.connect() + manual commit/
    rollback had, just handled by the pool instead of by hand."""
    pool = _get_pool()
    with pool.connection() as conn:
        yield conn


def init_db():
    with get_conn() as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS facts (
                id SERIAL PRIMARY KEY,
                content TEXT NOT NULL,
                owner TEXT NOT NULL DEFAULT 'private',
                interest INTEGER NOT NULL DEFAULT 3,
                created_at TEXT NOT NULL,
                UNIQUE(content, owner)
            );
            CREATE TABLE IF NOT EXISTS summaries (
                id SERIAL PRIMARY KEY,
                session_id TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS working_memory (
                id SERIAL PRIMARY KEY,
                session_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(session_id, key)
            );
            CREATE TABLE IF NOT EXISTS experience (
                id SERIAL PRIMARY KEY,
                task_description TEXT NOT NULL,
                date TEXT NOT NULL,
                tool_chain TEXT,
                result TEXT,
                success INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS experience_trgm_idx ON experience
                USING GIN (task_description gin_trgm_ops);
            CREATE TABLE IF NOT EXISTS reminders (
                id SERIAL PRIMARY KEY,
                session_id TEXT NOT NULL,
                text TEXT NOT NULL,
                remind_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                done INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS wishes (
                id SERIAL PRIMARY KEY,
                content TEXT NOT NULL,
                done INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id SERIAL PRIMARY KEY,
                session_id TEXT NOT NULL,
                content TEXT NOT NULL,
                priority INTEGER DEFAULT 3,
                tags TEXT DEFAULT '[]',
                category TEXT DEFAULT 'general',
                last_accessed TEXT,
                access_count INTEGER DEFAULT 0,
                archived INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                tsv tsvector GENERATED ALWAYS AS (
                    to_tsvector('russian', coalesce(content, '') || ' ' || coalesce(tags, ''))
                ) STORED
            );
            CREATE INDEX IF NOT EXISTS events_tsv_idx ON events USING GIN (tsv);
            CREATE TABLE IF NOT EXISTS internal_notes (
                id SERIAL PRIMARY KEY,
                content TEXT NOT NULL,
                tags TEXT DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS day_state (
                date TEXT PRIMARY KEY,
                briefing_done INTEGER DEFAULT 0,
                wrapup_done INTEGER DEFAULT 0,
                checkin_mode TEXT DEFAULT 'normal',
                notes TEXT DEFAULT '',
                is_dayoff INTEGER DEFAULT 0,
                weekly_plan_done INTEGER DEFAULT 0,
                wake_time TEXT,
                briefing_time TEXT,
                wrapup_time TEXT,
                lunch_time TEXT
            );
            ALTER TABLE day_state ADD COLUMN IF NOT EXISTS wake_time TEXT;
            ALTER TABLE day_state ADD COLUMN IF NOT EXISTS briefing_time TEXT;
            ALTER TABLE day_state ADD COLUMN IF NOT EXISTS wrapup_time TEXT;
            ALTER TABLE day_state ADD COLUMN IF NOT EXISTS lunch_time TEXT;
            CREATE TABLE IF NOT EXISTS day_tasks (
                id SERIAL PRIMARY KEY,
                date TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                type TEXT NOT NULL DEFAULT 'soft',
                scheduled_at TEXT,
                duration INTEGER DEFAULT 60,
                status TEXT DEFAULT 'pending',
                nudge_count INTEGER DEFAULT 0,
                last_nudge TEXT,
                position INTEGER DEFAULT 999,
                recurring_id INTEGER,
                verified_by TEXT,
                nudges_fired TEXT DEFAULT '{}',
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS recurring_tasks (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                type TEXT NOT NULL DEFAULT 'soft',
                days TEXT NOT NULL DEFAULT '["daily"]',
                time TEXT,
                duration INTEGER DEFAULT 60,
                active INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS week_events (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                event_date TEXT NOT NULL,
                event_time TEXT,
                week_of TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                remind_days TEXT DEFAULT '[1, 0]',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rubedo_tasks (
                id SERIAL PRIMARY KEY,
                type TEXT NOT NULL,
                payload TEXT DEFAULT '{}',
                trigger_at TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS insights (
                id SERIAL PRIMARY KEY,
                key TEXT UNIQUE,
                value TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pool_tasks (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                priority INTEGER DEFAULT 3,
                created_at TEXT NOT NULL,
                last_nudged_at TEXT,
                completed_at TEXT,
                nudge_count INTEGER DEFAULT 0,
                snoozed_until TEXT
            );
            CREATE TABLE IF NOT EXISTS rubedo_queue (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status TEXT DEFAULT 'pending',
                priority INTEGER DEFAULT 3,
                scheduled_at TEXT,
                depends_on INTEGER,
                max_retries INTEGER DEFAULT 2,
                retry_count INTEGER DEFAULT 0,
                result TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            );
            ALTER TABLE rubedo_queue ADD COLUMN IF NOT EXISTS session_id INTEGER;
            CREATE TABLE IF NOT EXISTS rubedo_queue_recurring (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                priority INTEGER DEFAULT 3,
                recurrence TEXT NOT NULL,
                next_run_at TEXT,
                created_at TEXT NOT NULL,
                enabled INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS profiles (
                id SERIAL PRIMARY KEY,
                entity TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(entity, key)
            );
            CREATE TABLE IF NOT EXISTS credentials (
                host TEXT PRIMARY KEY,
                secret_encrypted BYTEA NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_sessions (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                origin TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                paused_at TEXT,
                resumed_at TEXT,
                completed_at TEXT,
                result TEXT,
                error TEXT
            );
            ALTER TABLE task_sessions ADD COLUMN IF NOT EXISTS resource_tags TEXT;
            CREATE INDEX IF NOT EXISTS task_sessions_status_idx ON task_sessions (status);
            CREATE TABLE IF NOT EXISTS session_decisions (
                id SERIAL PRIMARY KEY,
                session_id INTEGER NOT NULL REFERENCES task_sessions(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS session_decisions_session_idx ON session_decisions (session_id, id);
            CREATE TABLE IF NOT EXISTS hanging_questions (
                id SERIAL PRIMARY KEY,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                task_session_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );
            CREATE INDEX IF NOT EXISTS hanging_questions_kind_status_idx
                ON hanging_questions (kind, status);
            CREATE INDEX IF NOT EXISTS hanging_questions_session_status_idx
                ON hanging_questions (task_session_id, status);
            CREATE TABLE IF NOT EXISTS message_bindings (
                message_id BIGINT PRIMARY KEY,
                session_id INTEGER NOT NULL REFERENCES task_sessions(id) ON DELETE CASCADE,
                sent_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS day_phase_state (
                id INTEGER PRIMARY KEY,
                phase TEXT NOT NULL DEFAULT 'night',
                entered_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notification_bundle (
                id SERIAL PRIMARY KEY,
                severity TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                delivered_at TEXT
            );
            CREATE TABLE IF NOT EXISTS agent_state (
                id INTEGER PRIMARY KEY DEFAULT 1,
                heartbeat_at TEXT,
                clean_shutdown BOOLEAN NOT NULL DEFAULT FALSE
            );
            CREATE INDEX IF NOT EXISTS notification_bundle_pending_idx
                ON notification_bundle (delivered_at);
        """)


# ─ Message bindings (§2 phase 2 step 3 — reply-to routing contract) ───
# message_id -> task_session_id, written by the outgoing transport layer
# whenever it sends a message on behalf of a task session (not built
# yet in this repo — port from rubedo4's aiogram layer, area 1.5 of the
# rubedo-map). Once it exists, an incoming reply's reply_to_message_id
# resolves here for a deterministic, no-LLM session bind — everything
# downstream (agent/routing.py) already checks this table first.

def message_binding_create(message_id: int, session_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO message_bindings (message_id, session_id, sent_at) VALUES (%s,%s,%s) "
            "ON CONFLICT (message_id) DO NOTHING",
            (message_id, session_id, _now()),
        )


def message_binding_get(message_id: int) -> int | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT session_id FROM message_bindings WHERE message_id=%s", (message_id,)
        ).fetchone()
    return row["session_id"] if row else None


# ─ Messages ────────────────────────────────────────────

def save_message(session_id: str, role: str, content: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, created_at) VALUES (%s, %s, %s, %s)",
            (session_id, role, content, _now()),
        )


def load_history(session_id: str, limit: int = 6) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT role, content, created_at FROM messages WHERE session_id=%s "
            "ORDER BY created_at DESC LIMIT %s",
            (session_id, limit),
        ).fetchall()
    result = []
    for row in reversed(rows):
        role, content, created_at = row["role"], row["content"], row["created_at"]
        try:
            ts = (created_at or "").replace("T", " ")[11:16]
            stamped = f"[{ts}] {content}" if (ts and role == "user") else content
        except Exception:
            stamped = content
        result.append({"role": role, "content": stamped})
    return result


def _last_summary_time(session_id: str, conn) -> str | None:
    row = conn.execute(
        "SELECT created_at FROM summaries WHERE session_id=%s ORDER BY created_at DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return row["created_at"] if row else None


def count_messages_since_last_summary(session_id: str) -> int:
    with get_conn() as conn:
        since = _last_summary_time(session_id, conn)
        if since:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE session_id=%s AND created_at > %s",
                (session_id, since),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE session_id=%s",
                (session_id,),
            ).fetchone()
    return row["c"] if row else 0


def load_messages_since_last_summary(session_id: str) -> list:
    with get_conn() as conn:
        since = _last_summary_time(session_id, conn)
        if since:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE session_id=%s AND created_at > %s "
                "ORDER BY created_at ASC",
                (session_id, since),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE session_id=%s ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def get_last_message_time(session_id: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT created_at FROM messages WHERE session_id=%s ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    return row["created_at"] if row else None


def cleanup_old_messages(keep_days: int = 90) -> int:
    """Delete messages older than keep_days. Returns number of deleted rows."""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat()
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM messages WHERE created_at < %s", (cutoff,))
        deleted = cur.rowcount
    if deleted:
        log.info(f"Cleaned up {deleted} old messages (older than {keep_days} days)")
    return deleted


def search_messages(query: str, session_id: str | None = None, limit: int = 10) -> list:
    like = f"%{query}%"
    with get_conn() as conn:
        if session_id:
            rows = conn.execute(
                "SELECT role, content, created_at FROM messages "
                "WHERE session_id=%s AND content LIKE %s ORDER BY created_at DESC LIMIT %s",
                (session_id, like, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT role, content, created_at FROM messages "
                "WHERE content LIKE %s ORDER BY created_at DESC LIMIT %s",
                (like, limit),
            ).fetchall()
    return [{"role": r["role"], "content": r["content"], "created_at": r["created_at"]} for r in rows]


# ─ Facts ───────────────────────────────────────────────────────────────

def save_fact(content: str, owner: str = "lin", interest: int = 3):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO facts (content, owner, interest, created_at) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (content, owner) DO NOTHING",
            (content, owner, max(1, min(5, interest)), _now()),
        )


def load_facts(owner: str = "lin", limit: int = 10) -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT content FROM facts WHERE owner=%s ORDER BY interest DESC, created_at DESC LIMIT %s",
            (owner, limit),
        ).fetchall()
    return [r["content"] for r in rows]


# ─ Summaries ──────────────────────────────────────────────

def save_summary(session_id: str, content: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO summaries (session_id, content, created_at) VALUES (%s, %s, %s)",
            (session_id, content, _now()),
        )


def load_latest_summary(session_id: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT content FROM summaries WHERE session_id=%s ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    return row["content"] if row else None


# ─ Experience ───────────────────────────────────────────────

def save_experience(task_description: str, tool_chain: str, result: str, success: bool = True):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO experience (task_description, date, tool_chain, result, success, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (task_description, datetime.now().date().isoformat(), tool_chain, result, int(success), _now()),
        )


def search_experience(query: str, limit: int = 3) -> list[dict]:
    """Find past task attempts whose description resembles `query`,
    ranked by trigram similarity (pg_trgm — already enabled in
    init_db()). Threshold 0.15 is a starting point tuned against real
    short Russian task titles, not a spec-mandated number; short/vague
    queries naturally score lower and just won't match anything, which
    is the right failure mode here (silence over a bad match)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT *, similarity(task_description, %s) AS sim FROM experience "
            "WHERE similarity(task_description, %s) > 0.15 "
            "ORDER BY sim DESC LIMIT %s",
            (query, query, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ─ Events (episodic memory) ───────────────────────────────────────────

def save_event(
    session_id: str, content: str, priority: int = 3,
    tags: list | None = None, category: str = "general",
) -> int:
    import json
    auto = _extract_tags(content).split()
    merged = list(dict.fromkeys((tags or []) + auto))[:10]
    tags_json = json.dumps(merged, ensure_ascii=False)
    now = _now()
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO events (session_id, content, priority, tags, category, created_at, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (session_id, content, max(1, min(5, priority)), tags_json, category, now, now),
        ).fetchone()
        return row["id"]


def load_recent_events(limit: int = 5) -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT content FROM events "
            "WHERE category IN ('proactive', 'skill_use', 'task', 'task_error', 'interaction') "
            "AND archived=0 ORDER BY created_at DESC LIMIT %s",
            (limit,),
        ).fetchall()
    return [r["content"] for r in rows]


def export_memory(filepath: str) -> None:
    with get_conn() as conn:
        facts = conn.execute(
            "SELECT content, owner, interest FROM facts ORDER BY interest DESC, created_at DESC"
        ).fetchall()
        evts = conn.execute(
            "SELECT content, category, priority, created_at FROM events "
            "WHERE archived=0 ORDER BY priority DESC, created_at DESC LIMIT 200"
        ).fetchall()
    lines = [f"# Rubedo memory export — {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"]
    lines.append("## Facts\n")
    for f in facts:
        lines.append(f"[{f['owner']}/{f['interest']}★] {f['content']}")
    lines.append("\n## Events\n")
    for e in evts:
        lines.append(f"[{e['category']}/{e['priority']}★ {e['created_at'][:10]}] {e['content']}")
    with open(filepath, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def _touch_events(conn, ids: list[int]) -> None:
    """Update last_accessed and access_count for retrieved events."""
    if not ids:
        return
    now = datetime.now().isoformat()
    conn.execute(
        "UPDATE events SET last_accessed=%s, access_count=access_count+1 WHERE id = ANY(%s)",
        (now, ids),
    )


def _to_tsquery(query: str) -> str | None:
    """Build an OR'd prefix tsquery from the query terms, mirroring the
    old FTS5 behavior (`term*` OR'd together) rather than requiring all
    terms to match."""
    safe_q = query.replace("'", "").replace('"', "")
    parts = [_normalize_tag(t) for t in safe_q.split() if len(t) >= 3]
    if not parts:
        return None
    return " | ".join(f"{p}:*" for p in parts)


def search_events(
    query: str, min_priority: int = 1, include_archived: bool = False, limit: int = 10,
) -> list:
    arch = "" if include_archived else "AND archived=0"
    with get_conn() as conn:
        tsquery = _to_tsquery(query)
        if tsquery:
            try:
                rows = conn.execute(
                    f"SELECT id, session_id, content, priority, tags, category, "
                    f"last_accessed, access_count, archived, created_at, "
                    f"ts_rank(tsv, to_tsquery('russian', %s)) AS rank "
                    f"FROM events WHERE tsv @@ to_tsquery('russian', %s) AND priority>=%s {arch} "
                    f"ORDER BY rank DESC LIMIT %s",
                    (tsquery, tsquery, min_priority, limit),
                ).fetchall()
                if rows:
                    result = _event_rows(rows)
                    _touch_events(conn, [r["id"] for r in result])
                    return result
            except Exception as e:
                log.debug(f"tsvector search failed, falling back to BM25: {e}")

        rows = conn.execute(
            f"SELECT id, session_id, content, priority, tags, category, "
            f"last_accessed, access_count, archived, created_at "
            f"FROM events WHERE priority>=%s {arch} "
            f"ORDER BY priority DESC, created_at DESC LIMIT %s",
            (min_priority, limit * 3),
        ).fetchall()
        items = _event_rows(rows)
        if not items:
            return []
        from memory.search import search_in
        result = search_in(query, items, "content", top_k=limit)
        _touch_events(conn, [r["id"] for r in result])
        return result


def _event_rows(rows) -> list:
    return [
        {"id": r["id"], "session_id": r["session_id"], "content": r["content"], "priority": r["priority"],
         "tags": r["tags"], "category": r["category"], "last_accessed": r["last_accessed"],
         "access_count": r["access_count"], "archived": bool(r["archived"]), "created_at": r["created_at"]}
        for r in rows
    ]


# ─ Internal notes ───────────────────────────────────────────

def save_internal_note(content: str) -> int:
    import json
    auto = _extract_tags(content).split()
    tags_json = json.dumps(auto[:7], ensure_ascii=False)
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO internal_notes (content, tags, created_at) VALUES (%s, %s, %s) RETURNING id",
            (content[:300], tags_json, _now()),
        ).fetchone()
        return row["id"]


def delete_internal_note(note_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM internal_notes WHERE id=%s", (note_id,))
        return cur.rowcount > 0


def list_internal_notes(limit: int = 20) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, content, created_at FROM internal_notes ORDER BY created_at DESC LIMIT %s",
            (limit,),
        ).fetchall()
    return [{"id": r["id"], "content": r["content"], "created_at": r["created_at"]} for r in rows]


def add_week_event(title: str, event_date: str, event_time: str = "",
                   description: str = "") -> int:
    from datetime import date
    week_of = date.fromisoformat(event_date).strftime("%Y-W%W")
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO week_events (title, description, event_date, event_time, week_of, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (title, description, event_date, event_time, week_of, _now()),
        ).fetchone()
        return row["id"]


def list_week_events(weeks_ahead: int = 2) -> list:
    from datetime import date, timedelta
    today = date.today().isoformat()
    until = (date.today() + timedelta(weeks=weeks_ahead)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, description, event_date, event_time, status "
            "FROM week_events WHERE event_date BETWEEN %s AND %s AND status='pending' "
            "ORDER BY event_date, event_time",
            (today, until),
        ).fetchall()
    return [{"id": r["id"], "title": r["title"], "description": r["description"],
             "event_date": r["event_date"], "event_time": r["event_time"], "status": r["status"]} for r in rows]


def delete_week_event(event_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM week_events WHERE id=%s", (event_id,))
        return cur.rowcount > 0


def update_event(event_id: int, new_content: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE events SET content=%s, updated_at=%s WHERE id=%s",
            (new_content, _now(), event_id),
        )
        return cur.rowcount > 0


def delete_event(event_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM events WHERE id=%s", (event_id,))
        return cur.rowcount > 0


# ─ Reminders ──────────────────────────────────────────────────────────────

def save_reminder(session_id: str, text: str, remind_at: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO reminders (session_id, text, remind_at, created_at) VALUES (%s, %s, %s, %s) RETURNING id",
            (session_id, text, remind_at, _now()),
        ).fetchone()
        return row["id"]


def get_pending_reminders() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, session_id, text, remind_at FROM reminders WHERE done=0 ORDER BY remind_at ASC"
        ).fetchall()
    return [{"id": r["id"], "session_id": r["session_id"], "text": r["text"], "remind_at": r["remind_at"]} for r in rows]


def list_reminders_for_session(session_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, text, remind_at, done FROM reminders WHERE session_id=%s ORDER BY remind_at ASC",
            (session_id,),
        ).fetchall()
    return [{"id": r["id"], "text": r["text"], "remind_at": r["remind_at"], "done": bool(r["done"])} for r in rows]


def mark_reminder_done(reminder_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE reminders SET done=1 WHERE id=%s", (reminder_id,))


def delete_reminder(reminder_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM reminders WHERE id=%s", (reminder_id,))
        return cur.rowcount > 0


# ─ Wishes ────────────────────────────────────────────────────────────────────────────

def save_wish(content: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO wishes (content, created_at) VALUES (%s, %s) RETURNING id",
            (content, _now()),
        ).fetchone()
        return row["id"]


def get_active_wishes() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, content FROM wishes WHERE done=0 ORDER BY created_at ASC"
        ).fetchall()
    return [{"id": r["id"], "content": r["content"]} for r in rows]


def mark_wish_done(wish_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE wishes SET done=1 WHERE id=%s", (wish_id,))


# ─ Meta ────────────────────────────────────────────────────────────────

def save_meta(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (%s,%s) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def load_meta(key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=%s", (key,)).fetchone()
    return row["value"] if row else None


# ─ Rubedo Queue ──────────────────────────────────────────────────────────────

_QUEUE_COLS = "id, title, description, status, priority, scheduled_at, depends_on, max_retries, retry_count, result, error, created_at, started_at, completed_at, session_id"


def queue_add(
    title: str,
    description: str = "",
    priority: int = 3,
    scheduled_at: str | None = None,
    depends_on: int | None = None,
    max_retries: int = 2,
) -> int:
    # Normalize ISO "T" separator to the space format the rest of the
    # queries use for string comparison.
    if scheduled_at:
        scheduled_at = scheduled_at.replace("T", " ")
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO rubedo_queue (title, description, priority, scheduled_at, depends_on, max_retries, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (title, description, priority, scheduled_at, depends_on, max_retries, _now()),
        ).fetchone()
        return row["id"]


def queue_list(status: str | None = None) -> list[dict]:
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                f"SELECT {_QUEUE_COLS} FROM rubedo_queue WHERE status=%s ORDER BY priority DESC, created_at ASC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_QUEUE_COLS} FROM rubedo_queue WHERE status NOT IN ('done','cancelled') ORDER BY priority DESC, created_at ASC"
            ).fetchall()
    return [dict(r) for r in rows]


def queue_get_next_scheduled() -> dict | None:
    """Return highest-priority pending task with scheduled_at <= now (local)."""
    from config import now_local
    now_str = now_local().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {_QUEUE_COLS} FROM rubedo_queue WHERE status='pending' AND scheduled_at IS NOT NULL"
            " AND replace(scheduled_at, 'T', ' ') <= %s ORDER BY priority DESC, scheduled_at ASC LIMIT 1",
            (now_str,),
        ).fetchone()
    return dict(row) if row else None


def queue_get_next_idle() -> dict | None:
    """Return highest-priority pending task with no scheduled_at (idle-triggered)."""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {_QUEUE_COLS} FROM rubedo_queue WHERE status='pending' AND scheduled_at IS NULL ORDER BY priority DESC, created_at ASC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def queue_is_running() -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT 1 AS one FROM rubedo_queue WHERE status='running' LIMIT 1").fetchone()
    return row is not None


def queue_get_running() -> dict | None:
    """The single claimed-in-flight task, if any (§2 phase 2) — the
    runner only ever claims one at a time, so at most one row can be
    'running', whether its session is actively executing or sitting
    blocked in 'waiting_dependency' waiting for agent.scheduler to
    unblock it."""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {_QUEUE_COLS} FROM rubedo_queue WHERE status='running' LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def queue_mark_running(task_id: int, session_id: int | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE rubedo_queue SET status='running', started_at=%s, session_id=%s WHERE id=%s",
            (_now(), session_id, task_id),
        )


def queue_requeue_after_crash(task_id: int) -> None:
    """Put a claimed-but-orphaned task back to 'pending' with no
    penalty (retry_count untouched) — a crash isn't the task's own
    failure, and re-running an autonomous task from scratch is cheap
    (agent/crash_recovery.py's resume protocol), unlike re-litigating a
    chat session's in-flight reasoning with Lin."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE rubedo_queue SET status='pending', started_at=NULL, session_id=NULL WHERE id=%s",
            (task_id,),
        )


def queue_mark_done(task_id: int, result: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE rubedo_queue SET status='done', result=%s, completed_at=%s WHERE id=%s",
            (result, _now(), task_id),
        )


def queue_mark_failed(task_id: int, error: str = "") -> bool:
    """Increment retry_count. Returns True if task should be retried, False if exhausted."""
    with get_conn() as conn:
        row = conn.execute("SELECT retry_count, max_retries FROM rubedo_queue WHERE id=%s", (task_id,)).fetchone()
        if not row:
            return False
        retry_count, max_retries = row["retry_count"], row["max_retries"]
        new_count = retry_count + 1
        if new_count <= max_retries:
            conn.execute(
                "UPDATE rubedo_queue SET status='pending', retry_count=%s, error=%s, "
                "started_at=NULL, session_id=NULL WHERE id=%s",
                (new_count, error, task_id),
            )
            return True
        else:
            conn.execute(
                "UPDATE rubedo_queue SET status='failed', retry_count=%s, error=%s, completed_at=%s WHERE id=%s",
                (new_count, error, _now(), task_id),
            )
            return False


def queue_cancel(task_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE rubedo_queue SET status='cancelled' WHERE id=%s AND status IN ('pending','running')",
            (task_id,),
        )
        return cur.rowcount > 0


def queue_depends_satisfied(task: dict) -> bool:
    """True if the task has no dependency, or the dependency is done."""
    dep = task.get("depends_on")
    if not dep:
        return True
    with get_conn() as conn:
        row = conn.execute("SELECT status FROM rubedo_queue WHERE id=%s", (dep,)).fetchone()
    return bool(row and row["status"] == "done")


def queue_depends_broken(task: dict) -> bool:
    """True if the dependency exists and is failed or cancelled."""
    dep = task.get("depends_on")
    if not dep:
        return False
    with get_conn() as conn:
        row = conn.execute("SELECT status FROM rubedo_queue WHERE id=%s", (dep,)).fetchone()
    return bool(row and row["status"] in ("failed", "cancelled"))


def queue_reschedule_to_idle(task_id: int) -> None:
    """Remove scheduled_at so the task runs at next idle slot."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE rubedo_queue SET scheduled_at=NULL WHERE id=%s", (task_id,)
        )


def queue_get_stale_scheduled(expiry_hours: int) -> list[dict]:
    """Return pending scheduled tasks whose scheduled_at is more than expiry_hours ago."""
    from datetime import timedelta
    from config import now_local
    cutoff = (now_local() - timedelta(hours=expiry_hours)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {_QUEUE_COLS} FROM rubedo_queue "
            "WHERE status='pending' AND scheduled_at IS NOT NULL "
            "AND replace(scheduled_at, 'T', ' ') <= %s",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def queue_get_blocked_by_broken_dep() -> list[dict]:
    """Return pending tasks whose dependency is failed or cancelled."""
    cols = ", ".join(f"q.{c}" for c in _QUEUE_COLS.split(", "))
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {cols} FROM rubedo_queue q "
            "JOIN rubedo_queue dep ON q.depends_on = dep.id "
            "WHERE q.status='pending' AND dep.status IN ('failed', 'cancelled')"
        ).fetchall()
    return [dict(r) for r in rows]


# ─ Rubedo Queue Recurring ──────────────────────────────────────────────────────

_QREC_COLS = "id, title, description, priority, recurrence, next_run_at, created_at, enabled"


def queue_recurring_add(title: str, description: str, recurrence: str, priority: int = 3) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO rubedo_queue_recurring (title, description, priority, recurrence, created_at) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (title, description, priority, recurrence, _now()),
        ).fetchone()
        return row["id"]


def queue_recurring_list() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {_QREC_COLS} FROM rubedo_queue_recurring ORDER BY id ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def queue_recurring_get_due() -> list[dict]:
    from config import now_local
    now_str = now_local().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {_QREC_COLS} FROM rubedo_queue_recurring WHERE enabled=1 AND (next_run_at IS NULL OR next_run_at <= %s)",
            (now_str,),
        ).fetchall()
    return [dict(r) for r in rows]


def queue_recurring_set_next(recurring_id: int, next_run_at: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE rubedo_queue_recurring SET next_run_at=%s WHERE id=%s",
            (next_run_at, recurring_id),
        )


# ─ Profiles ──────────────────────────────────────────────────────────────────
# entity='owner'  — structured info about the user (name, city, occupation, …)
# entity='self'   — Rubedo's self-perception (additive, does not replace _PERSONALITY)

def profile_set(entity: str, key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO profiles (entity, key, value, updated_at) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT(entity, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (entity, key, value, _now()),
        )


def profile_get(entity: str, key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM profiles WHERE entity=%s AND key=%s", (entity, key)
        ).fetchone()
    return row["value"] if row else None


def profile_delete(entity: str, key: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM profiles WHERE entity=%s AND key=%s", (entity, key)
        )
    return cur.rowcount > 0


def profile_get_all(entity: str) -> dict[str, str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT key, value FROM profiles WHERE entity=%s ORDER BY key ASC", (entity,)
        ).fetchall()
    return {r["key"]: r["value"] for r in rows}


# ─ Task sessions (techspec §2, phase 1: pause/sequential + decision journal) ─

def session_create(
    title: str, origin: str | None = None,
    status: str = "active", resource_tags: list | None = None,
) -> int:
    """`status` lets the scheduler (agent/scheduler.py, §2 phase 2)
    create a session directly as 'waiting_dependency' — blocked by a
    resource-tag conflict or a full concurrency slot before it ever
    runs — instead of always starting 'active'. `resource_tags` is the
    coarse tag list (agent/resources.py) the scheduler uses to detect
    conflicts between concurrently-active sessions; stored as a JSON
    array, same convention as events.tags."""
    import json
    now = _now()
    tags_json = json.dumps(resource_tags or [], ensure_ascii=False)
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO task_sessions (title, status, origin, resource_tags, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (title, status, origin, tags_json, now, now),
        ).fetchone()
        return row["id"]


def session_get(session_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM task_sessions WHERE id=%s", (session_id,)
        ).fetchone()
    return dict(row) if row else None


def session_get_active() -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM task_sessions WHERE status='active' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def session_set_status(
    session_id: int, status: str,
    result: str | None = None, error: str | None = None,
) -> None:
    """Update a session's status, stamping the timestamp column that
    matches the transition (paused_at/resumed_at/completed_at) — the
    caller (agent/sessions.py) decides which status is appropriate for
    which lifecycle event, this just persists it."""
    now = _now()
    sets = ["status=%s", "updated_at=%s"]
    params: list = [status, now]
    if status == "paused":
        sets.append("paused_at=%s")
        params.append(now)
    elif status == "active":
        sets.append("resumed_at=%s")
        params.append(now)
    elif status in ("done", "failed", "cancelled"):
        sets.append("completed_at=%s")
        params.append(now)
    if result is not None:
        sets.append("result=%s")
        params.append(result)
    if error is not None:
        sets.append("error=%s")
        params.append(error)
    params.append(session_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE task_sessions SET {', '.join(sets)} WHERE id=%s", params)


def session_list(status: str | None = None, limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM task_sessions WHERE status=%s ORDER BY id DESC LIMIT %s",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM task_sessions ORDER BY id DESC LIMIT %s", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


def session_log(session_id: int, kind: str, content: str) -> int:
    now = _now()
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO session_decisions (session_id, kind, content, created_at) "
            "VALUES (%s,%s,%s,%s) RETURNING id",
            (session_id, kind, content, now),
        ).fetchone()
        return row["id"]


def session_journal(session_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM session_decisions WHERE session_id=%s ORDER BY id ASC",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ─ Hanging questions (§5, stage 4) ─────────────────────────────────────
# The real, multi-slot entity behind what agent/approval.py and
# agent/questions.py each used to fake with a single meta-key slot — a
# meta key can only ever hold one pending item, so a second yellow-zone
# call (or ask_user) while the first was still unanswered silently
# overwrote it. Every call gets its own row here instead.

def hanging_create(kind: str, payload: str, task_session_id: int | None = None) -> int:
    now = _now()
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO hanging_questions (kind, payload, task_session_id, status, created_at) "
            "VALUES (%s,%s,%s,'pending',%s) RETURNING id",
            (kind, payload, task_session_id, now),
        ).fetchone()
        return row["id"]


def hanging_get(hq_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM hanging_questions WHERE id=%s", (hq_id,)
        ).fetchone()
    return dict(row) if row else None


def hanging_list_pending(kind: str) -> list[dict]:
    """Newest first — the default assumption for "which pending item
    does this reply answer" is the most recently asked one."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM hanging_questions WHERE kind=%s AND status='pending' ORDER BY id DESC",
            (kind,),
        ).fetchall()
    return [dict(r) for r in rows]


def hanging_get_pending_for_session(session_id: int) -> dict | None:
    """The single pending hanging item (any kind — "ask_user" or
    "approval") blocking this task session, if any. A session in
    'waiting_user' always has exactly one — this is how agent/routing.py
    (§2 phase 2 step 3) turns "which sessions are waiting, and on what"
    into something it can route a reply to, without caring which kind."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM hanging_questions WHERE task_session_id=%s AND status='pending' "
            "ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def hanging_resolve(hq_id: int, status: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE hanging_questions SET status=%s, resolved_at=%s WHERE id=%s",
            (status, _now(), hq_id),
        )


# ─ Day phase (§16, day engine 5.0) ─────────────────────────────────────
# Deliberately NOT keyed by date, unlike day_state — phase is a
# cross-day singleton (night can span past midnight until a real
# wake-up event fires), so there is no per-date row to silently reset
# it at midnight the way day_state's PK would.

def get_day_phase() -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM day_phase_state WHERE id=1").fetchone()
    return dict(row) if row else None


def init_day_phase(phase: str, entered_at: str) -> None:
    """Only ever inserts the id=1 row if it doesn't exist yet — use
    set_day_phase() for actual transitions."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO day_phase_state (id, phase, entered_at) VALUES (1, %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (phase, entered_at),
        )


def set_day_phase(phase: str, entered_at: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE day_phase_state SET phase=%s, entered_at=%s WHERE id=1",
            (phase, entered_at),
        )


# ─ Notification bundle (§7, day-engine 5.0 responsibility 3) ──────────
# Where a non-critical notification lands when the current delivery
# policy (agent/notify.py) says "not now" — accumulated here instead of
# lost, ready for a briefing (once that content-generation piece
# exists) to flush.

def save_bundled_notification(severity: str, content: str, source: str = "") -> int:
    now = _now()
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO notification_bundle (severity, content, source, created_at) "
            "VALUES (%s,%s,%s,%s) RETURNING id",
            (severity, content, source, now),
        ).fetchone()
        return row["id"]


def list_pending_bundle() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM notification_bundle WHERE delivered_at IS NULL ORDER BY id ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def mark_bundle_delivered(ids: list[int]) -> None:
    if not ids:
        return
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "UPDATE notification_bundle SET delivered_at=%s WHERE id = ANY(%s)",
            (now, ids),
        )


def list_experience_by_date(target_date: str) -> list[dict]:
    """Yesterday's condensed session outcomes — the cheapest thing to
    query meaningfully as "yesterday's decision journal" for briefing
    content (day/planner.py) without re-reading every raw
    session_decisions row."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM experience WHERE date=%s ORDER BY id ASC", (target_date,)
        ).fetchall()
    return [dict(r) for r in rows]


# ─ Agent state (crash isolation, §2 phase 2) ───────────────────────────
# Single row (id=1). heartbeat_at is stamped on every tick by whatever
# process drives day/tick.py (not wired to a live one yet, same as the
# rest of the day-engine); clean_shutdown flips true only right before
# a graceful exit. agent/crash_recovery.py reads both at startup to
# tell "the previous run ended on purpose" from "it just stopped".

def agent_heartbeat() -> None:
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO agent_state (id, heartbeat_at, clean_shutdown) VALUES (1, %s, FALSE) "
            "ON CONFLICT (id) DO UPDATE SET heartbeat_at=%s, clean_shutdown=FALSE",
            (now, now),
        )


def agent_mark_clean_shutdown() -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO agent_state (id, clean_shutdown) VALUES (1, TRUE) "
            "ON CONFLICT (id) DO UPDATE SET clean_shutdown=TRUE",
        )


def agent_state_get() -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM agent_state WHERE id=1").fetchone()
    return dict(row) if row else None
