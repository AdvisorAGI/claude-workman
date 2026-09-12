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

import shutil
import time

from . import owner_pause, shell_guard
from .platform import active, backend_name, load
from .platform import base

#: Canonical modifier names, shared by every backend.
_MODIFIERS = base.MODIFIERS
MODIFIERS = base.MODIFIERS

#: Window classes, apps and titles of remote-desktop viewers. With one focused,
#: agent keys and pointer motion are forwarded to ANOTHER machine.
REMOTE_VIEWERS = ("remmina", "vnc", "vncviewer", "virt-viewer", "krdc", "krfb",
                  "tigervnc", "xfreerdp", "screen sharing")
#: Pointer and key actions only: releases are never refused, and window
#: management sends nothing into the viewer (it is how the agent looks away).
_VIEWER_GUARDED = owner_pause.MOUSE_ACTIONS | owner_pause.KEYBOARD_ACTIONS
#: Clicks, drags and downs that name a point: refuse if a viewer is under it,
#: even when that viewer does not have keyboard focus.
_POINT_GUARDED = frozenset({
    "click", "click_with", "drag", "hover", "scroll_at", "mouse_down",
})
#: How long a `move` may reuse the last verdict; one human path is ~100 moves.
VIEWER_TTL_S = 0.2
_VIEWER_SEEN: dict = {"at": -1.0, "hit": False}
#: Sentinel: the xdotool fallback ran and could not identify the focused window.
FOCUS_UNREADABLE = object()


def backend():
    """The active backend module."""
    return active()


def is_remote_viewer(win: dict | None) -> bool:
    """True when a window's class, instance or app names a remote-desktop
    viewer. The title is consulted only when those are all empty: a Chrome
    tab about VNC is not a viewer, an unidentified window titled 'vnc' is
    treated as one (fail closed)."""
    if not isinstance(win, dict):
        return False
    owner = " ".join(str(win.get(k) or "") for k in ("class", "instance", "app")).lower()
    if owner.strip():
        return any(v in owner for v in REMOTE_VIEWERS)
    hay = str(win.get("name") or "").lower()
    return any(v in hay for v in REMOTE_VIEWERS)


def _xdotool_focused():
    """One (sometimes two) xdotool reads of the focused window. Used only
    when there is no backend hook and no XTest channel. Returns a window
    dict, or FOCUS_UNREADABLE when xdotool is missing or the read fails.

    Tests stub this so pytest never talks to :0.
    """
    if shutil.which("xdotool") is None:
        return FOCUS_UNREADABLE
    from . import x11
    try:
        res = x11._run(["xdotool", "getactivewindow", "getwindowclassname"], timeout=2)
    except Exception:
        return FOCUS_UNREADABLE
    if res.returncode != 0:
        return FOCUS_UNREADABLE
    lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return FOCUS_UNREADABLE
    cls = lines[-1]
    name = ""
    if not cls:
        try:
            nres = x11._run(["xdotool", "getactivewindow", "getwindowname"], timeout=2)
            if nres.returncode == 0:
                nlines = [ln.strip() for ln in (nres.stdout or "").splitlines() if ln.strip()]
                name = nlines[-1] if nlines else ""
        except Exception:
            pass
        if not name:
            return FOCUS_UNREADABLE
    return {"ok": True, "class": cls, "instance": "", "app": "", "name": name}


def _focused_window():
    """The focused window's identity from one cheap read: the backend's own
    hook (macOS: one Quartz window list), one EWMH read on the X11 channel,
    or one xdotool read when neither exists. FOCUS_UNREADABLE when that
    last-ditch read ran and failed."""
    try:
        hook = getattr(active(), "focused_window_identity", None)
        if hook is not None:
            return hook()
        from . import x11
        ch = x11._xt()
        if ch is not None:
            return ch.active_window_info()
        return _xdotool_focused()
    except Exception:
        return None


def _window_at_point(x: int, y: int) -> dict | None:
    """The topmost client window containing (x, y), or None when unknown."""
    try:
        hook = getattr(active(), "window_at_point", None)
        if hook is not None:
            return hook(int(x), int(y))
        from . import x11
        ch = x11._xt()
        fn = getattr(ch, "window_at_point", None) if ch is not None else None
        if fn is not None:
            return fn(int(x), int(y))
    except Exception:
        return None
    return None


def _points_for(name: str, args: tuple, kwargs: dict) -> list[tuple[int, int]]:
    """Screen points an action will send a button or wheel event to."""
    def pair(a, b):
        try:
            if a is None or b is None:
                return None
            return (int(a), int(b))
        except (TypeError, ValueError):
            return None

    if name in ("click", "click_with", "hover", "scroll_at"):
        pt = pair(args[0] if args else kwargs.get("x"),
                  args[1] if len(args) > 1 else kwargs.get("y"))
        return [pt] if pt else []
    if name == "drag":
        a = pair(args[0] if args else kwargs.get("from_x"),
                 args[1] if len(args) > 1 else kwargs.get("from_y"))
        b = pair(args[2] if len(args) > 2 else kwargs.get("to_x"),
                 args[3] if len(args) > 3 else kwargs.get("to_y"))
        return [p for p in (a, b) if p]
    if name == "mouse_down":
        pt = pair(kwargs.get("x"), kwargs.get("y"))
        if pt is None and len(args) > 2:
            pt = pair(args[1], args[2])
        if pt is not None:
            return [pt]
        try:
            p = active().pointer_position()
            if isinstance(p, dict) and p.get("ok"):
                hit = pair(p.get("x"), p.get("y"))
                return [hit] if hit else []
        except Exception:
            return []
    return []


