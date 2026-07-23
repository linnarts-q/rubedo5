"""launcher.py's process supervisor (§2 phase 2, stage 9.6's "kill
launcher-child -> restart" requirement). Drives supervise_tick()
directly against real child processes (trivial sleep scripts, not the
real interface.telegram -- no live Telegram connection needed to prove
the supervisor mechanics) rather than running main()'s infinite loop.

The actual crash-RESUME behavior ("продолжить?" after a restart) is a
separate concern already fully covered end-to-end over LocalTransport
in test_transport_e2e.py -- agent/crash_recovery.py's detect_crash()
only ever reads Postgres state (agent_state.clean_shutdown), never
inspects the OS process table, so a real kill -9 and a simulated
unclean heartbeat are indistinguishable from the system's own point of
view. This file is about the supervisor loop itself: does a dead
child actually get relaunched, respecting MAX_RESTARTS and the
intentional-restart/update exit codes.
"""
from __future__ import annotations

import signal
import subprocess
import sys
import time

import launcher


def _sleep_child(seconds: float = 30) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def _exit_code_child(code: int) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", f"import sys; sys.exit({code})"])


def test_killed_child_gets_restarted():
    proc = _sleep_child()
    procs = {"test": proc}
    restarts = {"test": 0}
    failed = set()
    processes = [{"name": "test", "cmd": [sys.executable, "-c", "import time; time.sleep(30)"]}]

    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=5)
    assert proc.poll() is not None

    launcher.supervise_tick(procs, restarts, failed, processes)

    assert restarts["test"] == 1
    assert procs["test"].poll() is None, "restarted child should be alive"
    assert procs["test"].pid != proc.pid

    procs["test"].terminate()
    procs["test"].wait(timeout=5)


def test_max_restarts_gives_up():
    processes = [{"name": "test", "cmd": [sys.executable, "-c", "import sys; sys.exit(1)"]}]
    procs = {"test": _exit_code_child(1)}
    restarts = {"test": 0}
    failed = set()

    for _ in range(launcher.MAX_RESTARTS):
        procs["test"].wait(timeout=5)
        launcher.supervise_tick(procs, restarts, failed, processes)

    assert "test" in failed
    # Once failed, further ticks must not keep trying to restart it.
    dead_pid = procs["test"].pid
    launcher.supervise_tick(procs, restarts, failed, processes)
    assert procs["test"].pid == dead_pid


def test_intentional_restart_code_resets_counter():
    processes = [{"name": "test", "cmd": [sys.executable, "-c", "import time; time.sleep(30)"]}]
    procs = {"test": _exit_code_child(launcher.RESTART_CODE)}
    restarts = {"test": 3}  # pretend it had already failed a few times
    failed = set()

    procs["test"].wait(timeout=5)
    launcher.supervise_tick(procs, restarts, failed, processes)

    assert restarts["test"] == 0
    assert procs["test"].poll() is None
    procs["test"].terminate()
    procs["test"].wait(timeout=5)


def test_update_code_signals_pending_update_without_restarting():
    processes = [{"name": "test", "cmd": [sys.executable, "-c", "import time; time.sleep(30)"]}]
    procs = {"test": _exit_code_child(launcher.UPDATE_CODE)}
    restarts = {"test": 0}
    failed = set()

    procs["test"].wait(timeout=5)
    pending = launcher.supervise_tick(procs, restarts, failed, processes)

    assert pending is True
    assert procs["test"].poll() is not None, "update exit code must not auto-restart the child"


def test_terminate_all_stops_every_child():
    procs = {"a": _sleep_child(), "b": _sleep_child()}
    launcher._terminate_all(procs)
    assert procs["a"].poll() is not None
    assert procs["b"].poll() is not None


def test_shutdown_marks_clean_before_terminating(tools_ctx):
    import memory.db as db
    with db.get_conn() as conn:
        conn.execute("UPDATE agent_state SET clean_shutdown=FALSE")
    launcher._mark_clean_shutdown()
    with db.get_conn() as conn:
        row = conn.execute("SELECT clean_shutdown FROM agent_state").fetchone()
    assert row["clean_shutdown"] is True
