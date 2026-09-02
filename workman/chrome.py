"""Chrome/Chromium control the way a person uses the browser.

The tools here drive the real window: focus it, type a URL into the omnibox,
click visible names from the AT-SPI tree, with human cadence on the pointer
and keyboard. CDP is used only as a cheap HTTP side channel
(`http://127.0.0.1:9222/json*`) for tab identity — never a WebSocket, never
a new dependency. Randomness is behind a seedable `random.Random` so tests
are deterministic.
"""
from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request

from . import a11y, autoscroll, desktop, human
from .human import (  # re-export so chrome_* and tests share one implementation
    CHAR_DELAY_MS,
    CLICK_PRESS_MS,
    ENTER_MIN_MS,
    PATH_DURATION_MS,
    PATH_STEPS,
    PATH_WAYPOINTS,
    click_press_ms,
    eased_path,
    enter_delay_ms,
    human_click,
    human_move,
    human_type,
    path_duration_ms,
    typing_cadence,
)

CDP_BASE = "http://127.0.0.1:9222"
CDP_TIMEOUT_S = 1.5
TEXT_CAP = 20000
MAX_TAB_SWITCHES = 16
TOOLBAR_PX = 90
AUTOSCROLL_MAX = 200
SETTLE_HUMAN_MS = (120, 280)
SETTLE_FAST_MS = 80
TAB_TYPES = {"page", "tab", "webview"}
CLICKABLE_ROLES = {
    "push button", "button", "toggle button", "check box", "radio button",
    "menu item", "link", "tab", "list item", "combo box", "entry", "text",
}
TEXT_ROLES = {
    "document web", "document", "paragraph", "heading", "static",
    "static text", "text", "label", "list item", "table cell", "article",
    "section", "note", "description", "heading 1", "heading 2", "heading 3",
    "heading 4", "heading 5", "heading 6",
}


def _rng(rng: random.Random | None = None) -> random.Random:
    return human.resolve_rng(rng)


def _sleep_ms(ms: float) -> None:
    if ms > 0:
        time.sleep(ms / 1000.0)


# ---- CDP (HTTP /json only) --------------------------------------------------

def cdp_request(path: str = "/json") -> tuple[object | None, str | None]:
    """GET a CDP HTTP endpoint. Returns (payload, error).

    `/json` and `/json/list` yield a list. `/json/activate/<id>` often returns
    plain text; that is treated as success (`{"ok": True, ...}`), not a parse
    failure. Unreachable CDP is (`None`, reason) — never an exception.
    """
    path = path if path.startswith("/") else f"/{path}"
    url = f"{CDP_BASE.rstrip('/')}{path}"
    try:
        with urllib.request.urlopen(url, timeout=CDP_TIMEOUT_S) as resp:
            body = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return None, str(exc) or exc.__class__.__name__
    text = body.decode("utf-8", errors="replace") if body else ""
    if not text:
        return {"ok": True}, None
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        return {"ok": True, "body": text}, None


def parse_tabs(payload, window_title: str = "") -> list[dict]:
    """Normalize a CDP /json list into `{id, title, url, active}` tabs."""
    if not isinstance(payload, list):
        return []
    tabs: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        typ = (item.get("type") or "page").lower()
        if typ not in TAB_TYPES:
            continue
        rec = {
            "id": str(item.get("id") or ""),
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "active": False,
        }
        if "loading" in item:
            rec["loading"] = bool(item["loading"])
        tabs.append(rec)
    return _mark_active(tabs, window_title)


def _tab_matches_window(tab: dict, window_title: str) -> bool:
    title = (tab.get("title") or "").strip()
    win = (window_title or "").strip()
    if not title or not win:
        return False
    title_l, win_l = title.lower(), win.lower()
    return win_l.startswith(title_l) or title_l in win_l


def _mark_active(tabs: list[dict], window_title: str) -> list[dict]:
    best = None
    best_len = -1
    for tab in tabs:
        if _tab_matches_window(tab, window_title):
            n = len(tab.get("title") or "")
            if n > best_len:
                best, best_len = tab, n
    for tab in tabs:
        tab["active"] = tab is best
    return tabs


