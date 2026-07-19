"""Deterministic stop-phrase (techspec §15).

Checked as a plain string comparison — before classification, before
any LLM call of any kind — so it still works when the models are down,
rate-limited, or stuck in a bad loop. Effect: every pending/active
session pauses and all autonomous activity (day-engine nudges, the
idle agenda, the queue) freezes; only direct owner messages get a
response, until the resume phrase is seen (also a plain string check,
same reasoning).

Frozen state is a single flag in `meta`, not per-session, since at this
stage rubedo5 doesn't have task sessions yet (§2) — this predates that
work and will keep gating whatever supersedes it.
"""
from __future__ import annotations

import logging
from config import STOP_PHRASE, RESUME_PHRASE

log = logging.getLogger("rubedo.agent.stopword")

_META_FROZEN = "autonomy_frozen"


def is_stop_phrase(text: str) -> bool:
    if not STOP_PHRASE:
        return False
    return text.strip() == STOP_PHRASE


def is_resume_phrase(text: str) -> bool:
    if not RESUME_PHRASE:
        return False
    return text.strip() == RESUME_PHRASE


def freeze() -> None:
    from memory.db import save_meta
    save_meta(_META_FROZEN, "1")
    log.warning("Autonomy frozen via stop-phrase")


def unfreeze() -> None:
    from memory.db import save_meta
    save_meta(_META_FROZEN, "0")
    log.warning("Autonomy resumed via resume-phrase")


def is_frozen() -> bool:
    from memory.db import load_meta
    return load_meta(_META_FROZEN) == "1"
