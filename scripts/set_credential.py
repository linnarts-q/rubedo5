#!/usr/bin/env python3
"""Set (or update) the sudo password for a host, encrypted at rest
(techspec §1.6).

Run this directly on the machine, in a real terminal — never through
Telegram, never through Rubedo, never through the LLM. The password is
entered via getpass: not echoed to the screen, never in shell history,
never in process args (`ps aux` can't see it).

Usage:
    python scripts/set_credential.py <host>

    host: "local"  — this machine (mini-PC)
          "server" — the separate server SpotRent lives on

Requires CREDENTIALS_KEY to already be set in .env — generate one
first if you haven't:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.credentials import set_password  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ("local", "server"):
        print("Usage: python scripts/set_credential.py <local|server>")
        sys.exit(1)
    host = sys.argv[1]

    password = getpass.getpass(f"Пароль sudo для '{host}': ")
    if not password:
        print("Пустой пароль, отмена.")
        sys.exit(1)
    confirm = getpass.getpass("Повтори: ")
    if password != confirm:
        print("Пароли не совпадают, отмена.")
        sys.exit(1)

    try:
        set_password(host, password)
    except RuntimeError as e:
        print(str(e))
        sys.exit(1)

    print(f"Пароль для '{host}' сохранён (зашифрован).")


if __name__ == "__main__":
    main()
