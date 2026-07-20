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
