"""Task-session tools (techspec §2 phase 1): `plan`, `report`, `ask_user`,
plus the read-only `session_history` introspection tool.

`plan`/`report` are the model's own way of writing to the current
session's decision journal — narrative entries alongside the raw
tool-call log agent/executor.py already appends automatically. Both are
no-ops (with a plain message, not an error) when no session is active,
since routes other than "deep" don't open one (agent/controller.py).

`ask_user` is special-cased in agent/executor.py itself, not dispatched
like a normal tool — it must halt the tool loop and hand control back
to the owner, the same way a yellow/red-zone call does. Its entry here
exists only so it has a schema/TOOLS_MAP slot (agent/tool_categories.py
requires every real tool to be covered exactly once); this body only
runs if some other code path calls it directly instead of going through
executor.py's loop.
"""
from __future__ import annotations

import logging

log = logging.getLogger("rubedo.tools.sessions")


def plan(steps: list) -> str:
    from agent import sessions
    s = sessions.active()
    if not s:
        return "Нет активной сессии задачи — план не сохранён."
    steps_list = steps if isinstance(steps, list) else [str(steps)]
    content = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(steps_list))
    sessions.log_decision(s["id"], "plan", content)
    return "План сохранён в журнал сессии."


def report(update: str) -> str:
    from agent import sessions
    s = sessions.active()
    if not s:
        return "Нет активной сессии задачи — заметка не сохранена."
    sessions.log_decision(s["id"], "report", update)
    return "Заметка сохранена в журнал сессии."


def ask_user(question: str) -> str:
    """Reached only when there's no task session to actually pause for
    (agent/executor.py special-cases the halt-and-wait behavior, but
    only when task_session_id is set). Tell the model plainly to ask
    in its own reply instead of quietly relaying a bracketed tool
    result the owner was never meant to see."""
    return "Задай этот вопрос прямо в своём ответе — здесь нет активной сессии задачи, чтобы дождаться ответа."


def session_history(session_id: int = 0, limit: int = 10) -> str:
    from agent import sessions
    if session_id:
        s = sessions.get(session_id)
        if not s:
            return f"Сессия #{session_id} не найдена."
        entries = sessions.journal(session_id)
        lines = [f"Сессия #{s['id']}: «{s['title']}» [{s['status']}]"]
        for e in entries:
            lines.append(f"  [{e['kind']}] {e['content'][:200]}")
        return "\n".join(lines)
    items = sessions.list_sessions(limit=limit)
    if not items:
        return "Сессий пока не было."
    return "\n".join(
        f"[{s['id']}] «{s['title']}» — {s['status']} ({(s['created_at'] or '')[:16]})"
        for s in items
    )
