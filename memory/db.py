from __future__ import annotations
import logging
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from config import DB_PATH

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

log = logging.getLogger("rubedo.db")

_db_lock = threading.Lock()
_fts_ok: bool | None = None

_RU_STOPWORDS = frozenset({
    'это', 'что', 'как', 'для', 'или', 'при', 'на', 'в', 'и', 'с', 'по', 'но', 'он',
    'она', 'они', 'мне', 'ты', 'я', 'не', 'то', 'из', 'от', 'до', 'за', 'под',
    'над', 'об', 'же', 'ли', 'бы', 'так', 'вот', 'уже', 'ещё', 'только', 'тоже',
    'был', 'есть', 'было', 'быть', 'the', 'is', 'are', 'was', 'for', 'and', 'not',
})

# ─ Schema versioning ────────────────────────────────────────────────────────

_SCHEMA_VERSION = 5

# Each entry migrates from (index) to (index+1).
# v0 → v1 is a no-op: all tables were already created by CREATE TABLE IF NOT EXISTS.
# v1 → v2 adds pool_tasks (untimed task backlog with priority-based nudge cadence).
# v2 → v3 adds proactivity columns to day_tasks: verified_by (user/auto/unknown),
# nudges_fired (JSON dict {point: ISO timestamp}), completed_at (ISO).
# v3 → v4 adds rubedo_queue and rubedo_queue_recurring (autonomous task execution).
# v4 → v5 adds profiles table (structured owner/self key-value store).
_MIGRATIONS: list[str] = [
    "",
    """
    CREATE TABLE IF NOT EXISTS pool_tasks (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        title           TEXT NOT NULL,
        description     TEXT DEFAULT '',
        priority        INTEGER DEFAULT 3,
        created_at      TEXT DEFAULT (datetime('now')),
        last_nudged_at  TEXT,
        completed_at    TEXT,
        nudge_count     INTEGER DEFAULT 0,
        snoozed_until   TEXT
    );
    """,
    """
    ALTER TABLE day_tasks ADD COLUMN verified_by TEXT;
    ALTER TABLE day_tasks ADD COLUMN nudges_fired TEXT DEFAULT '{}';
    ALTER TABLE day_tasks ADD COLUMN completed_at TEXT;
    """,
    """
    CREATE TABLE IF NOT EXISTS rubedo_queue (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        title        TEXT NOT NULL,
        description  TEXT NOT NULL DEFAULT '',
        status       TEXT DEFAULT 'pending',
        priority     INTEGER DEFAULT 3,
        scheduled_at TEXT,
        depends_on   INTEGER,
        max_retries  INTEGER DEFAULT 2,
        retry_count  INTEGER DEFAULT 0,
        result       TEXT,
        error        TEXT,
        created_at   TEXT DEFAULT (datetime('now')),
        started_at   TEXT,
        completed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS rubedo_queue_recurring (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        title       TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        priority    INTEGER DEFAULT 3,
        recurrence  TEXT NOT NULL,
        next_run_at TEXT,
        created_at  TEXT DEFAULT (datetime('now')),
        enabled     INTEGER DEFAULT 1
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS profiles (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        entity     TEXT NOT NULL,
        key        TEXT NOT NULL,
        value      TEXT NOT NULL,
        updated_at TEXT DEFAULT (datetime('now')),
        UNIQUE(entity, key)
    );
    """,
]


def _run_migrations(conn) -> None:
    """Apply any pending schema migrations. Must be called inside get_conn() context."""
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    current = int(row[0]) if row else 0
    if current >= _SCHEMA_VERSION:
        return
    for idx, sql in enumerate(_MIGRATIONS, start=1):
        if idx > current:
            if sql.strip():
                conn.executescript(sql)
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(idx),),
            )
    log.info(f"DB schema migrated v{current} → v{_SCHEMA_VERSION}")


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


def _check_fts(conn) -> bool:
    global _fts_ok
    if _fts_ok is not None:
        return _fts_ok
    try:
        conn.execute("SELECT * FROM memory_fts LIMIT 0")
        _fts_ok = True
    except Exception:
        _fts_ok = False
    return _fts_ok


def _fts_add(conn, source_type: str, source_id: int, content: str, extra: str = ""):
    if not _check_fts(conn):
        return
    try:
        tags = _extract_tags(content + " " + extra)
        conn.execute(
            "INSERT INTO memory_fts (source_type, source_id, content, tags) VALUES (?,?,?,?)",
            (source_type, source_id, content, tags),
        )
    except Exception:
        pass


