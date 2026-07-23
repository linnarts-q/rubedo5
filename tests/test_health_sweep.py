"""workspace/health_sweep.py (§6, pulled forward to stage 9.3) +
day/tick.py's wiring around it. Priority: a threshold breach must
reach Лин at "critical" severity (breaks through night/day-off/quiet
per С16), a normal reading must stay quiet, cooldown must suppress a
repeat within the window, and -- the one thing 9.3 specifically added
a safety net for -- a broken edit to her own file must never take the
tick down with it.
"""
from __future__ import annotations

import asyncio

import workspace.health_sweep as health_sweep
import day.tick as tick


def _fake_stats(**overrides):
    base = {"cpu": 10.0, "ram": 20.0, "disk": 30.0, "temp": 40.0}
    base.update(overrides)
    return base


def test_normal_readings_produce_no_alerts(monkeypatch):
    health_sweep._last_alert.clear()
    monkeypatch.setattr(health_sweep, "_get_stats", lambda: _fake_stats())
    alerts = asyncio.run(health_sweep.check())
    assert alerts == []


def test_cpu_breach_produces_alert(monkeypatch):
    health_sweep._last_alert.clear()
    monkeypatch.setattr(health_sweep, "_get_stats", lambda: _fake_stats(cpu=99.0))
    alerts = asyncio.run(health_sweep.check())
    assert len(alerts) == 1 and "CPU" in alerts[0]


def test_temp_none_never_false_positives(monkeypatch):
    """No sensor readings available (temp=None, e.g. no lm-sensors on
    this machine) must never be treated as a breach."""
    health_sweep._last_alert.clear()
    monkeypatch.setattr(health_sweep, "_get_stats", lambda: _fake_stats(temp=None))
    alerts = asyncio.run(health_sweep.check())
    assert alerts == []


def test_cooldown_suppresses_repeat_alert(monkeypatch):
    health_sweep._last_alert.clear()
    monkeypatch.setattr(health_sweep, "_get_stats", lambda: _fake_stats(cpu=99.0))
    first = asyncio.run(health_sweep.check())
    second = asyncio.run(health_sweep.check())
    assert len(first) == 1
    assert second == [], "same breach within the cooldown window must not re-alert"


def test_cooldown_is_per_metric(monkeypatch):
    health_sweep._last_alert.clear()
    monkeypatch.setattr(health_sweep, "_get_stats", lambda: _fake_stats(cpu=99.0))
    asyncio.run(health_sweep.check())  # arms the cpu cooldown only

    monkeypatch.setattr(health_sweep, "_get_stats", lambda: _fake_stats(cpu=99.0, ram=95.0))
    alerts = asyncio.run(health_sweep.check())
    assert len(alerts) == 1 and "RAM" in alerts[0], alerts


def test_tick_delivers_breach_at_critical_severity(tools_ctx, monkeypatch):
    health_sweep._last_alert.clear()
    monkeypatch.setattr(health_sweep, "_get_stats", lambda: _fake_stats(disk=99.0))

    delivered = {}
    async def _fake_deliver(severity, content, send_fn, source="", **kw):
        delivered["severity"] = severity
        delivered["content"] = content
        delivered["source"] = source
        return None
    import agent.notify as notify
    monkeypatch.setattr(notify, "deliver", _fake_deliver)

    async def send_fn(t):
        pass

    asyncio.run(tick._check_health_sweep(send_fn))
    assert delivered.get("severity") == "critical"
    assert delivered.get("source") == "health_sweep"
    assert "диск" in delivered.get("content", "")


def test_tick_stays_quiet_on_a_normal_reading(tools_ctx, monkeypatch):
    health_sweep._last_alert.clear()
    monkeypatch.setattr(health_sweep, "_get_stats", lambda: _fake_stats())

    delivered = {}
    async def _fake_deliver(severity, content, send_fn, source="", **kw):
        delivered["called"] = True
        return None
    import agent.notify as notify
    monkeypatch.setattr(notify, "deliver", _fake_deliver)

    async def send_fn(t):
        pass

    asyncio.run(tick._check_health_sweep(send_fn))
    assert "called" not in delivered


def test_tick_survives_a_broken_health_sweep_check(tools_ctx, monkeypatch):
    """The actual safety net 9.3 asked for: if her own edit makes
    check() itself blow up, _check_health_sweep must catch it, report
    it at 'normal' severity (not crash the tick), and never propagate."""
    async def _boom():
        raise RuntimeError("её правка сломала синтаксис где-то ниже")
    monkeypatch.setattr(health_sweep, "check", _boom)

    delivered = {}
    async def _fake_deliver(severity, content, send_fn, source="", **kw):
        delivered["severity"] = severity
        delivered["content"] = content
        return None
    import agent.notify as notify
    monkeypatch.setattr(notify, "deliver", _fake_deliver)

    async def send_fn(t):
        pass

    asyncio.run(tick._check_health_sweep(send_fn))  # must not raise

    assert delivered.get("severity") == "normal"
    assert "рефлекси" in delivered.get("content", "").lower()
