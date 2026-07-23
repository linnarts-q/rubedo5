"""Health-sweep (§6, pulled forward to stage 9.3 — the audit found
heartbeat/recover/shutdown blocked crash-isolation tests without a
live process; this is the analogous case for §6's own most useful
piece: nobody was watching CPU/RAM/disk/temperature at all until this
lands).

Deliberately her own file, in her own workspace, not agent/ or day/ —
a candidate for her first self-written tool (the original spec's own
framing), even before the full self-authored-tools mechanism (§8)
exists to formally register it. day/tick.py calls check() directly by
path on its own 60-second cadence; there is no separate asyncio loop
here the way rubedo4's monitor/system.py had one — that would just be
a second, competing timer.

Green zone: reading system stats needs no approval, and this file
lives in workspace/ where she can read/edit/fix it herself (§1). If
her own edit breaks this file, day/tick.py's caller is the one
responsible for not letting that take the whole tick down with it —
see day/tick.py's _check_health_sweep for the try/except and error
report back to her.
"""
from __future__ import annotations

import time

from config import (
    CPU_ALERT_PCT, RAM_ALERT_PCT, DISK_ALERT_PCT, TEMP_ALERT_C, ALERT_COOLDOWN_SEC,
)

# In-process only, like rubedo4's original _last_alert dict — this
# runs inside the same long-lived tick loop process, not respawned per
# check, so there's nothing to persist across a restart: a crash means
# a fresh cooldown window too, which is the conservative direction to
# err in for "did the disk fill up" style alerts.
_last_alert: dict[str, float] = {}


def _get_stats() -> dict:
    import psutil
    cpu = psutil.cpu_percent(interval=0.5)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    temp = None
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            all_readings = [r.current for readings in temps.values() for r in readings if r.current]
            if all_readings:
                temp = max(all_readings)
    except Exception:
        pass
    return {"cpu": cpu, "ram": ram.percent, "disk": disk.percent, "temp": temp}


def _should_alert(key: str) -> bool:
    now = time.time()
    if now - _last_alert.get(key, 0) >= ALERT_COOLDOWN_SEC:
        _last_alert[key] = now
        return True
    return False


async def check() -> list[str]:
    """Returns human-readable alert strings for whatever crossed its
    threshold and isn't still in its own cooldown window, or [] on a
    normal tick (the common case). Caller decides severity/delivery —
    this only measures and thresholds."""
    import asyncio
    stats = await asyncio.to_thread(_get_stats)
    alerts = []
    if stats["cpu"] > CPU_ALERT_PCT and _should_alert("cpu"):
        alerts.append(f"CPU {stats['cpu']:.0f}% (порог {CPU_ALERT_PCT}%)")
    if stats["ram"] > RAM_ALERT_PCT and _should_alert("ram"):
        alerts.append(f"RAM {stats['ram']:.0f}% (порог {RAM_ALERT_PCT}%)")
    if stats["disk"] > DISK_ALERT_PCT and _should_alert("disk"):
        alerts.append(f"диск {stats['disk']:.0f}% (порог {DISK_ALERT_PCT}%)")
    if stats["temp"] and stats["temp"] > TEMP_ALERT_C and _should_alert("temp"):
        alerts.append(f"температура {stats['temp']:.0f}°C (порог {TEMP_ALERT_C}°C)")
    return alerts
