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
import time

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


# ---- acting THROUGH the tree instead of at pixels ---------------------------
# A synthetic click can miss: the element may have scrolled, be occluded, or sit
# under a pointer-grabbing overlay. These paths ask the toolkit to do the thing
# directly, which is why they work where coordinate clicks drift.

def _walk(node, visit, limit: int = 4000, seen: int = 0) -> int:
    """Depth-first walk that survives nodes disappearing mid-traversal."""
    if node is None or seen >= limit:
        return seen
    seen += 1
    try:
        if visit(node):
            return limit  # visitor says stop
        for i in range(node.get_child_count()):
            seen = _walk(node.get_child_at_index(i), visit, limit, seen)
            if seen >= limit:
                break
    except Exception:
        return seen
    return seen


def _describe(node, app_name: str = "") -> dict:
    Atspi = _atspi()
    info = {"app": app_name, "role": "", "name": "", "x": None, "y": None, "w": None, "h": None}
    try:
        info["role"] = node.get_role_name() or ""
        info["name"] = node.get_name() or ""
        ext = node.get_extents(Atspi.CoordType.SCREEN)
        if ext and ext.width > 0:
            info.update({"x": ext.x, "y": ext.y, "w": ext.width, "h": ext.height,
                         "cx": ext.x + ext.width // 2, "cy": ext.y + ext.height // 2})
    except Exception:
        pass
    return info


def find_node(name: str, role: str | None = None, app: str | None = None):
    """Return the live Accessible for an element, not just its coordinates."""
    Atspi = _atspi()
    needle = (name or "").lower()
    hit = {}

    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        acc = desktop.get_child_at_index(i)
        if acc is None:
            continue
        app_name = acc.get_name() or ""
        if app and app.lower() not in app_name.lower():
            continue

        def visit(node):
            try:
                if needle not in (node.get_name() or "").lower():
                    return False
                if role is not None and (node.get_role_name() or "") != role:
                    return False
            except Exception:
                return False
            hit["node"] = node
            hit["info"] = _describe(node, app_name)
            return True

        _walk(acc, visit)
        if hit:
            return hit["node"], hit["info"]
    return None, None


def element_actions(name: str, role: str | None = None, app: str | None = None) -> dict:
    """List the toolkit-declared actions on an element (press, activate, ...)."""
    node, info = find_node(name, role=role, app=app)
    if node is None:
        return {"ok": False, "error": f"no element name~={name!r} role={role} app={app}"}
    names = []
    try:
        action = node.get_action_iface()
        if action is not None:
            names = [action.get_action_name(i) for i in range(action.get_n_actions())]
    except Exception as exc:
        return {"ok": False, "error": f"no action interface: {exc}", "element": info}
    return {"ok": True, "element": info, "actions": names}


def perform_action(name: str, action: str = "click", role: str | None = None,
                   app: str | None = None) -> dict:
    """Invoke a declared action on an element — the AX equivalent of AXPress.

    No pointer is moved, so this works on elements a synthetic click cannot
    reach. Falls back to nothing: if the toolkit exposes no matching action the
    caller should use click_element instead.
    """
    node, info = find_node(name, role=role, app=app)
    if node is None:
        return {"ok": False, "error": f"no element name~={name!r} role={role} app={app}"}
    try:
        iface = node.get_action_iface()
        if iface is None:
            return {"ok": False, "error": "element declares no actions", "element": info}
        available = [iface.get_action_name(i) for i in range(iface.get_n_actions())]
        wanted = action.lower()
        for index, candidate in enumerate(available):
            if (candidate or "").lower() == wanted:
                iface.do_action(index)
                return {"ok": True, "performed": candidate, "element": info}
        return {"ok": False, "error": f"action {action!r} not declared",
                "available": available, "element": info}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "element": info}


def set_value(name: str, value: str, role: str | None = None,
              app: str | None = None) -> dict:
    """Set a field's contents directly instead of clicking and typing.

    Beats select-all-then-type for long strings: no keystroke timing, no risk of
    a stray keybinding firing, and it cannot be corrupted by autocomplete.
    """
    node, info = find_node(name, role=role, app=app)
    if node is None:
        return {"ok": False, "error": f"no element name~={name!r} role={role} app={app}"}
    try:
        editable = node.get_editable_text_iface()
        if editable is not None:
            editable.set_text_contents(value)
            return {"ok": True, "set": len(value), "via": "editable_text", "element": info}
    except Exception:
        pass
    try:  # sliders/spinners expose a numeric value instead of text
        numeric = node.get_value_iface()
        if numeric is not None:
            numeric.set_current_value(float(value))
            return {"ok": True, "set": value, "via": "value", "element": info}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "element": info}
    return {"ok": False, "error": "element is not editable", "element": info}


def focused_element() -> dict:
    """The element that currently has keyboard focus — the reliable way to check
    that a click actually landed before typing into it."""
    Atspi = _atspi()
    desktop = Atspi.get_desktop(0)
    found = {}
    for i in range(desktop.get_child_count()):
        acc = desktop.get_child_at_index(i)
        if acc is None:
            continue
        app_name = acc.get_name() or ""

        def visit(node):
            try:
                states = node.get_state_set()
                if states and states.contains(Atspi.StateType.FOCUSED):
                    found["info"] = _describe(node, app_name)
                    return True
            except Exception:
                return False
            return False

        _walk(acc, visit)
        if found:
            return {"ok": True, "element": found["info"]}
    return {"ok": False, "error": "nothing reports keyboard focus"}


def wait_for_element(name: str, role: str | None = None, app: str | None = None,
                     timeout: float = 10.0, poll: float = 0.4) -> dict:
    """Block until an element appears. UI that is still animating in reports
    stale geometry, so waiting beats screenshotting on a timer."""
    deadline = time.monotonic() + max(0.0, timeout)
    attempts = 0
    while True:
        attempts += 1
        el = find(name, role=role, app=app)
        if el:
            return {"ok": True, "element": el, "waited_s": round(attempts * poll, 2)}
        if time.monotonic() >= deadline:
            return {"ok": False, "error": f"element {name!r} did not appear within {timeout}s",
                    "attempts": attempts}
        time.sleep(poll)
