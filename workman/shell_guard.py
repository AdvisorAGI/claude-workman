"""Refuse keyboard input while GNOME Shell's overview (or search) has focus.

Typing while the Activities overview is open lands in the overview search
box, not the window EWMH reports as active. One cached D-Bus property read
of `org.gnome.Shell OverviewActive` is the cheap check; a missing bus or a
non-GNOME desktop degrades to today's behaviour (type as before).

An AT-SPI gnome-shell hit is not enough on its own: some GTK apps never
report focus, so the last AT-SPI event is a stale shell window. Count that
hit only when the focused shell element is an editable, showing
text/entry, or when the X active window is none or gnome-shell itself.
When OverviewActive is False and X names a normal app, do not refuse.

The xdotool fallback of the X read cannot tell "no active window" from a
failed read, so both degrade (type, do not refuse).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

#: How long a verdict may be reused. One human_type burst is many type_text
#: calls; a fresh gdbus spawn per character would stall typing.
CACHE_S = 0.5
#: gdbus must not stall a keystroke if the session bus is wedged.
PROBE_TIMEOUT_S = 0.4

_CACHE: dict = {"at": -1.0, "focus": False, "known": False}

_GDBUS = [
    "gdbus", "call", "--session",
    "--dest", "org.gnome.Shell",
    "--object-path", "/org/gnome/Shell",
    "--method", "org.freedesktop.DBus.Properties.Get",
    "org.gnome.Shell", "OverviewActive",
]

_SHELL_APPS = ("gnome-shell", "org.gnome.shell")
#: AT-SPI roles of the overview search box, Alt+F2 run dialog, and password
#: prompts. A gnome-shell *window* (the live stale hit) is not in this set.
_EDITABLE_ROLES = frozenset({
    "text", "entry", "password text", "editbar", "edit bar",
})


def reset() -> None:
    _CACHE.update(at=-1.0, focus=False, known=False)


def _desktop_is_gnome() -> bool | None:
    """True/False when XDG_CURRENT_DESKTOP is set, else None (try the bus)."""
    raw = (os.environ.get("XDG_CURRENT_DESKTOP") or "").strip()
    if not raw:
        return None
    parts = {p.strip().upper() for p in raw.replace(";", ":").split(":") if p.strip()}
    return "GNOME" in parts


def _parse_overview(stdout: str) -> bool | None:
    text = (stdout or "").strip().lower()
    if not text:
        return None
    if "<true>" in text or text in {"true", "(true,)", "true\n"}:
        return True
    if "<false>" in text or text in {"false", "(false,)", "false\n"}:
        return False
    return None


def _probe_overview() -> bool | None:
    """True when OverviewActive, False when not, None when unknown."""
    if not sys.platform.startswith("linux"):
        return None
    gnome = _desktop_is_gnome()
    if gnome is False:
        return None
    try:
        proc = subprocess.run(
            _GDBUS, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return _parse_overview(proc.stdout)


def _names_shell(el: dict) -> bool:
    hay = " ".join(str(el.get(k) or "") for k in ("app", "name", "role")).lower()
    return any(s in hay for s in _SHELL_APPS)


def _element_showing(el: dict) -> bool:
    if "showing" in el:
        return bool(el["showing"])
    w, h = el.get("w"), el.get("h")
    if w is None or h is None:
        return True
    try:
        return int(w) > 0 and int(h) > 0
    except (TypeError, ValueError):
        return True


def _element_editable(el: dict) -> bool:
    if el.get("editable") is True:
        return True
    role = str(el.get("role") or "").strip().lower()
    return role in _EDITABLE_ROLES


def _probe_x_active() -> str | None:
    """'shell' | 'app' | 'none', or None when the X read is unreadable.

    Uses desktop._focused_window (EWMH in-process, no extra gdbus). The
    xdotool fallback cannot tell 'no active window' from a failed read, so
    both come back as None and the caller degrades.
    """
    try:
        from . import desktop
        info = desktop._focused_window()
    except Exception:
        return None
    if info is None or info is desktop.FOCUS_UNREADABLE:
        return None
    if not isinstance(info, dict):
        return None
    if info.get("ok") is False:
        if info.get("error") == "no active window":
            return "none"
        return None
    if info.get("ok") is not True:
        return None
    hay = " ".join(str(info.get(k) or "") for k in ("class", "instance")).lower()
    if any(s in hay for s in _SHELL_APPS):
        return "shell"
    return "app"


def _probe_atspi_shell() -> bool | None:
    """True when AT-SPI focus is a decisive gnome-shell keyboard grab.

    A gnome-shell *window* (stale last-focus) is not decisive: GTK apps
    often never report AT-SPI focus, so the last event is the shell. Off
    Linux this is always None (no per-keystroke AX query on macOS).
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        from . import a11y
        info = a11y.focused_element()
    except Exception:
        return None
    if not isinstance(info, dict) or not info.get("ok"):
        return None
    el = info.get("element") or {}
    if not isinstance(el, dict) or not _names_shell(el):
        return False
    if _element_editable(el) and _element_showing(el):
        return True
    x = _probe_x_active()
    if x in ("none", "shell"):
        return True
    if x == "app":
        return False
    return None


def shell_has_focus() -> bool:
    """True only when a probe positively reports GNOME Shell has focus.

    Unknown (no bus, not GNOME, probe error) is False: degrade, do not refuse.
    """
    now = time.monotonic()
    if _CACHE["known"] and now - _CACHE["at"] < CACHE_S:
        return bool(_CACHE["focus"])
    overview = _probe_overview()
    focus = False
    if overview is True:
        focus = True
    elif overview is False:
        atspi = _probe_atspi_shell()
        focus = bool(atspi)
    else:
        # No D-Bus / not GNOME: a positive AT-SPI hit still refuses, a miss
        # or error degrades.
        atspi = _probe_atspi_shell()
        focus = bool(atspi)
    _CACHE.update(at=now, focus=focus, known=True)
    return focus


def refusal(action: str) -> dict | None:
    """Error dict when the shell has focus, else None (type as before)."""
    if not shell_has_focus():
        return None
    return {
        "ok": False,
        "error": "shell_has_focus",
        "action": action,
        "instruction": (
            "GNOME Shell (Activities overview or search) has keyboard focus, "
            "so keystrokes would not reach the target window. Close the "
            "overview, focus the window, then retry."
        ),
    }
