"""AT-SPI2 backend — the accuracy layer.

Fuses the accessibility tree with pixel coordinates so clicks target elements by
role+name, not guessed pixels (the same principle as macOS AXUIElement fusion).
Import is `gi.repository.Atspi` (NOT the absent `pyatspi` module).

SPEECH HAZARD: enabling the screen-reader flag so Chromium/Electron export their
trees ALSO makes Orca narrate the screen aloud (speech-dispatcher -> espeak-ng).
`ensure_a11y()` therefore kills Orca right after enabling, and does it by exact
process name — never `pkill -f orca`, which self-matches its own shell command.
"""
from __future__ import annotations
import os
import subprocess

DISPLAY = os.environ.get("WORKMAN_DISPLAY") or os.environ.get("DISPLAY") or ":0"


def _env() -> dict:
    e = dict(os.environ)
    e["DISPLAY"] = DISPLAY
    return e


def _sh(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, env=_env(), capture_output=True, text=True, timeout=15)


def ensure_a11y(enable_web: bool = True, silence: bool = True) -> dict:
    """Turn on AT-SPI tree export. GTK apps only need toolkit-accessibility;
    Chromium/Electron also need the screen-reader flag (enable_web). We then kill
    Orca so nothing is spoken aloud."""
    _sh(["gsettings", "set", "org.gnome.desktop.interface", "toolkit-accessibility", "true"])
    if enable_web:
        _sh(["busctl", "--user", "set-property", "org.a11y.Bus", "/org/a11y/bus",
             "org.a11y.Status", "ScreenReaderEnabled", "b", "true"])
    if silence:
        # kill the narrator by EXACT name; -f would self-match this call
        _sh(["pkill", "-x", "orca"])
        _sh(["gsettings", "set", "org.gnome.desktop.a11y.applications", "screen-reader-enabled", "false"])
    return {"ok": True, "web_export": enable_web, "orca_silenced": silence}


def disable_web() -> dict:
    """Turn the Chromium/Electron screen-reader flag back off (GTK trees survive)."""
    _sh(["busctl", "--user", "set-property", "org.a11y.Bus", "/org/a11y/bus",
         "org.a11y.Status", "ScreenReaderEnabled", "b", "false"])
    _sh(["pkill", "-x", "orca"])
    return {"ok": True}


def _atspi():
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi  # noqa
    return Atspi


ACTIONABLE = {"push button", "button", "toggle button", "check box", "radio button",
              "menu item", "link", "text", "entry", "combo box", "list item", "tab"}


def tree(app: str | None = None, actionable_only: bool = True, limit: int = 400) -> list[dict]:
    """Return elements as {app, role, name, x, y, w, h} with screen coords."""
    Atspi = _atspi()
    out: list[dict] = []
    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        acc = desktop.get_child_at_index(i)
        if acc is None:
            continue
        aname = acc.get_name() or ""
        if app and app.lower() not in aname.lower():
            continue

        def walk(node, depth=0):
            if node is None or len(out) >= limit:
                return
            try:
                role = node.get_role_name() or ""
                name = node.get_name() or ""
                if (not actionable_only) or role in ACTIONABLE:
                    comp = node.get_component_iface() if hasattr(node, "get_component_iface") else None
                    ext = None
                    try:
                        ext = node.get_extents(Atspi.CoordType.SCREEN)
                    except Exception:
                        ext = None
                    if ext and ext.width > 0 and ext.height > 0:
                        out.append({"app": aname, "role": role, "name": name,
                                    "x": ext.x, "y": ext.y, "w": ext.width, "h": ext.height})
                for j in range(node.get_child_count()):
                    walk(node.get_child_at_index(j), depth + 1)
            except Exception:
                return

        walk(acc)
    return out


def find(name: str, role: str | None = None, app: str | None = None) -> dict | None:
    for el in tree(app=app, actionable_only=False):
        if name.lower() in (el["name"] or "").lower() and (role is None or role == el["role"]):
            el["cx"] = el["x"] + el["w"] // 2
            el["cy"] = el["y"] + el["h"] // 2
            return el
    return None


def click_element(name: str, role: str | None = None, app: str | None = None) -> dict:
    """Find an element by role+name and click its center via xdotool."""
    el = find(name, role=role, app=app)
    if not el:
        return {"ok": False, "error": f"no element name~={name!r} role={role} app={app}"}
    from . import x11
    x11.click(el["cx"], el["cy"])
    return {"ok": True, "clicked": el}
