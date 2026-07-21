from __future__ import annotations
import asyncio
import io
import logging
from openai import AsyncOpenAI, RateLimitError, AuthenticationError, BadRequestError, NotFoundError
from config import GROQ_API_KEYS, GROQ_BASE_URL, GROQ_MODEL, GROQ_FALLBACK_MODEL
from llm.exceptions import AllKeysExhausted
from llm.semaphore import llm_semaphore

log = logging.getLogger("rubedo.llm.groq")

_clients: list[AsyncOpenAI] = []
_key_idx: int = 0
_key_lock = asyncio.Lock()


def _get_clients() -> list[AsyncOpenAI]:
    global _clients
    if not _clients:
        if not GROQ_API_KEYS:
            raise AllKeysExhausted("No Groq API keys configured (GROQ_API_KEYS)")
        _clients = [
            AsyncOpenAI(api_key=key, base_url=GROQ_BASE_URL)
            for key in GROQ_API_KEYS
        ]
    return _clients


async def chat(
    messages: list,
    tools: list | None = None,
    temperature: float = 0.7,
    model: str | None = None,
    **kwargs,
):
    global _key_idx
    async with llm_semaphore:
        clients = _get_clients()
        n = len(clients)
        models_to_try = [model or GROQ_MODEL, GROQ_FALLBACK_MODEL]
        for try_model in models_to_try:
            async with _key_lock:
                start = _key_idx
            for offset in range(n):
                idx = (start + offset) % n
                try:
                    kw: dict = {"model": try_model, "messages": messages, "temperature": temperature}
                    if tools:
                        kw["tools"] = tools
                        kw["tool_choice"] = "auto"
                    kw.update(kwargs)
                    result = await clients[idx].chat.completions.create(**kw)
                    async with _key_lock:
                        _key_idx = (idx + 1) % n
                    return result
                except RateLimitError:
                    log.warning(f"[groq] Key #{idx} rate limited, rotating")
                except AuthenticationError:
                    log.warning(f"[groq] Key #{idx} auth error, rotating")
                except BadRequestError:
                    raise
                except NotFoundError:
                    log.warning(f"[groq] Model {try_model} not found, trying fallback")
                    break
                except Exception as e:
                    log.error(f"[groq] Key #{idx} error: {e}")
        raise AllKeysExhausted("All Groq API keys and models exhausted")


async def transcribe(audio_bytes: bytes) -> str:
    global _key_idx
    async with llm_semaphore:
        clients = _get_clients()
        n = len(clients)
        async with _key_lock:
            start = _key_idx
        for offset in range(n):
            idx = (start + offset) % n
            try:
                audio_file = io.BytesIO(audio_bytes)
                audio_file.name = "audio.ogg"
                result = await clients[idx].audio.transcriptions.create(
                    model="whisper-large-v3",
                    file=audio_file,
                )
                async with _key_lock:
                    _key_idx = (idx + 1) % n
                return result.text
            except RateLimitError:
                log.warning(f"[groq/whisper] Key #{idx} rate limited")
            except Exception as e:
                log.error(f"[groq/whisper] Key #{idx} error: {e}")
        raise AllKeysExhausted("All Groq keys exhausted for transcription")
