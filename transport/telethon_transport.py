"""TelethonTransport — the real channel (§2 phase 2, stage 7.5),
adapted from rubedo4's interface/telegram.py. Adapter, not copy: the
reply-context text prefix rubedo4 used to help the LLM guess ("[В ответ
на сообщение: «...»]") is gone — `reply_to_msg_id` is extracted as a
real id and passed through untouched; agent/routing.py already knows
what to do with it deterministically (memory.db.message_bindings), it
never needed the LLM to read a hint out of the text.

Message splitting, sticker replies, and MarkdownV2 escaping are kept as
they were — presentation, not architecture, and rubedo4 already had
these right.

Untestable in this sandbox: no network, no real account, and Telethon
needs an interactive phone-number login on first run anyway. This is
the one piece that only gets a real smoke test on the mini-PC itself —
everything it plugs into (routing, bindings, crash-resume) is already
proven end-to-end against transport/local.py's LocalTransport.

Voice/photo/document handling from rubedo4's interface/telegram.py
isn't ported here — stage 7.5's scope is the text pipeline, reply
routing, and the live process itself. An explicit, visible gap, not a
silent one; a natural follow-up once this lands.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re

from config import TELEGRAM_API_ID, TELEGRAM_API_HASH, OWNER_USER_ID

log = logging.getLogger("rubedo.transport.telethon")

_SEP_RE = re.compile(r"\n[ \t]*-{3,}[ \t]*(?:\n|$)")
_STRIKETHROUGH_RE = re.compile(r"~[^~\n]+~")
_MD2_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")

_RECONNECT_SECS = 15 * 60


def _to_md2(text: str) -> str:
    """Escape for MarkdownV2 while preserving ~strikethrough~ spans."""
    result = []
    last = 0
    for m in _STRIKETHROUGH_RE.finditer(text):
        before = text[last:m.start()]
        result.append(_MD2_ESCAPE_RE.sub(r"\\\1", before))
        inner = _MD2_ESCAPE_RE.sub(r"\\\1", m.group(0)[1:-1])
        result.append("~" + inner + "~")
        last = m.end()
    result.append(_MD2_ESCAPE_RE.sub(r"\\\1", text[last:]))
    return "".join(result)


def _split_reply(text: str) -> list[str]:
    """Split a reply into separate messages: first on --- separators,
    then by paragraph."""
    text = text.strip().replace("\r\n", "\n").replace("\r", "\n")
    if not text:
        return [text]
    if _SEP_RE.search(text):
        sections = [p.strip().lstrip("-").strip() for p in _SEP_RE.split(text) if p.strip().lstrip("-").strip()]
    else:
        sections = [text]
    parts: list[str] = []
    for section in sections:
        paras = [p.strip() for p in re.split(r"\n{2,}", section) if p.strip()]
        if len(paras) > 1:
            parts.extend(paras)
        else:
            parts.append(section)
    return [p for p in parts if p]


class TelethonTransport:
    def __init__(self) -> None:
        # Lazy import: telethon is a real, heavy dependency only needed
        # to actually run a live connection, not to import this module
        # (agent/tools that reference the transport type, or tests that
        # import interface/telegram.py, shouldn't require it installed).
        from telethon import TelegramClient
        self.client = TelegramClient("rubedo", TELEGRAM_API_ID, TELEGRAM_API_HASH)

    async def send(self, text: str) -> int | None:
        """Splits long/sectioned replies into several messages (kept
        from rubedo4) — returns the LAST message's id, since that's
        the one a reply would realistically attach to."""
        parts = _split_reply(text)
        last_id = None
        for i, part in enumerate(parts):
            if i > 0:
                await asyncio.sleep(random.uniform(1.0, 2.0))
            if _STRIKETHROUGH_RE.search(part):
                msg = await self.client.send_message(OWNER_USER_ID, _to_md2(part), parse_mode="MarkdownV2")
            else:
                msg = await self.client.send_message(OWNER_USER_ID, part)
            last_id = msg.id
        return last_id

    async def send_file(self, path: str) -> int | None:
        msg = await self.client.send_file(OWNER_USER_ID, path, force_document=True)
        return msg.id

    async def send_photo(self, path: str) -> int | None:
        msg = await self.client.send_file(OWNER_USER_ID, path, force_document=False)
        return msg.id

    async def run(self, on_message) -> None:
        """`on_message(event)` is awaited for every incoming text
        message from the owner, normalized to
        `{"text": str, "reply_to_message_id": int | None}`."""
        from telethon import events

        @self.client.on(events.NewMessage(incoming=True))
        async def _handler(event):
            sender = await event.get_sender()
            if not sender or sender.id != OWNER_USER_ID:
                return
            text = event.raw_text.strip()
            if not text:
                return
            reply_to_message_id = None
            if event.reply_to:
                reply_to_message_id = event.reply_to.reply_to_msg_id
            await on_message({"text": text, "reply_to_message_id": reply_to_message_id})

        first = True
        while True:
            try:
                if first:
                    await self.client.start()
                    first = False
                else:
                    await self.client.connect()
                log.info("Telethon transport connected")
                await self.client.run_until_disconnected()
                log.warning("Telethon disconnected")
            except Exception as e:
                log.error(f"Telethon connection error: {e}")
            log.info(f"Reconnecting in {_RECONNECT_SECS // 60} min...")
            await asyncio.sleep(_RECONNECT_SECS)
