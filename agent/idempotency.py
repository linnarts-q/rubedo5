"""Idempotency policy for side-effect tools.

Two layers:

1. **Per-turn dedup** — within one agent-run (one user message → final
   reply) any side-effect tool with identical args is blocked on the
   second call. Catches the LLM second-guessing itself or fanning out
   parallel calls.

2. **Cross-turn cooldown** — for tools that survive process restart and
   are very expensive to redo (agent_update, os_update, agent_restart,
   display_restart), a wall-clock cooldown blocks repeated calls across
   multiple turns. In production transcripts the LLM auto-called the
   update tool on "Спасибо!" and on a clarifying question — both fresh
   turns where per-turn dedup wouldn't help. Cooldown closes that.

Explicit user override within the cooldown window is intentionally not
supported: distinguishing "please update again" from misinterpretation
isn't reliable, and waiting 5 min or restarting manually is a small
cost compared to a runaway update loop.

Orthogonal to the zone/approval gate (agent/zones.py, agent/approval.py):
a yellow/red-zone tool still needs the owner's explicit "yes" before it
runs at all — idempotency/cooldown then stop it from firing *again*
right after that approval, on a second, unrelated turn.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

log = logging.getLogger("rubedo.idempotency")

SIDE_EFFECT_TOOLS: frozenset[str] = frozenset({
    "agent_update",
    "os_update",
    "agent_restart",
    "display_restart",
    "process_kill",
    "process_launch_app",
    "process_launch_browser",
    "process_close_browser",
    "system_volume",
    "send_file",
    "send_photo",
    "screenshot",
    "file_download",
    "alarm_skip",
    "alarm_cancel",
})


def is_side_effect(tool_name: str) -> bool:
    return tool_name in SIDE_EFFECT_TOOLS


DUPLICATE_BLOCK_MESSAGE = (
    "Этот инструмент уже был вызван в этом запросе с такими же аргументами — "
    "повторно не выполняю. Если нужно ещё раз, попроси явно."
)


# ─ Cross-turn cooldown ────────────────────────────────────────────────────

# Tools that should not fire more than once per cooldown window even if
# called from separate user turns. Each entry: name → cooldown seconds.
_COOLDOWN_TOOLS: dict[str, int] = {}


def _load_cooldown_config() -> dict[str, int]:
    global _COOLDOWN_TOOLS
    if _COOLDOWN_TOOLS:
        return _COOLDOWN_TOOLS
    try:
        from config import TOOL_COOLDOWN_SYSTEM_SEC
    except ImportError:
        TOOL_COOLDOWN_SYSTEM_SEC = 300
    _COOLDOWN_TOOLS = {
        "agent_update": TOOL_COOLDOWN_SYSTEM_SEC,
        "os_update": TOOL_COOLDOWN_SYSTEM_SEC,
        "agent_restart": TOOL_COOLDOWN_SYSTEM_SEC,
        "display_restart": TOOL_COOLDOWN_SYSTEM_SEC,
    }
    return _COOLDOWN_TOOLS


def _meta_key(tool_name: str) -> str:
    return f"tool_last_call:{tool_name}"


def check_cooldown(tool_name: str) -> tuple[bool, str | None]:
    """Return (blocked, message). If blocked is True, do not run the tool;
    return the message as the tool result instead."""
    cooldowns = _load_cooldown_config()
    cooldown_sec = cooldowns.get(tool_name)
    if cooldown_sec is None:
        return (False, None)

    try:
        from memory.db import load_meta
    except Exception:
        return (False, None)

    last = load_meta(_meta_key(tool_name))
    if not last:
        return (False, None)
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return (False, None)

    elapsed = datetime.now() - last_dt
    if elapsed >= timedelta(seconds=cooldown_sec):
        return (False, None)

    remaining = cooldown_sec - int(elapsed.total_seconds())
    mins = max(1, remaining // 60)
    msg = (
        f"Инструмент '{tool_name}' уже был вызван "
        f"{int(elapsed.total_seconds() // 60)} мин назад. "
        f"Повторный вызов заблокирован ещё на ~{mins} мин — "
        f"если действительно нужно сейчас, скажи и я подожду или попробуй позже."
    )
    return (True, msg)


def mark_called(tool_name: str) -> None:
    """Record that tool_name just ran successfully. Used by cooldown check
    on subsequent turns."""
    if tool_name not in _load_cooldown_config():
        return
    try:
        from memory.db import save_meta
    except Exception:
        return
    try:
        save_meta(_meta_key(tool_name), datetime.now().isoformat())
    except Exception as e:
        log.warning(f"cooldown: failed to mark {tool_name}: {e}")
