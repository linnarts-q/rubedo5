"""Display avatar/panel window (§19 — conservative port + session
state). Ported from rubedo4's display/window.py: the pygame rendering,
panels, alarm screen, sleep-mode xrandr handling, and interaction are
untouched — a full redesign is separate, later work (Лин's own call).

Exactly the changes §19 asks for, plus what's unavoidable to even run
in rubedo5 (SQLite is gone, replaced by Postgres in stage 1.5 — this
was never optional, just necessary translation, not a redesign):

  1. `_load_plan`/`_load_queue` read Postgres (memory.db.get_conn())
     instead of sqlite3.connect(DB_PATH) — same queries, adapted
     placeholders, same day_tasks/rubedo_queue/day_state schema.

  2. The main "Brain" label no longer comes from the bus's single
     idle/thinking/error activity flag — it's a priority read of
     task_sessions: error > waiting_user > working > idle. Two
     sessions can now genuinely coexist (§2 phase 2); a session
     waiting on Lin outranks one still working, even if that one's
     also live. "error" stays a transient bus-driven flash
     (AgentError) layered on top, same as before, since a poll alone
     can't tell "just failed" from "failed an hour ago" without
     tracking more state than this is worth. brain_tool/iter_count/
     logs stay exactly as they were, driven by ToolCalled/ToolFinished
     — those are the "what am I doing" gloss on the "which of these
     applies" priority state, and don't need to change.

  3. Background is a single persisted image (memory.db meta key
     "display_background", set by agent.tools.display.set_background)
     instead of the old hardcoded data/bg_idle.png + data/bg_thinking.png
     blend — nothing about a *default* two-mood blend was asked for,
     and blending one path against itself would just be dead weight.
     Polled the same 1-second cadence as everything else in draw() and
     hot-reloaded on change, no restart needed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from config import ENABLE_DISPLAY, DISPLAY_W, DISPLAY_H

log = logging.getLogger("rubedo.display")

_ALARM_ACTIVE_FILE = Path("data/.alarm_active")
_ALARM_DISMISSED_FILE = Path("data/.alarm_dismissed")
_PRE_ALARM_WAKE_FILE = Path("data/.pre_alarm_wake")
_SLEEP_REQUEST_FILE = Path("data/.sleep_request")
_RESTART_DISPLAY_FILE = Path("data/.restart_display")
_SLEEP_IMAGE = Path("images/sleep.png")
_ALARM_IMG_1 = Path("wake_alarm/alarm_1.png")
_ALARM_IMG_2 = Path("wake_alarm/alarm_2.png")
_DOUBLE_CLICK_MS = 500
_ALARM_TAPS_REQUIRED = 3
_LOGS_FILE = Path("data/.display_logs.json")

# State-driven color overlay. Applied as a translucent layer on top of the
# panel scene so text stays sharp. Keys: idle / thinking / waiting / error.
_PALETTES = {
    "idle":     (40, 110, 180, 60),   # cyan/blue
    "thinking": (255, 180, 40, 90),   # amber
    "waiting":  (150, 90, 210, 90),   # violet — a session needs Lin (§19)
    "error":    (175, 50, 50, 90),    # red
}
_TRANSITION_DURATION_SEC = 0.5
_TRANSITION_BLEND_STRIP_PX = 40
_ERROR_FLASH_MAX_SEC = 60.0  # safety-net auto-clear if no next turn ever starts


def _load_plan(state: dict) -> None:
    """Postgres, not SQLite (stage 1.5 migration) — otherwise identical
    to rubedo4's query and shape."""
    try:
        from datetime import date
        from memory.db import get_conn
        today_str = date.today().isoformat()
        with get_conn() as conn:
            day_off_row = conn.execute(
                "SELECT is_dayoff FROM day_state WHERE date=%s", (today_str,),
            ).fetchone()
            rows = conn.execute(
                "SELECT title, scheduled_at, status FROM day_tasks "
                "WHERE date=%s AND status IN ('pending','in_progress','done','failed') "
                "ORDER BY scheduled_at",
                (today_str,),
            ).fetchall()
        state["is_dayoff"] = bool(day_off_row["is_dayoff"]) if day_off_row else False
        plan_items = []
        for r in rows:
            t = r["scheduled_at"] or ""
            for sep in (" ", "T"):
                if sep in t:
                    t = t.split(sep, 1)[1]
            t = t[:5]  # HH:MM
            label = (f"{t} " if t else "") + r["title"]
            plan_items.append({"text": label, "status": r["status"]})
        state["plan"] = plan_items
    except Exception as e:
        log.warning(f"Plan load failed: {e}")


