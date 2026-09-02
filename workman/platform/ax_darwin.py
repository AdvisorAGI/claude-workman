"""macOS accessibility — the AXUIElement half of the AT-SPI story.

This is the macOS twin of `workman.atspi`: it answers the same questions (what
elements are on screen, where is the one named X, press it, set its value) so
`click_element("push button", "Continue")` means the same thing on either OS.

Two hard requirements, both reported rather than assumed:

* **pyobjc.** `pip install pyobjc-framework-ApplicationServices` (it comes with
  the `pyobjc` umbrella package too). Without it there is no AX API to call.
* **The Accessibility grant.** AX reads another app's UI, so macOS gates it on
  System Settings > Privacy & Security > Accessibility, granted to the app that
  launched Workman. `AXIsProcessTrusted()` tells us which side of that we are
  on, so the failure names the fix instead of returning an empty tree.
"""
from __future__ import annotations

import time

from . import base

PLATFORM = "macOS"

_ax = None
_ax_tried = False


def _api():
    """The ApplicationServices module, or None."""
    global _ax, _ax_tried
    if not _ax_tried:
        _ax_tried = True
        try:
            import ApplicationServices  # type: ignore

            _ax = ApplicationServices
        except Exception:
            _ax = None
    return _ax


def _need(feature: str) -> dict:
    return base.unsupported(feature, PLATFORM,
                            "pip install pyobjc-framework-ApplicationServices, "
                            "then grant Accessibility to the app running Workman")


def available() -> dict:
    """Whether the AX layer can actually be used right now."""
    api = _api()
    if api is None:
        return {"ok": False, "pyobjc": False, "trusted": False,
                "error": "pyobjc-framework-ApplicationServices is not installed"}
    trusted = bool(api.AXIsProcessTrusted())
    return {"ok": trusted, "pyobjc": True, "trusted": trusted,
            "error": "" if trusted else
                     "this process is not trusted for Accessibility"}


def ensure_a11y(enable_web: bool = True, silence: bool = True) -> dict:
    """macOS needs no per-session enabling; it needs a permission grant.

    Kept for API parity with the Linux backend so the same tool works on both,
    and so the reply says exactly what to click when the grant is missing.
    """
    state = available()
    if state["ok"]:
        return {"ok": True, "already": True,
                "note": "AX is available; nothing to enable on macOS"}
    return {"ok": False, "error": state["error"],
            "fix": "System Settings > Privacy & Security > Accessibility, add "
                   "the app that launches Workman (Terminal, iTerm, Claude), "
                   "then restart it"}


# ---- element plumbing ------------------------------------------------------
def _attr(element, name: str):
    api = _api()
    err, value = api.AXUIElementCopyAttributeValue(element, name, None)
    return None if err else value


def _point_size(element) -> tuple[int, int, int, int]:
    """Screen rect of an element, in points (the click coordinate space)."""
    api = _api()
    x = y = w = h = 0
    pos = _attr(element, api.kAXPositionAttribute)
    size = _attr(element, api.kAXSizeAttribute)
    if pos is not None:
        ok, point = api.AXValueGetValue(pos, api.kAXValueCGPointType, None)
        if ok:
            x, y = int(point.x), int(point.y)
    if size is not None:
        ok, dims = api.AXValueGetValue(size, api.kAXValueCGSizeType, None)
        if ok:
            w, h = int(dims.width), int(dims.height)
    return x, y, w, h


_ROLE_NAMES = {
    "AXButton": "push button", "AXTextField": "text", "AXTextArea": "text",
    "AXCheckBox": "check box", "AXRadioButton": "radio button",
    "AXPopUpButton": "combo box", "AXMenuItem": "menu item",
    "AXStaticText": "label", "AXLink": "link", "AXWindow": "frame",
    "AXTabGroup": "page tab list", "AXRow": "table row", "AXCell": "table cell",
}


