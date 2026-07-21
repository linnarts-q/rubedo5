"""Resource tags (§2 phase 2, day-engine 5.0 parallelism — rollout
step 2). Deterministic, coarse tags the scheduler (agent/scheduler.py)
uses to detect conflicts between concurrently-active sessions: no file
locks on the fly, no per-file granularity — "грубая гранулярность +
детерминизм лучше, чем умная система, которую слабая модель не
осилит."

Derived from the classifier's own tool_categories output (already
validated in agent/classifier.py) rather than as a second, independent
LLM-guessed field — this keeps tags always consistent with what a
session can actually touch, with zero extra LLM judgment calls to get
wrong. Over-tagging is cheap (two sessions wait a beat behind each
other unnecessarily); missing a real conflict is a race — same
"over-including is cheap, missing one isn't" principle
agent/classifier.py already applies to tool_categories itself.

Four tags: spotrent-server, workspace, core-repo, browser. "workspace"
is deliberately one coarse tag, not per-subfolder — subfolder-level
tagging would need predicting a specific path ahead of time, exactly
the kind of cleverness this design avoids.
"""
from __future__ import annotations

_CATEGORY_TAGS: dict[str, set[str]] = {
    "server": {"spotrent-server"},
    "files": {"workspace"},
    "agent_self": {"core-repo"},
    "web": {"browser"},
}


def tags_for_categories(categories: list[str]) -> list[str]:
    tags: set[str] = set()
    for cat in categories:
        tags |= _CATEGORY_TAGS.get(cat, set())
    return sorted(tags)