def _load_queue(state: dict) -> None:
    """Postgres, not SQLite — same shape as rubedo4's query."""
    try:
        from memory.db import get_conn
        from datetime import datetime as _dt, timedelta
        now = _dt.now()
        cutoff = (now - timedelta(hours=24)).isoformat(timespec="seconds").replace("T", " ")
        now_str = now.isoformat(timespec="seconds").replace("T", " ")
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT title, status, scheduled_at, created_at, completed_at
                FROM rubedo_queue
                WHERE status IN ('pending', 'running')
                   OR (status IN ('done', 'failed', 'cancelled') AND
                       replace(coalesce(completed_at, created_at), 'T', ' ') >= %s)
                ORDER BY
                    CASE WHEN scheduled_at IS NOT NULL AND scheduled_at != '' THEN 0 ELSE 1 END,
                    replace(coalesce(scheduled_at, created_at), 'T', ' ')
                """,
                (cutoff,),
            ).fetchall()
        queue_items = []
        for r in rows:
            status = r["status"]
            sched = (r["scheduled_at"] or "").replace("T", " ")
            created = (r["created_at"] or "").replace("T", " ")
            time_src = sched if sched else created
            t = ""
            if " " in time_src:
                t = time_src.split(" ", 1)[1][:5]
            elif time_src:
                t = time_src[:5]
            is_overdue = status == "pending" and sched and sched < now_str
            queue_items.append({
                "text": (f"{t} " if t else "") + r["title"],
                "status": "overdue" if is_overdue else status,
            })
        state["queue"] = queue_items
    except Exception as e:
        log.warning(f"Queue load failed: {e}")


def _compute_session_state() -> str:
    """error > waiting_user > working > idle (§19), reading
    task_sessions directly. "error" itself isn't computed here — a bare
    poll can't distinguish "just failed" from "failed yesterday" without
    tracking more than this is worth, so it stays the bus-driven
    transient flash it already was (AgentError); this only resolves
    the other three, since two sessions can now genuinely coexist (§2
    phase 2) and a session waiting on Lin should outrank one still
    working even if both are real."""
    try:
        from memory.db import get_conn
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT status FROM task_sessions WHERE status IN ('waiting_user','active')"
            ).fetchall()
    except Exception as e:
        log.warning(f"Session state poll failed: {e}")
        return "idle"
    statuses = {r["status"] for r in rows}
    if "waiting_user" in statuses:
        return "waiting"
    if "active" in statuses:
        return "thinking"
    return "idle"


def _current_background_path() -> str:
    try:
        from memory.db import load_meta
        return load_meta("display_background") or ""
    except Exception as e:
        log.warning(f"Background meta read failed: {e}")
        return ""


def _save_logs(logs: list) -> None:
    try:
        _LOGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _LOGS_FILE.write_text(json.dumps(logs[-40:]))
    except Exception as e:
        log.warning(f"Logs save failed: {e}")


def _restore_logs() -> list:
    try:
        if _LOGS_FILE.exists():
            return json.loads(_LOGS_FILE.read_text())
    except Exception as e:
        log.warning(f"Logs restore failed: {e}")
    return []


def _draw_palette_overlay(pygame, screen, state: dict, now_ts: float, w: int, h: int) -> None:
    """Render the state-driven color overlay. During a transition the new
    palette fills from the bottom upward over `_TRANSITION_DURATION_SEC`,
    with a soft blend strip between the old and new regions."""
    new_name = state.get("palette_name") or "idle"
    new_color = _PALETTES.get(new_name)
    if not new_color:
        return

    overlay = pygame.Surface((w, h), pygame.SRCALPHA)

    if state.get("transition_active"):
        elapsed = now_ts - state.get("transition_start", now_ts)
        progress = min(1.0, max(0.0, elapsed / _TRANSITION_DURATION_SEC))
        old_name = state.get("transition_old_name") or new_name
        old_color = _PALETTES.get(old_name, new_color)

        sweep_y = int(h * (1.0 - progress))
        if sweep_y > 0:
            pygame.draw.rect(overlay, old_color, (0, 0, w, sweep_y))
        pygame.draw.rect(overlay, new_color, (0, sweep_y, w, h - sweep_y))

        strip_h = _TRANSITION_BLEND_STRIP_PX
        for i in range(strip_h):
            y = sweep_y - strip_h // 2 + i
            if y < 0 or y >= h:
                continue
            t = i / max(1, strip_h - 1)
            r = int(old_color[0] * (1 - t) + new_color[0] * t)
            g = int(old_color[1] * (1 - t) + new_color[1] * t)
            b = int(old_color[2] * (1 - t) + new_color[2] * t)
            a = int(old_color[3] * (1 - t) + new_color[3] * t)
            pygame.draw.line(overlay, (r, g, b, a), (0, y), (w, y))

        if progress >= 1.0:
            state["transition_active"] = False
            state["transition_old_name"] = new_name
    else:
        pygame.draw.rect(overlay, new_color, (0, 0, w, h))

    screen.blit(overlay, (0, 0))


def _request_palette(state: dict, name: str) -> None:
    """Switch the active palette and arm a sweep transition.

    Bottom-up vertical sweep over `_TRANSITION_DURATION_SEC`: the previous
    palette stays visible in the top region and gradually recedes upward
    as the new palette fills in from below."""
    if name not in _PALETTES:
        return
    if state.get("palette_name") == name:
        return
    state["transition_old_name"] = state.get("palette_name", name)
    state["palette_name"] = name
    state["transition_start"] = time.time()
    state["transition_active"] = True


def _start_bus_listener(state: dict) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run():
        from bus.client import BusClient, sync_publisher
        from bus.events import (
            AgentStarted, AgentError,
            ToolCalled, ToolFinished, DayPlanUpdated, QueueUpdated,
            AlarmStarted, AlarmDismissed, PreAlarmWake,
            SleepRequested, DisplayRestartRequested,
        )

        async def handler(event):
            if isinstance(event, AgentStarted):
                # A fresh turn starting always clears a stale error
                # flash — the session-state poll (§19) owns idle/
                # waiting/thinking now, this only owns the error flag.
                state["error_active"] = False
                state["iter_count"] = 0
            elif isinstance(event, AgentError):
                state["error_active"] = True
                state["error_text"] = event.error[:40]
                state["error_ts"] = time.time()
                state["brain_tool"] = ""
                state["iter_count"] = 0
                _request_palette(state, "error")
            elif isinstance(event, ToolCalled):
                ts = datetime.now().strftime("%H:%M:%S")
                entry = f"{ts} -> {event.name}"
                state["brain_tool"] = event.name
                state["iter_count"] = state.get("iter_count", 0) + 1
                state["logs"].append(entry)
                if len(state["logs"]) > 40:
                    state["logs"].pop(0)
                _save_logs(state["logs"])
            elif isinstance(event, ToolFinished):
                ts = datetime.now().strftime("%H:%M:%S")
                mark = "ok" if event.success else "err"
                state["logs"].append(f"{ts} [{mark}] {event.name}")
                if len(state["logs"]) > 40:
                    state["logs"].pop(0)
                _save_logs(state["logs"])
            elif isinstance(event, DayPlanUpdated):
                _load_plan(state)
            elif isinstance(event, QueueUpdated):
                _load_queue(state)
            elif isinstance(event, AlarmStarted):
                state["bus_signal_alarm_active"] = True
            elif isinstance(event, AlarmDismissed):
                state["bus_signal_alarm_dismissed"] = True
            elif isinstance(event, PreAlarmWake):
                state["bus_signal_pre_alarm_wake"] = True
            elif isinstance(event, SleepRequested):
                state["bus_signal_sleep_request"] = event.mode
            elif isinstance(event, DisplayRestartRequested):
                state["bus_signal_restart_display"] = True

        client = BusClient()
        client.subscribe(handler)
        await client.connect()
        # Make sync_publisher available to pygame thread so it can
        # publish AlarmDismissed when the user taps to dismiss.
        sync_publisher.setup(loop, client)
        while True:
            await asyncio.sleep(1)

    loop.run_until_complete(_run())


def run() -> None:
    if not ENABLE_DISPLAY:
        log.info("Display disabled (ENABLE_DISPLAY=0)")
        return
    # Apply configured timezone before any datetime.now() (defensive
    # duplicate of launcher.py's apply_timezone — if the launcher is
    # older than this code, this still ensures the display child has
    # the correct local time).
    try:
        from config import apply_timezone
        _tz = apply_timezone()
        if _tz:
            log.info(f"Timezone applied (display child): {_tz}")
    except Exception as _e:
        log.warning(f"apply_timezone (display) failed: {_e}")
    try:
        import pygame
        _run_pygame()
    except ImportError:
        log.warning("pygame not installed, display unavailable")
    except Exception as e:
        log.error(f"Display error: {e}")


def _load_image_safe(pygame, path: Path, size: tuple):
    try:
        if path.exists():
            img = pygame.image.load(str(path))
            return pygame.transform.scale(img, size)
    except Exception as e:
        log.warning(f"Could not load image {path}: {e}")
    return None


def _wrap_text(font, text: str, max_w: int) -> list[str]:
    """Wrap text to fit within max_w pixels using font metrics."""
    words = text.split()
    if not words:
        return [text]
    lines, cur = [], ""
    for word in words:
        test = (cur + " " + word).strip()
        if font.size(test)[0] <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = word
            # Truncate a single word that still exceeds max_w
            while font.size(cur)[0] > max_w and len(cur) > 1:
                cur = cur[:-1]
    if cur:
        lines.append(cur)
    return lines or [text]


def _panel(
    screen, font_sm, font_md, c_panel, c_accent, c_text, c_expand,
    x, y, w, h, title: str, lines: list[str], expanded: dict,
    centered: bool = False,
    line_color_fn=None,
    title_color=None, title_font=None,
    auto_expand: bool = False,
) -> list:
    """Render a panel with pixel-accurate text wrapping.

    Returns a list of (pygame.Rect, panel_title, item_idx) hit areas for
    items that span more than one wrapped line (tap to expand/collapse).

    `title_color` / `title_font` override the border-derived defaults
    for the panel header. `auto_expand=True` shows every wrapped line
    instead of the first-line + tap-to-expand pattern (useful for short
    lists like the Day Plan).
    """
    import pygame
    rect = pygame.Rect(x, y, w, h)
    if c_panel is not None:
        pygame.draw.rect(screen, c_panel, rect, border_radius=4)
        pygame.draw.rect(screen, c_accent, rect, 1, border_radius=4)
    tcolor = title_color if title_color is not None else c_accent
    tfont = title_font if title_font is not None else font_md
    screen.blit(tfont.render(title, True, tcolor), (x + 8, y + 6))

    hit_areas = []
    max_text_w = w - 16
    content_top = y + tfont.get_linesize() + 10
    line_h = font_sm.get_linesize()
    bottom = y + h - 4

    if centered and lines:
        content_h = bottom - content_top
        total_h = len(lines) * line_h
        cur_y = content_top + max(0, (content_h - total_h) // 2)
        for line in lines:
            if cur_y >= bottom:
                break
            surf = font_sm.render(str(line), True, c_text)
            screen.blit(surf, (x + w // 2 - surf.get_width() // 2, cur_y))
            cur_y += line_h
        return hit_areas

    cur_y = content_top
    for i, line in enumerate(lines):
        if cur_y >= bottom:
            break
        wrapped = _wrap_text(font_sm, str(line), max_text_w)
        is_expandable = len(wrapped) > 1
        is_expanded = expanded.get((title, i), False)
        if auto_expand:
            rows_to_show = wrapped
        else:
            rows_to_show = wrapped if (is_expanded or not is_expandable) else wrapped[:1]

        item_start_y = cur_y
        line_override = line_color_fn(str(line)) if line_color_fn else None
        for j, row in enumerate(rows_to_show):
            if cur_y >= bottom:
                break
            if not auto_expand and is_expandable and j == 0 and not is_expanded:
                color = c_expand
            else:
                color = line_override if line_override is not None else c_text
            screen.blit(font_sm.render(row, True, color), (x + 8, cur_y))
            cur_y += line_h

        if is_expandable and not auto_expand:
            item_rect = pygame.Rect(x, item_start_y, w, cur_y - item_start_y)
            hit_areas.append((item_rect, title, i))

    return hit_areas


def _draw_plan_panel(
    screen, font_content, font_title, c_panel, c_accent, c_title,
    x, y, w, h,
    items: list[dict],  # list of {"text": str, "status": str}
) -> None:
    """Render the Day Plan panel with status-based colors and strikethrough."""
    import pygame
    C_DONE = (80, 200, 100)    # green for completed tasks
    C_FAILED = (220, 70, 70)   # red for failed tasks
    C_NORMAL = c_title

    rect = pygame.Rect(x, y, w, h)
    if c_panel is not None:
        pygame.draw.rect(screen, c_panel, rect, border_radius=4)
        pygame.draw.rect(screen, c_accent, rect, 1, border_radius=4)
    screen.blit(font_title.render("Day Plan", True, c_title), (x + 8, y + 6))

    cur_y = y + font_title.get_linesize() + 10
    bottom = y + h - 4
    line_h = font_content.get_linesize()
    max_w = w - 16

    if not items:
        surf = font_content.render("— очередь пуста —", True, (100, 100, 120))
        screen.blit(surf, (x + 8, cur_y))
        return

    for item in items:
        if cur_y >= bottom:
            break
        text = item.get("text", "")
        status = item.get("status", "pending")

        if status == "done":
            color = C_DONE
            strike = True
        elif status == "failed":
            color = C_FAILED
            strike = True
        else:
            color = C_NORMAL
            strike = False

        # Wrap text to max_w
        words = text.split()
        lines: list[str] = []
        cur_line = ""
        for word in words:
            test = (cur_line + " " + word).strip()
            if font_content.size(test)[0] <= max_w:
                cur_line = test
            else:
                if cur_line:
                    lines.append(cur_line)
                cur_line = word
        if cur_line:
            lines.append(cur_line)
        if not lines:
            lines = [text]

        for row in lines:
            if cur_y >= bottom:
                break
            surf = font_content.render(row, True, color)
            screen.blit(surf, (x + 8, cur_y))
            if strike:
                mid = cur_y + surf.get_height() // 2
                pygame.draw.line(screen, color, (x + 8, mid), (x + 8 + surf.get_width(), mid), 2)
            cur_y += line_h


def _draw_queue_panel(
    screen, font_content, font_title, c_panel, c_accent, c_title,
    x, y, w, h,
    items: list[dict],
) -> None:
    """Render the Rubedo's tasks panel showing Rubedo's queue tasks."""
    import pygame
    C_DONE = (80, 200, 100)
    C_FAILED = (220, 70, 70)
    C_OVERDUE = (220, 200, 60)
    C_RUNNING = (100, 160, 255)
    C_NORMAL = c_title

    rect = pygame.Rect(x, y, w, h)
    if c_panel is not None:
        pygame.draw.rect(screen, c_panel, rect, border_radius=4)
        pygame.draw.rect(screen, c_accent, rect, 1, border_radius=4)
    screen.blit(font_title.render("Rubedo's Tasks", True, c_title), (x + 8, y + 6))

    cur_y = y + font_title.get_linesize() + 10
    bottom = y + h - 4
    line_h = font_content.get_linesize()
    max_w = w - 16

    if not items:
        surf = font_content.render("— очередь пуста —", True, (100, 100, 120))
        screen.blit(surf, (x + 8, cur_y))
        return

    for item in items:
        if cur_y >= bottom:
            break
        text = item.get("text", "")
        status = item.get("status", "pending")

        if status == "done":
            color = C_DONE
            strike = True
        elif status in ("failed", "cancelled"):
            color = C_FAILED
            strike = True
        elif status == "overdue":
            color = C_OVERDUE
            strike = False
        elif status == "running":
            color = C_RUNNING
            strike = False
        else:
            color = C_NORMAL
            strike = False

        words = text.split()
        lines: list[str] = []
        cur_line = ""
        for word in words:
            test = (cur_line + " " + word).strip()
            if font_content.size(test)[0] <= max_w:
                cur_line = test
            else:
                if cur_line:
                    lines.append(cur_line)
                cur_line = word
        if cur_line:
            lines.append(cur_line)
        if not lines:
            lines = [text]

        for row in lines:
            if cur_y >= bottom:
                break
            surf = font_content.render(row, True, color)
            screen.blit(surf, (x + 8, cur_y))
            if strike:
                mid = cur_y + surf.get_height() // 2
                pygame.draw.line(screen, color, (x + 8, mid), (x + 8 + surf.get_width(), mid), 2)
            cur_y += line_h


# LXQt / xscreensaver-style autostart files known to interfere with the
# display: their respawning processes (lxqt-powermanagement, xscreensaver)
# carry their own DPMS schedules that override ours, causing periodic
# wake-blink during sleep_mode. We override these per-user (no sudo) by
# writing a Hidden=true entry into ~/.config/autostart/ which shadows
# the system-wide /etc/xdg/autostart/ one at next session start.
_AUTOSTART_OVERRIDES = (
    "lxqt-powermanagement.desktop",
    "lxqt-xscreensaver-autostart.desktop",
)


def _disable_interfering_autostart() -> None:
    """Write per-user Hidden=true overrides for known display-interfering
    autostart files. Idempotent and reversible (delete the override file)."""
    user_dir = Path.home() / ".config" / "autostart"
    try:
        user_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log.warning(f"Couldn't create {user_dir}: {e}")
        return
    body = "[Desktop Entry]\nType=Application\nHidden=true\n"
    for name in _AUTOSTART_OVERRIDES:
        target = user_dir / name
        try:
            existing = target.read_text() if target.exists() else ""
            if "Hidden=true" in existing:
                continue
            target.write_text(body)
            log.info(f"Autostart override written: {target}")
        except Exception as e:
            log.warning(f"Couldn't write autostart override {target}: {e}")


def _disable_screen_autosleep() -> None:
    """Stop everything that competes with us for DPMS / display power state.

    Two-layer defense:
      1. Kill currently-running offenders (xscreensaver,
         lxqt-powermanagement) so they don't keep re-applying their own
         settings during this session.
      2. Write user-space autostart overrides so they don't respawn
         after the next session start.

    Plus the usual `xset` setup as belt-and-suspenders.
    """
    for pkill_target in ("xscreensaver", "lxqt-powermanagement"):
        try:
            subprocess.run(
                ["pkill", "-9", pkill_target], timeout=2, capture_output=True,
            )
        except Exception as e:
            log.debug(f"pkill {pkill_target}: {e}")
    try:
        subprocess.run(
            ["xscreensaver-command", "-exit"], timeout=2, capture_output=True,
        )
    except FileNotFoundError:
        pass
    except Exception as e:
        log.debug(f"xscreensaver-command -exit: {e}")

    _disable_interfering_autostart()

    cmds = [
        ["xset", "s", "off"],
        ["xset", "s", "noblank"],
        ["xset", "+dpms"],
        ["xset", "dpms", "0", "0", "0"],
    ]
    for cmd in cmds:
        try:
            subprocess.run(cmd, timeout=2, capture_output=True)
        except Exception as e:
            log.warning(f"{' '.join(cmd)} failed: {e}")
    log.info(
        "Screen auto-sleep disabled (offenders killed, autostart overrides written, DPMS timers=0)"
    )


def _run_pygame() -> None:
    import pygame

    _disable_screen_autosleep()

    pygame.init()
    pygame.display.set_allow_screensaver(False)
    screen = pygame.display.set_mode((DISPLAY_W, DISPLAY_H), pygame.FULLSCREEN)
    pygame.display.set_caption("Rubedo")
    clock = pygame.time.Clock()
    font_sm = pygame.font.SysFont("monospace", 13)
    font_md = pygame.font.SysFont("monospace", 16)
    font_md_lg = pygame.font.SysFont("monospace", 32)
    font_lg = pygame.font.SysFont("monospace", 32, bold=True)
    font_content_lg = pygame.font.SysFont("monospace", 26)
    font_brain = pygame.font.SysFont("monospace", 78, bold=True)
    font_brain_sub = pygame.font.SysFont("monospace", 28)
    font_clock = pygame.font.SysFont("monospace", 192, bold=True)
    font_panel_title = pygame.font.SysFont("monospace", 38, bold=True)
    font_date = pygame.font.SysFont("monospace", 48)
    font_brand = pygame.font.SysFont("monospace", 17)

    FULL_SIZE = (DISPLAY_W, DISPLAY_H)
    PANEL_Y = DISPLAY_H // 2
    PANEL_H = DISPLAY_H - PANEL_Y
    PANEL_W = DISPLAY_W // 2

    def _load_bg(path: str):
        if not path:
            return None
        p = Path(path)
        if not p.exists():
            return None
        try:
            surf = pygame.image.load(str(p)).convert()
            return pygame.transform.scale(surf, FULL_SIZE)
        except Exception as _e:
            log.warning(f"Could not load background {path}: {_e}")
            return None

    _bg_path = _current_background_path()
    _bg_surf = _load_bg(_bg_path)
    _bg_top_overlay = pygame.Surface((DISPLAY_W, PANEL_Y), pygame.SRCALPHA)
    _bg_top_overlay.fill((15, 15, 25, 60))

    C_BG = (15, 15, 25)
    C_PANEL = (25, 25, 40)
    C_TEXT = (200, 200, 220)
    C_DIM = (255, 255, 255)
    C_ACCENT = (100, 160, 255)
    C_TITLE = (255, 255, 255)  # panel/header titles — kept independent of accent border
    C_ALARM = (255, 80, 80)
    C_EXPAND = (140, 200, 140)  # first-line hint color for expandable items

    sleep_img = _load_image_safe(pygame, _SLEEP_IMAGE, FULL_SIZE)
    alarm_img_1 = _load_image_safe(pygame, _ALARM_IMG_1, FULL_SIZE)
    alarm_img_2 = _load_image_safe(pygame, _ALARM_IMG_2, FULL_SIZE)

    state = {
        "brain": "idle",
        "brain_tool": "",
        "iter_count": 0,
        "error_active": False,
        "error_text": "",
        "error_ts": 0.0,
        "logs": [],
        "plan": [],
        "plan_last_refresh": 0.0,
        "queue": [],
        "queue_last_refresh": 0.0,
        "bg": list(C_BG),
        "bg_target": list(C_BG),
        "bg_path": _bg_path,
        "bg_last_check": 0.0,
        "sleep_mode": False,
        "alarm_mode": False,
        "alarm_tap_count": 0,
        "alarm_img_toggle": False,
        "alarm_last_toggle": 0.0,
        "alarm_last_check": 0.0,
        "last_click_time": 0.0,
        "sleep_last_off": 0.0,
        "expanded": {},  # (panel_title, item_idx) → bool
        # State-driven palette + sweep transition. Background and panel
        # rendering stay neutral; the overlay below applies the state color
        # as a translucent layer.
        "palette_name": "idle",
        "transition_active": False,
        "transition_old_name": "idle",
        "transition_start": 0.0,
    }

    _load_plan(state)
    _load_queue(state)
    state["logs"] = _restore_logs()
    threading.Thread(target=_start_bus_listener, args=(state,), daemon=True).start()

    # Populated each frame by draw(); used for tap-to-expand hit testing
    _hit_areas: list = []

    def _lerp_bg():
        for i in range(3):
            state["bg"][i] += (state["bg_target"][i] - state["bg"][i]) * 0.05

    def _detect_primary_output() -> str:
        """Cache the first connected xrandr output (e.g. 'HDMI-1', 'eDP-1').
        Used to drive xrandr-based sleep that bypasses DPMS entirely."""
        if state.get("xrandr_output"):
            return state["xrandr_output"]
        try:
            r = subprocess.run(
                ["xrandr"], capture_output=True, timeout=2, text=True,
            )
            for line in r.stdout.splitlines():
                if " connected" in line:
                    name = line.split()[0]
                    state["xrandr_output"] = name
                    log.info(f"Primary display output: {name}")
                    return name
        except Exception as e:
            log.warning(f"xrandr detect failed: {e}")
        state["xrandr_output"] = ""
        return ""

    def _capture_output_mode(out: str) -> tuple[str, str] | None:
        """Return (mode, pos) e.g. ('800x1280', '0x0') for the named output
        from the current `xrandr` state. Used before --off so we can
        explicitly restore the same mode on --on (xrandr --auto doesn't
        reliably bring an --off output back on every driver/X version)."""
        try:
            r = subprocess.run(
                ["xrandr"], capture_output=True, timeout=2, text=True,
            )
        except Exception as e:
            log.warning(f"xrandr capture failed: {e}")
            return None
        for line in r.stdout.splitlines():
            if not line.startswith(out + " "):
                continue
            for part in line.split():
                if "x" in part and "+" in part:
                    try:
                        res, x, y = part.split("+")
                        return (res, f"{x}x{y}")
                    except ValueError:
                        continue
        return None

    def _screen_off():
        """Hard-disable the display output via xrandr — bypasses DPMS so
        nothing (lxqt-powermanagement, xscreensaver, i915 PSR, etc.) can
        wake it. Falls back to DPMS if xrandr isn't available."""
        out = _detect_primary_output()
        if out:
            mode_info = _capture_output_mode(out)
            if mode_info:
                state["xrandr_saved_mode"] = mode_info
                log.info(f"Saved xrandr mode for {out}: {mode_info}")
            try:
                subprocess.run(
                    ["xrandr", "--output", out, "--off"],
                    timeout=3, capture_output=True,
                )
                return
            except Exception as e:
                log.warning(f"xrandr off failed: {e}")
        try:
            subprocess.run(
                ["xset", "dpms", "force", "off"], timeout=2, capture_output=True,
            )
        except Exception:
            pass

    def _screen_on():
        """Re-enable the display output. Prefers restoring the captured
        mode/position from the matching `_screen_off()` call."""
        try:
            subprocess.run(
                ["xset", "dpms", "force", "on"], timeout=2, capture_output=True,
            )
        except Exception:
            pass

        out = _detect_primary_output()
        if not out:
            return

        saved = state.get("xrandr_saved_mode")
        if saved:
            mode, pos = saved
            try:
                subprocess.run(
                    ["xrandr", "--output", out, "--mode", mode, "--pos", pos],
                    timeout=3, capture_output=True,
                )
                log.info(f"Restored xrandr mode {mode} at {pos} on {out}")
                return
            except Exception as e:
                log.warning(f"xrandr mode restore failed: {e}")

        for args in (["--auto"], ["--preferred"]):
            try:
                subprocess.run(
                    ["xrandr", "--output", out, *args],
                    timeout=3, capture_output=True,
                )
                log.info(f"xrandr {' '.join(args)} applied to {out}")
                return
            except Exception as e:
                log.warning(f"xrandr {' '.join(args)} failed: {e}")

    def draw():
        nonlocal _hit_areas, _bg_surf
        _hit_areas = []
        now_ts = time.time()

        # Poll files/DB every 1 second
        if now_ts - state["alarm_last_check"] >= 1.0:
            state["alarm_last_check"] = now_ts

            # Day plan auto-refresh every 5 minutes
            if now_ts - state["plan_last_refresh"] >= 300:
                _load_plan(state)
                state["plan_last_refresh"] = now_ts

            # Queue auto-refresh every 60 seconds
            if now_ts - state["queue_last_refresh"] >= 60:
                _load_queue(state)
                state["queue_last_refresh"] = now_ts

            # Session-state priority poll (§19) — error (bus-driven,
            # transient) > waiting_user > working > idle. Auto-expires
            # after _ERROR_FLASH_MAX_SEC even if no next turn ever
            # starts to clear it.
            if state.get("error_active") and now_ts - state.get("error_ts", 0) > _ERROR_FLASH_MAX_SEC:
                state["error_active"] = False
            if state.get("error_active"):
                state["brain"] = f"error: {state.get('error_text', '')}"
                _request_palette(state, "error")
            else:
                _sess_state = _compute_session_state()
                if _sess_state == "waiting":
                    state["brain"] = "waiting"
                    _request_palette(state, "waiting")
                elif _sess_state == "thinking":
                    state["brain"] = "thinking"
                    _request_palette(state, "thinking")
                else:
                    state["brain"] = "idle"
                    state["brain_tool"] = ""
                    state["iter_count"] = 0
                    _request_palette(state, "idle")

            # Background hot-reload (§19, agent.tools.display.set_background)
            _new_bg_path = _current_background_path()
            if _new_bg_path != state.get("bg_path"):
                state["bg_path"] = _new_bg_path
                _bg_surf = _load_bg(_new_bg_path)
                log.info(f"Background reloaded: {_new_bg_path or '(none)'}")

            # Restart request — bus event OR flag file (fallback)
            restart_requested = state.pop("bus_signal_restart_display", False)
            if _RESTART_DISPLAY_FILE.exists():
                try:
                    _RESTART_DISPLAY_FILE.unlink()
                except Exception:
                    pass
                restart_requested = True
            if restart_requested:
                import sys
                log.info("Display restart requested")
                pygame.quit()
                sys.exit(42)

            # Pre-alarm wake — bus OR file
            pre_wake = state.pop("bus_signal_pre_alarm_wake", False)
            if _PRE_ALARM_WAKE_FILE.exists():
                try:
                    _PRE_ALARM_WAKE_FILE.unlink()
                except Exception:
                    pass
                pre_wake = True
            if pre_wake:
                state["sleep_mode"] = False
                _screen_on()
                log.info("Pre-alarm wake: display woken")

            # Remote /sleep_on, /sleep_off — bus OR file
            sleep_mode_req = state.pop("bus_signal_sleep_request", None)
            if _SLEEP_REQUEST_FILE.exists():
                try:
                    sleep_mode_req = _SLEEP_REQUEST_FILE.read_text().strip().lower()
                except Exception:
                    pass
                try:
                    _SLEEP_REQUEST_FILE.unlink()
                except Exception:
                    pass
            if sleep_mode_req == "on" and not state["sleep_mode"]:
                state["sleep_mode"] = True
                state["sleep_last_off"] = now_ts
                _screen_off()
                log.info("Sleep mode entered (remote /sleep_on)")
            elif sleep_mode_req == "off" and state["sleep_mode"]:
                state["sleep_mode"] = False
                _screen_on()
                log.info("Sleep mode exited (remote /sleep_off)")

            # Alarm activity — bus event flags merge with file polling.
            state.pop("bus_signal_alarm_active", False)
            state.pop("bus_signal_alarm_dismissed", False)
            if _ALARM_ACTIVE_FILE.exists() and not _ALARM_DISMISSED_FILE.exists():
                if not state["alarm_mode"]:
                    state["alarm_mode"] = True
                    state["alarm_tap_count"] = 0
                    state["alarm_img_toggle"] = False
                    state["alarm_last_toggle"] = now_ts
                    log.info("Alarm mode activated")
            elif state["alarm_mode"]:
                state["alarm_mode"] = False
                log.info("Alarm mode deactivated")

        # Sleep mode — only when not in alarm mode.
        if state["sleep_mode"] and not state["alarm_mode"]:
            return  # no flip() — don't signal display activity to X11

        # Alarm mode
        if state["alarm_mode"]:
            if now_ts - state["alarm_last_toggle"] >= 1.0:
                state["alarm_img_toggle"] = not state["alarm_img_toggle"]
                state["alarm_last_toggle"] = now_ts
            img = alarm_img_2 if state["alarm_img_toggle"] else alarm_img_1
            if img:
                screen.blit(img, (0, 0))
            else:
                screen.fill((80, 0, 0))
            remaining = _ALARM_TAPS_REQUIRED - state["alarm_tap_count"]
            tap_lbl = font_lg.render(f"Нажми {remaining}x чтобы отключить", True, C_ALARM)
            screen.blit(tap_lbl, (DISPLAY_W // 2 - tap_lbl.get_width() // 2, DISPLAY_H - 80))
            pygame.display.flip()
            return

        # Normal mode
        _has_bg = _bg_surf is not None
        _c_panel = None if _has_bg else C_PANEL
        if _has_bg:
            screen.blit(_bg_surf, (0, 0))
            screen.blit(_bg_top_overlay, (0, 0))
        else:
            _lerp_bg()
            bg = tuple(int(v) for v in state["bg"])
            screen.fill(bg)
            pygame.draw.rect(screen, C_BG, (0, 0, DISPLAY_W, PANEL_Y))

        _DAYS_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
        now_dt = datetime.now()
        lbl_brand = font_brand.render("RUBEDO", True, C_DIM)
        screen.blit(lbl_brand, (10, 10))
        time_surf = font_clock.render(now_dt.strftime("%H:%M"), True, C_DIM)
        screen.blit(time_surf, (DISPLAY_W // 2 - time_surf.get_width() // 2, PANEL_Y // 2 - time_surf.get_height() // 2 - 20))
        date_str = f"{now_dt.strftime('%d.%m.%Y')}  {_DAYS_RU[now_dt.weekday()]}"
        date_surf = font_date.render(date_str, True, C_DIM)
        screen.blit(date_surf, (DISPLAY_W // 2 - date_surf.get_width() // 2, PANEL_Y // 2 + time_surf.get_height() // 2 - 10))

        # Brain panel — custom render: big state + optional smaller tool name
        _bx, _by, _bw, _bh = 0, PANEL_Y, PANEL_W, PANEL_H // 2
        if _c_panel is not None:
            pygame.draw.rect(screen, _c_panel, pygame.Rect(_bx, _by, _bw, _bh), border_radius=4)
            pygame.draw.rect(screen, C_ACCENT, pygame.Rect(_bx, _by, _bw, _bh), 1, border_radius=4)
        screen.blit(font_panel_title.render("Brain", True, C_TITLE), (_bx + 8, _by + 6))
        _b_top = _by + font_panel_title.get_linesize() + 10
        _b_bot = _by + _bh - 4
        _main_surf = font_brain.render(state["brain"], True, C_TEXT)
        if state["brain"] == "thinking":
            _tool = state["brain_tool"]
            _it = state.get("iter_count", 0)
            if _tool and _it:
                _sub = f"{_tool} ·{_it}"
            elif _tool:
                _sub = _tool
            elif _it:
                _sub = f"·{_it}"
            else:
                _sub = ""
        else:
            _sub = ""
        if _sub:
            _sub_surf = font_brain_sub.render(_sub, True, (130, 130, 130))
            _total_h = _main_surf.get_height() + 8 + _sub_surf.get_height()
            _sy = _b_top + max(0, (_b_bot - _b_top - _total_h) // 2)
            screen.blit(_main_surf, (_bx + _bw // 2 - _main_surf.get_width() // 2, _sy))
            screen.blit(_sub_surf, (_bx + _bw // 2 - _sub_surf.get_width() // 2, _sy + _main_surf.get_height() + 8))
        else:
            _sy = _b_top + max(0, (_b_bot - _b_top - _main_surf.get_height()) // 2)
            screen.blit(_main_surf, (_bx + _bw // 2 - _main_surf.get_width() // 2, _sy))
        _hit_areas += _panel(
            screen, font_sm, font_md_lg, _c_panel, C_ACCENT, C_TEXT, C_EXPAND,
            PANEL_W, PANEL_Y, PANEL_W, PANEL_H // 2,
            "Logs", state["logs"][-18:], state["expanded"],
            line_color_fn=lambda s: C_ALARM if "[err]" in s else None,
            title_color=C_TITLE, title_font=font_panel_title,
        )
        if state.get("is_dayoff"):
            # DAY OFF override — big centered label instead of the task list.
            _dpx, _dpy = 0, PANEL_Y + PANEL_H // 2
            _dpw, _dph = PANEL_W, PANEL_H // 2
            if _c_panel is not None:
                pygame.draw.rect(screen, _c_panel, pygame.Rect(_dpx, _dpy, _dpw, _dph), border_radius=4)
                pygame.draw.rect(screen, C_ACCENT, pygame.Rect(_dpx, _dpy, _dpw, _dph), 1, border_radius=4)
            screen.blit(font_panel_title.render("Day Plan", True, C_TITLE), (_dpx + 8, _dpy + 6))
            _dt = font_brain.render("DAY OFF", True, C_TITLE)
            _dty = _dpy + (_dph - _dt.get_height()) // 2 + 10
            screen.blit(_dt, (_dpx + _dpw // 2 - _dt.get_width() // 2, _dty))
        else:
            _draw_plan_panel(
                screen, font_content_lg, font_panel_title, _c_panel, C_ACCENT, C_TITLE,
                0, PANEL_Y + PANEL_H // 2, PANEL_W, PANEL_H // 2,
                state["plan"],
            )
        _draw_queue_panel(
            screen, font_content_lg, font_panel_title, _c_panel, C_ACCENT, C_TITLE,
            PANEL_W, PANEL_Y + PANEL_H // 2, PANEL_W, PANEL_H // 2,
            state["queue"],
        )

        if not _has_bg:
            _draw_palette_overlay(pygame, screen, state, now_ts, DISPLAY_W, DISPLAY_H)

        pygame.display.flip()

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False

            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                now_ts = time.time()

                if state["alarm_mode"]:
                    state["alarm_tap_count"] += 1
                    log.info(f"Alarm tap {state['alarm_tap_count']}/{_ALARM_TAPS_REQUIRED}")
                    if state["alarm_tap_count"] >= _ALARM_TAPS_REQUIRED:
                        try:
                            from bus.client import sync_publisher
                            from bus.events import AlarmDismissed
                            sync_publisher.publish(AlarmDismissed())
                        except Exception as e:
                            log.debug(f"AlarmDismissed publish skipped: {e}")
                        try:
                            _ALARM_DISMISSED_FILE.parent.mkdir(parents=True, exist_ok=True)
                            _ALARM_DISMISSED_FILE.write_text("1")
                        except Exception as e:
                            log.error(f"Could not write alarm dismissed: {e}")
                        state["alarm_mode"] = False
                        state["alarm_tap_count"] = 0
                        log.info("Alarm dismissed by user")
                    continue

                # Check expand/collapse hit areas (single tap)
                pos = event.pos
                hit_any = False
                for (rect, panel_title, item_idx) in _hit_areas:
                    if rect.collidepoint(pos):
                        key = (panel_title, item_idx)
                        state["expanded"][key] = not state["expanded"].get(key, False)
                        hit_any = True
                        break

                if hit_any:
                    continue

                # Double-click to toggle sleep (only outside alarm mode and not on expand area)
                delta_ms = (now_ts - state["last_click_time"]) * 1000
                state["last_click_time"] = now_ts
                if delta_ms <= _DOUBLE_CLICK_MS:
                    if state["sleep_mode"]:
                        state["sleep_mode"] = False
                        _screen_on()
                        log.info("Sleep mode exited")
                    else:
                        state["sleep_mode"] = True
                        _screen_off()
                        log.info("Sleep mode entered")

        draw()
        clock.tick(30)

    pygame.quit()


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
    run()
