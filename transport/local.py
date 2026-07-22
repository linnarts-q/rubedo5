"""LocalTransport — a file/stdin-shaped stand-in for a real channel
(§2 phase 2, stage 7.5). Exists so the entire pipeline — reply routing,
message_bindings, crash-resume's "продолжить?" — can be exercised end
to end in the sandbox (or in a real terminal) without a live Telegram
connection. `feed()`/`sent` are the whole test surface: push an
incoming message in, read what came back out.

Assigns sequential integer message ids starting from 1, exactly like a
real channel would — reply-binding tests need ids that behave like
ids, not None everywhere.
"""
from __future__ import annotations

import asyncio


class LocalTransport:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._next_id = 1
        self.incoming: asyncio.Queue = asyncio.Queue()

    async def send(self, text: str) -> int:
        mid = self._next_id
        self._next_id += 1
        self.sent.append({"id": mid, "text": text})
        return mid

    async def send_file(self, path: str) -> int:
        return await self.send(f"[файл] {path}")

    async def send_photo(self, path: str) -> int:
        return await self.send(f"[фото] {path}")

    async def feed(self, text: str, reply_to_message_id: int | None = None) -> None:
        """Enqueue an incoming message as if it arrived over the wire —
        the CLI/test-side entry point."""
        await self.incoming.put({"text": text, "reply_to_message_id": reply_to_message_id})

    async def next_incoming(self) -> dict | None:
        """None if nothing's queued — non-blocking, so a driving loop
        can poll it alongside other ticks instead of awaiting forever."""
        if self.incoming.empty():
            return None
        return await self.incoming.get()
