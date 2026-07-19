"""Per-agent-run audit log.

Each run produces one JSON file containing the user message, LLM
request/response metadata, every tool call, and the final reply.

Successful runs land in `data/agent_logs/normal/`; runs that ended in a
loop or unhandled exception land in `data/agent_logs/errors/` and the
file is meant to be sent to the owner as a Telegram attachment.

Milestone notifications fire at 25, 50, and 100 successful runs.
Each milestone fires exactly once (idempotent across restarts via meta KV).
After the owner archives and clears the folder the counter resets to 0.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path

log = logging.getLogger("rubedo.audit")

LOG_ROOT = Path("data/agent_logs")
NORMAL_DIR = LOG_ROOT / "normal"
ERRORS_DIR = LOG_ROOT / "errors"
_NOTIFY_MILESTONES = (25, 50, 100)
META_NOTIFIED_KEY = "audit_last_notified_count"
_RESULT_TRUNCATE = 1000


class AuditLogger:
    """Accumulates events for one agent-run and flushes to disk on close."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.run_id = uuid.uuid4().hex[:8]
        self.started_at = datetime.now()
        self.events: list[dict] = []
        self.is_error = False
        self.error_reason: str | None = None
        self.file_path: Path | None = None

    def _event(self, type_: str, **fields) -> None:
        self.events.append({
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "type": type_,
            **fields,
        })

    def user_message(self, text: str) -> None:
        self._event("user_message", text=text)

    def route(self, route: str, context_type: str, intent: str) -> None:
        self._event("route", route=route, context_type=context_type, intent=intent)

    def llm_request(self, message_count: int, has_tools: bool) -> None:
        self._event("llm_request", message_count=message_count, has_tools=has_tools)

    def llm_response(self, content: str | None, tool_call_count: int) -> None:
        self._event("llm_response", content=content, tool_call_count=tool_call_count)

    def tool_called(self, name: str, args: dict) -> None:
        self._event("tool_called", name=name, args=args)

    def tool_finished(
        self,
        name: str,
        result: str,
        success: bool,
        duration_ms: int | None = None,
    ) -> None:
        self._event(
            "tool_finished",
            name=name,
            success=success,
            result=str(result)[:_RESULT_TRUNCATE],
            duration_ms=duration_ms,
        )

    def idempotency_block(self, name: str, args: dict) -> None:
        self._event("idempotency_block", name=name, args=args)

    def loop_detected(self, name: str, args: dict) -> None:
        self.is_error = True
        self.error_reason = f"loop_detected:{name}"
        self._event("loop_detected", name=name, args=args)

    def final_reply(self, text: str) -> None:
        self._event("final_reply", text=text)

    def exception(self, where: str, error: str) -> None:
        self.is_error = True
        self.error_reason = f"{where}:{error[:80]}"
        self._event("exception", where=where, error=error)

    def close(self) -> Path | None:
        target_dir = ERRORS_DIR if self.is_error else NORMAL_DIR
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.warning(f"audit: cannot create dir {target_dir}: {e}")
            return None

        ts = self.started_at.strftime("%Y%m%d_%H%M%S")
        prefix = "error" if self.is_error else "run"
        path = target_dir / f"{prefix}_{ts}_{self.run_id}.json"
        payload = {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "started_at": self.started_at.isoformat(),
            "ended_at": datetime.now().isoformat(),
            "is_error": self.is_error,
            "error_reason": self.error_reason,
            "events": self.events,
        }
        try:
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.file_path = path
            return path
        except Exception as e:
            log.warning(f"audit: write failed for {path}: {e}")
            return None


def check_normal_threshold() -> int | None:
    """Return the current file count if it just crossed a milestone (25/50/100).

    Each milestone fires exactly once. Resets when archive_agent_logs() clears
    the folder and zeroes META_NOTIFIED_KEY.
    """
    try:
        from memory.db import load_meta, save_meta
    except Exception:
        return None

    if not NORMAL_DIR.exists():
        return None

    try:
        count = sum(
            1 for p in NORMAL_DIR.iterdir()
            if p.is_file() and p.suffix == ".json"
        )
    except Exception:
        return None

    last_str = load_meta(META_NOTIFIED_KEY) or "0"
    try:
        last = int(last_str)
    except ValueError:
        last = 0

    for milestone in _NOTIFY_MILESTONES:
        if last < milestone <= count:
            save_meta(META_NOTIFIED_KEY, str(milestone))
            return count
    return None


def archive_and_clear_normal_logs() -> tuple[Path | None, int]:
    """Zip all files in NORMAL_DIR, delete them, reset the milestone counter.

    Returns (zip_path, file_count). zip_path is None on failure.
    """
    import zipfile
    try:
        from memory.db import save_meta
    except Exception:
        save_meta = None  # type: ignore[assignment]

    if not NORMAL_DIR.exists():
        return None, 0

    files = [p for p in NORMAL_DIR.iterdir() if p.is_file() and p.suffix == ".json"]
    if not files:
        return None, 0

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_dir = LOG_ROOT / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)
    zip_path = archive_dir / f"agent_logs_normal_{ts}.zip"

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                zf.write(f, f.name)
    except Exception as e:
        log.error(f"archive_agent_logs: zip failed: {e}")
        return None, 0

    deleted = 0
    for f in files:
        try:
            f.unlink()
            deleted += 1
        except Exception as e:
            log.warning(f"archive_agent_logs: delete failed {f.name}: {e}")

    if save_meta:
        try:
            save_meta(META_NOTIFIED_KEY, "0")
        except Exception:
            pass

    log.info(f"Archived {deleted} normal logs → {zip_path}")
    return zip_path, deleted
