"""Pool tasks — untimed backlog with priority-based reminder cadence.

A pool task has no scheduled time. It's something the owner wants done
"eventually" — common examples: social outreach, errands, life admin.
Each task carries a priority 1-5 that maps to a reminder cadence:

    1 → every 30 days
    2 → every 14 days
    3 → every  7 days
    4 → every  3 days
    5 → every weekday (skip Sat/Sun)

The day-engine tick periodically picks at most POOL_MAX_NUDGES_PER_DAY
overdue tasks and emits a soft reminder via Telegram. Reminders are
skipped outside POOL_QUIET_START..POOL_QUIET_END.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, date, timezone
from memory.db import get_conn as _conn
from config import (
    POOL_CADENCE_DAYS,
    POOL_QUIET_START,
    POOL_QUIET_END,
    POOL_MAX_NUDGES_PER_DAY,
)

log = logging.getLogger("rubedo.pool")


def _now() -> str:
    """Naive-UTC text timestamp — same convention as memory.db._now(),
    since Postgres' own now() returns a tz-aware string that doesn't
    compare cleanly against the naive datetimes _is_due() works with."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ─ CRUD ─────────────────────────────────────────────────────────────────

def add(title: str, description: str = "", priority: int = 3) -> int:
    priority = max(1, min(5, int(priority)))
    with _conn() as conn:
        row = conn.execute(
            "INSERT INTO pool_tasks (title, description, priority, created_at) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (title, description, priority, _now()),
        ).fetchone()
        return row["id"]


def get(task_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM pool_tasks WHERE id=%s", (task_id,)
        ).fetchone()
    return dict(row) if row else None


def list_active() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM pool_tasks WHERE completed_at IS NULL "
            "ORDER BY priority DESC, created_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def mark_done(task_id: int) -> bool:
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE pool_tasks SET completed_at=%s "
            "WHERE id=%s AND completed_at IS NULL",
            (_now(), task_id),
        )
        return cur.rowcount > 0


def remove(task_id: int) -> bool:
    with _conn() as conn:
        cur = conn.execute("DELETE FROM pool_tasks WHERE id=%s", (task_id,))
        return cur.rowcount > 0


def set_priority(task_id: int, priority: int) -> bool:
    priority = max(1, min(5, int(priority)))
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE pool_tasks SET priority=%s WHERE id=%s AND completed_at IS NULL",
            (priority, task_id),
        )
        return cur.rowcount > 0


def snooze(task_id: int, days: int) -> bool:
    until = (datetime.now() + timedelta(days=max(1, days))).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE pool_tasks SET snoozed_until=%s "
            "WHERE id=%s AND completed_at IS NULL",
            (until, task_id),
        )
        return cur.rowcount > 0


def mark_nudged(task_id: int) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE pool_tasks SET last_nudged_at=%s, "
            "nudge_count=nudge_count+1 WHERE id=%s",
            (_now(), task_id),
        )


# ─ Cadence ──────────────────────────────────────────────────────────────

def _is_due(task: dict, now: datetime) -> bool:
    if task.get("completed_at"):
        return False

    snoozed_until = task.get("snoozed_until")
    if snoozed_until:
        try:
            if datetime.fromisoformat(snoozed_until) > now:
                return False
        except ValueError:
            pass

    priority = int(task.get("priority") or 3)
    last_nudged = task.get("last_nudged_at")

    if priority == 5:
        # Every weekday (Mon-Fri)
        if now.weekday() >= 5:
            return False
        if not last_nudged:
            return True
        try:
            last_dt = datetime.fromisoformat(last_nudged)
        except ValueError:
            return True
        return last_dt.date() < now.date()

    cadence_days = POOL_CADENCE_DAYS.get(priority, 7)
    if not last_nudged:
        # First nudge happens after the cadence period from creation
        created = task.get("created_at")
        if not created:
            return True
        try:
            created_dt = datetime.fromisoformat(created)
        except ValueError:
            return True
        return now - created_dt >= timedelta(days=cadence_days)

    try:
        last_dt = datetime.fromisoformat(last_nudged)
    except ValueError:
        return True
    return now - last_dt >= timedelta(days=cadence_days)


def get_due() -> list[dict]:
    """Return active tasks that are overdue for a nudge, highest priority first."""
    now = datetime.now()
    return [t for t in list_active() if _is_due(t, now)]


# ─ Tick ─────────────────────────────────────────────────────────────────

def _today_nudge_count(now: datetime) -> int:
    """Count today's primary nudge events. P5 piggybacks are excluded so
    they don't block subsequent P1-P3 events."""
    today = now.date().isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM pool_tasks "
            "WHERE last_nudged_at IS NOT NULL AND date(last_nudged_at)=%s "
            "AND priority < 5",
            (today,),
        ).fetchone()
    return int(row["n"]) if row else 0