@contextmanager
def get_conn():
    with _db_lock:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5.0)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                owner TEXT NOT NULL DEFAULT 'private',
                interest INTEGER NOT NULL DEFAULT 3,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(content, owner)
            );
            CREATE TABLE IF NOT EXISTS summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS working_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(session_id, key)
            );
            CREATE TABLE IF NOT EXISTS experience (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_description TEXT NOT NULL,
                date TEXT NOT NULL,
                tool_chain TEXT,
                result TEXT,
                success INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                text TEXT NOT NULL,
                remind_at TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                done INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS wishes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                done INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                content TEXT NOT NULL,
                priority INTEGER DEFAULT 3,
                tags TEXT DEFAULT '[]',
                category TEXT DEFAULT 'general',
                last_accessed TEXT,
                access_count INTEGER DEFAULT 0,
                archived INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS internal_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                tags TEXT DEFAULT '[]',
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS day_state (
                date              TEXT PRIMARY KEY,
                briefing_done     INTEGER DEFAULT 0,
                wrapup_done       INTEGER DEFAULT 0,
                checkin_mode      TEXT DEFAULT 'normal',
                notes             TEXT DEFAULT '',
                is_dayoff         INTEGER DEFAULT 0,
                weekly_plan_done  INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS day_tasks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                date         TEXT NOT NULL,
                title        TEXT NOT NULL,
                description  TEXT DEFAULT '',
                type         TEXT NOT NULL DEFAULT 'soft',
                scheduled_at TEXT,
                duration     INTEGER DEFAULT 60,
                status       TEXT DEFAULT 'pending',
                nudge_count  INTEGER DEFAULT 0,
                last_nudge   TEXT,
                position     INTEGER DEFAULT 999,
                recurring_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS recurring_tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT NOT NULL,
                description TEXT DEFAULT '',
                type        TEXT NOT NULL DEFAULT 'soft',
                days        TEXT NOT NULL DEFAULT '["daily"]',
                time        TEXT,
                duration    INTEGER DEFAULT 60,
                active      INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS week_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT NOT NULL,
                description TEXT DEFAULT '',
                event_date  TEXT NOT NULL,
                event_time  TEXT,
                week_of     TEXT NOT NULL,
                status      TEXT DEFAULT 'pending',
                remind_days TEXT DEFAULT '[1, 0]',
                created_at  TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS rubedo_tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                type        TEXT NOT NULL,
                payload     TEXT DEFAULT '{}',
                trigger_at  TEXT,
                status      TEXT DEFAULT 'pending',
                created_at  TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS insights (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                key         TEXT UNIQUE,
                value       TEXT,
                updated_at  TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS pool_tasks (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                title           TEXT NOT NULL,
                description     TEXT DEFAULT '',
                priority        INTEGER DEFAULT 3,
                created_at      TEXT DEFAULT (datetime('now')),
                last_nudged_at  TEXT,
                completed_at    TEXT,
                nudge_count     INTEGER DEFAULT 0,
                snoozed_until   TEXT
            );
        """)
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    source_type UNINDEXED, source_id UNINDEXED,
                    content, tags, tokenize='unicode61'
                )
            """)
            global _fts_ok
            _fts_ok = True
        except Exception:
            _fts_ok = False
        _run_migrations(conn)


# ─ Messages ────────────────────────────────────────────

def save_message(session_id: str, role: str, content: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
            (session_id, role, content),
        )


def load_history(session_id: str, limit: int = 6) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT role, content, created_at FROM messages WHERE session_id=? "
            "ORDER BY created_at DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    result = []
    for role, content, created_at in reversed(rows):
        try:
            ts = (created_at or "").replace("T", " ")[11:16]
            stamped = f"[{ts}] {content}" if (ts and role == "user") else content
        except Exception:
            stamped = content
        result.append({"role": role, "content": stamped})
    return result


def _last_summary_time(session_id: str, conn) -> str | None:
    row = conn.execute(
        "SELECT created_at FROM summaries WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return row[0] if row else None


def count_messages_since_last_summary(session_id: str) -> int:
    with get_conn() as conn:
        since = _last_summary_time(session_id, conn)
        if since:
            row = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id=? AND created_at > ?",
                (session_id, since),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id=?",
                (session_id,),
            ).fetchone()
    return row[0] if row else 0


def load_messages_since_last_summary(session_id: str) -> list:
    with get_conn() as conn:
        since = _last_summary_time(session_id, conn)
        if since:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE session_id=? AND created_at > ? "
                "ORDER BY created_at ASC",
                (session_id, since),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE session_id=? ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
    return [{"role": r[0], "content": r[1]} for r in rows]


def get_last_message_time(session_id: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT created_at FROM messages WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    return row[0] if row else None


def cleanup_old_messages(keep_days: int = 90) -> int:
    """Delete messages older than keep_days. Returns number of deleted rows."""
    from datetime import datetime as _dt, timedelta
    cutoff = (_dt.now() - timedelta(days=keep_days)).isoformat()
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
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
                "WHERE session_id=? AND content LIKE ? ORDER BY created_at DESC LIMIT ?",
                (session_id, like, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT role, content, created_at FROM messages "
                "WHERE content LIKE ? ORDER BY created_at DESC LIMIT ?",
                (like, limit),
            ).fetchall()
    return [{"role": r[0], "content": r[1], "created_at": r[2]} for r in rows]


# ─ Facts ───────────────────────────────────────────────────────────────

def save_fact(content: str, owner: str = "lin", interest: int = 3):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO facts (content, owner, interest) VALUES (?, ?, ?)",
            (content, owner, max(1, min(5, interest))),
        )
        if cur.lastrowid:
            _fts_add(conn, "fact", cur.lastrowid, content)


def load_facts(owner: str = "lin", limit: int = 10) -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT content FROM facts WHERE owner=? ORDER BY interest DESC, created_at DESC LIMIT ?",
            (owner, limit),
        ).fetchall()
    return [r[0] for r in rows]


# ─ Summaries ──────────────────────────────────────────────

def save_summary(session_id: str, content: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO summaries (session_id, content) VALUES (?, ?)",
            (session_id, content),
        )


def load_latest_summary(session_id: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT content FROM summaries WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    return row[0] if row else None


# ─ Experience ───────────────────────────────────────────────

def save_experience(task_description: str, tool_chain: str, result: str, success: bool = True):
    from datetime import datetime
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO experience (task_description, date, tool_chain, result, success) "
            "VALUES (?,?,?,?,?)",
            (task_description, datetime.now().date().isoformat(), tool_chain, result, int(success)),
        )


# ─ Events (episodic memory) ───────────────────────────────────────────

def save_event(
    session_id: str, content: str, priority: int = 3,
    tags: list | None = None, category: str = "general",
) -> int:
    import json
    auto = _extract_tags(content).split()
    merged = list(dict.fromkeys((tags or []) + auto))[:10]
    tags_json = json.dumps(merged, ensure_ascii=False)
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO events (session_id, content, priority, tags, category) VALUES (?,?,?,?,?)",
            (session_id, content, max(1, min(5, priority)), tags_json, category),
        )
        _fts_add(conn, "event", cur.lastrowid, content, " ".join(merged))
        return cur.lastrowid


