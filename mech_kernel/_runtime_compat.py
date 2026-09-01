"""Runtime compatibility helpers for third-party CAD dependencies."""

from __future__ import annotations

import glob as _glob_module
import importlib
import importlib.util
from pathlib import Path
from typing import Any


def _font_safe_glob(original: Any):
    """Return a glob function that ignores malformed Windows font files.

    build123d 0.11.1 scans Windows fonts during import and assumes every TTF/OTF
    file is valid. Some Windows images contain zero-byte placeholders and fonts
    with garbage payloads (bad sfntVersion), which should not prevent the CAD
    kernel from starting.
    """

    sfnt_magics = (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf")

    def safe_glob(pattern: str, *args: Any, **kwargs: Any) -> list[str]:
        paths = original(pattern, *args, **kwargs)
        if not any(token in pattern.lower() for token in ("ttf", "otf", "ttc")):
            return paths
        valid: list[str] = []
        for path in paths:
            try:
                if Path(path).stat().st_size < 1024:
                    continue
                with open(path, "rb") as stream:
                    magic = stream.read(4)
            except (OSError, ValueError):
                continue
            if magic in sfnt_magics:
                valid.append(path)
        return valid

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
