from dotenv import load_dotenv
import os
import sys

load_dotenv()


def _int(env: str, default: int) -> int:
    raw = os.getenv(env, str(default))
    try:
        return int(raw)
    except ValueError:
        print(f"[config] WARNING: {env}={raw!r} is not a valid integer, using {default}", file=sys.stderr)
        return default


# Telegram
TELEGRAM_API_ID = _int("TELEGRAM_API_ID", 0)
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
OWNER_USER_ID = _int("OWNER_USER_ID", 0)
OWNER_NAME = os.getenv("OWNER_NAME", "хозяин")

# LLM — Основной мозг — генерация и личность (OpenRouter / Nemotron)
OPENROUTER_API_KEYS = [k.strip() for k in os.getenv("OPENROUTER_API_KEYS", "").split(",") if k.strip()]
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")
OPENROUTER_FALLBACK_MODEL = os.getenv("OPENROUTER_FALLBACK_MODEL", "meta-llama/llama-3.3-70b-instruct:free")

# LLM — Аналитический мозг — классификация, суммаризация (Groq)
GROQ_API_KEYS = [k.strip() for k in os.getenv("GROQ_API_KEYS", "").split(",") if k.strip()]
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_FALLBACK_MODEL = os.getenv("GROQ_FALLBACK_MODEL", "llama-3.1-8b-instant")

# LLM — Vision (через OpenRouter, бесплатная мультимодальная модель)
OPENROUTER_VISION_MODEL = os.getenv("OPENROUTER_VISION_MODEL", "google/gemma-4-26b-a4b-it:free")

# Memory
DB_PATH = os.getenv("DB_PATH", "data/rubedo5.db")

# System
# NOTE: no SUDO_PASSWORD here by design (techspec stage 0/§10). Sudo
# credentials move to an encrypted per-host table (§1.6) instead of a
# plaintext env var — see agent/tools/shell.py:run_sudo, currently
# stubbed pending that table.

# Stop-phrase (techspec §15) — checked as a plain string comparison
# before any LLM call, so it works even if the models are down or
# looping. Empty by default: the feature is inert until the owner sets
# both phrases, rather than shipping a guessable default.
STOP_PHRASE = os.getenv("STOP_PHRASE", "")
RESUME_PHRASE = os.getenv("RESUME_PHRASE", "")

# Server transport (techspec §1.2) — SSH only, key-based auth, no
# password ever. Empty by default: agent/remote.py reports "not
# configured" rather than guessing connection details. This is the
# yellow zone's "сервер" bullets (§1) made physically possible.
SERVER_HOST = os.getenv("SERVER_HOST", "")
SERVER_USER = os.getenv("SERVER_USER", "")
SERVER_SSH_KEY = os.getenv("SERVER_SSH_KEY", "")

# SpotRent (production bot, lives on the server — 4.12). Paths are on
# the remote host, reached via agent/remote.py, not local subprocess
# calls like rubedo4 had (that assumed co-location on the mini-PC).
SPOTRENT_PYTHON = os.getenv("SPOTRENT_PYTHON", "/home/rubedo/spotrent/venv/bin/python")
SPOTRENT_LAUNCHER = os.getenv("SPOTRENT_LAUNCHER", "/home/rubedo/spotrent/spotrent_launcher.py")
SPOTRENT_CWD = os.getenv("SPOTRENT_CWD", "/home/rubedo/spotrent")

# Bus
BUS_HOST = "127.0.0.1"
BUS_PORT = 9999

# Agent behaviour
HISTORY_LIMIT = 20
SUMMARIZE_EVERY = 10
MAX_TOOL_ITERATIONS = 30

# Executor budgets (per agent-run)
EXECUTOR_MAX_ITER_SIMPLE = _int("EXECUTOR_MAX_ITER_SIMPLE", 8)
EXECUTOR_MAX_ITER_DEEP = _int("EXECUTOR_MAX_ITER_DEEP", 20)
EXECUTOR_MAX_ITER_DEFAULT = _int("EXECUTOR_MAX_ITER_DEFAULT", 15)
EXECUTOR_MAX_ITER_HARD_CAP = _int("EXECUTOR_MAX_ITER_HARD_CAP", 20)
EXECUTOR_TOOL_TIMEOUT_SEC = _int("EXECUTOR_TOOL_TIMEOUT_SEC", 60)
EXECUTOR_EXACT_DUPLICATE_LIMIT = _int("EXECUTOR_EXACT_DUPLICATE_LIMIT", 5)