def load_recent_events(limit: int = 5) -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT content FROM events "
            "WHERE category IN ('proactive', 'skill_use', 'task', 'task_error', 'interaction') "
            "AND archived=0 ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [r[0] for r in rows]


def export_memory(filepath: str) -> None:
    from datetime import datetime as _dt
    with get_conn() as conn:
        facts = conn.execute(
            "SELECT content, owner, interest FROM facts ORDER BY interest DESC, created_at DESC"
        ).fetchall()
        evts = conn.execute(
            "SELECT content, category, priority, created_at FROM events "
            "WHERE archived=0 ORDER BY priority DESC, created_at DESC LIMIT 200"
        ).fetchall()
    lines = [f"# Rubedo memory export — {_dt.now().strftime('%d.%m.%Y %H:%M')}\n"]
    lines.append("## Facts\n")
    for f in facts:
        lines.append(f"[{f[1]}/{f[2]}★] {f[0]}")
    lines.append("\n## Events\n")
    for e in evts:
        lines.append(f"[{e[1]}/{e[2]}★ {e[3][:10]}] {e[0]}")
    with open(filepath, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def _touch_events(conn, ids: list[int]) -> None:
    """Update last_accessed and access_count for retrieved events."""
    if not ids:
        return
    from datetime import datetime as _dt
    now = _dt.now().isoformat()
    ph = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE events SET last_accessed=?, access_count=access_count+1 WHERE id IN ({ph})",
        [now] + ids,
    )


