"""The Chrome front-end verbs the Remote Control paths use (open, read, click,
type, keys; never Escape), and the page and focus checks."""

from __future__ import annotations

from typing import Any

from .guards import norm


EDITABLE_ROLES = {"entry", "text", "paragraph", "editable text", "text field", "textbox",
                  "textarea", "document text", "section"}

class ChromeDriver:
    """The front-end verbs the RC paths use, over workman.chrome / a11y /
    desktop. Tests replace the whole object."""

    def open_url(self, url: str) -> dict:
        from .. import chrome
        return chrome.open_url(url, new_tab=True)

    def wait_load(self, timeout_s: float = 20) -> dict:
        from .. import chrome
        return chrome.wait_load(timeout_s)

    def read_page(self) -> dict:
        from .. import chrome
        return chrome.read_page()

    def click_text(self, text: str) -> dict:
        from .. import chrome
        return chrome.click_text(text)

    def focused(self) -> dict:
        from .. import a11y
        return a11y.focused_element()

    def type(self, text: str) -> dict:
        from .. import chrome
        return chrome.type_text(text, human=True)

    def key(self, key: str) -> dict:
        if key.lower() in ("escape", "esc"):
            raise ValueError("Escape is never pressed")
        from .. import desktop
        return desktop.press_key(key)

def page_shows(page: dict, titles: list[str]) -> bool:
    hay = norm((page or {}).get("title")) + " " + norm((page or {}).get("text"))
    return any(norm(t) and norm(t) in hay for t in titles)

def is_editable(el: dict) -> bool:
    role = str((el or {}).get("role") or "").lower()
    return role in EDITABLE_ROLES or bool((el or {}).get("editable"))
