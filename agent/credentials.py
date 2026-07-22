"""Encrypted per-host credential store for sudo (techspec §1.6).

Why encryption, not hashing: run_sudo needs the actual plaintext
password back to authenticate — a one-way hash (the way /etc/shadow
verifies logins, by comparing hashes and never recovering the
original) can't be reversed to produce it. So this is symmetric
encryption (Fernet), not hashing.

Why the password never reaches a tool argument: agent/audit.py logs
every tool call's arguments verbatim into the per-run JSON log. If the
password were a `run_sudo(command, password)` argument, it would end
up in cleartext in data/agent_logs/. Instead run_sudo takes a `host`
label ("local" / "server") and looks the real password up here,
server-side, invisible to the LLM and to the audit log.

Setting a password happens *outside* the agent entirely —
scripts/set_credential.py, run directly on the machine by the owner.
There is no tool and no conversational path that writes to this table;
that's deliberate, not a missing feature.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from config import CREDENTIALS_KEY
from memory.db import get_conn

log = logging.getLogger("rubedo.agent.credentials")


def _fernet():
    if not CREDENTIALS_KEY:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(CREDENTIALS_KEY.encode())
    except Exception as e:
        log.error(f"CREDENTIALS_KEY is invalid: {e}")
        return None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def set_password(host: str, password: str) -> None:
    """Called only from scripts/set_credential.py. The `credentials`
    table itself is created by memory.db.init_db() — this module never
    creates schema, only reads/writes rows."""
    f = _fernet()
    if f is None:
        raise RuntimeError(
            "CREDENTIALS_KEY не задан в .env. Сгенерируй: "
            "python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    encrypted = f.encrypt(password.encode())
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO credentials (host, secret_encrypted, updated_at) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (host) DO UPDATE SET "
            "secret_encrypted=excluded.secret_encrypted, updated_at=excluded.updated_at",
            (host, encrypted, _now()),
        )


def get_password(host: str) -> str | None:
    """Returns None if CREDENTIALS_KEY isn't configured, the host has no
    stored password, or decryption fails for any reason — callers
    (run_sudo) treat all three the same: can't proceed, say so plainly."""
    f = _fernet()
    if f is None:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT secret_encrypted FROM credentials WHERE host=%s", (host,)
        ).fetchone()
    if not row:
        return None
    try:
        return f.decrypt(bytes(row["secret_encrypted"])).decode()
    except Exception as e:
        log.error(f"Failed to decrypt credential for host={host!r}: {e}")
        return None


def has_password(host: str) -> bool:
    return get_password(host) is not None


def list_hosts() -> list[str]:
    """For diagnostics — which hosts have a password stored, not the
    passwords themselves."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT host, updated_at FROM credentials ORDER BY host"
        ).fetchall()
    return [f"{r['host']} (обновлён {(r['updated_at'] or '?')[:10]})" for r in rows]
