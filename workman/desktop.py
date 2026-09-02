"""The desktop facade — one API, whichever OS is underneath.

Everything above this line (the MCP tools, Human Mode, the Chrome driver) calls
`desktop.click(...)`, not `x11.click(...)`, so adding an OS means adding a
backend module and nothing else.

The proxies are written out rather than generated with `__getattr__` on purpose:
the signatures are the contract, and a reader (or a test, or an editor) should be
able to see it without running anything.

    from workman import desktop
    desktop.platform_info()["backend"]   # 'linux_x11' | 'darwin' | 'win32'
"""
from __future__ import annotations

from .platform import active, backend_name, load
from .platform import base

#: Canonical modifier names, shared by every backend.
_MODIFIERS = base.MODIFIERS
MODIFIERS = base.MODIFIERS


def backend():
    """The active backend module."""
    return active()


def _safe(name: str, *args, **kwargs) -> dict:
    """Call a backend action, converting an unsupported capability into a
    result the model can read instead of an exception it cannot."""
    module = active()
    # Name the backend that was actually asked, not the one this host would
    # pick: they differ whenever a backend is loaded explicitly for inspection.
    where = getattr(module, "PLATFORM", None) or backend_name()
    fn = getattr(module, name, None)
    if fn is None:
        return base.unsupported(name, where,
                                "this backend does not implement it yet")
    try:
        return fn(*args, **kwargs)
    except base.Unsupported as exc:
        return base.unsupported(name, where, str(exc))


# ---- see -------------------------------------------------------------------
def screen_size() -> tuple[int, int]:
    """Width and height of the whole desktop, in screen pixels."""
    return active().screen_size()


def screenshot(max_dim: int | None = None,
               region: tuple[int, int, int, int] | None = None) -> bytes:
    """PNG bytes of the desktop. region=(x, y, w, h) grabs a sub-rectangle.

    The image is always in the same coordinate space as `click`, on every OS.
    """
    return active().screenshot(max_dim=max_dim, region=region)


def monitors() -> list[dict]:
    """Physical outputs: name, position, size, which one is primary."""
    return active().monitors()


# ---- windows ---------------------------------------------------------------
def list_windows() -> list[dict]:
    return active().list_windows()


def focus_window(query: str, minimize_blockers: bool = True) -> dict:
    return _safe("focus_window", query, minimize_blockers=minimize_blockers)


def active_window() -> dict:
    return _safe("active_window")


def kill_window(query: str) -> dict:
    return _safe("kill_window", query)


def list_windows_rich() -> list[dict]:
    """Windows including WM state, where the platform can report it.

    Falls back to the plain list rather than erroring, because a caller asking
    for windows wants windows, not a lecture about Wnck.
    """
    fn = getattr(active(), "list_windows_rich", None)
    return fn() if fn else active().list_windows()


def window_action(query: str, action: str) -> dict:
    """activate | minimize | unminimize | maximize | unmaximize | fullscreen |
    unfullscreen | above | unabove | pin | unpin | close."""
    return _safe("window_action", query, action)


def window_geometry(query: str, x: int | None = None, y: int | None = None,
                    w: int | None = None, h: int | None = None) -> dict:
    return _safe("window_geometry", query, x=x, y=y, w=w, h=h)


def workspaces() -> dict:
    return _safe("workspaces")


def set_workspace(index: int = 0) -> dict:
    return _safe("set_workspace", index=index)


def move_to_workspace(query: str, index: int = 0) -> dict:
    return _safe("move_to_workspace", query, index=index)


# ---- pointer ---------------------------------------------------------------
def click(x: int, y: int, button: int = 1, count: int = 1) -> dict:
    return _safe("click", x, y, button=button, count=count)


def click_with(x: int, y: int, button: int = 1, count: int = 1,
               modifiers: list[str] | None = None) -> dict:
    return _safe("click_with", x, y, button=button, count=count,
                 modifiers=modifiers)


def move(x: int, y: int) -> dict:
    return _safe("move", x, y)


def drag(from_x: int, from_y: int, to_x: int, to_y: int) -> dict:
    return _safe("drag", from_x, from_y, to_x, to_y)


def hover(x: int, y: int, settle_ms: int = 350) -> dict:
    return _safe("hover", x, y, settle_ms=settle_ms)


def scroll(direction: str, amount: int = 3) -> dict:
    return _safe("scroll", direction, amount=amount)


def scroll_at(x: int, y: int, direction: str, amount: int = 3) -> dict:
    return _safe("scroll_at", x, y, direction, amount=amount)


def pointer_position() -> dict:
    return _safe("pointer_position")


def mouse_down(button: int = 1, x: int | None = None, y: int | None = None) -> dict:
    return _safe("mouse_down", button=button, x=x, y=y)


def mouse_up(button: int = 1, x: int | None = None, y: int | None = None) -> dict:
    return _safe("mouse_up", button=button, x=x, y=y)


# ---- keyboard --------------------------------------------------------------
def type_text(text: str, delay_ms: int = 40) -> dict:
    return _safe("type_text", text, delay_ms=delay_ms)


def press_key(key: str) -> dict:
    """xdotool key syntax on every platform: 'Return', 'ctrl+c', 'super+l'.

    `super` maps to Command on macOS and Win on Windows, so one combo string
    works everywhere and the model does not have to branch on the OS.
    """
    return _safe("press_key", key)


def key_down(key: str) -> dict:
    return _safe("key_down", key)


def key_up(key: str) -> dict:
    return _safe("key_up", key)


# ---- environment -----------------------------------------------------------
def clipboard_get(selection: str = "clipboard") -> dict:
    return _safe("clipboard_get", selection=selection)


def clipboard_set(text: str = "", selection: str = "clipboard") -> dict:
    return _safe("clipboard_set", text=text, selection=selection)


def emit_cursor(kind: str, x: int, y: int) -> None:
    """Notify the on-screen cursor overlay, where one exists."""
    fn = getattr(active(), "emit_cursor", None)
    if fn is not None:
        fn(kind, x, y)


def display_name() -> str:
    """A short label for the display this process drives (':0', 'quartz:...')."""
    return active().display_name()


def platform_info() -> dict:
    """Backend, OS, tool availability, and the platform's own gotchas.

    Worth calling once at the start of a session: it is what tells the model
    that `super` means Command here, or that Screen Recording is the reason a
    capture came back black.
    """
    import os

    info = dict(active().platform_info())
    info["selected_by"] = ("WORKMAN_BACKEND"
                           if os.environ.get("WORKMAN_BACKEND") else "auto")
    return info


__all__ = [name for name in base.CONTRACT] + [
    "backend", "backend_name", "load", "emit_cursor", "MODIFIERS",
]
