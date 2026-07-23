"""Process supervisor (§2 phase 2, stage 7.5 — "transport and the live
process"). Ported from rubedo4's launcher.py nearly as-is: spawn,
watch, restart-with-backoff, the intentional-restart and update exit
codes. Two additions:

  - A real SIGTERM handler (not just KeyboardInterrupt) marks a clean
    shutdown (memory.db.agent_state.clean_shutdown, via
    agent.crash_recovery.shutdown_clean()) before terminating children
    — so the next startup's crash_recovery.detect_crash() correctly
    tells "this was on purpose" from "it just died".
  - The update-restart defers while Lin has an active chat-origin
    session (§8: "рестарт агента откладывается, если есть активная
    сессия по задаче от Лин") — checked every supervisor tick (already
    polling every 2s) until it's clear, instead of restarting mid-task.
    Update source stays "git pull origin main" — merging a PR-flow
    branch into main already requires Лин's explicit approval (§8),
    the same gate rubedo4's git-fetch-compare always assumed.

ENABLE_DISPLAY keeps the same optional slot rubedo4 had for
display/window.py, now ported (§19, stage 8) with the session-state
and background adaptations described there.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger("rubedo.launcher")

try:
    from config import apply_timezone
    _tz = apply_timezone()
    if _tz:
        log.info(f"Timezone applied: {_tz}")
except Exception as _e:
    log.warning(f"apply_timezone failed: {_e}")

ROOT = Path(__file__).parent
RESTART_CODE = 42
UPDATE_CODE = 43
MAX_RESTARTS = 5

PROCESSES = [
    {"name": "telegram", "cmd": [sys.executable, "-m", "interface.telegram"]},
]

if os.getenv("ENABLE_DISPLAY", "0") == "1":
    PROCESSES.append({"name": "display", "cmd": [sys.executable, "-m", "display.window"]})


def _start(p: dict) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    log.info(f"Starting: {p['name']}")
    return subprocess.Popen(p["cmd"], cwd=str(ROOT), env=env)


def _lin_has_active_session() -> bool:
    """Restart-deferral gate (§8) — launcher.py runs in this same
    repo/venv, so it just reads Postgres directly rather than needing
    any IPC with the telegram subprocess. Any exception here (DB down,
    import failure) defaults to "proceed" — a stuck launcher that can
    never update is a worse failure mode than an update landing mid-
    task on a rare bad day."""
    try:
        sys.path.insert(0, str(ROOT))
        from memory.db import init_db, session_list
        init_db()
        return any(s.get("origin") != "queue" for s in session_list(status="active", limit=50))
    except Exception as e:
        log.warning(f"active-session check failed, assuming none (proceeding): {e}")
        return False


def _do_update() -> None:
    log.info("Running git pull...")
    subprocess.run(["git", "-C", str(ROOT), "pull", "origin", "main"], check=False)
    req = ROOT / "requirements.txt"
    if req.exists():
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "-r", str(req)],
            check=False,
        )
    (ROOT / "data" / ".update_done").touch()
    log.info("Update complete.")


def _mark_clean_shutdown() -> None:
    try:
        sys.path.insert(0, str(ROOT))
        from agent.crash_recovery import shutdown_clean
        shutdown_clean()
        log.info("Marked clean shutdown")
    except Exception as e:
        log.warning(f"shutdown_clean failed: {e}")


def _terminate_all(procs: dict[str, subprocess.Popen]) -> None:
    for proc in procs.values():
        if proc.poll() is None:
            proc.terminate()
    for proc in procs.values():
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def supervise_tick(
    procs: dict[str, subprocess.Popen],
    restarts: dict[str, int],
    failed: set[str],
    processes: list[dict] = PROCESSES,
) -> bool:
    """One supervisor pass: restart any process that died (unless it's
    permanently failed or an intentional/update exit code), respecting
    MAX_RESTARTS. Mutates procs/restarts/failed in place. Returns True
    if an update is now pending (caller decides when to actually apply
    it — see main()'s own pending_update handling, unchanged here).
    Split out from main()'s loop so a test can drive exactly one tick
    against a real child process without needing the infinite loop or
    a live Telegram connection."""
    pending_update = False
    for p in processes:
        name = p["name"]
        if name in failed:
            continue
        if procs[name].poll() is None:
            continue
        code = procs[name].returncode
        if code == RESTART_CODE:
            log.info(f"[{name}] Intentional restart")
            restarts[name] = 0
        elif code == UPDATE_CODE:
            log.info(f"[{name}] Update requested — will restart once no active Lin session")
            pending_update = True
            continue
        else:
            restarts[name] += 1
            log.warning(f"[{name}] Exited code={code} restart={restarts[name]}/{MAX_RESTARTS}")
        if restarts[name] >= MAX_RESTARTS:
            log.error(f"[{name}] Max restarts reached, giving up")
            failed.add(name)
            continue
        time.sleep(2)
        procs[name] = _start(p)
    return pending_update


def main() -> None:
    procs: dict[str, subprocess.Popen] = {}
    restarts: dict[str, int] = {}
    failed: set[str] = set()  # permanently dead, won't be restarted
    pending_update = False

    def _shutdown_and_exit(*_a) -> None:
        log.info("Shutting down...")
        _mark_clean_shutdown()
        _terminate_all(procs)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown_and_exit)

    for p in PROCESSES:
        procs[p["name"]] = _start(p)
        restarts[p["name"]] = 0

    try:
        while True:
            time.sleep(2)

            if pending_update:
                if _lin_has_active_session():
                    continue  # deferred (§8) — re-check next tick
                log.info("No active Lin session — proceeding with deferred update")
                _terminate_all(procs)
                _do_update()
                pending_update = False
                for p2 in PROCESSES:
                    restarts[p2["name"]] = 0
                    failed.discard(p2["name"])
                    procs[p2["name"]] = _start(p2)
                continue

            if supervise_tick(procs, restarts, failed, PROCESSES):
                pending_update = True
    except KeyboardInterrupt:
        _shutdown_and_exit()


if __name__ == "__main__":
    main()