def select_tab(tabs: list[dict], match: str) -> dict | None:
    """First tab whose title or url contains `match` (case-insensitive)."""
    needle = (match or "").lower()
    if not needle:
        return None
    for tab in tabs:
        if needle in (tab.get("title") or "").lower() or needle in (tab.get("url") or "").lower():
            return tab
    return None


def is_loading(tab: dict | None) -> bool:
    if not tab:
        return False
    if tab.get("loading") is True:
        return True
    title = (tab.get("title") or "").strip()
    return (not title) or title.lower() in {"loading", "loading...", "loading…"}


# ---- windows ----------------------------------------------------------------

def is_chrome_window(win: dict) -> bool:
    name = (win.get("name") or "").lower().strip()
    if "google chrome" in name or "chromium" in name:
        return True
    if name in {"chrome", "google-chrome", "chromium-browser"}:
        return True
    if name.endswith(" - chrome") or name.endswith(" – chrome"):
        return True
    return False


def find_chrome_window() -> dict | None:
    wins = desktop.list_windows()
    matches = [w for w in wins if is_chrome_window(w)]
    if not matches:
        return None
    try:
        active = desktop.active_window()
    except Exception:
        active = {}
    if active.get("ok") and any(m.get("id") == active.get("id") for m in matches):
        return {
            "id": active["id"], "name": active.get("name"), "pid": active.get("pid"),
            "x": active.get("x"), "y": active.get("y"),
            "w": active.get("w"), "h": active.get("h"),
        }
    return matches[0]


def _window_title() -> str:
    try:
        info = desktop.active_window()
    except Exception:
        info = {}
    return (info.get("name") or "") if info.get("ok") else ""


def _geo(win: dict | None) -> tuple[int, int, int, int]:
    win = win or {}

    def n(key: str, default: int = 0) -> int:
        try:
            val = win.get(key)
            return int(val) if val is not None and val != "" else default
        except (TypeError, ValueError):
            return default

    return n("x"), n("y"), n("w"), n("h")


def _wait_title_change(previous: str, timeout_s: float = 10.0,
                       poll_s: float = 0.25) -> str:
    deadline = time.monotonic() + max(0.0, timeout_s)
    latest = previous
    while time.monotonic() < deadline:
        latest = _window_title()
        if latest != previous:
            return latest
        time.sleep(poll_s)
    return latest


# ---- tools ------------------------------------------------------------------

def focus() -> dict:
    """Find, raise and focus a Chrome/Chromium window."""
    win = find_chrome_window()
    if not win:
        return {"ok": False, "error": "no Chrome/Chromium window"}
    result = desktop.focus_window(str(win["id"]))
    title = win.get("name") or ""
    if result.get("frontmost_now"):
        title = result["frontmost_now"]
    out = {"ok": bool(result.get("ok")), "id": win["id"], "title": title}
    if not out["ok"]:
        out["error"] = result.get("error") or "failed to focus Chrome"
    return out


def open_url(url: str, new_tab: bool = True, rng: random.Random | None = None) -> dict:
    """Focus Chrome, ctrl+t or ctrl+l, type the URL, press Return."""
    rng = _rng(rng)
    if not (url or "").strip():
        return {"ok": False, "error": "no url given"}
    focused = focus()
    if not focused.get("ok"):
        return focused
    desktop.press_key("ctrl+t" if new_tab else "ctrl+l")
    _sleep_ms(rng.randint(80, 180))
    before = _window_title() or focused.get("title") or ""
    typed = type_text(url, human=True, rng=rng)
    if not typed.get("ok"):
        return typed
    _sleep_ms(enter_delay_ms(rng))
    desktop.press_key("Return")
    title = _wait_title_change(before, timeout_s=10.0)
    return {"ok": True, "url": url, "new_tab": new_tab, "id": focused.get("id"),
            "title": title, "title_changed": title != before}


def list_tabs() -> dict:
    """Tabs from CDP /json. Unreachable CDP is a 'no CDP' result, not an exception."""
    win = find_chrome_window()
    title = (win or {}).get("name") or _window_title()
    payload, err = cdp_request("/json")
    if payload is None:
        return {"ok": False, "cdp": False, "error": "no CDP" if not err else f"no CDP: {err}",
                "tabs": [], "window_title": title}
    tabs = parse_tabs(payload, title)
    return {"ok": True, "cdp": True, "tabs": tabs, "window_title": title}


