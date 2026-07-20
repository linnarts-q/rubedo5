"""Approval gate for yellow/red-zone tool calls (techspec §1, minimal
precursor to the full "hanging question" entity in §5).

A yellow/red tool call never executes on the turn it was requested.
The executor halts the tool loop right there and the confirmation
question becomes this turn's reply (agent/executor.py). The next
message is intercepted by agent/controller.py before routing: a
recognizable "yes" runs the stored call for real and reports the
result; a recognizable "no" cancels it and says so; anything else
falls through to the normal conversation (the owner asked something
unrelated, or wants to talk about the request first — see
is_confirmation's None case).

TTL-armed the same way the other meta-based intercepts in
controller.py already are (wrapup plan/verification, T+60, pending
clarification) — an unrelated message hours later must not
accidentally execute a stale sudo command.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from config import APPROVAL_TTL_HOURS

log = logging.getLogger("rubedo.agent.approval")

_META_PENDING = "pending_tool_approval"
_META_ARMED_AT = "pending_tool_approval_armed_at"

_YES_WORDS = {
    "да", "давай", "ок", "окей", "ok", "yes", "делай", "выполняй",
    "го", "го давай", "подтверждаю", "confirm", "sure",
}
_NO_WORDS = {
    "нет", "не", "no", "отмена", "стоп", "не надо", "отмени", "не выполняй",
}


def request(name: str, args: dict, preview: str, task_session_id: int | None = None) -> None:
    """Store a pending tool call awaiting the owner's yes/no.

    `task_session_id` (§2 phase 1) — if this call halted a task session
    (agent/executor.py), carrying its id through lets
    agent/controller.py resume and then complete/cancel that same
    session once the owner answers, instead of leaving it paused
    forever with no way back."""
    from memory.db import save_meta
    save_meta(_META_PENDING, json.dumps({
        "name": name, "args": args, "preview": preview, "task_session_id": task_session_id,
    }))
    save_meta(_META_ARMED_AT, datetime.now().isoformat())


def pending() -> dict | None:
    """Return the pending call {name, args, preview}, or None if there
    isn't one or it went stale past APPROVAL_TTL_HOURS."""
    from memory.db import load_meta

    raw = load_meta(_META_PENDING)
    if not raw:
        return None
    armed = load_meta(_META_ARMED_AT) or ""
    try:
        armed_dt = datetime.fromisoformat(armed)
    except ValueError:
        clear()
        return None
    if (datetime.now() - armed_dt).total_seconds() > APPROVAL_TTL_HOURS * 3600:
        log.info("Pending approval expired (TTL), clearing")
        _fail_orphaned_session(raw)
        clear()
        return None
    try:
        return json.loads(raw)
    except Exception:
        clear()
        return None


def _fail_orphaned_session(raw: str) -> None:
    """A paused task session (§2 phase 1) whose approval went stale has
    nothing left to wait for — mark it failed rather than leaving it
    'paused' forever with no way back in. Best-effort: a malformed or
    session-less payload is silently ignored, same as the surrounding
    TTL-eviction paths."""
    try:
        tsid = json.loads(raw).get("task_session_id")
        if tsid is not None:
            from agent import sessions
            sessions.fail(tsid, "подтверждение просрочено (TTL)")
    except Exception as e:
        log.debug(f"orphaned-session fail skipped: {e}")


def clear() -> None:
    from memory.db import save_meta
    save_meta(_META_PENDING, "")
    save_meta(_META_ARMED_AT, "")


def is_confirmation(text: str) -> bool | None:
    """True = owner said yes, False = owner said no, None = the reply
    isn't a recognizable yes/no (caller should let it fall through to
    normal conversation rather than guess)."""
    t = text.strip().lower().rstrip("!.,")
    if t in _YES_WORDS:
        return True
    if t in _NO_WORDS:
        return False
    return None


