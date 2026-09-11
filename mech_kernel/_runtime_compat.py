"""Runtime compatibility helpers for third-party CAD dependencies."""

from __future__ import annotations

import glob as _glob_module
import importlib
import importlib.util
from pathlib import Path
from typing import Any


def _parseable_font(path: str) -> bool:
    """True when fontTools can at least open the file (header + table index).

    A magic-number sniff alone is not enough: Windows ships metrics-only
    files whose sfnt version is garbage, and users install corrupt fonts.
    Lazy loading reads the directory, not glyph outlines, so this stays fast
    while rejecting everything build123d's TTFont call would crash on.
    """
    try:
        if Path(path).stat().st_size < 1024:
            return False
    except OSError:
        return False
    try:
        from fontTools.ttLib import TTFont, ttCollection
    except ImportError:
        return True
    handle = None
    try:
        if path.lower().endswith(".ttc"):
            handle = ttCollection.TTCollection(path)
        else:
            handle = TTFont(path, lazy=True)
    except Exception:
        return False
    finally:
        try:
            if handle is not None:
                handle.close()
        except Exception:
            pass
    return True


def _font_safe_glob(original: Any):
    """Return a glob function that ignores malformed Windows font files.

    build123d 0.11.1 scans Windows fonts during import and assumes every TTF/OTF
    file is valid. Some Windows images contain zero-byte placeholders and fonts
    with garbage payloads (bad sfntVersion), which should not prevent the CAD
    kernel from starting.
    """

    def safe_glob(pattern: str, *args: Any, **kwargs: Any) -> list[str]:
        paths = original(pattern, *args, **kwargs)
        if not any(token in pattern.lower() for token in ("ttf", "otf", "ttc")):
            return paths
        return [path for path in paths if _parseable_font(path)]

    return safe_glob


def ensure_build123d_import() -> None:
    """Import build123d once with a narrow workaround for malformed system fonts."""

    if importlib.util.find_spec("build123d") is None:
        return

    original = _glob_module.glob
    _glob_module.glob = _font_safe_glob(original)
    try:
        importlib.import_module("build123d")
    finally:
        _glob_module.glob = original