def search_events(
    query: str, min_priority: int = 1, include_archived: bool = False, limit: int = 10,
) -> list:
    arch = "" if include_archived else "AND archived=0"
    with get_conn() as conn:
        if _check_fts(conn):
            try:
                safe_q = query.replace('"', '').replace("'", "")
                parts = [t for t in safe_q.split() if len(t) >= 3]
                if parts:
                    fts_query = ' OR '.join(_normalize_tag(p) + '*' for p in parts)
                else:
                    fts_query = safe_q
                fts_rows = conn.execute(
                    "SELECT source_id FROM memory_fts WHERE source_type='event' "
                    "AND memory_fts MATCH ? ORDER BY rank LIMIT ?",
                    (fts_query, limit * 2),
                ).fetchall()
                ids = [r[0] for r in fts_rows]
                if ids:
                    ph = ",".join("?" * len(ids))
                    rows = conn.execute(
                        f"SELECT id, session_id, content, priority, tags, category, "
                        f"last_accessed, access_count, archived, created_at "
                        f"FROM events WHERE id IN ({ph}) AND priority>=? {arch} "
                        f"ORDER BY priority DESC",
                        ids + [min_priority],
                    ).fetchall()
                    result = _event_rows(rows)[:limit]
                    _touch_events(conn, [r["id"] for r in result])
                    return result
            except Exception:
                pass
        rows = conn.execute(
            f"SELECT id, session_id, content, priority, tags, category, "
            f"last_accessed, access_count, archived, created_at "
            f"FROM events WHERE priority>=? {arch} "
            f"ORDER BY priority DESC, created_at DESC LIMIT ?",
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
        {"id": r[0], "session_id": r[1], "content": r[2], "priority": r[3],
         "tags": r[4], "category": r[5], "last_accessed": r[6],
         "access_count": r[7], "archived": bool(r[8]), "created_at": r[9]}
        for r in rows
    ]


# ─ Internal notes ───────────────────────────────────────────

def save_internal_note(content: str) -> int:
    import json
    auto = _extract_tags(content).split()
    tags_json = json.dumps(auto[:7], ensure_ascii=False)
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO internal_notes (content, tags) VALUES (?, ?)",
            (content[:300], tags_json),
        )
        _fts_add(conn, "note", cur.lastrowid, content)
        return cur.lastrowid


def delete_internal_note(note_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM internal_notes WHERE id=?", (note_id,))
        return cur.rowcount > 0


def list_internal_notes(limit: int = 20) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, content, created_at FROM internal_notes ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{"id": r[0], "content": r[1], "created_at": r[2]} for r in rows]


def add_week_event(title: str, event_date: str, event_time: str = "",
                   description: str = "") -> int:
    from datetime import date
    week_of = date.fromisoformat(event_date).strftime("%Y-W%W")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO week_events (title, description, event_date, event_time, week_of) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, description, event_date, event_time, week_of),
        )
        return cur.lastrowid


def list_week_events(weeks_ahead: int = 2) -> list:
    from datetime import date, timedelta
    today = date.today().isoformat()
    until = (date.today() + timedelta(weeks=weeks_ahead)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, description, event_date, event_time, status "
            "FROM week_events WHERE event_date BETWEEN ? AND ? AND status='pending' "
            "ORDER BY event_date, event_time",
            (today, until),
        ).fetchall()
    return [{"id": r[0], "title": r[1], "description": r[2],
             "event_date": r[3], "event_time": r[4], "status": r[5]} for r in rows]


