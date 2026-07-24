"""agent/zones.py -- the deterministic trust boundary (techspec §1).
Priority by risk, not coverage (9.2): every zone a tool call can land
in, plus the concrete bypass shapes an attacker (or a careless prompt)
would actually try.
"""
from __future__ import annotations

import pytest

from agent.zones import Zone, resolve_zone


# tool_name, args, expected_zone -- the tool x host x path table
_CASES = [
    # Green: her own workspace, reading, her own data.
    ("file_write", {"filename": "notes.txt"}, Zone.GREEN),
    ("file_write", {"filename": "sub/dir/notes.txt"}, Zone.GREEN),
    ("file_delete", {"filename": "notes.txt"}, Zone.GREEN),
    ("file_read", {"filename": "anything.txt"}, Zone.GREEN),  # not path-sensitive at all -- reading is always green
    ("web_search", {"query": "whatever"}, Zone.GREEN),
    ("task_add", {"title": "x"}, Zone.GREEN),
    ("spotrent_status", {}, Zone.GREEN),
    ("queue_add", {"title": "x"}, Zone.GREEN),
    ("display_set_background", {"path": "sunset.png"}, Zone.GREEN),

    # Yellow: named tools, regardless of arguments.
    ("system_shell", {"command": "ls -la"}, Zone.YELLOW),
    ("system_code", {"code": "print(1)"}, Zone.YELLOW),
    ("server_shell", {"command": "systemctl status spotrent"}, Zone.YELLOW),
    ("spotrent_start", {}, Zone.YELLOW),
    ("spotrent_stop", {}, Zone.YELLOW),
    ("propose_code_change", {"description": "fix a typo"}, Zone.YELLOW),

    # Yellow: path-sensitive tools whose path actually escapes workspace/.
    ("file_write", {"filename": "/etc/cron.d/evil"}, Zone.YELLOW),
    ("file_delete", {"filename": "/etc/passwd"}, Zone.YELLOW),
    ("file_move", {"destination": "/tmp/outside.txt"}, Zone.YELLOW),
    ("file_archive", {"archive_name": "/tmp/out.zip"}, Zone.YELLOW),
    ("file_extract", {"destination": "/tmp/extracted"}, Zone.YELLOW),
    ("file_convert_image", {"destination": "/tmp/out.png"}, Zone.YELLOW),
    ("file_download", {"filename": "/tmp/downloaded"}, Zone.YELLOW),

    # ".." bypass attempts -- must resolve OUTSIDE workspace/ even
    # though the raw string doesn't start with "/".
    ("file_write", {"filename": "../outside.txt"}, Zone.YELLOW),
    ("file_write", {"filename": "sub/../../outside.txt"}, Zone.YELLOW),
    ("file_write", {"filename": "../../../etc/passwd"}, Zone.YELLOW),

    # Red: core/system-level, regardless of how innocuous the args look.
    ("system_sudo", {"command": "echo hello"}, Zone.RED),
    ("os_update", {}, Zone.RED),
    ("system_env_set", {"key": "FOO", "value": "bar"}, Zone.RED),
    ("agent_update", {}, Zone.RED),
    ("agent_restart", {}, Zone.RED),
    ("process_kill", {"name_or_pid": "notepad"}, Zone.RED),
]


@pytest.mark.parametrize("tool_name,args,expected", _CASES)
def test_zone_table(tool_name, args, expected):
    assert resolve_zone(tool_name, args) is expected


def test_red_tool_ignores_green_looking_args():
    """A red tool never downgrades no matter how harmless its arguments
    look -- classification is by tool identity alone, never by
    inspecting command content for 'dangerousness'."""
    assert resolve_zone("system_sudo", {"command": "true"}) is Zone.RED
    assert resolve_zone("system_sudo", {"command": "rm -rf /"}) is Zone.RED
    # Same zone either way -- proves content is never even consulted.
    assert resolve_zone("system_sudo", {"command": "true"}) == resolve_zone(
        "system_sudo", {"command": "rm -rf /"}
    )


def test_symlink_out_of_workspace_is_caught(tmp_path, monkeypatch):
    """A symlink living inside workspace/ but pointing outside of it
    must not be trusted just because the raw path string looks local --
    Path.resolve() follows the symlink, so _escapes_workspace sees
    where it actually lands."""
    import agent.zones as zones

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    link = workspace / "escape_hatch"
    link.symlink_to(outside, target_is_directory=True)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(zones, "_WORKSPACE", zones.Path("workspace"))

    assert resolve_zone("file_write", {"filename": "escape_hatch/payload.txt"}) is Zone.YELLOW


def test_needs_approval_matches_zone():
    from agent.zones import needs_approval
    assert needs_approval("file_write", {"filename": "ok.txt"}) is False
    assert needs_approval("file_write", {"filename": "/etc/passwd"}) is True
    assert needs_approval("system_sudo", {}) is True
