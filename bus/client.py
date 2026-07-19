from __future__ import annotations
import asyncio
import logging
from typing import Callable, Awaitable
from bus.events import Event

log = logging.getLogger("rubedo.bus")
HOST = "127.0.0.1"
PORT = 9999
RECONNECT_DELAY = 2.0

Handler = Callable[[Event], Awaitable[None]]


class BusClient:
    def __init__(self):
        self._writer: asyncio.StreamWriter | None = None
        self._handlers: list[Handler] = []
        self._running = False

    def subscribe(self, handler: Handler) -> "BusClient":
        self._handlers.append(handler)
        return self

    async def connect(self):
        self._running = True
        asyncio.create_task(self._loop())

    async def disconnect(self):
        self._running = False
        if self._writer:
            self._writer.close()

    async def publish(self, event: Event):
        if self._writer is None:
            return
        try:
            self._writer.write((event.to_json() + "\n").encode())
            await self._writer.drain()
        except Exception as e:
            log.warning(f"[BUS] Publish error: {e}")
            self._writer = None

    async def _loop(self):
        while self._running:
            try:
                reader, writer = await asyncio.open_connection(HOST, PORT)
                self._writer = writer
                log.info("[BUS] Connected")
                await self._read_loop(reader)
            except (ConnectionRefusedError, OSError):
                log.warning(f"[BUS] Connection failed, retry in {RECONNECT_DELAY}s")
            except Exception as e:
                log.warning(f"[BUS] Error: {e}")
            finally:
                self._writer = None
            if self._running:
                await asyncio.sleep(RECONNECT_DELAY)

    async def _read_loop(self, reader: asyncio.StreamReader):
        while self._running:
            line = await reader.readline()
            if not line:
                break
            raw = line.decode().strip()
            if not raw:
                continue
            try:
                event = Event.from_json(raw)
            except Exception as e:
                log.warning(f"[BUS] Bad event: {e}")
                continue
            for handler in self._handlers:
                try:
                    await handler(event)
                except Exception as e:
                    log.error(f"[BUS] Handler error: {e}")


class SyncPublisher:
    """Bridge from sync code (tools, threads) to async event loop."""

    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: BusClient | None = None

    def setup(self, loop: asyncio.AbstractEventLoop, client: BusClient):
        self._loop = loop
        self._client = client

    def publish(self, event: Event):
        if self._loop and self._client:
            asyncio.run_coroutine_threadsafe(self._client.publish(event), self._loop)


sync_publisher = SyncPublisher()