# Cross-turn cooldown for system-changing tools (agent_update, os_update,
# agent_restart, display_restart). Prevents the LLM from auto-firing a
# second update right after a fresh user message.
TOOL_COOLDOWN_SYSTEM_SEC = _int("TOOL_COOLDOWN_SYSTEM_SEC", 300)

# Approval gate (techspec §1) — how long a pending yellow/red-zone
# confirmation stays armed before it's considered stale and discarded.
APPROVAL_TTL_HOURS = float(os.getenv("APPROVAL_TTL_HOURS", "1"))

# System monitoring
MONITOR_INTERVAL = 600
CPU_ALERT_PCT = 75
RAM_ALERT_PCT = 85
DISK_ALERT_PCT = 90
TEMP_ALERT_C = 80
ALERT_COOLDOWN_SEC = 3600

# Skills
DEFAULT_CITY = os.getenv("DEFAULT_CITY", "Dublin")
MUSIC_PLAYLIST = os.getenv("MUSIC_PLAYLIST", "")
HOME_ADDRESS = os.getenv("HOME_ADDRESS", "")

# Holidays — ISO 3166-1 country code for the `holidays` library used at
# wrapup. Derived from DEFAULT_CITY via a small lookup table; override
# directly with HOLIDAY_COUNTRY env var.
_CITY_COUNTRY = {
    "dublin": "IE", "cork": "IE", "galway": "IE",
    "moscow": "RU", "saint petersburg": "RU", "novosibirsk": "RU",
    "kyiv": "UA", "kiev": "UA",
    "minsk": "BY",
    "warsaw": "PL", "krakow": "PL",
    "berlin": "DE", "munich": "DE",
    "london": "GB", "manchester": "GB",
    "paris": "FR", "lyon": "FR",
    "amsterdam": "NL",
    "new york": "US", "los angeles": "US", "san francisco": "US", "chicago": "US",
    "toronto": "CA", "vancouver": "CA",
    "sydney": "AU", "melbourne": "AU",
    "tokyo": "JP",
    "tel aviv": "IL", "jerusalem": "IL",
    "istanbul": "TR",
    "madrid": "ES", "barcelona": "ES",
    "rome": "IT", "milan": "IT",
    "lisbon": "PT",
}
HOLIDAY_COUNTRY = os.getenv("HOLIDAY_COUNTRY") or _CITY_COUNTRY.get(DEFAULT_CITY.strip().lower(), "")

# Timezone — IANA name for the same DEFAULT_CITY. Used at process startup
# to force libc's TZ regardless of how systemd/locale is configured on
# the host. Override directly with TIMEZONE env var. Empty string means
# "use whatever the OS says".
_CITY_TZ = {
    "dublin": "Europe/Dublin", "cork": "Europe/Dublin", "galway": "Europe/Dublin",
    "moscow": "Europe/Moscow", "saint petersburg": "Europe/Moscow",
    "novosibirsk": "Asia/Novosibirsk",
    "kyiv": "Europe/Kyiv", "kiev": "Europe/Kyiv",
    "minsk": "Europe/Minsk",
    "warsaw": "Europe/Warsaw", "krakow": "Europe/Warsaw",
    "berlin": "Europe/Berlin", "munich": "Europe/Berlin",
    "london": "Europe/London", "manchester": "Europe/London",
    "paris": "Europe/Paris", "lyon": "Europe/Paris",
    "amsterdam": "Europe/Amsterdam",
    "new york": "America/New_York", "los angeles": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles", "chicago": "America/Chicago",
    "toronto": "America/Toronto", "vancouver": "America/Vancouver",
    "sydney": "Australia/Sydney", "melbourne": "Australia/Melbourne",
    "tokyo": "Asia/Tokyo",
    "tel aviv": "Asia/Jerusalem", "jerusalem": "Asia/Jerusalem",
    "istanbul": "Europe/Istanbul",
    "madrid": "Europe/Madrid", "barcelona": "Europe/Madrid",
    "rome": "Europe/Rome", "milan": "Europe/Rome",
    "lisbon": "Europe/Lisbon",
}
TIMEZONE = os.getenv("TIMEZONE") or _CITY_TZ.get(DEFAULT_CITY.strip().lower(), "")


