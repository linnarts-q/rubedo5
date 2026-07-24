"""Music tools (§13, stage 9.5) — ported from rubedo4's skills/music.py
as discrete tools instead of a single NL-keyword-matched execute(text)
dispatcher, per the audit's own instruction to fold the creature-
comfort skills into the ordinary tool mechanism rather than porting
skills/registry.py's separate, parallel dispatch route. The mpv IPC
plumbing itself (unix socket, state file) is presentation, not
architecture, and kept as rubedo4 had it.
"""
from __future__ import annotations

import json
import logging
import re
import socket
import subprocess
from datetime import datetime
from pathlib import Path

log = logging.getLogger("rubedo.tools.music")

_MPV_SOCKET = "/tmp/rubedo_mpv.sock"
_STATE_FILE = Path("data/.music_state.json")
_PAUSE_TIMEOUT_H = 2

_URL_RE = re.compile(r"https?://\S+")
_mpv_proc: subprocess.Popen | None = None


def _load_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(**kwargs) -> None:
    Path("data").mkdir(exist_ok=True)
    state = _load_state()
    for k, v in kwargs.items():
        if v is None:
            state.pop(k, None)
        else:
            state[k] = v
    _STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _cmd(command: list) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(_MPV_SOCKET)
            s.sendall(json.dumps({"command": command}).encode() + b"\n")
            data = b""
            while b"\n" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
            resp = json.loads(data.decode().strip())
            return resp.get("error") == "success"
    except Exception:
        return False


def _get_property(prop: str):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(_MPV_SOCKET)
            s.sendall(json.dumps({"command": ["get_property", prop]}).encode() + b"\n")
            chunks = []
            while True:
                chunk = s.recv(4096)
                chunks.append(chunk)
                if not chunk or b"\n" in chunk:
                    break
            data = json.loads(b"".join(chunks).decode().strip())
            return data.get("data")
    except Exception:
        return None


def _running() -> bool:
    return _mpv_proc is not None and _mpv_proc.poll() is None


def _start_mpv(playlist: str, start_index: int = 0) -> subprocess.Popen:
    args = [
        "mpv", "--no-video", "--shuffle",
        f"--input-ipc-server={_MPV_SOCKET}",
        "--ytdl-format=bestaudio/best",
    ]
    if start_index > 0:
        args.append(f"--playlist-start={start_index}")
    args.append(playlist)
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def music_play(url_or_query: str = "") -> str:
    """Start playback. Empty argument resumes the saved playlist (or
    config.MUSIC_PLAYLIST); a URL plays that instead."""
    global _mpv_proc
    url_match = _URL_RE.search(url_or_query or "")
    if url_match:
        playlist = url_match.group(0)
    else:
        state = _load_state()
        playlist = state.get("playlist") or ""
        if not playlist:
            from config import MUSIC_PLAYLIST
            playlist = MUSIC_PLAYLIST or ""
        if not playlist:
            return "Плейлист не задан. Скинь ссылку или пропиши MUSIC_PLAYLIST в .env."

    try:
        _mpv_proc = _start_mpv(playlist)
        _save_state(playlist=playlist, track_index=0, paused_at=None)
        return "Включила музыку."
    except FileNotFoundError:
        return "mpv не установлен.\nУстанови: sudo apt install mpv"
    except Exception as e:
        return f"Ошибка запуска mpv: {e}"


def music_pause() -> str:
    if not _running():
        return "Музыка не играет."
    track_idx = _get_property("playlist-pos") or 0
    _cmd(["set", "pause", "yes"])
    _save_state(paused_at=datetime.now().isoformat(), track_index=int(track_idx))
    return "Пауза."


def music_resume() -> str:
    """Handles the same stale-pause logic as rubedo4: paused over
    _PAUSE_TIMEOUT_H ago restarts mpv fresh at the saved track index
    rather than trusting a socket that's probably long gone."""
    global _mpv_proc
    state = _load_state()
    paused_at = state.get("paused_at")

    if paused_at:
        try:
            delta = (datetime.now() - datetime.fromisoformat(paused_at)).total_seconds()
        except ValueError:
            delta = 0
        _save_state(paused_at=None)

        if delta > _PAUSE_TIMEOUT_H * 3600:
            playlist = state.get("playlist") or ""
            track_idx = int(state.get("track_index") or 0)
            if not playlist:
                return "Плейлист не сохранён."
            if _running():
                _mpv_proc.terminate()
            _mpv_proc = _start_mpv(playlist, start_index=track_idx)
            h, m = int(delta // 3600), int((delta % 3600) // 60)
            elapsed = f"{h}ч {m}мин" if h else f"{m} мин"
            return f"Пауза была {elapsed}, продолжила с трека #{track_idx + 1}."
        if _running():
            _cmd(["set", "pause", "no"])
            return "Продолжаю."
        playlist = state.get("playlist") or ""
        track_idx = int(state.get("track_index") or 0)
        if playlist:
            _mpv_proc = _start_mpv(playlist, start_index=track_idx)
            return f"Перезапустила с трека #{track_idx + 1}."
        return "Музыка не играет."

    if _running():
        _cmd(["set", "pause", "no"])
        return "Продолжаю."
    return "Музыка не играет."


def music_stop() -> str:
    global _mpv_proc
    if _running():
        _mpv_proc.terminate()
        _mpv_proc = None
    _save_state(paused_at=None)
    return "Музыка остановлена."


def music_next() -> str:
    if not _cmd(["playlist-next"]):
        return "mpv не отвечает."
    idx = _get_property("playlist-pos")
    if idx is not None:
        _save_state(track_index=int(idx))
    return "Следующий трек."


def music_louder() -> str:
    return "Громче." if _cmd(["add", "volume", "10"]) else "mpv не отвечает."


def music_quieter() -> str:
    return "Тише." if _cmd(["add", "volume", "-10"]) else "mpv не отвечает."


def cleanup_if_stale() -> None:
    """Terminate mpv if paused longer than _PAUSE_TIMEOUT_H. Called
    from rubedo4's decay loop — rubedo5 doesn't have a decay/cleanup
    tick yet (out of this stage's scope), so nothing calls this yet;
    kept ready for whenever that mechanism lands."""
    global _mpv_proc
    state = _load_state()
    paused_at = state.get("paused_at")
    if not paused_at or not _running():
        return
    try:
        delta = (datetime.now() - datetime.fromisoformat(paused_at)).total_seconds()
        if delta > _PAUSE_TIMEOUT_H * 3600:
            _mpv_proc.terminate()
            _mpv_proc = None
            _save_state(paused_at=None)
            log.info("mpv terminated after extended pause")
    except Exception as e:
        log.warning(f"Music cleanup error: {e}")
