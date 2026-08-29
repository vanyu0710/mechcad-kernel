"""Runtime compatibility helpers for third-party CAD dependencies."""

from __future__ import annotations

import glob as _glob_module
import importlib
import importlib.util
from pathlib import Path
from typing import Any


def _font_safe_glob(original: Any):
    """Return a glob function that ignores truncated Windows font files.

    build123d 0.11.1 scans Windows fonts during import and assumes every TTF/OTF
    file is valid. Some Windows images contain zero-byte placeholder fonts, which
    should not prevent the CAD kernel from starting.
    """

    def safe_glob(pattern: str, *args: Any, **kwargs: Any) -> list[str]:
        paths = original(pattern, *args, **kwargs)
        if not any(token in pattern.lower() for token in ("ttf", "otf", "ttc")):
            return paths
        valid: list[str] = []
        for path in paths:
            try:
                if Path(path).stat().st_size >= 1024:
                    valid.append(path)
            except (OSError, ValueError):
                continue
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
