"""Message routing for pending approval/ask_user halts (§2 phase 2,
rollout step 3). Answers one question: when the owner sends a plain
message while one or more tool calls are waiting on her (agent/
executor.py's ask_user/approval halts), which one is this message
actually answering?

The waiting universe is every pending "approval" or "ask_user" hanging
item (agent/hanging.py) — NOT every 'waiting_user' task session. Most
yellow/red-zone confirmations happen on the "simple" route, which never
opens a task session at all (agent/controller.py only sessions "deep"
work); agent.sessions.wait_user()/resume()/etc. are all safe no-ops for
task_session_id=None, exactly like pause()/complete()/fail() always
were, so a sessionless approval is just as real a "waiting thing" as a
sessioned one — routing has to see both.

Under phase 1 there was at most one pending item ever, so agent/
approval.py and agent/questions.py each independently checking "is
there a pending item of MY kind" was already a correct, if implicit,
router. Phase 2 breaks that: an approval and an ask_user question (each
possibly on a different session, or no session at all) can be pending
at the same time — a bare "да" is ambiguous between them.

Resolution order, cheapest and most deterministic first (spec's own
priority, techspec §2 "Маршрутизация сообщений"):
  1. Reply-to-message binding (memory.db.message_bindings) — no LLM, no
     ambiguity, Lin said so explicitly by replying to that exact
     message. Only resolves items that do have a session (a binding is
     message_id -> session_id; a sessionless approval has nothing to
     bind to). Stays a no-op contract until the interface layer (not
     yet ported — rubedo-map area 1.5) actually writes bindings.
  2. Exactly one item pending -> that one, question closed.
  3. Two or more pending -> a fast-tier classify pass ("which of these
     briefly-described things is this about?").
  4. No confidence -> an honest question back to Lin, flat numbered
     text rather than inline buttons (no interface layer to render
     them yet either) — "1 — бэкапы, 2 — парсер". Her reply to THAT
     re-enters this exact same resolution path on the next message;
     nothing extra needs to be stored, since the same pending items are
     still there to disambiguate against, now with much stronger
     signal in the text itself.

Kind-specific validation (is "да" actually a recognizable yes/no? does
the ask_user answer need history replay?) stays owned by agent/
approval.py and agent/questions.py — this module only decides *which*
pending item a message is about, never whether the text resolves it.
"""
from __future__ import annotations

import json
import logging

from agent import hanging, sessions
from llm.groq import chat as groq_chat
from memory.db import message_binding_get

log = logging.getLogger("rubedo.agent.routing")

_BRIEF_LEN = 100
_KINDS = ("approval", "ask_user")


def pending_items() -> list[dict]:
    """Every pending approval/ask_user hanging row, oldest first (a
    stable, deterministic order for indexing in the disambiguation
    prompt and the numbered question). Sweeps TTL-expired items first
    (agent.hanging.list_pending), same as approval.pending()/
    questions.pending() always did."""
    items: list[dict] = []
    for kind in _KINDS:
        items.extend(hanging.list_pending(kind))
    items.sort(key=lambda r: r["id"])
    return items


def _payload(item: dict) -> dict:
    try:
        return json.loads(item["payload"])
    except Exception:
        return {}


def _brief(item: dict) -> str:
    payload = _payload(item)
    detail = (payload.get("question") or payload.get("preview") or "").strip()
    detail = detail.replace("\n", " ")[:_BRIEF_LEN]
    title = ""
    tsid = item.get("task_session_id")
    if tsid is not None:
        s = sessions.get(tsid)
        if s:
            title = s["title"]
    label = title or item["kind"]
    return f"{label}" + (f" — {detail}" if detail else "")


def _target(item: dict) -> dict:
    return {
        "session_id": item.get("task_session_id"),
        "kind": item["kind"],
        "hanging_id": item["id"],
        "payload": _payload(item),
    }


async def _classify_target(text: str, items: list[dict]) -> int | None:
    """Fast-tier disambiguation (§12 fast tier — Groq, temperature 0).
    Returns a 0-based index into `items`, or None on low confidence or
    any failure — callers treat both as "ask honestly", never guess
    further downstream."""
    listing = "\n".join(f"{i + 1}. {_brief(it)}" for i, it in enumerate(items))
    prompt = (
        f"Дела, ожидающие ответа хозяина:\n{listing}\n\n"
        f"Сообщение хозяина: «{text}»\n\n"
        "Хозяин может ответить и номером («1», «второе»), и по смыслу "
        "вопроса. Определи, к какому делу относится сообщение. Если "
        "неясно — не угадывай.\n"
        'Верни строго JSON: {"index": <номер от 1, или null если неясно>}'
    )
    try:
        resp = await groq_chat(
            [
                {"role": "system", "content": "Ты определяешь адресата сообщения. Отвечай только JSON."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
        )
        raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(raw)
        idx = data.get("index")
        if isinstance(idx, int) and 1 <= idx <= len(items):
            return idx - 1
    except Exception as e:
        log.debug(f"routing disambiguation failed, asking honestly instead: {e}")
    return None


async def resolve(text: str, reply_to_message_id: int | None = None, send_fn=None) -> dict | None:
    """Returns the resolved target `{session_id, kind, hanging_id,
    payload}` for agent/controller.py to dispatch on (`session_id` is
    None for a sessionless approval), `{"handled": True}` if this call
    already sent an honest disambiguation question (nothing left for
    the caller to do but save the message and return), or `None` if
    nothing is pending at all — the caller should proceed with normal
    routing."""
    items = pending_items()
    if not items:
        return None

    if reply_to_message_id is not None:
        bound_sid = message_binding_get(reply_to_message_id)
        if bound_sid is not None:
            match = next((it for it in items if it.get("task_session_id") == bound_sid), None)
            if match:
                return _target(match)

    if len(items) == 1:
        return _target(items[0])

    idx = await _classify_target(text, items)
    if idx is not None:
        return _target(items[idx])

    if send_fn:
        lines = [f"{i + 1} — {_brief(it)}" for i, it in enumerate(items)]
        await send_fn("Это про что?\n" + "\n".join(lines))
    return {"handled": True}
