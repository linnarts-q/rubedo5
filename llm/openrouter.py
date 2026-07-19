from __future__ import annotations
import asyncio
import logging
from openai import AsyncOpenAI, RateLimitError, AuthenticationError, BadRequestError, NotFoundError
from config import OPENROUTER_API_KEYS, OPENROUTER_BASE_URL, OPENROUTER_MODEL, OPENROUTER_FALLBACK_MODEL
from llm.exceptions import AllKeysExhausted

log = logging.getLogger("rubedo.llm.openrouter")

_clients: list[AsyncOpenAI] = []
_key_idx: int = 0
_key_lock = asyncio.Lock()


def _get_clients() -> list[AsyncOpenAI]:
    global _clients
    if not _clients:
        if not OPENROUTER_API_KEYS:
            raise AllKeysExhausted("No OpenRouter API keys configured (OPENROUTER_API_KEYS)")
        _clients = [
            AsyncOpenAI(
                api_key=key,
                base_url=OPENROUTER_BASE_URL,
                default_headers={
                    "HTTP-Referer": "https://github.com/linnarts-q/rubedo5",
                    "X-Title": "Rubedo",
                },
            )
            for key in OPENROUTER_API_KEYS
        ]
    return _clients


async def chat(
    messages: list,
    tools: list | None = None,
    temperature: float = 0.7,
    model: str | None = None,
    **kwargs,
):
    """Try every (model, key) combination once. Models tried in order:
    the caller's `model` (or default `OPENROUTER_MODEL`), then
    `OPENROUTER_FALLBACK_MODEL` if different. Keys round-robin within
    each model. Raises `AllKeysExhausted` only after the full grid is
    exhausted — no recursion, no chance of unbounded stack growth.
    """
    global _key_idx
    clients = _get_clients()
    primary = model or OPENROUTER_MODEL
    models = [primary]
    if OPENROUTER_FALLBACK_MODEL and OPENROUTER_FALLBACK_MODEL != primary:
        models.append(OPENROUTER_FALLBACK_MODEL)

    n = len(clients)
    async with _key_lock:
        start = _key_idx

    last_error: Exception | None = None
    for m in models:
        for offset in range(n):
            idx = (start + offset) % n
            try:
                kw: dict = {"model": m, "messages": messages, "temperature": temperature}
                if tools:
                    kw["tools"] = tools
                    kw["tool_choice"] = "auto"
                kw.update(kwargs)
                result = await clients[idx].chat.completions.create(**kw)
                async with _key_lock:
                    _key_idx = (idx + 1) % n
                return result
            except RateLimitError as e:
                log.warning(f"[openrouter] {m} key #{idx} rate limited, rotating")
                last_error = e
            except AuthenticationError as e:
                log.warning(f"[openrouter] {m} key #{idx} auth error, rotating")
                last_error = e
            except BadRequestError:
                raise
            except NotFoundError as e:
                # Whole model unavailable — no point trying remaining keys.
                log.warning(f"[openrouter] Model {m} not found, trying next model")
                last_error = e
                break
            except Exception as e:
                log.error(f"[openrouter] {m} key #{idx} error: {e}")
                last_error = e
    raise AllKeysExhausted(
        f"All OpenRouter models/keys exhausted (last error: {last_error})"
    )
