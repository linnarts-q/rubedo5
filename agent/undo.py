"""Undo snapshots for yellow-zone file writes (techspec §15).

Goal: make approvals psychologically cheap — "I'll just roll it back
if it's wrong" — for the two file operations that actually destroy
content: file_write (overwrites) and file_delete (removes). Before
agent/controller.py runs an approved call to either, it snapshots the
current file (if any) here; rollback_last() restores it.

Scope, deliberately: file_move's "undo" is just moving back (trivial,
handled inline where it matters rather than through this snapshot
mechanism); file_archive/file_extract/file_convert_image don't
destroy existing files in the common case and are left unprotected
for now — a documented gap, not an oversight, per the spec's own
"roll this out gradually, starting with the most dangerous" note.

TTL ~2 days (config.UNDO_TTL_DAYS): pruned opportunistically on each
new snapshot. There's no scheduler to do it proactively yet — that
needs the day-engine, a later stage.
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path

from config import UNDO_TTL_DAYS

log = logging.getLogger("rubedo.agent.undo")

_UNDO_DIR = Path("workspace/undo")
_META_LAST = "last_undo"


def _prune_old() -> None:
    if not _UNDO_DIR.exists():
        return
    cutoff = time.time() - UNDO_TTL_DAYS * 86400
    for p in _UNDO_DIR.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
        except Exception as e:
            log.debug(f"undo prune skipped for {p}: {e}")


def snapshot_before_write(target: Path) -> None:
    """Call right before an approved file_write/file_delete actually
    touches `target`. No-op-ish if the file doesn't exist yet — records
    that the file was *created* by this write, so rollback knows to
    delete it rather than restore old content."""
    from memory.db import save_meta

    _UNDO_DIR.mkdir(parents=True, exist_ok=True)
    _prune_old()

    if not target.exists():
        save_meta(_META_LAST, json.dumps({"kind": "created", "target": str(target)}))
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    snap_path = _UNDO_DIR / f"{ts}_{target.name}"
    try:
        shutil.copy2(target, snap_path)
    except Exception as e:
        log.warning(f"undo snapshot failed for {target}: {e}")
        return
    save_meta(_META_LAST, json.dumps({
        "kind": "overwritten", "target": str(target), "snapshot": str(snap_path),
    }))


def verify_last_write(filename_hint: str = "") -> str | None:
    """Read-only check of whether the most recently snapshotted yellow-
    zone file_write/file_delete actually landed, by comparing the
    target's current state against what was captured right before it
    ran (agent/crash_recovery.py — an interrupted write step must be
    verified, never blindly redone, §15's undo snapshot is exactly the
    "compare with reality, not guess" this needs).

    `filename_hint` is a loose sanity check, not a database key — this
    module only ever remembers the single most recent snapshot, so if
    it doesn't look like it belongs to the step being checked, this
    returns None (verification unavailable) rather than reporting on
    the wrong file. None also covers: no snapshot at all, snapshot
    pruned past TTL, or nothing green-zone/red-zone (§15 only
    snapshots yellow-zone file writes to begin with)."""
    from memory.db import load_meta

    raw = load_meta(_META_LAST)
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except Exception:
        return None

    target_str = info.get("target", "")
    if filename_hint and filename_hint not in target_str and target_str not in filename_hint:
        return None

    target = Path(target_str)
    if info.get("kind") == "created":
        return "похоже, выполнилась (файл создан)" if target.exists() else "похоже, не выполнилась — файла ещё нет"

    snap_path = Path(info.get("snapshot", ""))
    if not snap_path.exists():
        return None
    if not target.exists():
        return "похоже, выполнилась — файл удалён/перемещён"
    try:
        same = target.read_bytes() == snap_path.read_bytes()
    except Exception:
        return None
    return "похоже, НЕ выполнилась — файл не изменился с последнего снапшота" if same else "похоже, выполнилась — содержимое изменилось"


def rollback_last() -> str:
    """Undo the most recent yellow-zone file_write/file_delete."""
    from memory.db import load_meta, save_meta

    raw = load_meta(_META_LAST)
    if not raw:
        return "Нечего откатывать — нет сохранённых снапшотов."
    try:
        info = json.loads(raw)
    except Exception:
        return "Не удалось прочитать информацию о последнем изменении."

    target = Path(info["target"])
    if info.get("kind") == "created":
        try:
            if target.exists():
                target.unlink()
            save_meta(_META_LAST, "")
            return f"Откатила: удалила {target} (до записи файла не существовало)."
        except Exception as e:
            return f"Не удалось откатить: {e}"

    snap_path = Path(info.get("snapshot", ""))
    if not snap_path.exists():
        return f"Снапшот не найден — истёк TTL ({UNDO_TTL_DAYS} дн.) или уже был очищен."
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snap_path, target)
        save_meta(_META_LAST, "")
        return f"Откатила: восстановила {target} из снапшота."
    except Exception as e:
        return f"Не удалось восстановить: {e}"
