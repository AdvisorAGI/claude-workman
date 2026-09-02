"""Accessibility facade — element-accurate addressing, whichever OS is running.

Workman's central claim is that "click the push button named Continue" beats
guessing pixels. That claim has to survive changing operating systems, so this
module keeps one vocabulary (role + name -> element) over three very different
trees:

    Linux   AT-SPI2     workman.atspi          (full)
    macOS   AXUIElement workman.platform.ax_darwin  (full, needs pyobjc + grant)
    Windows UIAutomation  not implemented      (honest refusal, with the route
                                                that does work)

The Windows answer is deliberately a refusal rather than a silent empty tree: a
model that gets `[]` concludes the screen is empty and starts guessing pixels,
which is the failure this module exists to prevent.
"""
from __future__ import annotations

import sys

from .platform import base

#: The shared role vocabulary. AT-SPI's names are the canonical spelling and the
#: macOS backend maps AX roles onto them, so a caller's role filter means the
#: same thing on either tree.
ACTIONABLE_ROLES = frozenset({
    "push button", "button", "toggle button", "check box", "radio button",
    "menu item", "link", "text", "entry", "combo box", "list item", "tab",
})

_WINDOWS_HINT = ("no UI Automation backend yet on Windows — use screenshot + "
                 "zoom + click for native apps, or the chrome_*/bridge_* tools, "
                 "which address web UI by DOM selector on every OS")


def _impl():
    """The accessibility module for this platform, or None."""
    if sys.platform.startswith("linux"):
        from . import atspi

        return atspi
    if sys.platform == "darwin":
        from .platform import ax_darwin

        return ax_darwin
    return None


def _refuse(feature: str) -> dict:
    return base.unsupported(feature, sys.platform, _WINDOWS_HINT)


def ensure_a11y(enable_web: bool = True, silence: bool = True) -> dict:
    impl = _impl()
    if impl is None:
        return _refuse("accessibility")
    return impl.ensure_a11y(enable_web=enable_web, silence=silence)


def tree(app: str | None = None, actionable_only: bool = True,
         limit: int = 400) -> list[dict]:
    impl = _impl()
    if impl is None:
        return [_refuse("accessibility_tree")]
    return impl.tree(app=app, actionable_only=actionable_only, limit=limit)


def click_element(name: str, role: str | None = None,
                  app: str | None = None) -> dict:
    impl = _impl()
    if impl is None:
        return _refuse("click_element")
    return impl.click_element(name, role=role, app=app)


def element_actions(name: str, role: str | None = None,
                    app: str | None = None) -> dict:
    impl = _impl()
    if impl is None:
        return _refuse("element_actions")
    return impl.element_actions(name, role=role, app=app)


def perform_action(name: str, action: str = "click", role: str | None = None,
                   app: str | None = None) -> dict:
    impl = _impl()
    if impl is None:
        return _refuse("perform_element_action")
    return impl.perform_action(name, action=action, role=role, app=app)


def set_value(name: str, value: str, role: str | None = None,
              app: str | None = None) -> dict:
    impl = _impl()
    if impl is None:
        return _refuse("set_element_value")
    return impl.set_value(name, value, role=role, app=app)


def focused_element() -> dict:
    impl = _impl()
    if impl is None:
        return _refuse("focused_element")
    return impl.focused_element()


def wait_for_element(name: str, role: str | None = None, app: str | None = None,
                     timeout: float = 10.0, poll: float = 0.4) -> dict:
    impl = _impl()
    if impl is None:
        return _refuse("wait_for_element")
    return impl.wait_for_element(name, role=role, app=app, timeout=timeout,
                                 poll=poll)


def backend_name() -> str:
    impl = _impl()
    if impl is None:
        return "none"
    return "at-spi2" if impl.__name__.endswith("atspi") else "axuielement"
