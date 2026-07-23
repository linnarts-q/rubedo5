"""Tool registry, categorized (techspec §13).

All ~90 tools in every prompt is a budget overrun (6.4) and confusion
for a weak model. Instead: the classifier (agent/classifier.py) picks
which categories are relevant to the current message — a new
`tool_categories` field in its JSON output — and only those tools
(plus the always-present baseline) go into the prompt.

Misclassification insurance lives in agent/executor.py, not here: if a
tool call names something real but not in the currently-loaded
categories, the executor widens to the full tool set for the rest of
that run rather than failing outright. This is deliberately a lighter
version of the spec's "one follow-up category request, then honest
refusal" — the fuller version needs the reflective cycle (§3), which
is a later stage; this is the interim, and it already stops a category
miss from being a dead end.

`ask_user` / `session_report` / `session_plan` (the spec's "base set
present for any task") now exist (§2 phase 1, agent/tools/sessions.py)
alongside `think` — all four are foundational and task-agnostic rather
than tied to a particular category, so they live in _ALWAYS.
"""
from __future__ import annotations

import logging

from agent.tools import TOOLS_MAP, TOOLS_SCHEMA

log = logging.getLogger("rubedo.agent.tool_categories")

CATEGORIES: dict[str, list[str]] = {
    "memory": [
        "memory_save", "memory_search", "memory_edit", "memory_delete",
        "memory_fact_save", "memory_history_search", "memory_export",
        "profile_view", "profile_set", "profile_delete",
        "note_add", "note_list", "note_delete",
    ],
    "tasks": [
        "task_add", "task_list", "task_details", "task_done", "task_failed",
        "task_remove", "task_reschedule",
        "reminder_add", "reminder_list", "reminder_delete",
        "event_add", "event_list", "event_delete",
        "recurring_add", "recurring_list", "recurring_delete",
        "alarm_skip", "alarm_cancel",
        "work_mode_set", "work_mode_get",
    ],
    "queue": [
        "queue_add", "queue_list", "queue_cancel", "queue_pause", "queue_resume",
        "pool_add", "pool_list", "pool_done", "pool_remove", "pool_priority", "pool_snooze",
    ],
    "files": [
        "file_read", "file_write", "file_delete", "file_list", "file_move",
        "file_download", "file_archive", "file_extract", "file_convert_image",
    ],
    "web": [
        "web_search", "web_screenshot", "web_content", "navigate", "calculate",
        "weather", "research", "news",
    ],
    "media": [
        "music_play", "music_pause", "music_resume", "music_stop",
        "music_next", "music_louder", "music_quieter", "speak",
    ],
    "system": [
        "system_info", "system_uptime", "system_volume", "system_env_set",
        "system_shell", "system_sudo", "system_code",
        "process_list", "process_kill", "process_launch_app",
        "process_launch_browser", "process_close_browser", "screenshot",
    ],
    "server": [
        "server_shell", "spotrent_status", "spotrent_start", "spotrent_stop",
    ],
    "agent_self": [
        "agent_update", "agent_restart", "display_restart", "display_set_background",
        "os_update", "propose_code_change",
    ],
    "diagnostics": [
        "iterations_recent", "logs_archive", "rollback_last", "session_history",
    ],
    "send": [
        "send_file", "send_photo",
    ],
    "wishes": [
        "wish_add", "wish_list", "wish_done",
    ],
}

# Cheap, foundational, useful regardless of task type — see module docstring.
_ALWAYS: frozenset[str] = frozenset({"think", "ask_user", "session_report", "session_plan"})

CATEGORY_NAMES: list[str] = list(CATEGORIES.keys())


def _self_check() -> None:
    """Every real tool must be covered exactly once (in a category, or
    in _ALWAYS) — catches drift the moment a new tool is added to
    agent/tools without updating this file, rather than silently
    starving it of its own tool at runtime."""
    categorized: set[str] = set()
    for cat, names in CATEGORIES.items():
        for n in names:
            if n in categorized:
                raise ValueError(f"Tool '{n}' listed in more than one category")
            categorized.add(n)
    all_covered = categorized | _ALWAYS
    known = set(TOOLS_MAP.keys())
    missing = known - all_covered
    unknown = all_covered - known
    if missing:
        raise ValueError(f"Tools not assigned to any category: {sorted(missing)}")
    if unknown:
        raise ValueError(f"Categories reference tools that don't exist: {sorted(unknown)}")


_self_check()


def get_tools_for_categories(categories: list[str]) -> tuple[list[dict], dict[str, callable]]:
    """Filter TOOLS_SCHEMA/TOOLS_MAP down to the requested categories
    plus the always-present baseline. Unknown category names are
    ignored (logged), not fatal — a classifier hallucinating a category
    name shouldn't break the turn."""
    wanted: set[str] = set(_ALWAYS)
    for cat in categories:
        names = CATEGORIES.get(cat)
        if names is None:
            log.warning(f"Unknown tool category from classifier: {cat!r}, ignoring")
            continue
        wanted.update(names)

    schema = [s for s in TOOLS_SCHEMA if s["function"]["name"] in wanted]
    tools_map = {k: v for k, v in TOOLS_MAP.items() if k in wanted}
    return schema, tools_map