def apply_timezone() -> str:
    """Apply TIMEZONE to libc and the TZ env var so every `datetime.now()`
    in this process — and subprocesses that inherit env — returns the
    expected local time regardless of how the host's /etc/localtime or
    systemd timezone is configured.

    Returns the TZ name that was applied (or empty string if none)."""
    if not TIMEZONE:
        return ""
    os.environ["TZ"] = TIMEZONE
    try:
        import time as _t
        _t.tzset()
    except Exception:
        pass
    return TIMEZONE


def now_local():
    """Return current local datetime as a naive datetime object.

    Prefers zoneinfo (Python 3.9+) with the configured TIMEZONE so the
    result is always correct regardless of whether apply_timezone() ran
    or whether the process inherited the right TZ env var. Falls back
    to datetime.now() if no timezone is configured or zoneinfo fails.
    """
    from datetime import datetime as _dt
    if TIMEZONE:
        try:
            from zoneinfo import ZoneInfo
            return _dt.now(ZoneInfo(TIMEZONE)).replace(tzinfo=None)
        except Exception:
            pass
    return _dt.now()

# News
NEWS_LOCATION = os.getenv("NEWS_LOCATION", "Dublin")
NEWS_COUNT = _int("NEWS_COUNT", 3)

# Stickers
RUBEDO_STICKER_SET = os.getenv("RUBEDO_STICKER_SET", "")

# Display
ENABLE_DISPLAY = os.getenv("ENABLE_DISPLAY", "0") == "1"
DISPLAY_W = _int("DISPLAY_W", 800)
DISPLAY_H = _int("DISPLAY_H", 1280)
CONSOLE_H = _int("CONSOLE_H", 480)

# Search
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# Day engine
WAKE_TIME = os.getenv("WAKE_TIME", "09:00")
SLEEP_TIME = os.getenv("SLEEP_TIME", "23:30")
WRAPUP_TIME = os.getenv("WRAPUP_TIME", "23:00")
WORK_START = os.getenv("WORK_START", "09:00")
WORK_END = os.getenv("WORK_END", "21:00")
BUFFER_MINUTES = _int("BUFFER_MINUTES", 15)

# Check-in
MAX_NUDGES_TASK = _int("MAX_NUDGES_TASK", 2)
NUDGE_COOLDOWN = _int("NUDGE_COOLDOWN", 30)  # minutes

# Pool tasks (untimed backlog with priority-based reminder cadence).
# Cadence (days) per priority; priority 5 fires every weekday regardless.
POOL_CADENCE_DAYS = {
    1: _int("POOL_CADENCE_P1", 30),
    2: _int("POOL_CADENCE_P2", 14),
    3: _int("POOL_CADENCE_P3", 7),
    4: _int("POOL_CADENCE_P4", 3),
    5: 0,  # weekday-driven, see day/pool.py
}
POOL_QUIET_START = os.getenv("POOL_QUIET_START", "11:00")  # no nudges before
POOL_QUIET_END = os.getenv("POOL_QUIET_END", "21:00")      # no nudges after
POOL_MAX_NUDGES_PER_DAY = _int("POOL_MAX_NUDGES_PER_DAY", 1)
POOL_TICK_INTERVAL_MIN = _int("POOL_TICK_INTERVAL_MIN", 60)

# Rubedo queue (autonomous task execution)
QUEUE_IDLE_MINUTES = _int("QUEUE_IDLE_MINUTES", 15)
QUEUE_TASK_TIMEOUT_SEC = _int("QUEUE_TASK_TIMEOUT_SEC", 300)
QUEUE_QUIET_START = os.getenv("QUEUE_QUIET_START", "00:00")
QUEUE_QUIET_END = os.getenv("QUEUE_QUIET_END", WAKE_TIME)
# If a scheduled task is this many hours past due, reschedule it to run at next idle slot
QUEUE_SCHEDULED_EXPIRY_HOURS = _int("QUEUE_SCHEDULED_EXPIRY_HOURS", 6)

# TTS
TTS_ENABLED = os.getenv("TTS_ENABLED", "false").lower() == "true"
TTS_VOICE = os.getenv("TTS_VOICE", "ru-RU-SvetlanaNeural")
ALARM_SOUND = os.getenv("ALARM_SOUND", "")

# System prefix for proactive messages
SYSTEM_PREFIX = "[RUBeOS]"

# Launcher exit codes
RESTART_CODE = 42
UPDATE_CODE = 43
