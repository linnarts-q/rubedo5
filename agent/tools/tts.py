"""TTS tool (stage 9.5) — green zone, local speaker output only (no
external side effects). Calls tts.engine.speak_async directly rather
than the sync speak() wrapper, so a several-second TTS playback
doesn't block the event loop other concurrent sessions (§2 phase 2)
run on.
"""
from __future__ import annotations


async def speak(text: str) -> str:
    from tts.engine import speak_async
    await speak_async(text)
    return "Сказала вслух."
