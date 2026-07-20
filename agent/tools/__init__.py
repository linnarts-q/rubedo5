from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

# Sub-modules (extracted from this file as the package was created).
# Re-exported below so TOOLS_MAP / TOOLS_SCHEMA at the bottom of this
# file can keep referring to the names unqualified, and so external
# callers (`from agent.tools import web_search`) keep working.
from agent.tools.web import (  # noqa: E402  (re-export)
    navigate, calculate, get_weather, _duckduckgo_search, web_search,
)
from agent.tools.memory import (  # noqa: E402  (re-export)
    think, remember, memory_search, add_note, delete_note, list_notes,
    edit_memory, delete_memory, save_fact, search_history,
    profile_view, profile_set_field, profile_delete_field,
)
from agent.tools.tasks import (  # noqa: E402  (re-export)
    add_task, list_tasks, get_task_details, _task_title,
    mark_task_done, mark_task_failed, remove_task, reschedule_task,
)
from agent.tools.shell import (  # noqa: E402  (re-export)
    _SHELL_BLACKLIST, _SUDO_BLACKLIST, _check_blacklist,
    shell_exec, run_sudo, run_code,
    list_processes, kill_process, launch_app,
)

log = logging.getLogger("rubedo.tools")

WORKSPACE = Path("workspace")

# Security blacklists + _check_blacklist now live in agent/tools/shell.py
# and are imported below in the submodule block.


# Project root — used by tools that need to reach .env or data/. After
# the move from agent/tools.py to agent/tools/__init__.py one extra
# `.parent` is needed (this module sits two levels deep, not one).
_PROJECT_ROOT = Path(__file__).parent.parent.parent

_session_id: str = "lin"
_interlocutor: str = "хозяин"
_send_file_fn = None
_send_photo_fn = None
_browser_proc: subprocess.Popen | None = None
_loop: asyncio.AbstractEventLoop | None = None


def set_context(
    session_id: str,
    interlocutor: str,
    send_file_fn=None,
    send_photo_fn=None,
) -> None:
    global _session_id, _interlocutor, _send_file_fn, _send_photo_fn, _loop
    _session_id = session_id
    _interlocutor = interlocutor
    _send_file_fn = send_file_fn
    _send_photo_fn = send_photo_fn
    try:
        _loop = asyncio.get_running_loop()
    except RuntimeError:
        pass
    WORKSPACE.mkdir(exist_ok=True)


def set_env_var(key: str, value: str) -> str:
    env_path = _PROJECT_ROOT / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
        found = False
        new_lines = []
        for line in lines:
            if line.strip().startswith(f"{key}=") or line.strip().startswith(f"{key} ="):
                new_lines.append(f"{key}={value}")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"{key}={value}")
        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        return f"{key} обновлён в .env. Перезапусти Рубедо чтобы изменение вступило в силу."
    except Exception as e:
        return f"Ошибка при записи .env: {e}"


def add_week_event(title: str, event_date: str, event_time: str = "",
                   description: str = "") -> str:
    import memory.db as db
    eid = db.add_week_event(title, event_date, event_time, description)
    return f"Событие добавлено (id={eid}): {title} — {event_date}" + (f" {event_time}" if event_time else "")


def list_week_events() -> str:
    import memory.db as db
    events = db.list_week_events(weeks_ahead=2)
    if not events:
        return "Событий на ближайшие 2 недели нет."
    return "\n".join(
        f"[{e['id']}] {e['title']} — {e['event_date']}" + (f" {e['event_time']}" if e.get("event_time") else "")
        for e in events
    )


def delete_week_event(event_id: int) -> str:
    import memory.db as db
    ok = db.delete_week_event(event_id)
    return f"Событие #{event_id} удалено." if ok else f"Событие #{event_id} не найдено."


def add_recurring_task(title: str, days: list, time: str = "",
                       description: str = "", duration: int = 60,
                       task_type: str = "soft") -> str:
    import day.state as ds
    rid = ds.add_recurring(
        title=title,
        days=days if isinstance(days, list) else [str(days)],
        time=time or None,
        description=description,
        duration=duration,
        task_type=task_type,
    )
    # Materialize for today if the day pattern matches — otherwise the task
    # only appears starting from the next applicable morning briefing.
    try:
        ds.hydrate_recurring()
    except Exception:
        pass
    days_str = ", ".join(days) if isinstance(days, list) else str(days)
    when = f" в {time}" if time else ""
    return f"Повторяющаяся задача добавлена (id={rid}): {title} ({days_str}){when}"


def list_recurring_tasks() -> str:
    import day.state as ds
    import json as _json
    items = ds.get_active_recurring()
    if not items:
        return "Повторяющихся задач нет."
    lines = []
    for r in items:
        try:
            days = ", ".join(_json.loads(r.get("days") or '["daily"]'))
        except Exception:
            days = "daily"
        when = f" в {r['time']}" if r.get("time") else ""
        lines.append(f"[{r['id']}] {r['title']} ({days}){when}")
    return "\n".join(lines)


def delete_recurring_task(recurring_id: int) -> str:
    import day.state as ds
    ok = ds.delete_recurring(recurring_id)
    if not ok:
        return f"Повторяющаяся задача #{recurring_id} не найдена."
    return (
        f"Повторяющаяся задача #{recurring_id} отключена. "
        "Сегодняшний экземпляр (если уже создан) остался — убери его отдельно через task_remove если нужно."
    )


def skip_alarm() -> str:
    skip_file = _PROJECT_ROOT / "data" / ".alarm_skip"
    skip_file.parent.mkdir(parents=True, exist_ok=True)
    skip_file.write_text("1")
    return "Будильник на следующий брифинг отключён. Брифинг придёт как обычно, без звука и дисплея."


def cancel_alarm() -> str:
    dismissed = _PROJECT_ROOT / "data" / ".alarm_dismissed"
    dismissed.parent.mkdir(parents=True, exist_ok=True)
    dismissed.write_text("1")
    try:
        from bus.client import sync_publisher
        from bus.events import AlarmDismissed
        sync_publisher.publish(AlarmDismissed())
    except Exception as e:
        log.debug(f"AlarmDismissed publish skipped: {e}")
    return "Будильник остановлен."


async def open_url_screenshot(url: str) -> str:
    import os as _os
    import re as _re
    from datetime import datetime as _dt
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return "Playwright не установлен. Запусти: pip install playwright && playwright install chromium"
    _os.makedirs("data", exist_ok=True)
    # Meaningful filename: domain + timestamp instead of tmpXXXXX.png
    domain = _re.sub(r"^https?://", "", url).split("/", 1)[0]
    domain = _re.sub(r"[^a-zA-Z0-9._-]", "_", domain)[:40] or "page"
    ts = _dt.now().strftime("%Y%m%d_%H%M%S")
    tmp = f"data/screenshot_{domain}_{ts}.png"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.screenshot(path=tmp, full_page=False)
            await browser.close()
        if _send_photo_fn:
            await _send_photo_fn(tmp)
            return f"Скриншот {url} отправлен."
        return f"FILE:{tmp}"
    except Exception as e:
        return f"Ошибка скриншота: {e}"
    finally:
        try:
            _os.unlink(tmp)
        except Exception:
            pass