def activate_tab(match: str, rng: random.Random | None = None) -> dict:
    """Activate the first tab whose title or url contains `match`."""
    rng = _rng(rng)
    if not (match or "").strip():
        return {"ok": False, "error": "no match given"}
    payload, err = cdp_request("/json")
    if payload is not None:
        tabs = parse_tabs(payload, _window_title())
        hit = select_tab(tabs, match)
        if not hit:
            return {"ok": False, "error": f"no tab matching {match!r}", "tabs": tabs, "via": "cdp"}
        activate_path = f"/json/activate/{urllib.parse.quote(hit['id'], safe='')}"
        activated, activate_err = cdp_request(activate_path)
        if activated is not None:
            return {"ok": True, "via": "cdp", "tab": hit}
        # CDP listed the tab but activate failed — fall through to the UI walk.
        err = activate_err

    focused = focus()
    if not focused.get("ok"):
        return {**focused, "via": "ui",
                "error": focused.get("error") or (f"no CDP: {err}" if err else "no CDP")}
    needle = match.lower()
    title = _window_title() or focused.get("title") or ""
    start = title
    for step in range(MAX_TAB_SWITCHES):
        if needle in title.lower():
            return {"ok": True, "via": "ui", "title": title, "steps": step}
        desktop.press_key("ctrl+Tab")
        _sleep_ms(rng.randint(80, 160))
        title = _window_title()
        if step > 0 and title == start:
            break
    return {"ok": False, "via": "ui",
            "error": f"no tab matching {match!r} after {MAX_TAB_SWITCHES} switches",
            "window_title": title}


def read_page() -> dict:
    """Visible text + interactive role/name structure from Chrome's AT-SPI tree."""
    focused = focus()
    title = focused.get("title") or _window_title()
    try:
        nodes = a11y.tree(app="chrom", actionable_only=False, limit=4000)
    except Exception as exc:
        return {"ok": False, "error": f"AT-SPI unavailable: {exc}", "title": title}
    if not nodes:
        return {"ok": False, "title": title,
                "error": "no AT-SPI elements in Chrome — call enable_accessibility first"}
    texts: list[str] = []
    seen_text: set[str] = set()
    elements: list[dict] = []
    seen_el: set[tuple[str, str]] = set()
    for el in nodes:
        role = el.get("role") or ""
        name = (el.get("name") or "").strip()
        if not name:
            continue
        role_l = role.lower()
        if role_l in TEXT_ROLES and name not in seen_text:
            seen_text.add(name)
            texts.append(name)
        if role_l in CLICKABLE_ROLES or role_l in a11y.ACTIONABLE_ROLES:
            key = (role_l, name)
            if key not in seen_el:
                seen_el.add(key)
                elements.append({"role": role, "name": name})
    text = "\n".join(texts)
    if len(text) > TEXT_CAP:
        text = text[:TEXT_CAP]
    return {"ok": True, "title": title, "text": text, "elements": elements,
            "n_elements": len(elements)}


def _find_clickable(text: str) -> dict | None:
    needle = (text or "").lower()
    if not needle:
        return None
    try:
        nodes = a11y.tree(app="chrom", actionable_only=False, limit=2000)
    except Exception:
        return None
    clickable: list[dict] = []
    fallback: list[dict] = []
    for el in nodes:
        name = el.get("name") or ""
        if needle not in name.lower():
            continue
        w, h = el.get("w") or 0, el.get("h") or 0
        if w <= 0 or h <= 0:
            continue
        rec = dict(el)
        rec["cx"] = el["x"] + el["w"] // 2
        rec["cy"] = el["y"] + el["h"] // 2
        if (el.get("role") or "").lower() in CLICKABLE_ROLES:
            clickable.append(rec)
        else:
            fallback.append(rec)

    def prefer_exact(group: list[dict]) -> list[dict]:
        exact = [e for e in group if (e.get("name") or "").lower() == needle]
        return exact or group

    pool = prefer_exact(clickable) or prefer_exact(fallback)
    return pool[0] if pool else None