def _describe(element, app_name: str = "") -> dict:
    api = _api()
    role = _attr(element, api.kAXRoleAttribute) or ""
    title = (_attr(element, api.kAXTitleAttribute)
             or _attr(element, api.kAXDescriptionAttribute)
             or _attr(element, api.kAXValueAttribute) or "")
    x, y, w, h = _point_size(element)
    return {
        "app": app_name,
        "role": _ROLE_NAMES.get(str(role), str(role).replace("AX", "").lower()),
        "ax_role": str(role),
        "name": str(title)[:200],
        "x": x, "y": y, "w": w, "h": h,
        "center": [x + w // 2, y + h // 2],
    }


_ACTIONABLE = {"AXButton", "AXTextField", "AXTextArea", "AXCheckBox",
               "AXRadioButton", "AXPopUpButton", "AXMenuItem", "AXLink",
               "AXComboBox", "AXSlider", "AXTabGroup", "AXToolbar"}


def _frontmost_app():
    """(AXUIElement, name) of the frontmost application."""
    api = _api()
    try:
        from AppKit import NSWorkspace  # type: ignore
    except Exception:
        return None, ""
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return None, ""
    return (api.AXUIElementCreateApplication(app.processIdentifier()),
            str(app.localizedName()))


def _app_element(app: str | None):
    api = _api()
    if not app:
        return _frontmost_app()
    try:
        from AppKit import NSWorkspace  # type: ignore
    except Exception:
        return None, ""
    needle = app.lower()
    for running in NSWorkspace.sharedWorkspace().runningApplications():
        name = str(running.localizedName() or "")
        if needle in name.lower():
            return (api.AXUIElementCreateApplication(running.processIdentifier()),
                    name)
    return None, ""


def _walk(element, app_name: str, out: list[dict], limit: int,
          actionable_only: bool, depth: int = 0) -> None:
    """Depth-first over the AX tree.

    Depth is capped as well as breadth: a web view under Safari or Chrome can be
    tens of thousands of nodes deep, and an unbounded walk there looks exactly
    like a hang.
    """
    if len(out) >= limit or depth > 40:
        return
    api = _api()
    children = _attr(element, api.kAXChildrenAttribute) or []
    for child in children:
        if len(out) >= limit:
            return
        role = str(_attr(child, api.kAXRoleAttribute) or "")
        if not actionable_only or role in _ACTIONABLE:
            described = _describe(child, app_name)
            if described["name"] or not actionable_only:
                out.append(described)
        _walk(child, app_name, out, limit, actionable_only, depth + 1)


def tree(app: str | None = None, actionable_only: bool = True,
         limit: int = 400) -> list[dict]:
    state = available()
    if not state["ok"]:
        return [{"error": state["error"], "unsupported": True,
                 "hint": ensure_a11y()["fix"]}]
    element, name = _app_element(app)
    if element is None:
        return []
    out: list[dict] = []
    _walk(element, name, out, limit, actionable_only)
    return out


def find_node(name: str, role: str | None = None, app: str | None = None):
    """(element, description) for the first match, or (None, None)."""
    state = available()
    if not state["ok"]:
        return None, None
    element, app_name = _app_element(app)
    if element is None:
        return None, None
    needle = (name or "").lower()
    wanted = (role or "").lower()
    found: list = []

    def search(node, depth: int = 0) -> None:
        if found or depth > 40:
            return
        api = _api()
        for child in _attr(node, api.kAXChildrenAttribute) or []:
            if found:
                return
            described = _describe(child, app_name)
            if needle in described["name"].lower() and (
                    not wanted or wanted in described["role"].lower()
                    or wanted in described["ax_role"].lower()):
                found.append((child, described))
                return
            search(child, depth + 1)

    search(element)
    return found[0] if found else (None, None)


def find(name: str, role: str | None = None, app: str | None = None) -> dict | None:
    _element, described = find_node(name, role=role, app=app)
    return described


def click_element(name: str, role: str | None = None,
                  app: str | None = None) -> dict:
    """Press an element by name. AXPress first, a real click on its centre if
    the element offers no press action."""
    element, described = find_node(name, role=role, app=app)
    if element is None:
        state = available()
        return ({"ok": False, "error": state["error"], "unsupported": True}
                if not state["ok"] else
                {"ok": False, "error": f"no element named {name!r}"})
    api = _api()
    err = api.AXUIElementPerformAction(element, api.kAXPressAction)
    if err == 0:
        return {"ok": True, "element": described, "via": "AXPress"}
    from . import darwin

    cx, cy = described["center"]
    result = darwin.click(cx, cy)
    result["element"] = described
    result["via"] = "click on element centre"
    return result


def element_actions(name: str, role: str | None = None,
                    app: str | None = None) -> dict:
    element, described = find_node(name, role=role, app=app)
    if element is None:
        return {"ok": False, "error": f"no element named {name!r}"}
    api = _api()
    err, actions = api.AXUIElementCopyActionNames(element, None)
    return {"ok": True, "element": described,
            "actions": [str(a) for a in (actions or [])] if not err else []}


def perform_action(name: str, action: str = "click", role: str | None = None,
                   app: str | None = None) -> dict:
    element, described = find_node(name, role=role, app=app)
    if element is None:
        return {"ok": False, "error": f"no element named {name!r}"}
    api = _api()
    ax_action = action if action.startswith("AX") else {
        "click": "AXPress", "press": "AXPress", "open": "AXPress",
        "increment": "AXIncrement", "decrement": "AXDecrement",
        "show_menu": "AXShowMenu", "cancel": "AXCancel",
    }.get(action, "AXPress")
    err = api.AXUIElementPerformAction(element, ax_action)
    if err != 0:
        return {"ok": False, "error": f"{ax_action} failed (AX error {err})",
                "element": described}
    return {"ok": True, "element": described, "action": ax_action}


def set_value(name: str, value: str, role: str | None = None,
              app: str | None = None) -> dict:
    """Set a field's contents directly — no keystrokes, so nothing can be eaten
    by an autocomplete dropdown mid-word."""
    element, described = find_node(name, role=role, app=app)
    if element is None:
        return {"ok": False, "error": f"no element named {name!r}"}
    api = _api()
    err = api.AXUIElementSetAttributeValue(element, api.kAXValueAttribute, value)
    if err != 0:
        return {"ok": False, "error": f"could not set value (AX error {err})",
                "element": described,
                "hint": "the field may be read-only; try click_element then type_text"}
    return {"ok": True, "element": described, "set_len": len(value)}


def focused_element() -> dict:
    state = available()
    if not state["ok"]:
        return {"ok": False, "error": state["error"], "unsupported": True}
    api = _api()
    system = api.AXUIElementCreateSystemWide()
    element = _attr(system, api.kAXFocusedUIElementAttribute)
    if element is None:
        return {"ok": False, "error": "nothing has keyboard focus"}
    return {"ok": True, "element": _describe(element)}


def wait_for_element(name: str, role: str | None = None, app: str | None = None,
                     timeout: float = 10.0, poll: float = 0.4) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        described = find(name, role=role, app=app)
        if described:
            return {"ok": True, "element": described,
                    "waited_s": round(timeout - (deadline - time.monotonic()), 2)}
        time.sleep(poll)
    return {"ok": False, "error": f"{name!r} did not appear within {timeout}s"}


def disable_web() -> dict:
    return {"ok": True, "note": "macOS has no separate web-accessibility switch"}
