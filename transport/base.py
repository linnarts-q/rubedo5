"""Transport abstraction (§2 phase 2, stage 7.5 — "transport and the
live process"). Everything upstream (agent/controller.py, day/tick.py,
agent/queue_runner.py) only ever talks to an opaque async `send_fn`-
shaped callable — it never imports Telethon, never knows a live
connection exists at all. That's the direction §17 always meant
("day engine doesn't send messages itself"), just made literal: the
dependency points from transport towards the agent, never back.

`send()` returns the outgoing message's id when the channel has one.
That id is the missing link reply-to-message routing needed — nothing
before stage 7.5 ever wrote to memory.db.message_bindings, only read
from it (agent/routing.py). A caller tied to a task session captures
this id and binds it (see agent/notify.py's `deliver()`).

Two implementations:
  - TelethonTransport (transport/telethon_transport.py) — the real
    thing, adapted from rubedo4's interface/telegram.py.
  - LocalTransport (transport/local.py) — stdin/file-shaped, for
    testing the entire pipeline (routing, bindings, crash-resume)
    without a live Telegram connection at all.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Transport(Protocol):
    async def send(self, text: str) -> int | None: ...
    async def send_file(self, path: str) -> int | None: ...
    async def send_photo(self, path: str) -> int | None: ...
