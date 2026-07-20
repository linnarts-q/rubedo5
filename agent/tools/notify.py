"""Work-mode tools (stage 5 part 1) — the owner, or Rubedo herself
self-reflectively, can check or change the current notification mode
through conversation instead of only through internal plumbing.
"""
from __future__ import annotations

from agent.notify import _VALID_MODES


def work_mode_set(mode: str) -> str:
    from agent import notify
    mode = (mode or "").strip().lower()
    if mode not in _VALID_MODES:
        return f"Неизвестный режим «{mode}». Доступны: {', '.join(sorted(_VALID_MODES))}."
    notify.set_mode(mode)
    return f"Режим уведомлений: {mode}."


def work_mode_get() -> str:
    from agent import notify
    return f"Текущий режим уведомлений: {notify.get_mode()}."
