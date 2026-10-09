"""The model/effort pickers: model ids and labels (Fable only by explicit
choice), the desktop footer picker through AT-SPI and the same picker on
claude.ai/code through Chrome. Never Escape."""

from __future__ import annotations

import re
import time
from typing import Any, Callable

from .drivers import ChromeDriver
from .guards import norm


MODEL_RE = re.compile(
    r"^claude-(opus|sonnet|haiku|fable)-\d+(?:-\d+)?(?:-\d{8})?(?:\[1m\])?$")

MODEL_ALIASES = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
                 "haiku": "claude-haiku-5-5", "fable": "claude-fable-5-1"}

                                            # uncached: never above 400k
EFFORT_LABELS = {"low": ("low",), "medium": ("medium", "med"), "high": ("high",),
                 "xhigh": ("xhigh", "extrahigh", "veryhigh"), "max": ("max", "maximum")}

OPENER_ROLES = {"push button", "button", "toggle button", "combo box", "menu button",
                "popup button", "label", "text"}

MENU_ROLES = {"menu item", "radio menu item", "check menu item", "list item", "option",
              "menuitem", "menuitemradio", "radio button"}

# Only an exact name match counts outside a menu: a chat line that starts with
# "Opus 5.5" must never be taken for the menu item.
ITEM_ROLES = MENU_ROLES | {"push button", "button", "label", "text"}

PICK_SETTLE_S = 0.8

def model_label(model_id: str) -> str:
    """claude-opus-5-5 -> "Opus 5.5" (the picker's wording)."""
    m = re.match(r"^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?", model_id or "")
    if not m:
        return model_id or ""
    ver = m.group(2) + ("." + m.group(3) if m.group(3) else "")
    return f"{m.group(1).title()} {ver}"

def normalize_model(model: str, allow_fable: bool = False) -> tuple[str, str]:
    """(model_id, error). Fable only by the owner's explicit choice:
    allow_fable=True must be passed; by default it is refused."""
    m = (model or "").strip().lower()
    if not m:
        return "", ""
    if "fable" in m and not allow_fable:
        return "", ("refused: Fable is picked only by explicit choice; pass allow_fable=True "
                    "(--allow-fable) if the owner chose it")
    m = MODEL_ALIASES.get(m, m)
    if not MODEL_RE.match(m):
        return "", f"unknown model {model!r}: use opus|sonnet|haiku or a claude-<family>-<ver> id"
    return m, ""

def effort_labels(effort: str) -> tuple[str, ...]:
    return EFFORT_LABELS.get(effort, (effort,))

def model_labels(model_id: str) -> tuple[str, ...]:
    lab = model_label(model_id)
    return (lab, lab.replace(" ", ""), model_id)

class DesktopPicker:
    """The composer-footer picker of the desktop app through AT-SPI. Opens the
    control showing the current value, picks the target, and never presses
    Escape: a wrong menu is closed by pressing its opener again."""

    APP = "claude"

    def __init__(self, a11y_mod: Any = None, sleep: Callable[[float], None] = time.sleep):
        if a11y_mod is None:
            from .. import a11y as a11y_mod
        self.a11y, self.sleep = a11y_mod, sleep

    def nodes(self) -> list[dict]:
        return self.a11y.tree(app=self.APP, actionable_only=False, limit=4000) or []

    def find(self, labels: tuple[str, ...], roles: set[str], exact: bool) -> dict | None:
        want = [norm(x) for x in labels if norm(x)]
        for el in self.nodes():
            n, role = norm(el.get("name")), str(el.get("role") or "").lower()
            if not n or role not in roles:
                continue
            if any(n == w or (not exact and n.startswith(w)) for w in want):
                return el
        return None

    def click(self, el: dict) -> dict:
        return self.a11y.click_element(el.get("name"), role=el.get("role"), app=self.APP)

    def pick(self, current: tuple[str, ...], target: tuple[str, ...]) -> dict:
        opener = self.find(current, OPENER_ROLES, exact=True)
        if not opener:
            return {"ok": False, "error": f"picker showing {current[0]!r} not found"}
        if not self.click(opener).get("ok"):
            return {"ok": False, "error": "could not open the picker"}
        self.sleep(PICK_SETTLE_S)
        item = (self.find(target, MENU_ROLES, exact=False)
                or self.find(target, ITEM_ROLES - MENU_ROLES, exact=True))
        if not item or norm(item.get("name")) == norm(opener.get("name")):
            self.click(opener)  # close it the way it opened; never Escape
            return {"ok": False, "error": f"no menu item {target[0]!r}"}
        if not self.click(item).get("ok"):
            self.click(opener)
            return {"ok": False, "error": f"could not pick {target[0]!r}"}
        self.sleep(PICK_SETTLE_S)
        return {"ok": True, "picked": item.get("name")}

    def shows(self, labels: tuple[str, ...]) -> bool:
        return self.find(labels, OPENER_ROLES, exact=True) is not None

class ChromePicker:
    """The same picker on claude.ai/code, through workman.chrome."""

    def __init__(self, driver: Any = None, sleep: Callable[[float], None] = time.sleep):
        self.drv, self.sleep = driver or ChromeDriver(), sleep

    def pick(self, current: tuple[str, ...], target: tuple[str, ...]) -> dict:
        opened = self.drv.click_text(current[0])
        if not opened.get("ok"):
            return {"ok": False, "error": f"picker showing {current[0]!r} not found"}
        self.sleep(PICK_SETTLE_S)
        got = self.drv.click_text(target[0])
        if not got.get("ok"):
            self.drv.click_text(current[0])  # close it the way it opened; never Escape
            return {"ok": False, "error": f"no menu item {target[0]!r}"}
        self.sleep(PICK_SETTLE_S)
        return {"ok": True, "picked": target[0]}

    def shows(self, labels: tuple[str, ...]) -> bool:
        page = self.drv.read_page()
        names = {norm(e.get("name")) for e in (page or {}).get("elements") or []}
        return any(norm(x) in names for x in labels)
