"""scripts/migrate_from_rubedo4.py's [закрыто] tagging (stage 9.6) --
critical per §11 layer 1: agent/outcomes.py's outcome annotation only
ever checks TODAY's day_tasks, so it can never retroactively mark an
old instruction among migrated messages as done/cancelled. Without
this tag, a months-old "сделай Х" could read as a still-standing order
the moment it lands in fresh context.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import migrate_from_rubedo4 as migrate


def _make_fixture(tmp_path) -> str:
    db_path = str(tmp_path / "rubedo4.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id TEXT, role TEXT, content TEXT, created_at TEXT)"
    )
    for i in range(6):
        role = "user" if i % 2 == 0 else "assistant"
        conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES ('lin', ?, ?)",
            (role, f"сообщение {i}"),
        )
    conn.commit()
    conn.close()
    return db_path


def test_user_messages_get_tagged_assistant_do_not(tmp_path):
    db_path = _make_fixture(tmp_path)
    conn = sqlite3.connect(db_path)
    rows = migrate._fetch_recent_messages(conn, limit=20)
    conn.close()

    for row in rows:
        if row["role"] == "user":
            assert row["content"].startswith("[закрыто] "), row
        else:
            assert not row["content"].startswith("[закрыто]"), row


def test_tagging_is_idempotent_on_already_tagged_content(tmp_path):
    """A re-run (or a message that already happened to start with the
    literal tag) must not double-tag."""
    db_path = str(tmp_path / "rubedo4.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id TEXT, role TEXT, content TEXT, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content) VALUES ('lin', 'user', '[закрыто] уже помечено')"
    )
    conn.commit()
    rows = migrate._fetch_recent_messages(conn, limit=20)
    conn.close()

    assert rows[0]["content"] == "[закрыто] уже помечено"
    assert rows[0]["content"].count("[закрыто]") == 1


def test_respects_messages_limit_and_keeps_chronological_order(tmp_path):
    db_path = _make_fixture(tmp_path)
    conn = sqlite3.connect(db_path)
    rows = migrate._fetch_recent_messages(conn, limit=2)
    conn.close()

    assert len(rows) == 2
    assert rows[0]["id"] < rows[1]["id"]
    assert "сообщение 4" in rows[0]["content"]
    assert "сообщение 5" in rows[1]["content"]