def delete_week_event(event_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM week_events WHERE id=?", (event_id,))
        return cur.rowcount > 0


def update_event(event_id: int, new_content: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE events SET content=?, updated_at=datetime('now') WHERE id=?",
            (new_content, event_id),
        )
        return cur.rowcount > 0


def delete_event(event_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM events WHERE id=?", (event_id,))
        return cur.rowcount > 0


# ─ Reminders ──────────────────────────────────────────────────────────────

def save_reminder(session_id: str, text: str, remind_at: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO reminders (session_id, text, remind_at) VALUES (?, ?, ?)",
            (session_id, text, remind_at),
        )
        return cur.lastrowid


def get_pending_reminders() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, session_id, text, remind_at FROM reminders WHERE done=0 ORDER BY remind_at ASC"
        ).fetchall()
    return [{"id": r[0], "session_id": r[1], "text": r[2], "remind_at": r[3]} for r in rows]


def list_reminders_for_session(session_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, text, remind_at, done FROM reminders WHERE session_id=? ORDER BY remind_at ASC",
            (session_id,),
        ).fetchall()
    return [{"id": r[0], "text": r[1], "remind_at": r[2], "done": bool(r[3])} for r in rows]


def mark_reminder_done(reminder_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE reminders SET done=1 WHERE id=?", (reminder_id,))


def delete_reminder(reminder_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM reminders WHERE id=?", (reminder_id,))
        return cur.rowcount > 0


# ─ Wishes ────────────────────────────────────────────────────────────────────────────

def save_wish(content: str) -> int:
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO wishes (content) VALUES (?)", (content,))
        return cur.lastrowid


def get_active_wishes() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, content FROM wishes WHERE done=0 ORDER BY created_at ASC"
        ).fetchall()
    return [{"id": r[0], "content": r[1]} for r in rows]


def mark_wish_done(wish_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE wishes SET done=1 WHERE id=?", (wish_id,))


# ─ Meta ────────────────────────────────────────────────────────────────

def save_meta(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def load_meta(key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


# ─ Rubedo Queue ──────────────────────────────────────────────────────────────

_QUEUE_COLS = "id, title, description, status, priority, scheduled_at, depends_on, max_retries, retry_count, result, error, created_at, started_at, completed_at"


def _queue_row(row) -> dict:
    keys = _QUEUE_COLS.split(", ")
    return dict(zip(keys, row))


def queue_add(
    title: str,
    description: str = "",
    priority: int = 3,
    scheduled_at: str | None = None,
    depends_on: int | None = None,
    max_retries: int = 2,
) -> int:
    # Normalize ISO "T" separator to SQLite space format for correct string comparison
    if scheduled_at:
        scheduled_at = scheduled_at.replace("T", " ")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO rubedo_queue (title, description, priority, scheduled_at, depends_on, max_retries) VALUES (?,?,?,?,?,?)",
            (title, description, priority, scheduled_at, depends_on, max_retries),
        )
        return cur.lastrowid


def queue_list(status: str | None = None) -> list[dict]:
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                f"SELECT {_QUEUE_COLS} FROM rubedo_queue WHERE status=? ORDER BY priority DESC, created_at ASC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_QUEUE_COLS} FROM rubedo_queue WHERE status NOT IN ('done','cancelled') ORDER BY priority DESC, created_at ASC"
            ).fetchall()
    return [_queue_row(r) for r in rows]


def queue_get_next_scheduled() -> dict | None:
    """Return highest-priority pending task with scheduled_at <= now."""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {_QUEUE_COLS} FROM rubedo_queue WHERE status='pending' AND scheduled_at IS NOT NULL"
            " AND replace(scheduled_at, 'T', ' ') <= datetime('now', 'localtime') ORDER BY priority DESC, scheduled_at ASC LIMIT 1"
        ).fetchone()
    return _queue_row(row) if row else None


def queue_get_next_idle() -> dict | None:
    """Return highest-priority pending task with no scheduled_at (idle-triggered)."""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {_QUEUE_COLS} FROM rubedo_queue WHERE status='pending' AND scheduled_at IS NULL ORDER BY priority DESC, created_at ASC LIMIT 1"
        ).fetchone()
    return _queue_row(row) if row else None


def queue_is_running() -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM rubedo_queue WHERE status='running' LIMIT 1").fetchone()
    return row is not None


def queue_mark_running(task_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE rubedo_queue SET status='running', started_at=datetime('now') WHERE id=?",
            (task_id,),
        )


def queue_mark_done(task_id: int, result: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE rubedo_queue SET status='done', result=?, completed_at=datetime('now') WHERE id=?",
            (result, task_id),
        )


def queue_mark_failed(task_id: int, error: str = "") -> bool:
    """Increment retry_count. Returns True if task should be retried, False if exhausted."""
    with get_conn() as conn:
        row = conn.execute("SELECT retry_count, max_retries FROM rubedo_queue WHERE id=?", (task_id,)).fetchone()
        if not row:
            return False
        retry_count, max_retries = row
        new_count = retry_count + 1
        if new_count <= max_retries:
            conn.execute(
                "UPDATE rubedo_queue SET status='pending', retry_count=?, error=?, started_at=NULL WHERE id=?",
                (new_count, error, task_id),
            )
            return True
        else:
            conn.execute(
                "UPDATE rubedo_queue SET status='failed', retry_count=?, error=?, completed_at=datetime('now') WHERE id=?",
                (new_count, error, task_id),
            )
            return False


def queue_cancel(task_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE rubedo_queue SET status='cancelled' WHERE id=? AND status IN ('pending','running')",
            (task_id,),
        )
        return cur.rowcount > 0


def queue_depends_satisfied(task: dict) -> bool:
    """True if the task has no dependency, or the dependency is done."""
    dep = task.get("depends_on")
    if not dep:
        return True
    with get_conn() as conn:
        row = conn.execute("SELECT status FROM rubedo_queue WHERE id=?", (dep,)).fetchone()
    return bool(row and row[0] == "done")


def queue_depends_broken(task: dict) -> bool:
    """True if the dependency exists and is failed or cancelled."""
    dep = task.get("depends_on")
    if not dep:
        return False
    with get_conn() as conn:
        row = conn.execute("SELECT status FROM rubedo_queue WHERE id=?", (dep,)).fetchone()
    return bool(row and row[0] in ("failed", "cancelled"))


def queue_reschedule_to_idle(task_id: int) -> None:
    """Remove scheduled_at so the task runs at next idle slot."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE rubedo_queue SET scheduled_at=NULL WHERE id=?", (task_id,)
        )


def queue_get_stale_scheduled(expiry_hours: int) -> list[dict]:
    """Return pending scheduled tasks whose scheduled_at is more than expiry_hours ago."""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {_QUEUE_COLS} FROM rubedo_queue "
            "WHERE status='pending' AND scheduled_at IS NOT NULL "
            "AND replace(scheduled_at, 'T', ' ') <= datetime('now', 'localtime', ? || ' hours')",
            (f"-{expiry_hours}",),
        ).fetchall()
    return [_queue_row(r) for r in rows]


def queue_get_blocked_by_broken_dep() -> list[dict]:
    """Return pending tasks whose dependency is failed or cancelled."""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT q.{', q.'.join(_QUEUE_COLS.split(', '))} FROM rubedo_queue q "
            "JOIN rubedo_queue dep ON q.depends_on = dep.id "
            "WHERE q.status='pending' AND dep.status IN ('failed', 'cancelled')"
        ).fetchall()
    return [_queue_row(r) for r in rows]


# ─ Rubedo Queue Recurring ──────────────────────────────────────────────────────

_QREC_COLS = "id, title, description, priority, recurrence, next_run_at, created_at, enabled"


def _qrec_row(row) -> dict:
    return dict(zip(_QREC_COLS.split(", "), row))


def queue_recurring_add(title: str, description: str, recurrence: str, priority: int = 3) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO rubedo_queue_recurring (title, description, priority, recurrence) VALUES (?,?,?,?)",
            (title, description, priority, recurrence),
        )
        return cur.lastrowid


def queue_recurring_list() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {_QREC_COLS} FROM rubedo_queue_recurring ORDER BY id ASC"
        ).fetchall()
    return [_qrec_row(r) for r in rows]


def queue_recurring_get_due() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {_QREC_COLS} FROM rubedo_queue_recurring WHERE enabled=1 AND (next_run_at IS NULL OR next_run_at <= datetime('now', 'localtime'))"
        ).fetchall()
    return [_qrec_row(r) for r in rows]


def queue_recurring_set_next(recurring_id: int, next_run_at: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE rubedo_queue_recurring SET next_run_at=? WHERE id=?",
            (next_run_at, recurring_id),
        )


# ─ Profiles ──────────────────────────────────────────────────────────────────
# entity='owner'  — structured info about the user (name, city, occupation, …)
# entity='self'   — Rubedo's self-perception (additive, does not replace _PERSONALITY)

def profile_set(entity: str, key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO profiles (entity, key, value, updated_at) VALUES (?,?,?,datetime('now')) "
            "ON CONFLICT(entity, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (entity, key, value),
        )


def profile_get(entity: str, key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM profiles WHERE entity=? AND key=?", (entity, key)
        ).fetchone()
    return row[0] if row else None


def profile_delete(entity: str, key: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM profiles WHERE entity=? AND key=?", (entity, key)
        )
    return cur.rowcount > 0


def profile_get_all(entity: str) -> dict[str, str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT key, value FROM profiles WHERE entity=? ORDER BY key ASC", (entity,)
        ).fetchall()
    return {r[0]: r[1] for r in rows}