async def run_tick(tg_client, owner_id: int) -> None:
    """One tick: pick a due P1-P3 task, piggy-back any due P5s, and nudge.

    P4 and P5 are not the primary subject of between-tasks ticks anymore —
    they are surfaced at morning briefing via `get_morning_batch()`. P5
    additionally rides along when a P1-P3 primary fires here.
    """
    from agent import notify

    now = datetime.now()
    if not notify.should_notify("low", quiet_start=POOL_QUIET_START, quiet_end=POOL_QUIET_END):
        return
    if _today_nudge_count(now) >= POOL_MAX_NUDGES_PER_DAY:
        return

    due = get_due()

    from day.state import get_today_state
    state = get_today_state()
    if state and state.get("is_dayoff"):
        due = [t for t in due if int(t.get("priority") or 3) >= 3]

    primary_candidates = [t for t in due if int(t.get("priority") or 3) <= 3]
    if not primary_candidates:
        return

    primary = primary_candidates[0]
    piggyback = [t for t in due if int(t.get("priority") or 3) == 5]

    try:
        text = await _gen_nudge(primary, piggyback)
    except Exception as e:
        log.warning(f"nudge gen failed for #{primary['id']}: {e}")
        text = f"Напоминаю про задачу: «{primary['title']}»."

    try:
        await tg_client.send_message(owner_id, text)
        mark_nudged(primary["id"])
        for t in piggyback:
            mark_nudged(t["id"])
        log.info(
            f"pool nudge: primary #{primary['id']}"
            + (f", piggyback {[t['id'] for t in piggyback]}" if piggyback else "")
        )
    except Exception as e:
        log.error(f"pool nudge send failed for #{primary['id']}: {e}")


def get_morning_batch() -> list[dict]:
    """Return all P4/P5 active tasks due today — used by briefing to
    surface them in one morning batch."""
    now = datetime.now()
    return [
        t for t in list_active()
        if int(t.get("priority") or 3) >= 4 and _is_due(t, now)
    ]


def mark_morning_batch_nudged(tasks: list[dict]) -> None:
    """After briefing displays the morning batch, mark each as nudged so
    cadence resets correctly."""
    for t in tasks:
        mark_nudged(t["id"])


async def _gen_nudge(task: dict, piggyback: list[dict] | None = None) -> str:
    """Generate the pool-nudge message. If `piggyback` is non-empty, those
    P5 tasks ride along in the same message after the primary."""
    from llm.groq import chat as groq_chat
    from agent.prompts import build_analytics_system

    dt = datetime.now().strftime("%d.%m.%Y %H:%M")
    desc = task.get("description") or ""
    priority = int(task.get("priority") or 3)
    cadence_label = {
        1: "редко (раз в месяц)",
        2: "не очень часто (раз в 2 недели)",
        3: "регулярно (раз в неделю)",
        4: "часто (раз в 3 дня)",
        5: "ежедневно по будням",
    }.get(priority, "периодически")

    from config import OWNER_NAME
    no_interjections = (
        "Сразу к сути, не начинай с междометий или обращений "
        "(«Эй», «Слушай», «Хей», «Привет», «Ну что»)."
    )
    role_rules = (
        f"Ты — Рубедо, напоминаешь {OWNER_NAME}у про его задачу из бэклога. "
        f"Обращайся к {OWNER_NAME}у на «ты», себя — в женском роде, если упоминаешь. "
        f"Не пиши задачу от лица {OWNER_NAME}а. "
    )

    if not piggyback:
        prompt = (
            role_rules
            + "Это не срочное дело со временем — это что-то, что он откладывает. "
            "Тон: разговорный, чуть подталкивающий, без давления. "
            + no_interjections + " "
            + "Одно-два предложения, на русском.\n\n"
            f"Задача: «{task['title']}»"
            + (f"\nКонтекст: {desc}" if desc else "")
            + f"\nПриоритет: {priority}/5, напоминание идёт {cadence_label}."
        )
    else:
        pb_titles = ", ".join(f"«{t['title']}»" for t in piggyback)
        prompt = (
            role_rules
            + "Одна основная задача и пара ежедневных, про которые тоже пора. "
            "Естественной речью, не списком. "
            + no_interjections + " "
            + "2-3 предложения.\n\n"
            f"Основная: «{task['title']}»"
            + (f"\nКонтекст: {desc}" if desc else "")
            + f"\nЕжедневные (P5): {pb_titles}"
        )
    resp = await groq_chat(
        [
            {"role": "system", "content": build_analytics_system(dt)},
            {"role": "user", "content": prompt},
        ],
        temperature=0.7,
    )
    return resp.choices[0].message.content.strip()


# ─ Auto-priority classifier ─────────────────────────────────────────────

async def classify_priority(title: str, description: str = "") -> int:
    """Classify a new pool task into priority 1-5 via Groq.

    Falls back to 3 (neutral) on any error.
    """
    from llm.groq import chat as groq_chat
    from agent.prompts import build_analytics_system

    dt = datetime.now().strftime("%d.%m.%Y %H:%M")
    prompt = (
        "Классифицируй приоритет задачи из личного бэклога Лина по шкале 1-5, "
        "где приоритет = как часто напоминать:\n"
        "1 = раз в месяц (нет срочности, задача-фон)\n"
        "2 = раз в 2 недели (умеренно)\n"
        "3 = раз в неделю (стандартно)\n"
        "4 = раз в 3 дня (важно но не горит)\n"
        "5 = каждый будний день (надо двигать активно)\n\n"
        "Учитывай: социальная активность для интроверта = выше приоритет, "
        "так как откладывается легко. Бытовые мелочи = 1-2. "
        "Здоровье/важные дела = 4-5. Хобби и развитие = 2-3.\n\n"
        f"Задача: «{title}»"
        + (f"\nОписание: {description}" if description else "")
        + "\n\nОтветь ТОЛЬКО одной цифрой от 1 до 5, без пояснений."
    )
    try:
        resp = await groq_chat(
            [
                {"role": "system", "content": build_analytics_system(dt)},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
        )
        raw = (resp.choices[0].message.content or "").strip()
        for ch in raw:
            if ch.isdigit():
                p = int(ch)
                if 1 <= p <= 5:
                    return p
    except Exception as e:
        log.warning(f"priority classify failed: {e}")
    return 3
