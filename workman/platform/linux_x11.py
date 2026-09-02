"""Linux/X11 backend.

The implementation is `workman.x11` (ffmpeg x11grab + xdotool), which predates
this package and is the most exercised code in the project. Rather than move it
and churn every call site, this module adapts it to the contract: it re-exports
the primitives unchanged and adds the four names the contract gained when the
other two backends arrived.
"""
from __future__ import annotations

import os
import shutil
import subprocess

from .. import x11
from . import base

PLATFORM = "Linux/X11"

# Re-exported unchanged — these already match the contract exactly.
screen_size = x11.screen_size
screenshot = x11.screenshot
monitors = x11.monitors
list_windows = x11.list_windows
focus_window = x11.focus_window
active_window = x11.active_window
kill_window = x11.kill_window
click = x11.click
click_with = x11.click_with
move = x11.move
drag = x11.drag
hover = x11.hover
scroll = x11.scroll
scroll_at = x11.scroll_at
pointer_position = x11.pointer_position
mouse_down = x11.mouse_down
mouse_up = x11.mouse_up
type_text = x11.type_text
press_key = x11.press_key
key_down = x11.key_down
key_up = x11.key_up
emit_cursor = x11.emit_cursor


def display_name() -> str:
    return x11.DISPLAY


def clipboard_get(selection: str = "clipboard") -> dict:
    """Read the clipboard. GTK first (it owns the selection properly), xclip as
    the fallback for boxes without the GI typelibs."""
    from .. import gtkops

    out = gtkops.call("clipboard_get", selection=selection)
    if out.get("ok"):
        return out
    if shutil.which("xclip"):
        sel = "primary" if selection == "primary" else "clipboard"
        res = x11._run(["xclip", "-selection", sel, "-o"])
        if res.returncode == 0:
            return {"ok": True, "text": res.stdout, "selection": selection,
                    "via": "xclip"}
    return out


def clipboard_set(text: str = "", selection: str = "clipboard") -> dict:
    from .. import gtkops

    out = gtkops.call("clipboard_set", text=text, selection=selection)
    if out.get("ok"):
        return out
    if shutil.which("xclip"):
        sel = "primary" if selection == "primary" else "clipboard"
        proc = subprocess.run(["xclip", "-selection", sel],
                              input=text, text=True, env=x11._env(),
                              capture_output=True, timeout=20)
        if proc.returncode == 0:
            return {"ok": True, "set_len": len(text), "selection": selection,
                    "via": "xclip"}
    return out


# ---- optional capabilities -------------------------------------------------
# All of these are Wnck/EWMH features, so they route through the gtkops helper
# process rather than being reimplemented here.


def list_windows_rich() -> list[dict]:
    """Windows with WM state (minimized/maximized/fullscreen/workspace).
    Falls back to raw geometry when the GI typelibs are missing."""
    from .. import gtkops

    result = gtkops.call("windows")
    return result["windows"] if result.get("ok") else x11.list_windows()


def window_action(query: str, action: str) -> dict:
    from .. import gtkops

    return gtkops.call("window_action", query=query, action=action)


def window_geometry(query: str, x: int | None = None, y: int | None = None,
                    w: int | None = None, h: int | None = None) -> dict:
    from .. import gtkops

    return gtkops.call("window_geometry", query=query, x=x, y=y, w=w, h=h)


def workspaces() -> dict:
    from .. import gtkops

    return gtkops.call("workspaces")


def set_workspace(index: int = 0) -> dict:
    from .. import gtkops

    return gtkops.call("set_workspace", index=index)


def move_to_workspace(query: str, index: int = 0) -> dict:
    from .. import gtkops

    return gtkops.call("move_to_workspace", query=query, index=index)


def platform_info() -> dict:
    """What this backend is standing on, and whether its tools are installed.

    Reported rather than asserted: a missing xdotool is a fixable install, and
    the model can say so instead of failing every action with a stack trace.
    """
    session = (os.environ.get("XDG_SESSION_TYPE") or "").lower()
    return {
        "backend": "linux_x11",
        "os": "linux",
        "display": x11.DISPLAY,
        "session_type": session or "unknown",
        "wayland": session == "wayland",
        "tools": {name: bool(shutil.which(name))
                  for name in ("xdotool", "ffmpeg", "xrandr", "xclip")},
        "modifier_super": "Super",
        "notes": ("Under a Wayland session XTEST input and x11grab only reach "
                  "XWayland clients; log into an Xorg session for full control."
                  if session == "wayland" else ""),
        "contract": list(base.CONTRACT),
    }
