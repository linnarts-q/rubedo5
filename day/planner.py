"""Morning briefing (day-engine 5.0) — content generation from live
state, no templates. Per the plan: a briefing on a day with three
deadlines and a briefing on an empty Saturday should read differently
because the underlying facts differ, not because of separate
templates — gather_briefing_state() returns structured facts,
generate_briefing_text() hands them to the model with a strict
no-hallucination system prompt (agent/prompts.py:build_briefing_system).

Doesn't decide WHEN to run — a future morning trigger (tied to
day/phase.py's on_wake_confirmed() and the negotiated briefing_time
anchor from agent/anchors.py, checked by a day-engine tick that isn't
built yet) calls run_briefing() once, same pattern as day/phase.py,
agent/anchors.py, and day/wrapup.py before it.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from llm.tiers import generation_chat
from agent.prompts import build_briefing_system
from config import now_local

log = logging.getLogger("rubedo.day.planner")


def gather_briefing_state() -> dict:
    from day.state import get_today_tasks, get_anchor_times
    from day.pool import get_morning_batch
    from memory.db import list_experience_by_date

    today = now_local().date()
    today_str = today.isoformat()
    yesterday_str = (today - timedelta(days=1)).isoformat()

    tasks = get_today_tasks()
    anchors_today = get_anchor_times(today_str)
    pool_batch = get_morning_batch()
    yesterday_experience = list_experience_by_date(yesterday_str)

    return {
        "date": today_str,
        "tasks": [
            {"title": t["title"], "scheduled_at": t.get("scheduled_at")} for t in tasks
        ],
        "wrapup_time": anchors_today.get("wrapup_time"),
        "pool_batch": [t["title"] for t in pool_batch],
        "yesterday_done": sum(1 for e in yesterday_experience if e["success"]),
        "yesterday_failed": sum(1 for e in yesterday_experience if not e["success"]),
    }


def _format_briefing_state(state: dict) -> str:
    lines = [f"Дата: {state['date']}"]
    if state["tasks"]:
        task_lines = []
        for t in state["tasks"]:
            line = f"- {t['title']}"
            if t.get("scheduled_at"):
                line += f" [{t['scheduled_at']}]"
            task_lines.append(line)
        lines.append("План на сегодня:\n" + "\n".join(task_lines))
    else:
        lines.append("На сегодня в плане пока ничего нет.")
    if state["pool_batch"]:
        lines.append("Из бэклога, пора вспомнить:\n" + "\n".join(f"- {t}" for t in state["pool_batch"]))
    if state.get("wrapup_time"):
        lines.append(f"Врапап сегодня запланирован на {state['wrapup_time']}.")
    if state["yesterday_done"] or state["yesterday_failed"]:
        lines.append(
            f"Вчера самостоятельно выполнено задач: {state['yesterday_done']}, "
            f"не получилось: {state['yesterday_failed']}."
        )
    return "\n\n".join(lines)


async def generate_briefing_text() -> str:
    state = gather_briefing_state()
    dt = now_local().strftime("%d.%m.%Y %H:%M")
    facts = _format_briefing_state(state)
    try:
        resp = await generation_chat(
            [
                {"role": "system", "content": build_briefing_system(dt)},
                {"role": "user", "content": f"Собери утренний брифинг по этим фактам:\n\n{facts}"},
            ],
            temperature=0.4,
        )
        return (resp.choices[0].message.content or "").strip() or "Доброе утро."
    except Exception as e:
        log.warning(f"Briefing generation failed: {e}")
        return "Доброе утро — не получилось собрать брифинг словами, но день начался."


async def run_briefing() -> str:
    """Generate and deliver the morning briefing. Doesn't touch phase
    itself — the night -> morning transition is on_wake_confirmed()
    (agent/phase.py), a separate event this doesn't fire; the briefing
    is content delivered once that phase is already active."""
    from agent import notify
    from day.state import set_briefing_done

    text = await generate_briefing_text()
    notify.notify_or_bundle("normal", text, source="briefing")
    set_briefing_done(True)
    return text


async def handle_plan_response(text: str) -> str:
    """Reply handler for agent/controller.py's briefing-plan-response
    intercept. Deliberately minimal: acknowledges the reply rather than
    attempting free-text plan editing here — task_add/reschedule/
    task_done are already real tools available in normal conversation
    for actual plan changes; this only exists so the intercept has
    somewhere to route instead of a dangling import."""
    return "Приняла."


async def adjust_plan(user_text: str, session_id: str) -> str | None:
    """Called from agent/controller.py._post_process for "plan"/
    "day_review" turns. Deliberately a no-op today: auto-adjusting
    day_tasks from free conversation needs real intent extraction this
    slice doesn't build. Returns None (no adjustment) rather than
    guessing at one."""
    return None
