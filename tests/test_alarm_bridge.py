"""Wake-alarm bridge (§19 display + §17 phase, stage 9.5): day-engine
-> Postgres -> display -> phase. Priority: the forward direction
(alarm fires -> display_alarm_active set) and the backward direction
(display dismiss -> AlarmDismissed on the bus -> night -> morning) both
have to actually work, or a physical tap is just a bell with no return
path back to the phase machine.
"""
from __future__ import annotations

import asyncio

import memory.db as db
import day.phase as phase
import day.tick as tick
import interface.telegram as interface_telegram
from bus.events import AlarmDismissed, AgentStarted
import display.window as window


def _arm_wake_time_in_the_past():
    """00:00 is always <= the current wall-clock time, satisfying
    _check_wake_alarm's "past today's negotiated wake_time" condition
    regardless of when this test actually runs."""
    from datetime import datetime
    today = datetime.now().date().isoformat()
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO day_state (date, wake_time) VALUES (%s, %s) "
            "ON CONFLICT (date) DO UPDATE SET wake_time=excluded.wake_time",
            (today, "00:00"),
        )


def test_wake_alarm_arms_the_display_flag(tools_ctx):
    with db.get_conn() as conn:
        conn.execute("UPDATE day_phase_state SET phase='night' WHERE id=1")
    _arm_wake_time_in_the_past()

    sent = []
    async def send_fn(t):
        sent.append(t)
    asyncio.run(tick._check_wake_alarm(send_fn))

    assert sent and "вставать" in sent[0].lower()
    assert db.load_meta("display_alarm_active") == "1"


def test_display_polls_the_flag_not_a_dead_file(tools_ctx):
    db.save_meta("display_alarm_active", "1")
    assert window._current_alarm_active() is True
    db.save_meta("display_alarm_active", "0")
    assert window._current_alarm_active() is False


def test_dismiss_on_display_confirms_phase_transition(tools_ctx):
    with db.get_conn() as conn:
        conn.execute("UPDATE day_phase_state SET phase='night' WHERE id=1")
    from datetime import datetime
    today = datetime.now().date().isoformat()
    db.save_meta(f"wake_alarm_fired_{today}", "1")
    db.save_meta("display_alarm_active", "1")

    asyncio.run(interface_telegram._on_bus_event(AlarmDismissed()))

    assert phase.current() == "morning"


def test_dismiss_without_a_fired_alarm_today_does_not_confirm(tools_ctx):
    """A stray AlarmDismissed (e.g. an old queued bus message) must not
    force a transition if today's alarm never actually fired."""
    with db.get_conn() as conn:
        conn.execute("UPDATE day_phase_state SET phase='night' WHERE id=1")

    asyncio.run(interface_telegram._on_bus_event(AlarmDismissed()))

    assert phase.current() == "night"


def test_dismiss_when_already_past_night_is_a_harmless_noop(tools_ctx):
    with db.get_conn() as conn:
        conn.execute("UPDATE day_phase_state SET phase='morning' WHERE id=1")
    from datetime import datetime
    today = datetime.now().date().isoformat()
    db.save_meta(f"wake_alarm_fired_{today}", "1")

    asyncio.run(interface_telegram._on_bus_event(AlarmDismissed()))

    assert phase.current() == "morning"  # unchanged, no crash


def test_unrelated_bus_events_are_ignored(tools_ctx):
    with db.get_conn() as conn:
        conn.execute("UPDATE day_phase_state SET phase='night' WHERE id=1")
    from datetime import datetime
    today = datetime.now().date().isoformat()
    db.save_meta(f"wake_alarm_fired_{today}", "1")

    asyncio.run(interface_telegram._on_bus_event(AgentStarted(session_id="lin")))

    assert phase.current() == "night"