def _in_content_area(el: dict, win: dict | None) -> bool:
    wx, wy, ww, wh = _geo(win)
    if ww <= 0 or wh <= 0:
        return True
    cx = el.get("cx", (el.get("x") or 0) + (el.get("w") or 0) // 2)
    cy = el.get("cy", (el.get("y") or 0) + (el.get("h") or 0) // 2)
    return (wx <= cx <= wx + ww) and (wy + TOOLBAR_PX <= cy <= wy + wh - 8)


def _scroll_towards(el: dict, win: dict | None) -> None:
    wx, wy, ww, wh = _geo(win)
    if ww <= 0 or wh <= 0:
        return
    px, py = wx + ww // 2, wy + wh // 2
    cy = el.get("cy", 0)
    if cy > wy + wh - 8:
        desktop.scroll_at(px, py, "down", amount=3)
    elif cy < wy + TOOLBAR_PX:
        desktop.scroll_at(px, py, "up", amount=3)


def click_text(text: str, rng: random.Random | None = None) -> dict:
    """Locate a clickable AT-SPI name in Chrome, scroll into view, human-click."""
    rng = _rng(rng)
    if not (text or "").strip():
        return {"ok": False, "error": "no text given"}
    focused = focus()
    if not focused.get("ok"):
        return focused
    el = _find_clickable(text)
    if not el:
        return {"ok": False, "error": f"no clickable element matching {text!r}"}
    win = find_chrome_window() or {}
    for _ in range(8):
        if _in_content_area(el, win):
            break
        _scroll_towards(el, win)
        time.sleep(0.15)
        found = _find_clickable(text)
        if not found:
            break
        el = found
    cx, cy = int(el["cx"]), int(el["cy"])
    clicked = human_click(cx, cy, rng=rng)
    return {"ok": True, "clicked": {"role": el.get("role"), "name": el.get("name"),
                                    "x": cx, "y": cy},
            "press_ms": clicked.get("press_ms"), "at": clicked.get("at")}


def type_text(text: str, human: bool = True, rng: random.Random | None = None) -> dict:
    """Type into the focused window, wrapping `desktop.type_text` per character."""
    if not human:
        return desktop.type_text(text)
    return human_type(text, rng=_rng(rng), typos=False)


def wait_load(timeout_s: float = 15, poll_s: float = 0.35,
              stable_needed: int = 3) -> dict:
    """Wait until the title stops changing and the active CDP tab is not loading."""
    timeout_s = max(0.0, min(float(timeout_s), 60.0))
    deadline = time.monotonic() + timeout_s
    last_title = None
    stable = 0
    last_loading = None
    cdp_up = False
    while time.monotonic() <= deadline:
        title = _window_title()
        loading = False
        payload, _err = cdp_request("/json")
        if isinstance(payload, list):
            cdp_up = True
            tabs = parse_tabs(payload, title)
            active = next((t for t in tabs if t.get("active")), None)
            loading = is_loading(active)
            last_loading = loading
        if title == last_title and not loading:
            stable += 1
            if stable >= stable_needed:
                return {"ok": True, "title": title, "stable_polls": stable, "cdp": cdp_up}
        else:
            stable = 0
            last_title = title
        time.sleep(poll_s)
    return {"ok": False, "error": f"page did not settle within {timeout_s}s",
            "title": last_title, "loading": last_loading, "cdp": cdp_up}


# ---- autoscroll-and-read ----------------------------------------------------

def _use_human_flag(flag: bool | None) -> bool:
    return human.enabled() if flag is None else bool(flag)


def _document_container(win: dict | None) -> dict:
    x, y, w, h = _geo(win)
    top = y + TOOLBAR_PX
    height = max(1, h - TOOLBAR_PX - 8)
    width = max(1, w)
    return {"role": "document", "name": "", "x": x, "y": top, "w": width, "h": height,
            "cx": x + width // 2, "cy": top + height // 2, "via": "document"}


def _container_public(container: dict) -> dict:
    return {k: container.get(k) for k in ("role", "name", "x", "y", "w", "h", "via")}


def _scroll_container(container: dict, direction: str, amount: int,
                      use_human: bool) -> dict:
    cx = int(container.get("cx") or 0)
    cy = int(container.get("cy") or 0)
    if use_human:
        return human.human_scroll(direction, amount=amount, x=cx, y=cy)
    return desktop.scroll_at(cx, cy, direction, amount=amount)


def _settle(use_human: bool, rng: random.Random | None = None) -> None:
    if use_human:
        _sleep_ms(_rng(rng).randint(*SETTLE_HUMAN_MS))
    else:
        time.sleep(SETTLE_FAST_MS / 1000.0)


def _save_step_png(data: bytes, step: int) -> str:
    directory = os.environ.get(
        "WORKMAN_IMAGE_DIR", os.path.expanduser("~/.cache/workman/shots")
    )
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"workman-autoscroll-{step:03d}-{os.getpid()}.png")
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def _chrome_nodes() -> list[dict]:
    return a11y.tree(app="chrom", actionable_only=False, limit=4000)


def _prepare_container(selector: str | None) -> tuple[dict | None, dict | None]:
    """Focus Chrome, dump the tree, pick the scrollable. Error dict or (container, None)."""
    focused = focus()
    if not focused.get("ok"):
        return None, focused
    win = find_chrome_window() or {}
    try:
        nodes = _chrome_nodes()
    except Exception as exc:
        return None, {"ok": False, "error": f"AT-SPI unavailable: {exc}",
                      "title": focused.get("title") or _window_title()}
    fallback = _document_container(win)
    container = autoscroll.pick_container(nodes, selector, fallback)
    return container, None


def autoscroll_read(selector: str | None = None, max_scrolls: int = 40,
                    overlap_lines: int = 3, screenshots: bool = False,
                    human: bool | None = None) -> dict:
    """Walk a scrollable region and return the complete visible text.

    Container: AT-SPI match for `selector` if given, else the tallest
    scrollable, else the document. Each step captures visible text via
    the same AT-SPI path as `read_page`, scrolls ~85% of the viewport
    through the existing x11/human scroll pathway, then stitches
    overlapping captures.
    """
    container, err = _prepare_container(selector)
    if err or container is None:
        return err or {"ok": False, "error": "no scroll container"}
    use_human = _use_human_flag(human)
    rng = _rng()
    try:
        max_scrolls = max(0, min(int(max_scrolls), AUTOSCROLL_MAX))
        overlap_lines = max(0, min(int(overlap_lines), 50))
    except (TypeError, ValueError):
        max_scrolls, overlap_lines = autoscroll.DEFAULT_MAX_SCROLLS, autoscroll.DEFAULT_OVERLAP
    amount = autoscroll.wheel_ticks(container.get("h") or 0)

    def capture() -> dict:
        try:
            nodes = _chrome_nodes()
        except Exception:
            nodes = []
        text = autoscroll.lines_from_nodes(nodes, container, TEXT_ROLES)
        return {"text": text, "position": text}

    def do_scroll() -> None:
        _scroll_container(container, "down", amount, use_human)

    step = {"n": 0}

    def shot() -> str | None:
        try:
            data = desktop.screenshot()
        except Exception:
            return None
        step["n"] += 1
        return _save_step_png(data, step["n"])

    result = autoscroll.run_autoscroll(
        capture, do_scroll,
        max_scrolls=max_scrolls,
        overlap_lines=overlap_lines,
        text_cap=autoscroll.TEXT_CAP,
        settle=lambda: _settle(use_human, rng),
        screenshot=shot if screenshots else None,
    )
    result["ok"] = True
    result["human"] = use_human
    result["container"] = _container_public(container)
    return result


def autoscroll_to_top(selector: str | None = None) -> dict:
    """Scroll the autoscroll_read container back to its top."""
    container, err = _prepare_container(selector)
    if err or container is None:
        return err or {"ok": False, "error": "no scroll container"}
    use_human = human.enabled()
    rng = _rng()
    amount = autoscroll.wheel_ticks(container.get("h") or 0)

    def capture_pos() -> str:
        try:
            nodes = _chrome_nodes()
        except Exception:
            nodes = []
        return autoscroll.lines_from_nodes(nodes, container, TEXT_ROLES)

    pos = capture_pos()
    scrolls = 0
    stopped = autoscroll.STOP_MAX_SCROLLS
    for _ in range(autoscroll.DEFAULT_MAX_SCROLLS):
        _scroll_container(container, "up", amount, use_human)
        scrolls += 1
        _settle(use_human, rng)
        new = capture_pos()
        if new == pos:
            stopped = autoscroll.STOP_SCROLL_POSITION
            break
        pos = new
    return {"ok": True, "scrolls": scrolls, "stopped_because": stopped,
            "container": _container_public(container), "human": use_human}
