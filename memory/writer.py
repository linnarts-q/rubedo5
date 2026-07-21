"""Single writer for facts/experience/journal writes (§2 phase 2,
day-engine 5.0 parallelism — rollout step 1, deliberately built and
proven before session parallelism itself is turned on).

Postgres already handles genuinely concurrent writes correctly — this
isn't a technical necessity for correctness at the DB level, unlike
the old SQLite-era global lock this deliberately echoes (memory/db.py
dropped that one specifically because Postgres didn't need it). This
one exists for a different reason: routing every fact/experience/
decision-journal write through a single lock removes an entire class
of "did two sessions race on this" questions before they can exist,
at negligible cost — the write volumes here are trivial. A plain
threading.Lock (not an asyncio queue) on purpose: these call sites are
a mix of sync and async code (agent/sessions.py's lifecycle functions
are sync, called from both async executor code and plain tool
functions dispatched via asyncio.to_thread), and a lock serializes
correctly across that mix without requiring every caller to become
async just to acquire it.
"""
from __future__ import annotations

import threading

_writer_lock = threading.Lock()


def write(fn, *args, **kwargs):
    """Run `fn(*args, **kwargs)` with the single writer lock held,
    returning its result. Use for facts/experience/decision-journal
    writes specifically — not a general-purpose replacement for
    memory.db.get_conn()'s own connection-level safety."""
    with _writer_lock:
        return fn(*args, **kwargs)
