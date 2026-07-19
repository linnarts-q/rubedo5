"""Memory-related tools — events, facts, notes, history.

`_session_id` is read lazily from the parent `agent.tools` package
inside the functions that need it. Doing the import at call-time avoids
the circular-import issue you'd get if memory.py tried to `from
agent.tools import _session_id` at module level (the parent's
__init__.py is still loading when memory.py is being parsed).
"""
from __future__ import annotations

import logging

log = logging.getLogger("rubedo.tools.memory")


def think(thought: str) -> str:
    return f"[думаю] {thought}"


def remember(content: str, priority: int = 3, category: str = "general") -> str:
    import memory.db as db
    from agent.tools import _session_id
    eid = db.save_event(
        _session_id, content, priority=max(1, min(5, priority)), category=category,
    )
    return f"Сохранено в память (id={eid})"


def memory_search(query: str) -> str:
    import memory.db as db
    results = db.search_events(query, limit=5)
    if not results:
        return "Ничего не нашла в памяти."
    return "\n".join(f"[{r['priority']}] {r['content']}" for r in results)


def add_note(content: str) -> str:
    import memory.db as db
    db.save_internal_note(content[:300])
    return "Заметка сохранена."


def delete_note(note_id: int) -> str:
    import memory.db as db
    ok = db.delete_internal_note(note_id)
    return f"Заметка #{note_id} удалена." if ok else f"Заметка #{note_id} не найдена."


def list_notes() -> str:
    import memory.db as db
    notes = db.list_internal_notes(limit=20)
    if not notes:
        return "Заметок нет."
    return "\n".join(
        f"[{n['id']}] {n['content']} ({n['created_at'][:10]})" for n in notes
    )


def edit_memory(event_id: int, new_content: str) -> str:
    import memory.db as db
    ok = db.update_event(event_id, new_content)
    return f"Запись #{event_id} обновлена." if ok else f"Запись #{event_id} не найдена."


def delete_memory(event_id: int) -> str:
    import memory.db as db
    ok = db.delete_event(event_id)
    return (
        f"Запись #{event_id} удалена из памяти." if ok
        else f"Запись #{event_id} не найдена."
    )


def save_fact(content: str) -> str:
    import memory.db as db
    from agent.tools import _session_id
    db.save_fact(content, owner=_session_id)
    return "Факт сохранён."


def search_history(query: str) -> str:
    import memory.db as db
    results = db.search_messages(query, limit=5)
    if not results:
        return "Ничего не найдено в истории разговоров."
    return "\n".join(
        f"[{r['role']} {r['created_at'][:16]}] {r['content'][:200]}" for r in results
    )


# ─ Profiles ──────────────────────────────────────────────────────────────────

def profile_view(entity: str) -> str:
    """View all fields for 'owner' or 'self' profile."""
    import memory.db as db
    if entity not in ("owner", "self"):
        return "entity должен быть 'owner' или 'self'."
    data = db.profile_get_all(entity)
    if not data:
        label = "Профиль владельца" if entity == "owner" else "Моё самовосприятие"
        return f"{label} пуст."
    label = "Профиль владельца" if entity == "owner" else "Моё самовосприятие"
    lines = [f"{label}:"] + [f"  {k}: {v}" for k, v in data.items()]
    return "\n".join(lines)


def profile_set_field(entity: str, key: str, value: str) -> str:
    """Set a profile field. entity = 'owner' | 'self'."""
    import memory.db as db
    if entity not in ("owner", "self"):
        return "entity должен быть 'owner' или 'self'."
    if not key.strip():
        return "key не может быть пустым."
    db.profile_set(entity, key.strip().lower(), value.strip())
    label = "владельца" if entity == "owner" else "моём самовосприятии"
    return f"Профиль {label} обновлён: {key} = {value}"


def profile_delete_field(entity: str, key: str) -> str:
    """Delete a field from a profile."""
    import memory.db as db
    if entity not in ("owner", "self"):
        return "entity должен быть 'owner' или 'self'."
    ok = db.profile_delete(entity, key.strip().lower())
    return f"Поле '{key}' удалено." if ok else f"Поле '{key}' не найдено."