def remote_viewer_focused() -> bool:
    """Whether a remote-desktop viewer has focus right now (read afresh).

    Unreadable focus is treated as a viewer: park_pointer must not send
    motion into a window it cannot identify.
    """
    win = _focused_window()
    if win is FOCUS_UNREADABLE:
        return True
    return is_remote_viewer(win)


def _viewer_refusal(name: str, *args, **kwargs) -> dict | None:
    now = time.monotonic()
    if name == "move" and now - _VIEWER_SEEN["at"] < VIEWER_TTL_S:
        hit = _VIEWER_SEEN["hit"]
        win = None
    else:
        win = _focused_window()
        if win is FOCUS_UNREADABLE:
            # Keys and clicks fail closed: a click we cannot attribute might
            # land in a viewer. Pointer moves still go through so the agent
            # can look away; releases are not in this function.
            _VIEWER_SEEN.update(at=now if name == "move" else -1.0, hit=False)
            if name in owner_pause.KEYBOARD_ACTIONS or name in _POINT_GUARDED:
                return {"ok": False, "error": "remote_viewer_unknown", "action": name,
                        "instruction": "Could not tell whether a remote-desktop viewer "
                                       "has focus (no XTest channel and the xdotool "
                                       "read failed). Focus a local window first, or "
                                       "leave it to the owner."}
            hit = False
        else:
            hit = is_remote_viewer(win)
            # Only a move reuses a verdict; any other action reads afresh and
            # clears it, so a focus change the agent just made is always seen.
            _VIEWER_SEEN.update(at=now if name == "move" else -1.0, hit=hit)
    if hit:
        return {"ok": False, "error": "remote_viewer_focused", "action": name,
                "instruction": "A remote-desktop viewer has focus, so input would reach "
                               "another machine. Focus a local window first, or leave it "
                               "to the owner."}
    if name in _POINT_GUARDED:
        for x, y in _points_for(name, args, kwargs):
            under = _window_at_point(x, y)
            if is_remote_viewer(under):
                return {"ok": False, "error": "remote_viewer_under_target",
                        "action": name, "at": [x, y],
                        "instruction": "A remote-desktop viewer is under that point, so "
                                       "the click would reach another machine. Click a "
                                       "local window, or leave it to the owner."}
    return None


def _safe(name: str, *args, **kwargs) -> dict:
    """Call a backend action, converting an unsupported capability into a
    result the model can read instead of an exception it cannot.

    Input actions defer to the owner twice over: the pause switch (Escape)
    refuses outright, and while he is physically on the mouse or keyboard the
    action waits for a moment of quiet and refuses with `human_active` if it
    does not come. Releases (mouse_up, key_up) are never held back. No key or
    pointer event is sent while a remote-desktop viewer has focus: the
    viewer would forward it to another machine.
    """
    if name in owner_pause.INPUT_ACTIONS:
        if owner_pause.blocked(name):
            return owner_pause.refusal(name)
        if name in owner_pause.MOUSE_ACTIONS:
            # A backend that can tell the pointer was moved by someone else
            # (X11: it is not where the agent last put it) reports the owner.
            moved = getattr(active(), "foreign_pointer_motion", None)
            if moved is not None and moved():
                owner_pause.mark_human_input()
        if owner_pause.human_active():
            still_busy = owner_pause.wait_for_human_quiet()
            if owner_pause.blocked(name):
                return owner_pause.refusal(name)
            if still_busy:
                return owner_pause.refusal(name, error="human_active")
        if name in _VIEWER_GUARDED:
            if (refused := _viewer_refusal(name, *args, **kwargs)) is not None:
                return refused
        else:
            _VIEWER_SEEN["at"] = -1.0  # a window change: the next move reads afresh
        if name in owner_pause.KEYBOARD_ACTIONS:
            if (refused := shell_guard.refusal(name)) is not None:
                return refused
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
_ACCEPTS: dict = {}


def _accepts(name: str, kwarg: str) -> bool:
    """Whether the active backend's `name` takes keyword `kwarg`. Cached, so
    Human Mode's per-key dwell costs nothing on a backend that has it and
    is dropped silently on one that does not."""
    import inspect

    module = active()
    key = (id(module), name, kwarg)
    hit = _ACCEPTS.get(key)
    if hit is None:
        fn = getattr(module, name, None)
        try:
            params = inspect.signature(fn).parameters
            hit = kwarg in params or any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
        except (TypeError, ValueError):
            hit = False
        _ACCEPTS[key] = hit
    return hit


def type_text(text: str, delay_ms: int = 40, dwell_ms: int | None = None) -> dict:
    """dwell_ms holds each key down for that long (Human Mode); backends that
    cannot express it type the text the plain way."""
    if dwell_ms is not None and _accepts("type_text", "dwell_ms"):
        return _safe("type_text", text, delay_ms=delay_ms, dwell_ms=dwell_ms)
    return _safe("type_text", text, delay_ms=delay_ms)


def press_key(key: str, hold_ms: int | None = None) -> dict:
    """xdotool key syntax on every platform: 'Return', 'ctrl+c', 'super+l'.

    `super` maps to Command on macOS and Win on Windows, so one combo string
    works everywhere and the model does not have to branch on the OS.
    hold_ms holds the key down (Human Mode); backends that cannot express it
    press the combo the plain way.
    """
    if hold_ms is not None and _accepts("press_key", "hold_ms"):
        return _safe("press_key", key, hold_ms=hold_ms)
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
