"""Telegram interface (§2 phase 2, stage 7.5 — "transport and the live
process"). Owns the transport (transport/telethon_transport.py) and the
day-engine tick loop; agent/controller.py and day/tick.py know nothing
about either one. Adapter over rubedo4's interface/telegram.py, not a
copy — direction of dependency is reversed: rubedo4 called
`tick(self.tg, owner_id, self.bus)` directly; here `day/tick.py` only
ever sees a plain `send_fn`, never the transport itself (§17 — the day
engine doesn't send messages, it only ever goes through
agent/notify.py's severity gate).

Each incoming message is handled as its own asyncio Task, not queued
through a single consumer the way rubedo4 did — genuinely concurrent
handling is the entire point of §2 phase 2 (agent/scheduler.py,
memory/writer.py's single-writer lock, the LLM semaphore); serializing
messages here would silently defeat all of it.

Deliberately not ported from rubedo4 in this pass — explicit gaps, not
silent ones:
  - voice/photo/document handling (transport/telethon_transport.py's
    scope is text-only for now)
  - the human-cadence touches (typing delay, read-acknowledge, a
    bounded queue with backpressure) — presentation, not architecture
  - display/window.py (a separate optional process, gated by
    ENABLE_DISPLAY same as rubedo4 — launcher.py leaves a slot for it,
    the window itself isn't ported)
Each is a natural, self-contained follow-up once this lands.
"""
from __future__ import annotations

import asyncio
import logging

from config import OWNER_USER_ID
from bus.client import BusClient
from transport.telethon_transport import TelethonTransport

log = logging.getLogger("rubedo.interface.telegram")

_TICK_INTERVAL_SEC = 60
_HEARTBEAT_INTERVAL_SEC = 60


async def _tick_loop(transport: TelethonTransport) -> None:
    import day.tick as tick

    await asyncio.sleep(5)
    while True:
        try:
            await tick.run_day_tick(transport.send)
        except Exception as e:
            log.error(f"day tick error: {e}")
        await asyncio.sleep(_TICK_INTERVAL_SEC)


async def _heartbeat_loop() -> None:
    from memory.db import agent_heartbeat

    while True:
        try:
            agent_heartbeat()
        except Exception as e:
            log.error(f"heartbeat error: {e}")
        await asyncio.sleep(_HEARTBEAT_INTERVAL_SEC)


async def _handle_incoming(transport: TelethonTransport, bus: BusClient, event: dict) -> None:
    from agent.controller import handle_message

    try:
        await handle_message(
            user_id=OWNER_USER_ID,
            text=event["text"],
            bus_client=bus,
            send_fn=transport.send,
            send_file_fn=transport.send_file,
            send_photo_fn=transport.send_photo,
            reply_to_message_id=event.get("reply_to_message_id"),
        )
    except Exception as e:
        log.exception(f"handle_message failed: {e}")
        try:
            await transport.send("Что-то пошло не так.")
        except Exception:
            pass


async def main() -> None:
    from memory.db import init_db
    from agent import crash_recovery, notify

    init_db()

    transport = TelethonTransport()
    bus = BusClient()

    # Crash recovery (§2 phase 2) — before anything else touches a
    # session. Whatever chat-origin session the last crash orphaned
    # gets its one "продолжить?" here, at "critical" severity (see
    # agent/crash_recovery.py's own note on why not "normal") — through
    # the same severity-gate + bind pipe every other message goes
    # through.
    crash_msg = crash_recovery.recover_after_crash()
    if crash_msg:
        await notify.deliver("critical", crash_msg, transport.send, source="crash_recovery")

    async def on_message(event: dict) -> None:
        asyncio.create_task(_handle_incoming(transport, bus, event))

    asyncio.create_task(_tick_loop(transport))
    asyncio.create_task(_heartbeat_loop())
    await transport.run(on_message)


if __name__ == "__main__":
    import os
    from logging.handlers import RotatingFileHandler

    os.makedirs("logs", exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)

    rotate = RotatingFileHandler(
        "logs/rubedo.log", maxBytes=1024 * 1024, backupCount=20, encoding="utf-8",
    )
    rotate.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(console)
    root.addHandler(rotate)

    asyncio.run(main())
