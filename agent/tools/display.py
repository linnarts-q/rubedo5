"""Display background tool (§19). Green zone — restricted to
workspace/backgrounds/ so there's nothing to approve: she can only ever
point the display at a file inside her own sandboxed folder, sourced
through her existing green capabilities (web_search/file_download,
run_code with matplotlib/PIL) — no new fetch/generate mechanism here,
just the pointer + validation + persistence.
"""
from __future__ import annotations

from pathlib import Path

_BACKGROUNDS_DIR = Path("workspace/backgrounds")

# Below this, a background would be visibly blown up scaling to the
# display's 800x1280 default (config.DISPLAY_W/H) — reject rather than
# silently show something blurry.
_MIN_W = 400
_MIN_H = 400


def _resolve_background_path(filename: str) -> Path:
    if filename.startswith("/"):
        raise ValueError("Путь должен быть внутри workspace/backgrounds/, не абсолютным")
    resolved = (_BACKGROUNDS_DIR / filename).resolve()
    backgrounds_resolved = _BACKGROUNDS_DIR.resolve()
    if not (
        resolved == backgrounds_resolved
        or str(resolved).startswith(str(backgrounds_resolved) + "/")
    ):
        raise ValueError(f"Путь '{filename}' выходит за пределы workspace/backgrounds/")
    return resolved


def set_background(path: str) -> str:
    """Set the display's background image (§19). path is relative to
    workspace/backgrounds/ — get an image there first (web_download or
    run_code with matplotlib/PIL), then point the display at it."""
    try:
        resolved = _resolve_background_path(path)
    except ValueError as e:
        return f"Ошибка: {e}"

    if not resolved.exists():
        return f"Файл не найден: {resolved}"

    try:
        from PIL import Image
    except ImportError:
        return "PIL не установлен: pip install Pillow"

    try:
        with Image.open(resolved) as img:
            img.verify()
        with Image.open(resolved) as img:
            w, h = img.size
            fmt = img.format
    except Exception as e:
        return f"Не удалось прочитать изображение: {e}"

    if w < _MIN_W or h < _MIN_H:
        return f"Разрешение {w}x{h} слишком маленькое (минимум {_MIN_W}x{_MIN_H})."

    from memory.db import save_meta
    save_meta("display_background", str(resolved))
    return f"Фон дисплея обновлён: {resolved.name} ({w}x{h}, {fmt})."
