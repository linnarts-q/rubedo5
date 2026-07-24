"""day/agenda.py (§6, stage 9.4) + day/tick.py's trigger gate around
it. Priority: idle-agenda must be a pure generator (only ever calls
queue_add/notify, never a tool), the trigger must fire only when truly
idle, cooldowns must actually suppress repeats, and pattern-mining must
never fire on a title already linked to a recurring task.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import memory.db as db
import day.agenda as agenda
import day.tick as tick
import agent.sessions as sessions


def _add_day_task(title: str, when: date, status: str = "done", recurring_id=None):
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO day_tasks (date, title, status, recurring_id) VALUES (%s,%s,%s,%s)",
            (when.isoformat(), title, status, recurring_id),
        )


def _same_weekday_dates(n: int, weeks_back_start: int = 1):
    """n dates, all the same weekday, each a week apart."""
    anchor = date.today() - timedelta(days=date.today().weekday())  # this week's Monday
    return [anchor - timedelta(weeks=w) for w in range(weeks_back_start, weeks_back_start + n)]


def test_pattern_mining_finds_a_real_weekday_repeat(tools_ctx):
    for d in _same_weekday_dates(3):
        _add_day_task("Полить цветы", d)
    pattern = agenda._mine_weekday_pattern()
    assert pattern is not None
    assert pattern["title"] == "Полить цветы"
    assert pattern["count"] >= 3


def test_pattern_mining_ignores_already_recurring_titles(tools_ctx):
    for d in _same_weekday_dates(3):
        _add_day_task("Уже регулярная задача", d, recurring_id=1)
    pattern = agenda._mine_weekday_pattern()
    assert pattern is None or pattern["title"] != "Уже регулярная задача"


def test_pattern_mining_ignores_below_threshold(tools_ctx):
    for d in _same_weekday_dates(2):  # below IDLE_AGENDA_PATTERN_MIN_COUNT=3
        _add_day_task("Разовое совпадение", d)
    pattern = agenda._mine_weekday_pattern()
    assert pattern is None or pattern["title"] != "Разовое совпадение"


def test_run_queues_pattern_task_with_the_why_in_description(tools_ctx):
    for d in _same_weekday_dates(3):
        _add_day_task("Полить цветы", d)
    db.save_meta(agenda._META_LAST_RUN, "")

    sent = []
    async def send_fn(t):
        sent.append(t)
    asyncio.run(agenda.run(send_fn))

    queued = db.queue_list()
    assert len(queued) == 1
    assert "Полить цветы" in queued[0]["title"]
    assert "Инициатива idle-агенды" in queued[0]["description"]
    assert sent == [], "a queued task is not a message -- notify only happens for the fallback question"


def test_run_never_calls_a_tool_directly_only_queues_or_notifies(tools_ctx, monkeypatch):
    """The actual boundary 9.4 confirmed: idle-agenda is a generator
    only. Poison every tool-execution entry point; run() must never
    reach any of them."""
    import agent.tools as tools
    def _boom(*a, **kw):
        raise AssertionError("idle-agenda must never execute a tool directly")
    monkeypatch.setattr(tools, "TOOLS_MAP", {k: _boom for k in tools.TOOLS_MAP})

    for d in _same_weekday_dates(3):
        _add_day_task("Полить цветы", d)
    db.save_meta(agenda._META_LAST_RUN, "")

    async def send_fn(t):
        pass
    asyncio.run(agenda.run(send_fn))  # must not raise


def test_run_falls_back_to_wish_research_when_no_pattern(tools_ctx):
    db.save_wish("узнать про новый фотоаппарат")
    db.save_meta(agenda._META_LAST_RUN, "")

    async def send_fn(t):
        pass
    asyncio.run(agenda.run(send_fn))

    queued = db.queue_list()
    assert len(queued) == 1
    assert "фотоаппарат" in queued[0]["title"]


def test_run_asks_have_tasks_when_truly_nothing(tools_ctx):
    # "normal" severity (agent/notify.py) is bundled, not delivered,
    # during night -- conftest's default phase. Set it to "day" so this
    # test is actually about the question logic, not severity policy.
    with db.get_conn() as conn:
        conn.execute("UPDATE day_phase_state SET phase='day' WHERE id=1")
    db.save_meta(agenda._META_LAST_RUN, "")
    db.save_meta(agenda._META_LAST_QUESTION, "")

    sent = []
    async def send_fn(t):
        sent.append(t)
    asyncio.run(agenda.run(send_fn))

    assert db.queue_list() == []
    assert sent and "задачи для меня" in sent[0]


def test_question_cooldown_suppresses_repeat(tools_ctx):
    with db.get_conn() as conn:
        conn.execute("UPDATE day_phase_state SET phase='day' WHERE id=1")
    db.save_meta(agenda._META_LAST_RUN, "")
    db.save_meta(agenda._META_LAST_QUESTION, "")
    sent = []
    async def send_fn(t):
        sent.append(t)
    asyncio.run(agenda.run(send_fn))
    assert len(sent) == 1

    db.save_meta(agenda._META_LAST_RUN, "")  # bypass the outer run-cooldown only
    asyncio.run(agenda.run(send_fn))
    assert len(sent) == 1, "question cooldown must suppress a repeat ask"


def test_overall_cooldown_suppresses_rapid_reruns(tools_ctx):
    db.save_meta(agenda._META_LAST_RUN, "")
    for d in _same_weekday_dates(3):
        _add_day_task("Полить цветы", d)

    async def send_fn(t):
        pass
    asyncio.run(agenda.run(send_fn))
    assert len(db.queue_list()) == 1

    for d in _same_weekday_dates(3, weeks_back_start=10):
        _add_day_task("Вымыть окна", d)
    asyncio.run(agenda.run(send_fn))  # still within IDLE_AGENDA_COOLDOWN_HOURS
    assert len(db.queue_list()) == 1, "overall cooldown must block a second run so soon"


# ── day/tick.py trigger gate ──────────────────────────────────────────

def test_tick_skips_agenda_when_a_session_is_active(tools_ctx, monkeypatch):
    sessions.create("Что-то активное", origin="chat")
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE task_sessions SET status='active' WHERE title='Что-то активное'"
        )
    called = {"n": 0}
    async def _fake_run(send_fn):
        called["n"] += 1
    monkeypatch.setattr(agenda, "run", _fake_run)

    async def send_fn(t):
        pass
    asyncio.run(tick._check_idle_agenda(send_fn))
    assert called["n"] == 0


def test_tick_skips_agenda_when_queue_not_empty(tools_ctx, monkeypatch):
    db.queue_add("Что-то в очереди")
    called = {"n": 0}
    async def _fake_run(send_fn):
        called["n"] += 1
    monkeypatch.setattr(agenda, "run", _fake_run)

    async def send_fn(t):
        pass
    asyncio.run(tick._check_idle_agenda(send_fn))
    assert called["n"] == 0


def test_tick_skips_agenda_at_night(tools_ctx, monkeypatch):
    import day.phase as phase
    monkeypatch.setattr(phase, "current", lambda: "night")
    called = {"n": 0}
    async def _fake_run(send_fn):
        called["n"] += 1
    monkeypatch.setattr(agenda, "run", _fake_run)

    async def send_fn(t):
        pass
    asyncio.run(tick._check_idle_agenda(send_fn))
    assert called["n"] == 0


def test_tick_runs_agenda_when_truly_idle(tools_ctx, monkeypatch):
    import day.phase as phase
    monkeypatch.setattr(phase, "current", lambda: "day")
    called = {"n": 0}
    async def _fake_run(send_fn):
        called["n"] += 1
    monkeypatch.setattr(agenda, "run", _fake_run)

    async def send_fn(t):
        pass
    asyncio.run(tick._check_idle_agenda(send_fn))
    assert called["n"] == 1