# ─ Dry-run previews (§15) ──────────────────────────────────────────────
# "attach a fact, not a generated description" — where we can cheaply
# compute the real, concrete effect of a call before running it, do
# that instead of describing it in prose. Starting with the most
# consequential tools first, per the spec's own rollout note; the rest
# fall back to a plain "tool(args)" line.

def preview_for(tool_name: str, args: dict) -> str:
    try:
        if tool_name == "file_delete":
            return _preview_file_delete(args)
        if tool_name == "file_write":
            return _preview_file_write(args)
        if tool_name == "file_move":
            return _preview_file_move(args)
        if tool_name == "system_env_set":
            return _preview_env_set(args)
        if tool_name == "process_kill":
            return _preview_process_kill(args)
        if tool_name in ("system_shell", "system_sudo", "system_code", "server_shell"):
            return _preview_command(tool_name, args)
    except Exception as e:
        log.debug(f"preview_for({tool_name}) failed, falling back: {e}")
    return _preview_generic(tool_name, args)


def _preview_generic(tool_name: str, args: dict) -> str:
    args_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return f"{tool_name}({args_str})"


def _preview_file_delete(args: dict) -> str:
    from agent.tools import _resolve_path
    filename = str(args.get("filename", ""))
    try:
        path = _resolve_path(filename)
    except ValueError:
        return f"file_delete: путь вне рабочей папки — {filename!r}"
    if not path.exists():
        return f"file_delete: {path} — файл не найден (удалять нечего)"
    size = path.stat().st_size
    return f"file_delete: удалит {path} ({size:,} байт)"


def _preview_file_write(args: dict) -> str:
    from agent.tools import _resolve_path
    filename = str(args.get("filename", ""))
    try:
        path = _resolve_path(filename)
    except ValueError:
        return f"file_write: путь вне рабочей папки — {filename!r}"
    if path.exists():
        size = path.stat().st_size
        return f"file_write: перезапишет {path} (сейчас {size:,} байт)"
    return f"file_write: создаст новый файл {path}"


def _preview_file_move(args: dict) -> str:
    from agent.tools import _resolve_path
    source = str(args.get("source", ""))
    destination = str(args.get("destination", ""))
    try:
        src = _resolve_path(source)
        dst = _resolve_path(destination)
    except ValueError as e:
        return f"file_move: {e}"
    if not src.exists():
        return f"file_move: источник {src} не найден"
    note = f"file_move: {src} → {dst}"
    if dst.exists():
        note += f" (перезапишет существующий файл, {dst.stat().st_size:,} байт)"
    return note


def _preview_env_set(args: dict) -> str:
    from pathlib import Path
    key = str(args.get("key", ""))
    value = str(args.get("value", ""))
    env_path = Path(".env")
    old = None
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{key}="):
                old = line.split("=", 1)[1]
                break
    if old is None:
        return f"system_env_set: добавит новую переменную {key}={value!r}"
    return f"system_env_set: {key}: {old!r} → {value!r}"


def _preview_process_kill(args: dict) -> str:
    name_or_pid = str(args.get("name_or_pid", "")).strip()
    try:
        import psutil
        pid = int(name_or_pid)
        p = psutil.Process(pid)
        return f"process_kill: завершит PID {pid} ({p.name()})"
    except ValueError:
        try:
            import psutil
            matches = [
                p.info["name"] for p in psutil.process_iter(["name"])
                if name_or_pid.lower() in (p.info["name"] or "").lower()
            ]
            if matches:
                return f"process_kill: завершит процессы по маске «{name_or_pid}»: {', '.join(matches[:10])}"
            return f"process_kill: по маске «{name_or_pid}» ничего не найдено"
        except Exception:
            return f"process_kill: завершит процессы, совпадающие с «{name_or_pid}»"
    except Exception:
        return f"process_kill: PID {name_or_pid} — процесс не найден"


def _preview_command(tool_name: str, args: dict) -> str:
    command = args.get("command") or args.get("code") or ""
    host = args.get("host")
    host_note = f" на '{host}'" if host else ""
    return f"{tool_name}{host_note}: выполнит —\n{command}"
