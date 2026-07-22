"""Shell-execution tools — exec / sudo / python eval / process control.

Blacklists live here (they're only checked by shell_exec — see the
zone gate in agent/zones.py for the actual trust boundary, this is the
last-resort backstop behind it, not the only defense per techspec §1)
along with the `_check_blacklist` helper. The parent `agent.tools`
package re-imports the blacklist constants so existing references stay
valid.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("rubedo.tools.shell")

_WORKSPACE = Path("workspace")


# ─ Security blacklists ─────────────────────────────────────────────────

_SHELL_BLACKLIST = [
    (r"rm\s+-[a-z]*r[a-z]*f\s+/",              "rm -rf /"),
    (r"rm\s+-[a-z]*r[a-z]*f\s+~",              "rm -rf ~"),
    (r"\bmkfs\b",                               "mkfs"),
    (r"\bdd\s+if=",                             "dd if="),
    (r":\s*\(\s*\)\s*\{.*:.*\|.*:.*&",         "fork bomb"),
    (r">\s*/dev/(sda|hda|vda|nvme)",            "> /dev/sdX"),
    (r"\bshred\s+/dev/",                        "shred /dev/"),
    (r"\bwipefs\b",                             "wipefs"),
    (r"curl\b.+\|\s*(ba)?sh\b",                "curl pipe to shell"),
    (r"wget\b.+\|\s*(ba)?sh\b",                "wget pipe to shell"),
    (r"base64\b.+\|\s*(ba)?sh\b",              "base64 pipe to shell"),
    (r"\beval\s+['\"`\$\(]",                   "eval injection"),
]

_SUDO_BLACKLIST = [
    (r"\breboot\b",                       "reboot"),
    (r"\bshutdown\b",                     "shutdown"),
    (r"\bhalt\b",                         "halt"),
    (r"\bpoweroff\b",                     "poweroff"),
    (r"\bmkfs\b",                         "mkfs"),
    (r"\bdd\s+if=",                       "dd if="),
    (r"rm\s+-[a-z]*r[a-z]*f\s+/",        "rm -rf /"),
    (r"\bpasswd\s+root\b",                "passwd root"),
]


def _check_blacklist(command: str, blacklist: list) -> str | None:
    for pattern, label in blacklist:
        if re.search(pattern, command, re.IGNORECASE | re.DOTALL):
            return label
    return None


# ─ Tools ───────────────────────────────────────────────────────────────

_SHELL_OUTPUT_CAP = 4000


def shell_exec(command: str, timeout: int = 30) -> str:
    blocked = _check_blacklist(command, _SHELL_BLACKLIST)
    if blocked:
        return f"Команда заблокирована в целях безопасности: {blocked}"
    try:
        cwd = os.getcwd()
        r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
        out = r.stdout.strip()
        err = r.stderr.strip()
        if r.returncode != 0:
            result = f"[код {r.returncode}]\n{err or out}"
        else:
            result = out or "(пусто)"
        if len(result) > _SHELL_OUTPUT_CAP:
            result = result[:_SHELL_OUTPUT_CAP] + f"\n…[обрезано, cwd={cwd}]"
        elif result == "(пусто)":
            result = f"(пусто, cwd={cwd})"
        return result
    except subprocess.TimeoutExpired:
        return "Таймаут команды."
    except Exception as e:
        return f"Ошибка: {e}"


def run_sudo(command: str, host: str = "local") -> str:
    """Red zone (agent/zones.py) — requires the owner's explicit go-ahead
    every time, no exceptions (see techspec §1/C12/C13).

    Password comes from the encrypted per-host table (techspec §1.6,
    agent/credentials.py) by `host` label — never from config, never
    passed in as an LLM-visible argument. `host`: "local" (this
    machine) or "server" (the separate server, over SSH via
    agent/remote.py). Set a password with scripts/set_credential.py,
    run directly on the target machine — never through the agent.
    """
    from agent.credentials import get_password

    blocked = _check_blacklist(command, _SUDO_BLACKLIST)
    if blocked:
        return f"Команда sudo заблокирована в целях безопасности: {blocked}"

    password = get_password(host)
    if password is None:
        return (
            f"Пароль sudo для '{host}' не настроен (или CREDENTIALS_KEY не задан). "
            f"Задай его на самой машине: python scripts/set_credential.py {host}"
        )

    if host == "local":
        try:
            import shlex
            args = ["sudo", "-S"] + shlex.split(command)
            r = subprocess.run(
                args,
                input=password + "\n",
                capture_output=True,
                text=True,
                timeout=30,
            )
            out = r.stdout.strip()
            err = "\n".join(
                line for line in r.stderr.splitlines()
                if "password" not in line.lower() and "[sudo]" not in line.lower()
            ).strip()
            if r.returncode != 0:
                return f"[код {r.returncode}]\n{err or out}"
            return out or "(пусто)"
        except subprocess.TimeoutExpired:
            return "Таймаут команды."
        except Exception as e:
            return f"Ошибка: {e}"

    from agent import remote
    return remote.run(f"sudo -S {command}", stdin_input=password + "\n")


def run_code(code: str) -> str:
    """Runs with cwd inside workspace/ so relative file access the code
    does lands there, not wherever the process happened to start —
    partial sandboxing per techspec §1 ("run_code — в песочнице,
    ограниченной workspace"). This does not limit network access, CPU,
    or absolute-path file access; it only scopes relative paths.
    """
    import tempfile
    _WORKSPACE.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(code)
        fname = f.name
    try:
        r = subprocess.run(
            [sys.executable, fname],
            capture_output=True, text=True, timeout=30,
            cwd=str(_WORKSPACE.resolve()),
        )
        return r.stdout.strip() or r.stderr.strip() or "(нет вывода)"
    except subprocess.TimeoutExpired:
        return "Таймаут."
    except Exception as e:
        return f"Ошибка: {e}"
    finally:
        os.unlink(fname)


def list_processes(filter: str = "") -> str:
    try:
        import psutil
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "status"]):
            try:
                info = p.info
                name = info["name"] or ""
                if filter and filter.lower() not in name.lower():
                    continue
                mem_mb = (info["memory_info"].rss // 1024 // 1024) if info["memory_info"] else 0
                procs.append((info["cpu_percent"] or 0, info["pid"], name, mem_mb, info["status"]))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        procs.sort(reverse=True)
        if not procs:
            return "Процессы не найдены."
        lines = ["PID    CPU%  MEM(MB)  СТАТУС    ИМЯ"]
        for cpu, pid, name, mem, status in procs[:30]:
            lines.append(f"{pid:<6} {cpu:>4.1f}  {mem:>7}  {status:<9} {name}")
        return "\n".join(lines)
    except ImportError:
        return shell_exec("ps aux --sort=-%cpu | head -20")


def kill_process(name_or_pid: str) -> str:
    name_or_pid = name_or_pid.strip()
    try:
        pid = int(name_or_pid)
        os.kill(pid, 15)
        return f"Процесс {pid} завершён."
    except ValueError:
        try:
            r = subprocess.run(
                ["pkill", "-f", name_or_pid], capture_output=True, text=True, timeout=5
            )
            if r.returncode == 0:
                return f"Процессы '{name_or_pid}' завершены."
            return f"Процессы '{name_or_pid}' не найдены."
        except Exception as e:
            return f"Ошибка: {e}"
    except ProcessLookupError:
        return f"Процесс {name_or_pid} не найден."
    except Exception as e:
        return f"Ошибка: {e}"


def launch_app(command: str) -> str:
    try:
        subprocess.Popen(
            command.split(),
            env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")},
            start_new_session=True,
        )
        return f"Запущено: {command}"
    except FileNotFoundError:
        return f"Команда не найдена: {command.split()[0]}"
    except Exception as e:
        return f"Ошибка запуска: {e}"
