"""Platform backends — one desktop contract, three implementations.

`workman.desktop` is the only module callers should import. This package holds
the per-OS implementations it dispatches to:

    linux_x11 : ffmpeg (x11grab) + xdotool          (the original backend)
    darwin    : Quartz/CoreGraphics, screencapture, osascript
    win32     : ctypes -> user32/gdi32 (SendInput, BitBlt, EnumWindows)

Selection is by `sys.platform`, overridable with WORKMAN_BACKEND=linux|darwin|win32
so a backend can be imported and unit-tested from any machine. Import is lazy and
cached: importing this package on Windows must not drag in the X11 module, and
vice versa.
"""
from __future__ import annotations

import importlib
import os
import sys
from types import ModuleType

_ALIASES = {
    "linux": "linux_x11",
    "linux_x11": "linux_x11",
    "x11": "linux_x11",
    "darwin": "darwin",
    "mac": "darwin",
    "macos": "darwin",
    "win32": "win32",
    "win": "win32",
    "windows": "win32",
}

_cache: dict[str, ModuleType] = {}


def backend_name() -> str:
    """Which backend this process will use, as a module name."""
    override = (os.environ.get("WORKMAN_BACKEND") or "").strip().lower()
    if override:
        if override not in _ALIASES:
            raise ValueError(
                f"WORKMAN_BACKEND={override!r} is not one of {sorted(set(_ALIASES))}"
            )
        return _ALIASES[override]
    if sys.platform.startswith("linux"):
        return "linux_x11"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("win"):
        return "win32"
    # Unknown UNIX (BSD, Solaris) still has X11 far more often than not, so try
    # that rather than refusing to start: the backend itself reports honestly if
    # xdotool/ffmpeg are missing.
    return "linux_x11"


def load(name: str | None = None) -> ModuleType:
    """Import and cache a backend module."""
    key = _ALIASES.get((name or backend_name()).lower(), name or backend_name())
    mod = _cache.get(key)
    if mod is None:
        mod = importlib.import_module(f".{key}", __name__)
        _cache[key] = mod
    return mod


def active() -> ModuleType:
    """The backend for this machine."""
    return load(None)
