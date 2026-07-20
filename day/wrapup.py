"""Evening wrapup (day-engine 5.0) — content generation from live
state, plus the day -> evening phase transition (§16).

Per the plan: "Текст брифинга/врапапа собирает smart-tier модель из
живого состояния... Шаблоны с подстановками — удалить." No templates
here: gather_wrapup_state() returns structured facts about what
actually happened today; generate_wrapup_text() hands them to the
model with a strict no-hallucination system prompt
(agent/prompts.py:build_wrapup_system) and lets the content vary with
what's true, rather than filling blanks in fixed prose.

day -> evening only transitions once the wrapup is CONFIRMED (§16) —
if nothing needs verifying, run_wrapup() confirms it immediately;
otherwise the transition waits for handle_verification_response() to
resolve the last unverified task, same as day/state.py's existing
verified_by tracking already implied but nothing consumed until now.

Doesn't decide WHEN to run — a future evening trigger (the negotiated
wrapup_time anchor from agent/anchors.py, checked by a day-engine tick
that isn't built yet) calls run_wrapup() once, same pattern as
day/phase.py and agent/anchors.py before it.
"""
from __future__ import annotations

import logging

from llm.tiers import generation_chat
from agent.prompts import build_wrapup_system
from config import now_local

log = logging.getLogger("rubedo.day.wrapup")

_CONFIRM_WORDS = {"да", "все верно", "всё верно", "ок", "окей", "ok", "подтверждаю", "верно"}


def gather_wrapup_state() -> dict:
    from day.state import get_today_tasks, get_unverified_today
    from agent.sessions import list_sessions

    today_str = now_local().date().isoformat()
    tasks = get_today_tasks()
    done = [t["title"] for t in tasks if t["status"] == "done"]
    pending = [t["title"] for t in tasks if t["status"] == "pending"]
    failed = [t["title"] for t in tasks if t["status"] == "failed"]
    unverified = [t["title"] for t in get_unverified_today()]

    sessions_today = [
        s for s in list_sessions(limit=50)
        if (s.get("created_at") or "").startswith(today_str)
    ]
    return {
        "date": today_str,
        "tasks_done": done,
        "tasks_pending": pending,
        "tasks_failed": failed,
        "unverified": unverified,
        "sessions_completed": [s["title"] for s in sessions_today if s["status"] == "done"],
        "sessions_failed": [s["title"] for s in sessions_today if s["status"] == "failed"],
    }


def _format_state_for_model(state: dict) -> str:
    lines = [f"Дата: {state['date']}"]
    if state["tasks_done"]:
        lines.append("Сделано:\n" + "\n".join(f"✓ {t}" for t in state["tasks_done"]))
    if state["tasks_pending"]:
        lines.append("Осталось не сделано:\n" + "\n".join(f"○ {t}" for t in state["tasks_pending"]))
    if state["tasks_failed"]:
        lines.append("Провалено:\n" + "\n".join(f"✗ {t}" for t in state["tasks_failed"]))
    if state["sessions_completed"]:
        lines.append(
            "Автономные задачи, выполненные сегодня:\n"
            + "\n".join(f"- {t}" for t in state["sessions_completed"])
        )
    if state["sessions_failed"]:
        lines.append(
            "Автономные задачи, не получившиеся сегодня:\n"
            + "\n".join(f"- {t}" for t in state["sessions_failed"])
        )
    if not any([state["tasks_done"], state["tasks_pending"], state["tasks_failed"]]):
        lines.append("На сегодня задач в плане не было вообще.")
    return "\n\n".join(lines)


async def generate_wrapup_text() -> str:
    state = gather_wrapup_state()
    dt = now_local().strftime("%d.%m.%Y %H:%M")
    facts = _format_state_for_model(state)
    try:
        resp = await generation_chat(
            [
                {"role": "system", "content": build_wrapup_system(dt)},
                {"role": "user", "content": f"Подведи итог дня по этим фактам:\n\n{facts}"},
            ],
            temperature=0.4,
        )
        return (resp.choices[0].message.content or "").strip() or "День закончился, подробностей нет."
    except Exception as e:
        log.warning(f"Wrapup generation failed: {e}")
        return "Не получилось собрать итог дня словами, но день завершён."


async def run_wrapup(send_fn=None) -> str:
    """Generate the wrapup and, if agent/notify.py's policy says now is
    an OK time, actually deliver it via `send_fn` — otherwise it's
    bundled instead (same "send only if notify_or_bundle says so"
    contract day/pool.py's run_tick already follows). Then confirms the
    day -> evening transition immediately if nothing needs verifying.
    Returns the generated text regardless of whether it was sent."""
    import day.phase as phase
    from day.state import get_unverified_today, set_wrapup_done
    from agent import notify

    text = await generate_wrapup_text()
    if notify.notify_or_bundle("normal", text, source="wrapup") and send_fn:
        await send_fn(text)
    set_wrapup_done(True)

    if not get_unverified_today():
        phase.on_wrapup_confirmed()
    return text


async def handle_verification_response(text: str, self_send) -> None:
    """Reply handler for agent/controller.py's wrapup-verification
    intercept — the owner confirming whether an auto-marked-done task
    actually happened. Deliberately simple: a plain confirmation marks
    everything still unverified as owner-confirmed; anything else is
    read as naming what didn't actually happen and reopens just that
    task. Once nothing is left unverified, this is what fires the
    day -> evening transition — the wrapup itself only queued it."""
    import day.phase as phase
    from day.state import get_unverified_today, update_task_status

    unverified = get_unverified_today()
    if not unverified:
        await self_send("Подтверждать уже нечего.")
        return

    stripped = text.strip().lower().rstrip("!.,")
    if stripped in _CONFIRM_WORDS:
        for t in unverified:
            update_task_status(t["id"], "done", verified_by="owner")
        await self_send("Приняла, сверила с реальностью.")
    else:
        matched_any = False
        for t in unverified:
            if t["title"].lower() in text.lower():
                update_task_status(t["id"], "failed", verified_by="owner")
                matched_any = True
        await self_send("Поправила по твоему ответу." if matched_any else "Не поняла, что именно поправить — оставила как есть.")

    if not get_unverified_today():
        new_phase = phase.on_wrapup_confirmed()
        if new_phase:
            log.info("Wrapup verification complete — day -> evening")
