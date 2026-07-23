from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "x")
os.environ.setdefault("OWNER_USER_ID", "1")
os.environ.setdefault("OPENROUTER_API_KEYS", "x")
os.environ.setdefault("GROQ_API_KEYS", "x")

import pytest

import memory.db as db

db.init_db()


# Same statements the session's own manual reset_state.py script (used
# throughout the 5.0 rework's development) accumulated to clear
# test-data pollution between runs -- promoted here to the actual
# fixture every test runs against, instead of a scratch script only one
# person remembers to run.
_RESET_STATEMENTS = [
    "UPDATE day_phase_state SET phase='night' WHERE id=1",
    "UPDATE task_sessions SET status='cancelled' WHERE status IN "
    "('active','paused','waiting_user','waiting_dependency')",
    "DELETE FROM rubedo_queue WHERE status IN ('pending','running')",
    "UPDATE hanging_questions SET status='reset' WHERE status='pending'",
    "DELETE FROM notification_bundle WHERE delivered_at IS NULL",
    "DELETE FROM day_tasks",
    "DELETE FROM experience",
    "UPDATE agent_state SET clean_shutdown=TRUE",
    "DELETE FROM meta WHERE key LIKE 'wake_alarm_fired_%' OR key LIKE 'anchors_proposed_%'",
    "DELETE FROM day_state",
    "DELETE FROM pool_tasks",
    "DELETE FROM message_bindings",
    # Left uncleared, message count for session_id='lin' crosses
    # SUMMARIZE_EVERY across enough tests and triggers a REAL
    # background summarize_session() LLM call (agent/controller.py's
    # fire-and-forget _post_process) against the fake API keys tests
    # run with -- a multi-second network timeout eaten by asyncio.run()
    # waiting on task cancellation at the end of every such test.
    "DELETE FROM messages",
    # tests/test_stop_phrase.py exercises both frozen and unfrozen
    # states -- without this, whichever such test runs last leaves
    # autonomy_frozen=1 in meta, silently disabling tool dispatch for
    # every test (and every scratchpad smoke script) that runs after.
    "DELETE FROM meta WHERE key='autonomy_frozen'",
    "DELETE FROM meta WHERE key='queue_paused'",
]


@pytest.fixture(autouse=True)
def clean_db():
    with db.get_conn() as conn:
        for stmt in _RESET_STATEMENTS:
            conn.execute(stmt)
    yield


@pytest.fixture
def tools_ctx():
    import agent.tools as tools
    tools.set_context(session_id="lin", interlocutor="Лин")
    return tools
