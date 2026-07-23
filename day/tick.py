"""Day-engine tick (day-engine 5.0) — the periodic heartbeat that
drives everything the other day-engine modules (day/phase.py,
agent/anchors.py, agent/notify.py, day/wrapup.py, day/planner.py,
day/pool.py, agent/queue_runner.py) left as plain, independently-
callable functions this session: silence-based phase transitions, the
wake alarm, the evening anchor negotiation, firing the briefing/wrapup
once their negotiated times actually arrive, and giving the queue
runner (§2 infrastructure) somewhere to attach to.

Wired into interface/telegram.py's tick loop (§2 phase 2, stage 7.5) —
run_day_tick(send_fn) is called once a minute; every check inside it is
idempotent and cheap to repeat, so an occasional missed or doubled
tick is harmless. Uses meta keys (day-keyed: "wake_alarm_fired_<date>",
"anchors_proposed_<date>") to make the once-per-day checks idempotent
without a schema change, the same convention agent/approval.py and
friends already use for other one-shot-per-context flags.

`send_fn` is a plain opaque async callable (text) -> message_id | None
— this module never imports a transport, never knows Telethon exists
(§17: the day engine doesn't send messages itself; it only ever
severity-gates through agent/notify.py, which is what actually decides
whether `send_fn` gets called at all)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import day.phase as phase
import day.wrapup as wrapup
import day.planner as planner
import day.pool as pool
from agent import anchors
from agent.queue_runner import run_queue_tick

log = logging.getLogger("rubedo.day.tick")

# evening -> night after this long without a message from the owner.
SILENCE_HOURS = 3.0


def _today() -> str:
    return datetime.now().date().isoformat()


def _now_hm() -> str:
    return datetime.now().strftime("%H:%M")


async def _check_silence() -> None:
    from memory.db import get_last_message_time
    raw = get_last_message_time("lin")
    if not raw:
        return
    try:
        # get_last_message_time is stored via memory.db._now() — naive
        # UTC text — while phase.check_silence compares against local
        # datetime.now(). Convert explicitly rather than risk the exact
        # class of tz-mismatch bug already caught once in
        # agent/hanging.py (harmless in this sandbox since it runs in
        # UTC, but wrong on the mini-PC's actual local timezone).
        last_utc = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        last_local = last_utc.astimezone().replace(tzinfo=None)
    except Exception:
        return
    phase.check_silence(SILENCE_HOURS, last_local)


async def _check_wake_alarm(send_fn) -> None:
    """night phase + past today's negotiated wake_time -> fire the
    alarm exactly once per day, at "critical" severity — the only
    level night-phase policy lets through at all (agent/notify.py).
    Critical is always deliverable (never bundled) but notify.deliver()
    still has to actually be called, or the alarm is computed and
    marked fired without ever reaching the owner.

    Also arms the physical display's alarm screen (§19, stage 9.5's
    bridge — display_alarm_active in meta, polled by display/window.py
    every second, replacing the dead file-flag rubedo4 used). Confirming
    wake-up (night -> morning) happens two ways from here: on the
    owner's next real message (agent/controller.py, unchanged), or on
    the physical screen's tap-to-dismiss, which publishes AlarmDismissed
    on the bus for interface/telegram.py to turn into the same
    phase.on_wake_confirmed() call (§17) — a bare screen with no return
    path back to the phase machine would just be a bell, not a wake-up."""
    if phase.current() != "night":
        return
    from day.state import get_anchor_times
    from memory.db import load_meta, save_meta
    from agent import notify

    today = _today()
    wake_time = get_anchor_times(today).get("wake_time")
    if not wake_time or _now_hm() < wake_time:
        return
    fired_key = f"wake_alarm_fired_{today}"
    if load_meta(fired_key) == "1":
        return
    await notify.deliver("critical", "Пора вставать.", send_fn, source="alarm")
    save_meta(fired_key, "1")
    save_meta("display_alarm_active", "1")


async def _check_evening_negotiation(send_fn) -> None:
    """Once per evening, propose tomorrow's anchor times.

    Passes an always-open window to notify_or_bundle rather than the
    generic WAKE_TIME..SLEEP_TIME default: entering "evening" phase (a
    real event, not a clock check) is already the timing decision here
    — evening can legitimately start after SLEEP_TIME on a late night,
    and the proposal shouldn't be silently suppressed by a second,
    unrelated generic window (mode/day-off restrictions still apply)."""
    if phase.current() != "evening":
        return
    from memory.db import load_meta, save_meta
    from agent import notify

    today = _today()
    proposed_key = f"anchors_proposed_{today}"
    if load_meta(proposed_key) == "1":
        return
    text = anchors.propose_tonight()
    await notify.deliver(
        "normal", text, send_fn, source="anchor_negotiation", quiet_start="00:00", quiet_end="23:59",
    )
    save_meta(proposed_key, "1")


async def _check_briefing(send_fn) -> None:
    from day.state import get_today_state, get_anchor_times
    state = get_today_state() or {}
    if state.get("briefing_done"):
        return
    if phase.current() not in ("morning", "day"):
        return
    briefing_time = get_anchor_times(_today()).get("briefing_time")
    if not briefing_time or _now_hm() < briefing_time:
        return
    await planner.run_briefing(send_fn)


async def _check_wrapup(send_fn) -> None:
    from day.state import get_today_state, get_anchor_times
    state = get_today_state() or {}
    if state.get("wrapup_done"):
        return
    if phase.current() != "day":
        return
    wrapup_time = get_anchor_times(_today()).get("wrapup_time")
    if not wrapup_time or _now_hm() < wrapup_time:
        return
    await wrapup.run_wrapup(send_fn)


async def _check_health_sweep(send_fn) -> None:
    """workspace/health_sweep.py (§6, pulled forward to stage 9.3) is
    her own editable file, not core — a broken edit must never take
    the tick down with it. A plain cached import (not a per-tick
    re-exec from disk) on purpose: her own per-metric alert cooldown
    lives in that module's process-lifetime state, and reloading fresh
    every 60 seconds would silently reset it every tick. A successful
    import stays cached for the rest of this process's life (edits
    need a restart to take effect, same as any other code change); a
    failed import is retried on every following tick automatically
    (Python doesn't cache a module that failed mid-exec) — so once she
    fixes it, it picks back up on its own, no restart needed for that
    direction. Any failure at all — import-time or from check() itself
    — is reported back to her at "normal" severity so she can fix it
    via the reflexive cycle, and the tick moves on regardless. A real
    threshold breach is "critical" (§7) — it has to break through
    night/day-off/quiet-mode (С16), the whole reason this got pulled
    forward ahead of the rest of §6."""
    import importlib
    from agent import notify

    try:
        health_sweep = importlib.import_module("workspace.health_sweep")
        alerts = await health_sweep.check()
    except Exception as e:
        log.error(f"health_sweep failed (her own edit broke it?): {e}")
        await notify.deliver(
            "normal", f"health_sweep упал: {e}. Почини через рефлексию.", send_fn,
            source="health_sweep",
        )
        return

    if alerts:
        text = "Показатели системы вышли за пределы: " + ", ".join(alerts)
        await notify.deliver("critical", text, send_fn, source="health_sweep")


async def _check_idle_agenda(send_fn) -> None:
    """Idle-agenda (§6, stage 9.4) — only when there's truly nothing
    else going on: no active sessions, an empty rubedo_queue, and the
    day phase isn't night. day/agenda.py owns its own cooldowns from
    here on; this is just the trigger gate."""
    if phase.current() == "night":
        return
    import agent.sessions as sessions
    if sessions.list_active():
        return
    from memory.db import queue_list
    if queue_list():
        return
    import day.agenda as agenda
    await agenda.run(send_fn)


async def run_day_tick(send_fn) -> None:
    """One tick. Every check is idempotent — safe to call repeatedly
    (e.g. once a minute) without double-firing anything. `send_fn` is
    the transport's plain (text) -> message_id | None callable
    (transport/base.py) — this module owns none of it, per §17."""
    anchors.ensure_resolved(_today())
    await _check_silence()
    await _check_wake_alarm(send_fn)
    await _check_briefing(send_fn)
    await _check_wrapup(send_fn)
    await _check_evening_negotiation(send_fn)
    await _check_health_sweep(send_fn)
    await pool.run_tick(send_fn)
    await run_queue_tick(send_fn)
    await _check_idle_agenda(send_fn)
