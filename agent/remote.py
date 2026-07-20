"""SSH transport to the separate server (techspec §1.2) — the physical
mechanism behind the yellow zone's "сервер" bullets (§1): SpotRent
control, general non-sudo shell, package installs outside her own
sandbox.

Uses the system `ssh` binary with a dedicated key — never a password,
key-based auth only — rather than adding an async-SSH dependency;
mirrors the subprocess style already used throughout agent/tools/shell.py.
Sync functions, dispatched via asyncio.to_thread by the executor like
every other blocking tool.

Nothing here bypasses the zone/approval gate (agent/zones.py,
agent/approval.py) — this module only provides the "how do I actually
reach the server" plumbing that tools call into; whether a given tool
needs the owner's yes/no first is still decided per-tool in
agent/zones.py, same as it is for anything else.
"""
from __future__ import annotations

import logging
import subprocess

from config import SERVER_HOST, SERVER_USER, SERVER_SSH_KEY

log = logging.getLogger("rubedo.remote")

_SSH_TIMEOUT_SEC = 30
_OUTPUT_CAP = 4000


class ServerNotConfigured(Exception):
    pass


def _ssh_base_args() -> list[str]:
    if not SERVER_HOST or not SERVER_USER:
        raise ServerNotConfigured(
            "Сервер не настроен — заполни SERVER_HOST и SERVER_USER (и, если "
            "нужно, SERVER_SSH_KEY) в .env."
        )
    args = [
        "ssh",
        "-o", "BatchMode=yes",           # never prompt for a password — key auth or fail
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
    ]
    if SERVER_SSH_KEY:
        args += ["-i", SERVER_SSH_KEY]
    args.append(f"{SERVER_USER}@{SERVER_HOST}")
    return args


def run(command: str, timeout: int = _SSH_TIMEOUT_SEC, stdin_input: str | None = None) -> str:
    """Run a single command on the server over SSH and return its output
    (or a plain-language error — never raises for the caller's sake,
    same convention as agent/tools/shell.py:shell_exec).

    `stdin_input`, when given, is piped to the remote command's stdin
    over the encrypted SSH channel — used by run_sudo to hand a
    password to `sudo -S` on the server without it ever appearing in
    a command line/process list on either machine."""
    try:
        base = _ssh_base_args()
    except ServerNotConfigured as e:
        return str(e)
    try:
        r = subprocess.run(
            base + [command], capture_output=True, text=True, timeout=timeout,
            input=stdin_input,
        )
        out = r.stdout.strip()
        err = r.stderr.strip()
        if r.returncode != 0:
            result = f"[код {r.returncode}]\n{err or out}"
        else:
            result = out or "(пусто)"
        if len(result) > _OUTPUT_CAP:
            result = result[:_OUTPUT_CAP] + "\n…[обрезано]"
        return result
    except subprocess.TimeoutExpired:
        return "Таймаут SSH-команды."
    except FileNotFoundError:
        return "ssh не установлен в системе."
    except Exception as e:
        return f"Ошибка SSH: {e}"


def is_reachable() -> bool:
    """Cheap connectivity check so callers (spotrent_status etc.) can
    report "сервер недоступен" instead of a raw SSH error string."""
    try:
        base = _ssh_base_args()
    except ServerNotConfigured:
        return False
    try:
        r = subprocess.run(base + ["true"], capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def is_error_output(text: str) -> bool:
    """True if a run() result looks like a transport failure rather than
    real command output — used by callers that need to tell "the server
    said no processes matched" apart from "I couldn't reach the server
    at all"."""
    return (
        text.startswith("[код")
        or text.startswith("Ошибка SSH")
        or text.startswith("Таймаут")
        or text.startswith("ssh не установлен")
        or "Сервер не настроен" in text
    )