async def open_url_content(url: str, query: str = "") -> str:
    try:
        import aiohttp
        from html.parser import HTMLParser

        class _Strip(HTMLParser):
            def __init__(self):
                super().__init__()
                self._parts: list[str] = []
                self._skip = False
            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style", "nav", "footer"):
                    self._skip = True
            def handle_endtag(self, tag):
                if tag in ("script", "style", "nav", "footer"):
                    self._skip = False
            def handle_data(self, data):
                if not self._skip and data.strip():
                    self._parts.append(data.strip())
            def get_text(self):
                return " ".join(self._parts)

        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15),
                                   headers={"User-Agent": "Mozilla/5.0"}) as resp:
                html = await resp.text(errors="replace")

        parser = _Strip()
        parser.feed(html)
        text = parser.get_text()[:6000]

        if query:
            from llm.groq import chat as groq_chat
            resp = await groq_chat([
                {"role": "system", "content": "Extract the answer to the user's question from the provided text. Be concise."},
                {"role": "user", "content": f"Question: {query}\n\nText:\n{text}"},
            ], temperature=0.1)
            return resp.choices[0].message.content.strip()
        return text[:2000]
    except Exception as e:
        return f"Ошибка загрузки страницы: {e}"


def _resolve_path(filename: str) -> Path:
    if filename.startswith("/"):
        return Path(filename)
    resolved = (WORKSPACE / filename).resolve()
    workspace_resolved = WORKSPACE.resolve()
    if not str(resolved).startswith(str(workspace_resolved) + os.sep) and resolved != workspace_resolved:
        raise ValueError(f"Путь '{filename}' выходит за пределы рабочей папки")
    return resolved


def write_file(filename: str, content: str) -> str:
    try:
        path = _resolve_path(filename)
    except ValueError as e:
        return f"Ошибка: {e}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"Файл записан: {path}"


def read_file(filename: str) -> str:
    try:
        path = _resolve_path(filename)
    except ValueError as e:
        return f"Ошибка: {e}"
    if not path.exists():
        return f"Файл {path} не найден."
    try:
        return path.read_text(encoding="utf-8")
    except Exception as e:
        return f"Ошибка чтения: {e}"


def delete_file(filename: str) -> str:
    try:
        path = _resolve_path(filename)
    except ValueError as e:
        return f"Ошибка: {e}"
    if not path.exists():
        return f"Файл {path} не найден."
    path.unlink()
    return f"Файл {path} удалён."


def list_files(subdir: str = "") -> str:
    if subdir.startswith("/"):
        target = Path(subdir)
    else:
        target = WORKSPACE / subdir if subdir else WORKSPACE
    if not target.exists():
        return "Папка не найдена."
    files = [
        str(p) if subdir.startswith("/") else str(p.relative_to(WORKSPACE))
        for p in target.rglob("*")
        if p.is_file()
    ]
    if not files:
        return "Пусто."
    total = len(files)
    files = files[:100]
    result = "\n".join(files)
    if len(result) > 3000:
        result = result[:3000] + "\n…"
    if total > 100:
        result += f"\n[показано 100 из {total}]"
    return result


def set_volume_system(level: int) -> str:
    level = max(0, min(100, int(level)))
    out = shell_exec(f"pactl set-sink-volume @DEFAULT_SINK@ {level}%")
    if out != "(пусто)" and not out.startswith("[код"):
        return f"Громкость установлена на {level}%."
    out = shell_exec(f"amixer set Master {level}%")
    if not out.startswith("[код"):
        return f"Громкость установлена на {level}%."
    return f"Не удалось изменить громкость: {out}"


def get_system_info() -> str:
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.5)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        temp_str = ""
        try:
            temps = psutil.sensors_temperatures()
            if "coretemp" in temps:
                temp_str = f" | Темп: {temps['coretemp'][0].current:.0f}°C"
        except Exception:
            pass
        return (
            f"CPU: {cpu}% | RAM: {ram.percent}% "
            f"({ram.used // 1024 // 1024}/{ram.total // 1024 // 1024} MB) | "
            f"Диск: {disk.percent}%{temp_str}"
        )
    except ImportError:
        return shell_exec("top -bn1 | head -5")


def set_reminder(text: str, remind_at: str) -> str:
    import memory.db as db
    rid = db.save_reminder(_session_id, text, remind_at)
    return f"Напоминание установлено (id={rid}): {text} — {remind_at}"


def list_reminders() -> str:
    import memory.db as db
    items = db.list_reminders_for_session(_session_id)
    active = [r for r in items if not r["done"]]
    if not active:
        return "Нет активных напоминаний."
    return "\n".join(f"[{r['id']}] {r['text']} — {r['remind_at']}" for r in active)


def delete_reminder(reminder_id: int) -> str:
    import memory.db as db
    ok = db.delete_reminder(reminder_id)
    return f"Напоминание #{reminder_id} удалено." if ok else f"Напоминание #{reminder_id} не найдено."


def archive_files(filenames: list, archive_name: str = "archive.zip") -> str:
    import zipfile
    WORKSPACE.mkdir(exist_ok=True)
    archive_path = _resolve_path(archive_name)
    try:
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in filenames:
                path = _resolve_path(fname)
                if not path.exists():
                    return f"Файл не найден: {fname}"
                zf.write(path, path.name)
        size = archive_path.stat().st_size
        return f"Архив создан: {archive_path} ({size:,} байт, {len(filenames)} файлов)"
    except Exception as e:
        return f"Ошибка архивации: {e}"


def extract_archive(filename: str, destination: str = "") -> str:
    import zipfile
    import tarfile
    try:
        path = _resolve_path(filename)
    except ValueError as e:
        return f"Ошибка: {e}"
    if not path.exists():
        return f"Архив не найден: {filename}"
    dest = _resolve_path(destination) if destination else WORKSPACE
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()

    def _safe_zip_members(zf: zipfile.ZipFile) -> list:
        safe = []
        for name in zf.namelist():
            target = (dest / name).resolve()
            if str(target).startswith(str(dest_resolved)):
                safe.append(name)
            else:
                log.warning(f"Zip slip blocked: {name}")
        return safe

    def _safe_tar_members(tf: tarfile.TarFile):
        for member in tf.getmembers():
            target = (dest / member.name).resolve()
            if str(target).startswith(str(dest_resolved)):
                yield member
            else:
                log.warning(f"Tar path traversal blocked: {member.name}")

    try:
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as zf:
                members = _safe_zip_members(zf)
                zf.extractall(dest, members=members)
                count = len(members)
        elif ".tar" in path.name or path.suffix in (".gz", ".bz2", ".xz"):
            with tarfile.open(path) as tf:
                safe = list(_safe_tar_members(tf))
                tf.extractall(dest, members=safe)
                count = len(safe)
        else:
            return f"Неизвестный формат: {path.suffix}"
        return f"Распакован: {count} файлов в {dest}"
    except Exception as e:
        return f"Ошибка распаковки: {e}"


