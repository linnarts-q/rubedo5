from __future__ import annotations
from dataclasses import dataclass, field, asdict
import json
import time


@dataclass
class Event:
    ts: float = field(default_factory=time.time)

    def to_json(self) -> str:
        d = asdict(self)
        d["__type__"] = type(self).__name__
        return json.dumps(d, ensure_ascii=False)

    @staticmethod
    def from_json(raw: str) -> "Event":
        d = json.loads(raw)
        name = d.pop("__type__", None)
        cls = _REGISTRY.get(name)
        if cls is None:
            raise ValueError(f"Unknown event: {name}")
        return cls(**d)


# ─── Message / Agent lifecycle ────────────────────────────────

@dataclass
class MessageReceived(Event):
    user_id: int = 0
    session_id: str = ""
    text: str = ""

@dataclass
class AgentStarted(Event):
    session_id: str = ""

@dataclass
class AgentThinking(Event):
    session_id: str = ""

@dataclass
class AgentFinished(Event):
    session_id: str = ""
    reply: str = ""

@dataclass
class AgentReplied(Event):
    session_id: str = ""

@dataclass
class AgentIdle(Event):
    session_id: str = ""

@dataclass
class AgentError(Event):
    session_id: str = ""
    error: str = ""


# ─── Tool lifecycle ───────────────────────────────────────────

@dataclass
class ToolCalled(Event):
    name: str = ""
    args_preview: str = ""

@dataclass
class ToolFinished(Event):
    name: str = ""
    success: bool = True

@dataclass
class WorkStarted(Event):
    session_id: str = ""

@dataclass
class WorkCompleted(Event):
    session_id: str = ""


# ─── Task events ──────────────────────────────────────────────

@dataclass
class TaskAdded(Event):
    task_id: int = 0
    title: str = ""

@dataclass
class TaskStarted(Event):
    task_id: int = 0

@dataclass
class TaskCompleted(Event):
    task_id: int = 0
    result: str = ""


# ─── Day engine events ────────────────────────────────────────

@dataclass
class BriefingStarted(Event):
    pass

@dataclass
class BriefingDone(Event):
    pass

@dataclass
class DayPlanUpdated(Event):
    date: str = ""

@dataclass
class QueueUpdated(Event):
    pass

@dataclass
class DriftDetected(Event):
    overdue_count: int = 0

@dataclass
class WeekPlanUpdated(Event):
    week_of: str = ""


# ─── System ───────────────────────────────────────────────────

@dataclass
class SystemReady(Event):
    pass

@dataclass
class SystemShutdown(Event):
    reason: str = ""

@dataclass
class LLMExhausted(Event):
    session_id: str = ""

@dataclass
class LLMRetrying(Event):
    session_id: str = ""
    key_index: int = 0

@dataclass
class TemperatureWarning(Event):
    celsius: float = 0.0


# ─── Display ──────────────────────────────────────────────────

@dataclass
class DisplayReady(Event):
    pass

@dataclass
class DisplaySleeping(Event):
    pass

@dataclass
class DisplayRestartRequested(Event):
    """Request the display process to exit (launcher will restart it)."""
    pass


# ─── Alarm / wake flow ────────────────────────────────────────

@dataclass
class AlarmStarted(Event):
    """Telegram-side started the alarm sound loop; display switches to
    alarm screen and counts user taps until dismissed."""
    pass

@dataclass
class AlarmDismissed(Event):
    """Display side reports the user dismissed the alarm; telegram
    stops the sound loop."""
    pass

@dataclass
class PreAlarmWake(Event):
    """5 min before WAKE_TIME — wake the display from sleep_mode so the
    user is visible by the time the alarm sound fires."""
    pass


# ─── Remote display control ───────────────────────────────────

@dataclass
class SleepRequested(Event):
    """`/sleep_on` or `/sleep_off` from chat. `mode` is "on" or "off"."""
    mode: str = "off"


# ─── Update lifecycle ─────────────────────────────────────────

@dataclass
class UpdateDone(Event):
    """Launcher just finished `git pull + pip install` and restarted
    children. Telegram interface sends the "обновление завершено"
    notice when it sees this."""
    pass


_REGISTRY: dict[str, type[Event]] = {
    cls.__name__: cls for cls in [
        MessageReceived, AgentStarted, AgentThinking, AgentFinished,
        AgentReplied, AgentIdle, AgentError,
        ToolCalled, ToolFinished, WorkStarted, WorkCompleted,
        TaskAdded, TaskStarted, TaskCompleted,
        BriefingStarted, BriefingDone, DayPlanUpdated, QueueUpdated, DriftDetected, WeekPlanUpdated,
        SystemReady, SystemShutdown, LLMExhausted, LLMRetrying,
        TemperatureWarning, DisplayReady, DisplaySleeping,
        DisplayRestartRequested,
        AlarmStarted, AlarmDismissed, PreAlarmWake,
        SleepRequested, UpdateDone,
    ]
}
