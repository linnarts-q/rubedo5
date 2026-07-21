"""Local bus relay (§2 phase 2, stage 7.5's own follow-up — display
needs it). Ported from rubedo4 verbatim; a bare local TCP fan-out so
separate OS processes (interface/telegram.py, display/window.py) can
share Event traffic without both connecting to the same in-process
BusClient list — they aren't the same process."""
from __future__ import annotations

import asyncio
import logging

from bus.events import Event

log = logging.getLogger("rubedo.bus")
HOST = "127.0.0.1"
PORT = 9999


class BusServer:
    def __init__(self):
        self._subscribers: set[asyncio.StreamWriter] = set()
        self._server: asyncio.Server | None = None

    async def start(self):
        self._server = await asyncio.start_server(self._handle_client, HOST, PORT)
        log.info(f"[BUS] Server started on {HOST}:{PORT}")

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        log.info("[BUS] Server stopped")

    async def publish(self, event: Event):
        await self._broadcast(event.to_json())

    async def _broadcast(self, line: str):
        dead = set()
        for writer in self._subscribers:
            try:
                writer.write((line + "\n").encode())
                await writer.drain()
            except Exception:
                dead.add(writer)
        self._subscribers -= dead

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        addr = writer.get_extra_info("peername")
        log.info(f"[BUS] Client connected: {addr}")
        self._subscribers.add(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                raw = line.decode().strip()
                if raw:
                    await self._broadcast(raw)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        except Exception as e:
            log.warning(f"[BUS] Client error {addr}: {e}")
        finally:
            self._subscribers.discard(writer)
            writer.close()
            log.info(f"[BUS] Client disconnected: {addr}")