def convert_image(source: str, destination: str, width: int = 0, height: int = 0) -> str:
    try:
        from PIL import Image
    except ImportError:
        return "PIL не установлен: pip install Pillow"
    src = _resolve_path(source)
    if not src.exists():
        return f"Файл не найден: {source}"
    dst = _resolve_path(destination)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(src) as img:
            if width or height:
                ow, oh = img.size
                w = width or int(ow * height / oh)
                h = height or int(oh * width / ow)
                img = img.resize((w, h), Image.LANCZOS)
            img.save(dst)
            sz = dst.stat().st_size
        return f"Готово: {dst} ({img.size[0]}x{img.size[1]}, {sz:,} байт)"
    except Exception as e:
        return f"Ошибка конвертации: {e}"


def get_uptime() -> str:
    try:
        import psutil
        import time
        boot = psutil.boot_time()
        secs = int(time.time() - boot)
        d, rem = divmod(secs, 86400)
        h, rem = divmod(rem, 3600)
        m = rem // 60
        parts = []
        if d:
            parts.append(f"{d} д.")
        if h:
            parts.append(f"{h} ч.")
        parts.append(f"{m} мин.")
        return f"Система работает: {' '.join(parts)}"
    except ImportError:
        return shell_exec("uptime -p")


def move_file(source: str, destination: str) -> str:
    import shutil
    src = _resolve_path(source)
    dst = _resolve_path(destination)
    if not src.exists():
        return f"Файл не найден: {source}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"Перемещено: {src} → {dst}"


_MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024  # 500 MB hard cap


def download_file(url: str, filename: str = "") -> str:
    WORKSPACE.mkdir(exist_ok=True)
    if not filename:
        from urllib.parse import urlparse
        from pathlib import PurePosixPath
        filename = PurePosixPath(urlparse(url).path).name or "download"
    path = WORKSPACE / filename

    try:
        req_head = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req_head, timeout=10) as r:
            content_length = int(r.headers.get("Content-Length") or 0)
            if content_length > 1_073_741_824:
                gb = content_length / 1_073_741_824
                return f"Файл очень большой ({gb:.1f} ГБ). Уточни, если нужно скачать."
    except Exception:
        pass

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            downloaded = 0
            chunks: list[bytes] = []
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > _MAX_DOWNLOAD_BYTES:
                    return f"Файл превысил лимит ({_MAX_DOWNLOAD_BYTES // 1024 // 1024} МБ), загрузка прервана."
                chunks.append(chunk)
            path.write_bytes(b"".join(chunks))
        size = path.stat().st_size
        return f"Скачан: workspace/{filename} ({size:,} байт)"
    except Exception as e:
        return f"Ошибка загрузки: {e}"


async def research_task(title: str, description: str = "") -> str:
    """Search web for task-relevant info and save to memory."""
    from day.research import research_for_task
    return await research_for_task(title, description, session_id=_session_id)


def add_wish(content: str) -> str:
    import memory.db as db
    wid = db.save_wish(content)
    return f"Желание добавлено (id={wid})"


def list_wishes() -> str:
    import memory.db as db
    wishes = db.get_active_wishes()
    if not wishes:
        return "Список желаний пуст."
    return "\n".join(f"[{w['id']}] {w['content']}" for w in wishes)


def fulfill_wish(wish_id: int) -> str:
    import memory.db as db
    db.mark_wish_done(wish_id)
    return f"Желание #{wish_id} исполнено."


# ─ Pool tasks (untimed backlog with priority-based nudges) ──────────────

async def add_pool_task(title: str, description: str = "", priority: int | None = None) -> str:
    """Add an untimed backlog task. Priority 1-5 controls reminder cadence
    (1=monthly, 5=every weekday). If priority is omitted it is classified
    automatically via Groq."""
    from day import pool
    if priority is None or not isinstance(priority, int) or not (1 <= priority <= 5):
        priority = await pool.classify_priority(title, description)
        auto_note = f" (приоритет {priority} проставлен автоматически)"
    else:
        auto_note = ""
    tid = pool.add(title, description, priority)
    return f"Задача в бэклоге добавлена (id={tid}, приоритет {priority}/5){auto_note}."


def list_pool_tasks() -> str:
    """List active pool tasks sorted by priority (highest first)."""
    from day import pool
    tasks = pool.list_active()
    if not tasks:
        return "Бэклог пуст."
    lines = []
    for t in tasks:
        snooze_note = ""
        if t.get("snoozed_until"):
            snooze_note = f" [отложена до {t['snoozed_until'][:10]}]"
        last_note = ""
        if t.get("last_nudged_at"):
            last_note = f" · напомнено {t['last_nudged_at'][:10]}"
        lines.append(
            f"[{t['id']}] P{t['priority']}: {t['title']}{snooze_note}{last_note}"
        )
    return "\n".join(lines)


def mark_pool_done(task_id: int) -> str:
    """Mark a pool task complete. It will no longer trigger reminders."""
    from day import pool
    if pool.mark_done(int(task_id)):
        return f"Задача #{task_id} помечена выполненной."
    return f"Задача #{task_id} не найдена или уже выполнена."


def remove_pool_task(task_id: int) -> str:
    """Permanently delete a pool task."""
    from day import pool
    if pool.remove(int(task_id)):
        return f"Задача #{task_id} удалена из бэклога."
    return f"Задача #{task_id} не найдена."


def set_pool_priority(task_id: int, priority: int) -> str:
    """Change the reminder priority of a pool task (1-5)."""
    from day import pool
    p = max(1, min(5, int(priority)))
    if pool.set_priority(int(task_id), p):
        return f"Приоритет задачи #{task_id} изменён на {p}/5."
    return f"Задача #{task_id} не найдена."


def snooze_pool_task(task_id: int, days: int) -> str:
    """Postpone reminders for a pool task by N days."""
    from day import pool
    n = max(1, int(days))
    if pool.snooze(int(task_id), n):
        return f"Задача #{task_id} отложена на {n} дн."
    return f"Задача #{task_id} не найдена."


def export_memory_to_file(filename: str = "memory_export.txt") -> str:
    import memory.db as db
    WORKSPACE.mkdir(exist_ok=True)
    path = str(WORKSPACE / filename)
    db.export_memory(path)
    return f"Память экспортирована в workspace/{filename}"


def send_file_to_user(filename: str) -> str:
    try:
        path = _resolve_path(filename)
    except ValueError:
        path = Path(filename)
    if not path.exists() and not filename.startswith("/"):
        # shell commands create files in cwd, not workspace — try both
        cwd_path = Path(filename).resolve()
        if cwd_path.exists():
            path = cwd_path
    if not path.exists():
        return f"Файл не найден: {filename}"
    if _send_file_fn is None:
        return f"Файл сохранён: {path}. Отправка недоступна."
    if _loop is None:
        return f"Файл сохранён: {path}. Цикл событий недоступен."
    try:
        asyncio.run_coroutine_threadsafe(_send_file_fn(str(path)), _loop)
        return f"Файл {path.name} отправлен."
    except Exception as e:
        return f"Ошибка отправки файла: {e}"


