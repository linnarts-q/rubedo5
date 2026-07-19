"""Trust zones (techspec §1) — replaces the flat, ungraded tool-access
model rubedo4 had (map area 9.5).

A tool's zone is a static, deterministic property — decided by which
tool it is and, for path-sensitive tools, where the path actually
points — never a runtime guess at "how dangerous does this look" the
way the shell denylists (9.3, still in agent/tools/shell.py as a last
line of defense) work. Three zones:

    GREEN  — she just does it, no approval needed. Her own workspace,
             her own data (tasks/reminders/pool/queue/memory), reading
             anything, sending content back to the owner.
    YELLOW — she asks first, then does it, via agent.approval. Writes
             outside her workspace, SpotRent start/stop, anything on
             the separate server once that transport exists (§1.2).
    RED    — only ever runs when the owner directly asked for this
             exact action in this conversation, and even then goes
             through agent.approval first. Rubedo's own core, sudo,
             self-update/restart, killing someone else's process.

Only GREEN skips the approval gate. YELLOW and RED both go through
agent.approval — the difference between them is not mechanical here,
it's who is allowed to trigger them (§1: red actions may be *proposed*
on her own initiative, per C13, but never run without the owner's
explicit go-ahead in-thread).
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path


class Zone(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


# Tools that are always yellow, regardless of arguments.
_YELLOW: frozenset[str] = frozenset({
    "spotrent_start",
    "spotrent_stop",
    # Generic non-sudo shell can still write outside workspace, install
    # packages, touch arbitrary system state — too broad to trust as green.
    "system_shell",
    "system_code",
    # Server transport (§1.2) — same reasoning as system_shell, just on
    # the other host. spotrent_status stays green (read-only, §1).
    "server_shell",
})

# Tools that are always red: Rubedo's own core, sudo, system-level change,
# killing processes that aren't necessarily her own.
_RED: frozenset[str] = frozenset({
    "system_sudo",
    "os_update",
    "system_env_set",
    "agent_update",
    "agent_restart",
    "process_kill",
})

# Tools whose zone depends on whether the path argument stays inside
# workspace/ — mirrors the same check agent/tools/__init__.py:_resolve_path
# already does for sandboxing. {tool_name: path_arg_name}
_PATH_SENSITIVE: dict[str, str] = {
    "file_write": "filename",
    "file_delete": "filename",
    "file_move": "destination",
    "file_archive": "archive_name",
    "file_extract": "destination",
    "file_convert_image": "destination",
    "file_download": "filename",
}

_WORKSPACE = Path("workspace")


def _escapes_workspace(raw_path: str) -> bool:
    """True if raw_path (as agent/tools/__init__.py:_resolve_path would
    resolve it) lands outside workspace/. Mirrors that function's rule:
    empty/relative paths stay inside; a leading "/" is absolute and
    almost always outside."""
    if not raw_path:
        return False
    if raw_path.startswith("/"):
        return True
    resolved = (_WORKSPACE / raw_path).resolve()
    workspace_resolved = _WORKSPACE.resolve()
    return not (
        str(resolved) == str(workspace_resolved)
        or str(resolved).startswith(str(workspace_resolved) + "/")
    )


def resolve_zone(tool_name: str, args: dict) -> Zone:
    """The single source of truth for "does this tool call need approval?"."""
    if tool_name in _RED:
        return Zone.RED
    if tool_name in _YELLOW:
        return Zone.YELLOW
    if tool_name in _PATH_SENSITIVE:
        arg_name = _PATH_SENSITIVE[tool_name]
        raw_path = str(args.get(arg_name, "") or "")
        if _escapes_workspace(raw_path):
            return Zone.YELLOW
    return Zone.GREEN


def needs_approval(tool_name: str, args: dict) -> bool:
    return resolve_zone(tool_name, args) is not Zone.GREEN
