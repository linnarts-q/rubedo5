"""TTS engine (stage 9.5) — ported from rubedo4 as-is, per the audit's
own instruction ("как есть, без редизайна"): mechanism, not
architecture. edge-tts primary, pyttsx3 fallback if it's not installed
or fails.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
from config import TTS_VOICE

log = logging.getLogger("rubedo.tts")


async def speak_async(text: str) -> None:
    """TTS via edge-tts, fallback to pyttsx3."""
    try:
        import edge_tts
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            out_path = f.name
        try:
            communicate = edge_tts.Communicate(text, TTS_VOICE)
            await communicate.save(out_path)
            for player in ("mpg123", "ffplay", "aplay"):
                try:
                    await asyncio.to_thread(
                        subprocess.run, [player, "-q", out_path], timeout=60, check=True,
                    )
                    return
                except (FileNotFoundError, subprocess.CalledProcessError):
                    continue
            log.warning("No audio player found (tried mpg123, ffplay, aplay)")
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass
    except ImportError:
        log.warning("edge-tts not installed, trying pyttsx3")
        await asyncio.to_thread(_speak_pyttsx3, text)
    except Exception as e:
        log.warning(f"edge-tts failed: {e}, trying pyttsx3")
        await asyncio.to_thread(_speak_pyttsx3, text)


def speak(text: str) -> None:
    """Sync wrapper — call from asyncio.to_thread."""
    try:
        asyncio.run(speak_async(text))
    except Exception as e:
        log.warning(f"speak() failed: {e}")
        _speak_pyttsx3(text)


def _speak_pyttsx3(text: str) -> None:
    try:
        import pyttsx3
        engine = pyttsx3.init()
        engine.setProperty("rate", 160)
        engine.say(text)
        engine.runAndWait()
    except Exception as e:
        log.warning(f"pyttsx3 also failed: {e}")