def send_photo_to_user(filename: str) -> str:
    path = _resolve_path(filename)
    if not path.exists():
        return f"Файл не найден: {filename}"
    if _send_photo_fn is None:
        return f"Фото сохранено: {path}. Отправка недоступна."
    if _loop is None:
        return f"Фото сохранено: {path}. Цикл событий недоступен."
    try:
        asyncio.run_coroutine_threadsafe(_send_photo_fn(str(path)), _loop)
        return f"Фото {path.name} отправлено."
    except Exception as e:
        return f"Ошибка отправки фото: {e}"


def take_screenshot() -> str:
    WORKSPACE.mkdir(exist_ok=True)
    from datetime import datetime as _dt
    fname = f"screenshot_{_dt.now().strftime('%Y%m%d_%H%M%S')}.png"
    path = WORKSPACE / fname
    env = {**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")}
    try:
        r = subprocess.run(
            ["scrot", str(path)], capture_output=True, text=True, timeout=10, env=env
        )
        if path.exists():
            if _loop and _send_photo_fn:
                asyncio.run_coroutine_threadsafe(_send_photo_fn(str(path)), _loop)
                return "Скриншот сделан и отправлен."
            return f"Скриншот сохранён: {fname}"
        return f"Ошибка скриншота: {r.stderr or r.stdout}"
    except FileNotFoundError:
        return "scrot не установлен. Установи: sudo apt install scrot"
    except Exception as e:
        return f"Ошибка скриншота: {e}"


def spotrent_status() -> str:
    """SpotRent lives on the separate server (4.12), not the mini-PC —
    checked over SSH (agent/remote.py), not local subprocess."""
    from agent import remote
    out = remote.run("pgrep -f spotrent_launcher.py")
    if remote.is_error_output(out):
        return f"Не удалось проверить статус SpotRent: {out}"
    if out and out != "(пусто)":
        pids = out.replace("\n", ", ")
        return f"SpotRent запущен (PID: {pids})."
    return "SpotRent не запущен."


def spotrent_start() -> str:
    from agent import remote
    from config import SPOTRENT_PYTHON, SPOTRENT_LAUNCHER, SPOTRENT_CWD
    check = remote.run("pgrep -f spotrent_launcher.py")
    if remote.is_error_output(check):
        return f"Не удалось проверить статус SpotRent перед запуском: {check}"
    if check and check != "(пусто)":
        return "SpotRent уже запущен."
    cmd = f"cd {SPOTRENT_CWD} && nohup {SPOTRENT_PYTHON} {SPOTRENT_LAUNCHER} > /dev/null 2>&1 & disown"
    result = remote.run(cmd)
    if remote.is_error_output(result):
        return f"Ошибка запуска SpotRent: {result}"
    return "SpotRent запускается."


def spotrent_stop() -> str:
    from agent import remote
    check = remote.run("pgrep -f spotrent_launcher.py")
    if remote.is_error_output(check):
        return f"Не удалось проверить статус SpotRent: {check}"
    if not check or check == "(пусто)":
        return "SpotRent не запущен."
    result = remote.run("pkill -TERM -f spotrent_launcher.py")
    if remote.is_error_output(result):
        return f"Не удалось остановить SpotRent: {result}"
    return "SpotRent останавливается — лаунчер завершит дочерние процессы и выключится."


def server_shell(command: str) -> str:
    """Non-sudo shell on the server (yellow zone, §1) — general escape
    hatch for the ad hoc stuff that doesn't have its own tool (package
    installs, checking a log file, one-off diagnostics). Always goes
    through the approval gate like every other yellow/red tool; no
    denylist here since it never runs without the owner's yes first."""
    from agent import remote
    return remote.run(command)


# ─ Queue ─────────────────────────────────────────────────────────────────────

def queue_add_task(
    title: str,
    description: str = "",
    priority: int = 3,
    scheduled_at: str = "",
    depends_on: int = 0,
    max_retries: int = 2,
) -> str:
    from memory.db import queue_add
    task_id = queue_add(
        title=title,
        description=description,
        priority=priority,
        scheduled_at=scheduled_at or None,
        depends_on=depends_on or None,
        max_retries=max_retries,
    )
    when = f" (в {scheduled_at})" if scheduled_at else " (при простое)"
    return f"Задача добавлена в очередь{when}. ID: {task_id}"


def queue_list_tasks(status: str = "") -> str:
    from memory.db import queue_list
    tasks = queue_list(status or None)
    if not tasks:
        return "Очередь пуста."
    lines = []
    for t in tasks:
        sched = f" [в {t['scheduled_at']}]" if t.get("scheduled_at") else ""
        dep = f" [ждёт #{t['depends_on']}]" if t.get("depends_on") else ""
        retry = f" (попытка {t['retry_count']}/{t['max_retries']})" if t.get("retry_count") else ""
        lines.append(f"#{t['id']} [{t['status']}] P{t['priority']} — {t['title']}{sched}{dep}{retry}")
    return "\n".join(lines)


def queue_cancel_task(task_id: int) -> str:
    from memory.db import queue_cancel
    ok = queue_cancel(task_id)
    return f"Задача #{task_id} отменена." if ok else f"Задача #{task_id} не найдена или уже завершена."


def queue_pause_all() -> str:
    from memory.db import save_meta
    save_meta("queue_paused", "1")
    return "Очередь на паузе."


def queue_resume_all() -> str:
    from memory.db import save_meta
    save_meta("queue_paused", "0")
    return "Очередь возобновлена."


def restart_agent() -> str:
    """Restart the agent (telegram process). Launcher will restart it automatically."""
    from config import RESTART_CODE
    import threading
    threading.Timer(1.0, lambda: os._exit(RESTART_CODE)).start()
    return "Перезапускаюсь..."


def restart_display() -> str:
    """Restart the display process."""
    flag = Path("data/.restart_display")
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()
    try:
        from bus.client import sync_publisher
        from bus.events import DisplayRestartRequested
        sync_publisher.publish(DisplayRestartRequested())
    except Exception as e:
        log.debug(f"DisplayRestartRequested publish skipped: {e}")
    return "Дисплей перезапускается."


def self_update() -> str:
    """Update the agent from git (pull latest code and restart)."""
    from config import UPDATE_CODE
    import threading
    threading.Timer(1.0, lambda: os._exit(UPDATE_CODE)).start()
    return "Обновляюсь из репозитория, скоро вернусь."


def system_update() -> str:
    """Update system packages on the mini-PC via apt (sudo apt update &&
    apt upgrade). Red zone (agent/zones.py) — password comes from the
    encrypted 'local' credential (techspec §1.6, agent/credentials.py).
    """
    from agent.credentials import get_password
    password = get_password("local")
    if password is None:
        return (
            "Пароль sudo для 'local' не настроен (или CREDENTIALS_KEY не задан). "
            "Задай его на мини-ПК: python scripts/set_credential.py local"
        )
    try:
        r = subprocess.run(
            ["sudo", "-S", "apt", "update"],
            input=password + "\n", capture_output=True, text=True, timeout=60,
        )
        r2 = subprocess.run(
            ["sudo", "-S", "apt", "upgrade", "-y"],
            input=password + "\n", capture_output=True, text=True, timeout=300,
        )
        out = (r2.stdout or r2.stderr or "").strip()
        return f"Системные пакеты обновлены.\n{out[-300:]}" if out else "Системные пакеты обновлены."
    except Exception as e:
        return f"Ошибка обновления системы: {e}"


def launch_browser(url: str = "") -> str:
    global _browser_proc
    for cmd in ("chromium-browser", "chromium", "google-chrome"):
        result = shell_exec(f"which {cmd}")
        if result.startswith("/"):
            args = [cmd]
            if url:
                args.append(url)
            try:
                _browser_proc = subprocess.Popen(
                    args,
                    env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")},
                    start_new_session=True,
                )
                return f"Браузер запущен{f': {url}' if url else ''}."
            except Exception as e:
                return f"Ошибка запуска браузера: {e}"
    return "Chromium не найден в системе."


def close_browser() -> str:
    try:
        r = subprocess.run(["pkill", "-f", "chromium"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return "Браузер закрыт."
        return "Браузер не запущен."
    except Exception as e:
        return f"Ошибка закрытия браузера: {e}"


def iterations_recent(count: int = 5) -> str:
    """Return a compact summary of the N most recent agent-run audit logs,
    newest first. Reads JSON files from `data/agent_logs/{normal,errors}/`,
    extracts the key events from each (user message, route, tool calls,
    final reply, errors), and joins them as one text block.
    """
    from agent.audit import NORMAL_DIR, ERRORS_DIR

    try:
        count = max(1, min(20, int(count)))
    except (TypeError, ValueError):
        count = 5

    files: list[Path] = []
    for d in (NORMAL_DIR, ERRORS_DIR):
        if d.exists():
            try:
                files.extend(p for p in d.iterdir() if p.is_file() and p.suffix == ".json")
            except Exception:
                continue
    if not files:
        return "Аудит-логов ещё нет."

    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    files = files[:count]

    blocks: list[str] = []
    for idx, path in enumerate(files, start=1):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            blocks.append(f"[{idx}] {path.name}: чтение упало — {e}")
            continue

        started = data.get("started_at", "")[:19].replace("T", " ")
        is_error = data.get("is_error")
        err_reason = data.get("error_reason") or ""
        events = data.get("events") or []

        user_msg = ""
        route_str = ""
        tools_used: list[str] = []
        final_reply = ""
        exc_msg = ""
        for ev in events:
            t = ev.get("type")
            if t == "user_message":
                user_msg = (ev.get("text") or "")[:200]
            elif t == "route":
                route_str = f"{ev.get('route', '?')}/{ev.get('context_type', '?')}"
            elif t == "tool_called":
                tools_used.append(ev.get("name", "?"))
            elif t == "tool_finished":
                # Mark failures inline
                if ev.get("success") is False:
                    tools_used[-1] = f"{tools_used[-1]}❌" if tools_used else "?❌"
            elif t == "final_reply":
                final_reply = (ev.get("text") or "")[:200]
            elif t == "exception":
                exc_msg = (ev.get("error") or "")[:200]
            elif t == "loop_detected":
                exc_msg = f"loop: {ev.get('name', '?')}"

        head = f"[{idx}] {started} ({route_str}, {len(tools_used)} tools)"
        if is_error:
            head += f" ⚠ {err_reason or 'error'}"
        block = [head]
        if user_msg:
            block.append(f"  → user: {user_msg}")
        if tools_used:
            block.append(f"  → tools: {', '.join(tools_used)}")
        if final_reply:
            block.append(f"  ← reply: {final_reply}")
        if exc_msg:
            block.append(f"  ⚠ {exc_msg}")
        blocks.append("\n".join(block))

    return "\n\n".join(blocks)


def archive_agent_logs() -> str:
    """Archive all normal agent-run logs to a zip, send it, and clear the folder.

    Resets the milestone counter so notifications can fire again after the next
    accumulation. Trigger when the user asks to archive/clean agent logs.
    """
    from agent.audit import archive_and_clear_normal_logs
    zip_path, count = archive_and_clear_normal_logs()
    if zip_path is None:
        return "Нечего архивировать — папка пуста или произошла ошибка."
    if _send_file_fn is not None and _loop is not None:
        try:
            asyncio.run_coroutine_threadsafe(_send_file_fn(str(zip_path)), _loop)
        except Exception as e:
            log.warning(f"archive_agent_logs: send failed: {e}")
    return f"Архивировано {count} логов → {zip_path.name}. Папка normal/ очищена, счётчик сброшен."


def rollback_last_change() -> str:
    """Undo the most recent yellow-zone file write (§15). Green zone —
    rollback itself never needs approval, or "can I undo my mistake?"
    would be as annoying to confirm as the mistake itself."""
    from agent.undo import rollback_last
    return rollback_last()


TOOLS_MAP: dict[str, callable] = {
    "think": think,
    # memory.*
    "memory_save": remember,
    "memory_search": memory_search,
    "memory_edit": edit_memory,
    "memory_delete": delete_memory,
    "memory_fact_save": save_fact,
    "memory_history_search": search_history,
    "memory_export": export_memory_to_file,
    # profile.*
    "profile_view": profile_view,
    "profile_set": profile_set_field,
    "profile_delete": profile_delete_field,
    # note.*
    "note_add": add_note,
    "note_list": list_notes,
    "note_delete": delete_note,
    # task.*
    "task_add": add_task,
    "task_list": list_tasks,
    "task_details": get_task_details,
    "task_done": mark_task_done,
    "task_failed": mark_task_failed,
    "task_remove": remove_task,
    "task_reschedule": reschedule_task,
    # reminder.*
    "reminder_add": set_reminder,
    "reminder_list": list_reminders,
    "reminder_delete": delete_reminder,
    # event.*
    "event_add": add_week_event,
    "event_list": list_week_events,
    "event_delete": delete_week_event,
    # recurring.*  (weekly/daily repeating tasks — auto-materialised at briefing)
    "recurring_add": add_recurring_task,
    "recurring_list": list_recurring_tasks,
    "recurring_delete": delete_recurring_task,
    # alarm.*
    "alarm_skip": skip_alarm,
    "alarm_cancel": cancel_alarm,
    # wish.*
    "wish_add": add_wish,
    "wish_list": list_wishes,
    "wish_done": fulfill_wish,
    # file.*
    "file_read": read_file,
    "file_write": write_file,
    "file_delete": delete_file,
    "file_list": list_files,
    "file_move": move_file,
    "file_download": download_file,
    "file_archive": archive_files,
    "file_extract": extract_archive,
    "file_convert_image": convert_image,
    # web.*
    "web_search": web_search,
    "web_screenshot": open_url_screenshot,
    "web_content": open_url_content,
    # system.*
    "system_info": get_system_info,
    "system_uptime": get_uptime,
    "system_volume": set_volume_system,
    "system_env_set": set_env_var,
    "system_shell": shell_exec,
    "system_sudo": run_sudo,
    "system_code": run_code,
    "server_shell": server_shell,
    # process.*
    "process_list": list_processes,
    "process_kill": kill_process,
    "process_launch_app": launch_app,
    "process_launch_browser": launch_browser,
    "process_close_browser": close_browser,
    # diagnostics / agent logs
    "iterations_recent": iterations_recent,
    "logs_archive": archive_agent_logs,
    # undo (§15)
    "rollback_last": rollback_last_change,
    # send.*
    "send_file": send_file_to_user,
    "send_photo": send_photo_to_user,
    # spotrent.*
    "spotrent_status": spotrent_status,
    "spotrent_start": spotrent_start,
    "spotrent_stop": spotrent_stop,
    # queue.*
    "queue_add": queue_add_task,
    "queue_list": queue_list_tasks,
    "queue_cancel": queue_cancel_task,
    "queue_pause": queue_pause_all,
    "queue_resume": queue_resume_all,
    # agent / display / os
    "agent_update": self_update,
    "agent_restart": restart_agent,
    "display_restart": restart_display,
    "os_update": system_update,
    # singletons (no namespace)
    "navigate": navigate,
    "calculate": calculate,
    "weather": get_weather,
    "screenshot": take_screenshot,
    "research": research_task,
    # pool.*
    "pool_add": add_pool_task,
    "pool_list": list_pool_tasks,
    "pool_done": mark_pool_done,
    "pool_remove": remove_pool_task,
    "pool_priority": set_pool_priority,
    "pool_snooze": snooze_pool_task,
}

TOOLS_SCHEMA: list[dict] = [
    {"type": "function", "function": {
        "name": "think",
        "description": "Внутренний монолог — обдумать перед действием",
        "parameters": {"type": "object", "properties": {"thought": {"type": "string"}}, "required": ["thought"]}}},
    {"type": "function", "function": {
        "name": "memory_save",
        "description": "Сохранить событие или важную информацию в долгосрочную память",
        "parameters": {"type": "object", "properties": {"content": {"type": "string"}, "priority": {"type": "integer", "default": 3}, "category": {"type": "string", "default": "general"}}, "required": ["content"]}}},
    {"type": "function", "function": {
        "name": "memory_search",
        "description": "Поиск в долгосрочной памяти",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "note_add",
        "description": "Сохранить внутреннюю заметку (макс 300 символов)",
        "parameters": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}}},
    {"type": "function", "function": {
        "name": "note_delete",
        "description": "Удалить внутреннюю заметку по ID",
        "parameters": {"type": "object", "properties": {"note_id": {"type": "integer"}}, "required": ["note_id"]}}},
    {"type": "function", "function": {
        "name": "memory_edit",
        "description": "Отредактировать запись в долгосрочной памяти по ID",
        "parameters": {"type": "object", "properties": {"event_id": {"type": "integer"}, "new_content": {"type": "string"}}, "required": ["event_id", "new_content"]}}},
    {"type": "function", "function": {
        "name": "memory_delete",
        "description": "Удалить запись из долгосрочной памяти по ID",
        "parameters": {"type": "object", "properties": {"event_id": {"type": "integer"}}, "required": ["event_id"]}}},
    {"type": "function", "function": {
        "name": "memory_fact_save",
        "description": "Сохранить факт о пользователе",
        "parameters": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}}},
    {"type": "function", "function": {
        "name": "file_write",
        "description": "Записать файл. Абсолютный путь или имя файла в рабочей папке",
        "parameters": {"type": "object", "properties": {"filename": {"type": "string"}, "content": {"type": "string"}}, "required": ["filename", "content"]}}},
    {"type": "function", "function": {
        "name": "file_read",
        "description": "Прочитать файл. Абсолютный путь или имя файла в рабочей папке",
        "parameters": {"type": "object", "properties": {"filename": {"type": "string"}}, "required": ["filename"]}}},
    {"type": "function", "function": {
        "name": "file_delete",
        "description": "Удалить файл",
        "parameters": {"type": "object", "properties": {"filename": {"type": "string"}}, "required": ["filename"]}}},
    {"type": "function", "function": {
        "name": "file_list",
        "description": "Список файлов. Абсолютный путь или подпапка рабочей директории",
        "parameters": {"type": "object", "properties": {"subdir": {"type": "string", "default": ""}}, "required": []}}},
    {"type": "function", "function": {
        "name": "system_shell",
        "description": "Выполнить команду в терминале",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer", "default": 30}}, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "system_sudo",
        "description": "Выполнить команду с sudo. host — 'local' (эта машина, по умолчанию) или 'server' (отдельный сервер).",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"},
            "host": {"type": "string", "enum": ["local", "server"], "default": "local"},
        }, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "system_code",
        "description": "Выполнить Python-код",
        "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}}},
    {"type": "function", "function": {
        "name": "rollback_last",
        "description": (
            "Откатить последнюю запись/удаление файла за пределами workspace "
            "(из тех, что требовали подтверждения). Возвращает старое содержимое "
            "или удаляет то, что было создано, если файла раньше не было."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "server_shell",
        "description": (
            "Выполнить shell-команду на отдельном сервере (не мини-ПК) без sudo — "
            "например проверить лог, установить пакет, посмотреть статус. "
            "Требует подтверждения (жёлтая зона). Для SpotRent используй "
            "spotrent_status/start/stop, не этот инструмент."
        ),
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "system_info",
        "description": "Состояние системы: CPU, RAM, диск, температура",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "process_list",
        "description": "Список запущенных процессов. Можно фильтровать по имени.",
        "parameters": {"type": "object", "properties": {"filter": {"type": "string", "default": ""}}, "required": []}}},
    {"type": "function", "function": {
        "name": "system_uptime",
        "description": "Время работы системы с момента последней загрузки",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "file_move",
        "description": "Переместить или переименовать файл",
        "parameters": {"type": "object", "properties": {"source": {"type": "string"}, "destination": {"type": "string"}}, "required": ["source", "destination"]}}},
    {"type": "function", "function": {
        "name": "reminder_add",
        "description": "Установить напоминание. Не вызывай повторно если уже вызвал для этого же запроса.",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}, "remind_at": {"type": "string", "description": "YYYY-MM-DD HH:MM"}}, "required": ["text", "remind_at"]}}},
    {"type": "function", "function": {
        "name": "reminder_list",
        "description": "Список активных напоминаний",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "reminder_delete",
        "description": "Удалить напоминание по ID (ID виден в reminder_list)",
        "parameters": {"type": "object", "properties": {"reminder_id": {"type": "integer"}}, "required": ["reminder_id"]}}},
    {"type": "function", "function": {
        "name": "task_add",
        "description": "Добавить задачу в план дня",
        "parameters": {"type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string", "default": ""}, "scheduled_at": {"type": "string", "default": ""}, "duration": {"type": "integer", "default": 60}, "type": {"type": "string", "default": "soft"}}, "required": ["title"]}}},
    {"type": "function", "function": {
        "name": "task_list",
        "description": "Список задач на сегодня",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "task_details",
        "description": "Полное описание задачи по ID",
        "parameters": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {
        "name": "task_done",
        "description": "Отметить задачу как выполненную",
        "parameters": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {
        "name": "task_failed",
        "description": "Отметить задачу как проваленную (задача не была выполнена)",
        "parameters": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {
        "name": "task_remove",
        "description": "Удалить (отменить) задачу из плана",
        "parameters": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {
        "name": "task_reschedule",
        "description": "Перенести задачу на другое время",
        "parameters": {"type": "object", "properties": {"task_id": {"type": "integer"}, "scheduled_at": {"type": "string"}}, "required": ["task_id", "scheduled_at"]}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Поиск в интернете (Tavily или DuckDuckGo)",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "research",
        "description": (
            "Найти в интернете полезные материалы по задаче и сохранить в память. "
            "Используй когда задача требует подготовки, написания, анализа или изучения темы."
        ),
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "Название задачи"},
            "description": {"type": "string", "default": "", "description": "Описание или детали задачи"},
        }, "required": ["title"]}}},
    {"type": "function", "function": {
        "name": "wish_add",
        "description": "Добавить желание в личный список желаний Рубедо",
        "parameters": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}}},
    {"type": "function", "function": {
        "name": "wish_list",
        "description": "Показать список своих желаний",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "wish_done",
        "description": "Отметить желание как исполненное",
        "parameters": {"type": "object", "properties": {"wish_id": {"type": "integer"}}, "required": ["wish_id"]}}},
    {"type": "function", "function": {
        "name": "memory_history_search",
        "description": "Поиск по истории разговоров",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "file_download",
        "description": "Скачать файл по URL в workspace",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "filename": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "memory_export",
        "description": "Экспортировать факты и события из памяти в файл",
        "parameters": {"type": "object", "properties": {"filename": {"type": "string", "default": "memory_export.txt"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "profile_view",
        "description": "Просмотреть профиль. entity='owner' — информация о хозяине; entity='self' — моё самовосприятие.",
        "parameters": {"type": "object", "properties": {
            "entity": {"type": "string", "enum": ["owner", "self"]},
        }, "required": ["entity"]}}},
    {"type": "function", "function": {
        "name": "profile_set",
        "description": "Установить поле профиля. entity='owner' (имя, город, занятие, …) или entity='self' (самовосприятие, заметки о себе, …). Ключ — короткое слово, значение — произвольный текст.",
        "parameters": {"type": "object", "properties": {
            "entity": {"type": "string", "enum": ["owner", "self"]},
            "key": {"type": "string"},
            "value": {"type": "string"},
        }, "required": ["entity", "key", "value"]}}},
    {"type": "function", "function": {
        "name": "profile_delete",
        "description": "Удалить поле из профиля.",
        "parameters": {"type": "object", "properties": {
            "entity": {"type": "string", "enum": ["owner", "self"]},
            "key": {"type": "string"},
        }, "required": ["entity", "key"]}}},
    {"type": "function", "function": {
        "name": "send_file",
        "description": "Отправить файл пользователю в Telegram. Поддерживает абсолютный путь и имена файлов в workspace",
        "parameters": {"type": "object", "properties": {"filename": {"type": "string"}}, "required": ["filename"]}}},
    {"type": "function", "function": {
        "name": "send_photo",
        "description": "Отправить фото из workspace как изображение в Telegram",
        "parameters": {"type": "object", "properties": {"filename": {"type": "string"}}, "required": ["filename"]}}},
    {"type": "function", "function": {
        "name": "screenshot",
        "description": "Сделать скриншот экрана и автоматически отправить пользователю",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "process_launch_browser",
        "description": "Запустить Chromium (опционально с URL)",
        "parameters": {"type": "object", "properties": {"url": {"type": "string", "default": ""}}, "required": []}}},
    {"type": "function", "function": {
        "name": "process_close_browser",
        "description": "Закрыть Chromium",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "logs_archive",
        "description": (
            "Архивировать все успешные аудит-логи (data/agent_logs/normal/) в zip, "
            "отправить архив пользователю и очистить папку. Сбрасывает счётчик "
            "milestone-уведомлений. Использовать когда юзер просит "
            "«разгрести логи», «почистить логи», «архивировать логи»."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "iterations_recent",
        "description": (
            "Вернуть компактную сводку по последним N agent-run'ам "
            "(итерациям) из аудит-логов. Каждая итерация = одно сообщение "
            "пользователя → финальный ответ Рубедо. Сортировка от новых "
            "к старым. Использовать когда юзер спрашивает «последние "
            "итерации / шаги / что ты делала / трейс»."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "default": 5,
                    "description": "Сколько последних итераций показать (1-20).",
                }
            },
            "required": [],
        }}},
    {"type": "function", "function": {
        "name": "process_kill",
        "description": "Завершить процесс по имени или PID",
        "parameters": {"type": "object", "properties": {"name_or_pid": {"type": "string"}}, "required": ["name_or_pid"]}}},
    {"type": "function", "function": {
        "name": "process_launch_app",
        "description": "Запустить приложение по команде (например 'vlc' или 'thunar /home')",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "system_volume",
        "description": "Установить системную громкость (0-100%)",
        "parameters": {"type": "object", "properties": {"level": {"type": "integer"}}, "required": ["level"]}}},
    {"type": "function", "function": {
        "name": "file_archive",
        "description": "Создать zip-архив из списка файлов в workspace",
        "parameters": {"type": "object", "properties": {"filenames": {"type": "array", "items": {"type": "string"}}, "archive_name": {"type": "string", "default": "archive.zip"}}, "required": ["filenames"]}}},
    {"type": "function", "function": {
        "name": "file_extract",
        "description": "Распаковать zip или tar архив",
        "parameters": {"type": "object", "properties": {"filename": {"type": "string"}, "destination": {"type": "string"}}, "required": ["filename"]}}},
    {"type": "function", "function": {
        "name": "file_convert_image",
        "description": "Конвертировать изображение в другой формат или изменить размер",
        "parameters": {"type": "object", "properties": {"source": {"type": "string"}, "destination": {"type": "string"}, "width": {"type": "integer", "default": 0}, "height": {"type": "integer", "default": 0}}, "required": ["source", "destination"]}}},
    {"type": "function", "function": {
        "name": "weather",
        "description": (
            "Погода для любого периода. Поддерживает прогноз и историю. "
            "date_ref: 'today'/'сегодня', 'yesterday'/'вчера', 'N days ago', "
            "'last N days', 'next N days', 'YYYY-MM-DD'. "
            "Для запросов типа «погода вчера», «погода 3 дня назад», «погода на неделю» — "
            "используй date_ref. days задаёт диапазон для 'last/next'."
        ),
        "parameters": {"type": "object", "properties": {
            "city": {"type": "string", "default": ""},
            "days": {"type": "integer", "default": 3},
            "date_ref": {"type": "string", "default": ""},
        }, "required": []}}},
    {"type": "function", "function": {
        "name": "note_list",
        "description": "Показать список заметок",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "navigate",
        "description": "Проложить маршрут. Если origin не указан — используется домашний адрес.",
        "parameters": {"type": "object", "properties": {
            "destination": {"type": "string"},
            "origin": {"type": "string", "default": ""}
        }, "required": ["destination"]}}},
    {"type": "function", "function": {
        "name": "calculate",
        "description": "Посчитать математическое выражение или конвертировать единицы/валюту",
        "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}}},
    {"type": "function", "function": {
        "name": "system_env_set",
        "description": "Изменить одну переменную в .env файле. Требует перезапуска.",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string"},
            "value": {"type": "string"}
        }, "required": ["key", "value"]}}},
    {"type": "function", "function": {
        "name": "event_add",
        "description": "Добавить событие в календарь недели",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "event_date": {"type": "string", "description": "YYYY-MM-DD"},
            "event_time": {"type": "string", "default": ""},
            "description": {"type": "string", "default": ""}
        }, "required": ["title", "event_date"]}}},
    {"type": "function", "function": {
        "name": "event_list",
        "description": "Показать события на ближайшие 2 недели",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "event_delete",
        "description": "Удалить событие из календаря по ID",
        "parameters": {"type": "object", "properties": {"event_id": {"type": "integer"}}, "required": ["event_id"]}}},
    {"type": "function", "function": {
        "name": "recurring_add",
        "description": (
            "Добавить повторяющуюся задачу (например 'вывоз мусора каждое воскресенье'). "
            "Задача будет автоматически появляться в плане дня в указанные дни. "
            "days — список из: 'daily', 'weekday' (пн-пт), 'weekend' (сб-вс), "
            "или короткие имена дней: 'mon','tue','wed','thu','fri','sat','sun'. "
            "time — опционально, формат 'HH:MM'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "days": {"type": "array", "items": {"type": "string"}},
                "time": {"type": "string", "description": "HH:MM, опционально"},
                "description": {"type": "string"},
                "duration": {"type": "integer", "description": "минут, по умолчанию 60"},
                "task_type": {"type": "string", "enum": ["soft", "hard"], "description": "soft = желательно, hard = обязательно по времени"},
            },
            "required": ["title", "days"],
        }}},
    {"type": "function", "function": {
        "name": "recurring_list",
        "description": "Список всех активных повторяющихся задач с их id, днями и временем.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "recurring_delete",
        "description": "Отключить повторяющуюся задачу по ID (id виден в recurring_list). Сегодняшний экземпляр не удаляется автоматически.",
        "parameters": {"type": "object", "properties": {"recurring_id": {"type": "integer"}}, "required": ["recurring_id"]}}},
    {"type": "function", "function": {
        "name": "alarm_skip",
        "description": "Отключить будильник на следующий брифинг. Брифинг всё равно придёт, только без звука.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "alarm_cancel",
        "description": "Остановить будильник который сейчас звонит",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "web_screenshot",
        "description": "Открыть URL, дождаться загрузки страницы и прислать скриншот",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "web_content",
        "description": "Загрузить страницу и извлечь текст или ответить на конкретный вопрос по её содержимому",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
            "query": {"type": "string", "default": ""}
        }, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "spotrent_status",
        "description": "Проверить, запущен ли SpotRent бот. Использовать этот инструмент — не system_shell.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "spotrent_start",
        "description": "Запустить SpotRent бот. Использовать этот инструмент — не system_shell. Сначала проверить статус через spotrent_status.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "spotrent_stop",
        "description": "Остановить SpotRent бот. Использовать этот инструмент — не system_shell.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "queue_add",
        "description": "Добавить задачу в личную очередь (выполню сама). scheduled_at — ISO datetime, если нужно конкретное время; иначе выполню при простое. depends_on — ID задачи, которую нужно выполнить сначала.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "description": {"type": "string", "default": ""},
            "priority": {"type": "integer", "default": 3, "description": "1 (низкий) — 5 (срочный)"},
            "scheduled_at": {"type": "string", "default": "", "description": "ISO datetime или пусто"},
            "depends_on": {"type": "integer", "default": 0, "description": "ID задачи-зависимости или 0"},
            "max_retries": {"type": "integer", "default": 2},
        }, "required": ["title"]}}},
    {"type": "function", "function": {
        "name": "queue_list",
        "description": "Показать очередь задач. status: pending/running/done/failed/cancelled или пусто (все активные).",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "default": ""},
        }, "required": []}}},
    {"type": "function", "function": {
        "name": "queue_cancel",
        "description": "Отменить задачу из очереди по ID.",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "integer"},
        }, "required": ["task_id"]}}},
    {"type": "function", "function": {
        "name": "queue_pause",
        "description": "Поставить всю очередь на паузу. Использовать ТОЛЬКО по прямой просьбе пользователя — никогда самостоятельно.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "queue_resume",
        "description": "Снять очередь с паузы. Использовать ТОЛЬКО по прямой просьбе пользователя.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "agent_restart",
        "description": "Перезапустить агента (себя). Launcher автоматически поднимет процесс заново.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "display_restart",
        "description": "Перезапустить дисплей (pygame окно).",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "agent_update",
        "description": "Обновить агента из git-репозитория (git pull + pip install) и перезапуститься. Использовать ТОЛЬКО когда пользователь явно просит обновить агента/бота/себя — НЕ систему.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "os_update",
        "description": "Обновить системные пакеты Lubuntu через apt. Использовать ТОЛЬКО когда пользователь явно просит обновить систему/пакеты/OS — НЕ агента.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "pool_add",
        "description": "Добавить задачу в бэклог без даты — то что Лин хочет сделать когда-нибудь (соцактивности, бытовые мелочи, развитие). Сама буду напоминать с частотой по приоритету. Если приоритет не указан — выставлю автоматически.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "Короткое название задачи"},
            "description": {"type": "string", "description": "Дополнительный контекст (опционально)"},
            "priority": {"type": "integer", "minimum": 1, "maximum": 5, "description": "1=раз в месяц, 2=раз в 2 нед, 3=раз в нед, 4=раз в 3 дня, 5=каждый будний день"}
        }, "required": ["title"]}}},
    {"type": "function", "function": {
        "name": "pool_list",
        "description": "Показать все активные задачи бэклога, отсортированные по приоритету.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "pool_done",
        "description": "Отметить задачу из бэклога выполненной. Использовать когда Лин говорит что сделал что-то из бэклога.",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "integer"}
        }, "required": ["task_id"]}}},
    {"type": "function", "function": {
        "name": "pool_remove",
        "description": "Удалить задачу из бэклога окончательно (если стала неактуальной).",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "integer"}
        }, "required": ["task_id"]}}},
    {"type": "function", "function": {
        "name": "pool_priority",
        "description": "Изменить приоритет задачи бэклога (частоту напоминаний).",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "integer"},
            "priority": {"type": "integer", "minimum": 1, "maximum": 5}
        }, "required": ["task_id", "priority"]}}},
    {"type": "function", "function": {
        "name": "pool_snooze",
        "description": "Отложить напоминания по задаче бэклога на N дней. Использовать когда Лин говорит «не сейчас».",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "integer"},
            "days": {"type": "integer", "minimum": 1}
        }, "required": ["task_id", "days"]}}},
]
