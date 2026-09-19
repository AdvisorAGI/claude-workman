"""Bot safety check for Human Mode.

Human Mode makes each single input look human (curved pointer, typing
cadence). It does not stop a session from *behaving* like a script: typing
addresses into the location bar a dozen times, pasting every value, acting
every 200 ms, waiting exactly 2.000 s between steps. On a site that scores
behaviour (Stripe, Google Ads, Meta, banks) those patterns are what get an
owner's account challenged or locked.

This module watches the agent's own input stream and does three things:

1. Guards: before an input on a sensitive site it refuses the strong tells
   (Human Mode off, synthetic DOM events, address-bar navigation, pasting a
   short value) with an instruction saying what a person would do instead.
2. Pacing: it enforces a human minimum gap between actions, a per-minute
   budget, and breaks up a metronome rhythm. Waits get natural jitter.
3. Measurement: every tell is counted, and `report()` returns a 0-100 score
   with the reasons, so a session can check itself (tool `bot_safety_check`).

It never solves or bypasses a challenge page: that stays with the owner
(see chrome.CHALLENGE_INSTRUCTION). Everything here is about the agent not
causing one on the owner's own accounts.

Modes (env WORKMAN_BOT_SAFETY or set_mode): enforce (default) refuses and
paces, warn only measures and annotates, off does nothing.
"""
from __future__ import annotations

import math
import os
import random
import statistics
import threading
import time
from collections import Counter, deque

MODES = ("enforce", "warn", "off")

#: Active-window title fragments that mark a site where behaviour is scored
#: and a lockout costs real money. Lower-case substring match.
SENSITIVE_MARKERS = (
    "stripe", "google ads", "merchant center", "ads manager", "meta business",
    "business suite", "facebook", "instagram", "paypal", "wise", "payoneer",
    "bank", "gmail", "google account", "sign in", "log in", "login",
    "checkout", "payment", "billing", "invoice", "linkedin", "tiktok",
    "x.com", "twitter", "shopify", "hetzner", "cloudflare", "godaddy",
    "namecheap", "github",
)
#: Keys that jump to the address bar.
ADDRESS_BAR_KEYS = frozenset(("ctrl+l", "alt+d", "f6"))
PASTE_KEYS = frozenset(("ctrl+v", "shift+insert", "ctrl+shift+v"))
#: A person pastes long things; a short value they type.
PASTE_MIN_CHARS = 60
#: Human floor between two discrete actions (see, decide, move): log-normal
#: around ~650 ms, never under MIN_GAP_MS.
MIN_GAP_MS = 350
GAP_LOGNORM = (math.log(650.0), 0.35)
#: Actions per rolling minute before pacing kicks in.
RATE_PER_MIN = {"sensitive": 30, "normal": 60}
#: Coefficient of variation below this over RHYTHM_WINDOW intervals is a
#: metronome.
RHYTHM_WINDOW = 6
RHYTHM_MIN_CV = 0.15
RHYTHM_BREAK_MS = (400, 1600)
#: Wait jitter: sigma of the log-normal multiplier, and the clip band.
WAIT_SIGMA = 0.18
WAIT_CLIP = (0.7, 1.4)

#: Tools that are OS-level input (paced, recorded).
INPUT_TOOLS = frozenset((
    "click", "move", "hover", "drag", "scroll", "mouse_button", "type_text",
    "press_key", "key_hold", "click_element", "perform_element_action",
    "set_element_value", "chrome_click_text", "chrome_type", "chrome_open_url",
    "bridge_locate_and_click",
))
#: Synthetic channels a page can tell apart from real input.
SYNTHETIC_TOOLS = frozenset(("bridge_dom_click", "bridge_dom_type"))

#: Tools that read the page without looking at it. A person reads the screen;
#: an agent that cannot see well starts scraping, page after page.
SCRAPE_TOOLS = frozenset((
    "chrome_read_page", "chrome_autoscroll_read", "bridge_dom_read",
    "bridge_dom_query", "accessibility_tree",
))
#: Reads allowed in a row on a sensitive site before a look is required.
SCRAPE_STREAK_MAX = 2

#: Penalty per tell for the score (100 = clean, clipped at 0).
PENALTY = {
    "human_mode_off": 25, "synthetic_input": 20, "address_bar_nav": 6,
    "short_paste": 5, "too_fast": 2, "rate_burst": 4, "metronome": 4,
    "round_wait": 1, "challenge_seen": 30,
    "page_scrape": 1, "zombie_scrape": 5, "scrape_scroll": 8,
}
ADVICE = {
    "human_mode_off": "Keep Human Mode on (workman_set_human_mode on=true).",
    "synthetic_input": "Use bridge_locate_and_click / type_text, not bridge_dom_click/type.",
    "address_bar_nav": "Navigate by clicking links and menus on the page, not ctrl+l + URL.",
    "short_paste": "Type short values with type_text; paste only long text.",
    "too_fast": "Look (screenshot/zoom) between actions; do not fire inputs back to back.",
    "rate_burst": "Slow down: fewer actions per minute on this site.",
    "metronome": "Vary the pace; identical gaps between actions read as a script.",
    "round_wait": "Prefer wait_for_element / chrome_wait_load over fixed waits.",
    "challenge_seen": "A bot challenge appeared: stop, leave it to the owner.",
    "page_scrape": "Read the screen: screenshot, then zoom on what you need.",
    "zombie_scrape": "Stop scraping page after page; look (screenshot + zoom) first.",
    "scrape_scroll": "Do not machine-scroll a whole page on this site; scroll and zoom like a reader.",
}

_lock = threading.RLock()
_state: dict = {}


def reset(mode: str | None = None) -> None:
    """Fresh session state. For tests and server start."""
    with _lock:
        _state.clear()
        _state.update({
            "mode": _env_mode() if mode is None else _check_mode(mode),
            "times": deque(maxlen=200),      # monotonic time of each input
            "tells": Counter(),
            "refused": Counter(),
            "actions": 0,
            "sensitive_actions": 0,
            "clipboard_len": None,           # length of text the agent last set
            "slept_ms": 0.0,
            "last_title": "",
            "scrape_streak": 0,
            "rng": random.Random(),
        })


def _env_mode() -> str:
    raw = os.environ.get("WORKMAN_BOT_SAFETY", "enforce").strip().lower()
    return raw if raw in MODES else "enforce"


def _check_mode(mode: str) -> str:
    m = (mode or "").strip().lower()
    if m not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    return m


def set_mode(mode: str) -> dict:
    with _lock:
        _state["mode"] = _check_mode(mode)
    return {"ok": True, "bot_safety": _state["mode"]}


def mode() -> str:
    return _state.get("mode", "enforce")


# ---- clock seams (tests replace these) -------------------------------------
def _now() -> float:
    return time.monotonic()


def _sleep(ms: float) -> None:
    if ms > 0:
        time.sleep(ms / 1000.0)
        _state["slept_ms"] = _state.get("slept_ms", 0.0) + ms


def _active_title() -> str:
    try:
        from . import desktop
        win = desktop.active_window()
    except Exception:
        return ""
    if isinstance(win, dict) and win.get("ok"):
        return str(win.get("name") or "")
    return ""


def is_sensitive(title: str) -> bool:
    t = (title or "").lower()
    return any(m in t for m in SENSITIVE_MARKERS)


def _human_on() -> bool:
    from . import human
    return human.enabled()


# ---- guards ----------------------------------------------------------------
def _refusal(tell: str, action: str, instruction: str, title: str) -> dict:
    return {"ok": False, "error": "bot_safety", "tell": tell, "action": action,
            "site": title, "instruction": instruction}


def note_clipboard(text: str) -> None:
    with _lock:
        _state["clipboard_len"] = len(text or "")


def _tells_for(name: str, args: dict, human_on: bool) -> list[tuple[str, str]]:
    """(tell, instruction) pairs this action would show, before pacing."""
    out: list[tuple[str, str]] = []
    if name in SYNTHETIC_TOOLS:
        out.append(("synthetic_input",
                    "Synthetic DOM events are detectable. Use bridge_locate_and_click, "
                    "or bridge_dom_locate then click/type_text with Human Mode."))
    elif name in INPUT_TOOLS and not human_on:
        out.append(("human_mode_off",
                    "Human Mode is off on a sensitive site. Call "
                    "workman_set_human_mode(on=true) first."))
    if name == "chrome_type" and args.get("human") is False:
        out.append(("synthetic_input", "chrome_type human=False is instant typing; use human=True."))
    if name == "press_key":
        key = str(args.get("key", "")).strip().lower().replace(" ", "")
        if key in ADDRESS_BAR_KEYS:
            out.append(("address_bar_nav",
                        "Do not jump to the address bar on this site. Click the link, tab "
                        "or menu item on the page. If the target truly is not on screen, "
                        "open it with chrome_open_url(new_tab=True) once."))
        elif key in PASTE_KEYS:
            n = _state.get("clipboard_len")
            if n is not None and n < PASTE_MIN_CHARS:
                out.append(("short_paste",
                            f"Pasting a {n}-char value is a tell. Type it with type_text "
                            "(Human Mode cadence). Paste only long text."))
    return out


def before(name: str, args: dict | None = None) -> dict | None:
    """Run before an input. Returns a refusal dict to stop it, or None to go
    ahead (after any pacing sleep)."""
    args = args or {}
    with _lock:
        if not _state:
            reset()
        m = _state["mode"]
        if m == "off":
            return None
        title = _active_title()
        _state["last_title"] = title
        sensitive = is_sensitive(title)
        tells = _tells_for(name, args, _human_on())
        for tell, instruction in tells:
            _state["tells"][tell] += 1
            if m == "enforce" and sensitive:
                _state["refused"][tell] += 1
                return _refusal(tell, name, instruction, title)
        if name in INPUT_TOOLS or name in SYNTHETIC_TOOLS:
            _pace(sensitive, enforce=(m == "enforce"))
        return None


def note_look() -> None:
    """A screenshot or zoom: the agent looked, the scrape streak ends."""
    with _lock:
        if _state:
            _state["scrape_streak"] = 0


def before_read(name: str) -> dict | None:
    """Run before a page-scraping read. Counts it; on a sensitive site in
    enforce mode refuses a whole-page autoscroll and a scrape streak with no
    look in between. Returns a refusal dict or None."""
    with _lock:
        if not _state:
            reset()
        m = _state["mode"]
        if m == "off" or name not in SCRAPE_TOOLS:
            return None
        title = _active_title()
        sensitive = is_sensitive(title)
        _state["tells"]["page_scrape"] += 1
        _state["scrape_streak"] += 1
        tell = instruction = None
        if name == "chrome_autoscroll_read":
            tell, instruction = "scrape_scroll", (
                "Machine-scrolling the whole page is a tell here. Take a screenshot, "
                "zoom on the part you need, scroll a little with scroll(), repeat.")
        elif _state["scrape_streak"] > SCRAPE_STREAK_MAX:
            tell, instruction = "zombie_scrape", (
                "Too many page reads without looking. Take a screenshot, zoom on the "
                "region you need to read, then act on what you see.")
        if tell is None:
            return None
        _state["tells"][tell] += 1
        if m == "enforce" and sensitive:
            _state["refused"][tell] += 1
            return _refusal(tell, name, instruction, title)
        return None


def after(name: str, result=None) -> None:
    """Record a finished input."""
    with _lock:
        if not _state:
            reset()
        if _state["mode"] == "off":
            return
        _state["times"].append(_now())
        _state["actions"] += 1
        if is_sensitive(_state.get("last_title", "")):
            _state["sensitive_actions"] += 1
        if isinstance(result, dict) and result.get("error") == "bot_challenge":
            _state["tells"]["challenge_seen"] += 1


def _pace(sensitive: bool, enforce: bool) -> None:
    times = _state["times"]
    if not times:
        return
    rng = _state["rng"]
    now = _now()
    wait_ms = 0.0
    # 1. human minimum gap since the last input
    gap_ms = (now - times[-1]) * 1000.0
    floor = max(MIN_GAP_MS, rng.lognormvariate(*GAP_LOGNORM)) if sensitive else MIN_GAP_MS
    if gap_ms < floor:
        _state["tells"]["too_fast"] += 1
        wait_ms = max(wait_ms, floor - gap_ms)
    # 2. per-minute budget
    budget = RATE_PER_MIN["sensitive" if sensitive else "normal"]
    recent = [t for t in times if now - t < 60.0]
    if len(recent) >= budget:
        _state["tells"]["rate_burst"] += 1
        wait_ms = max(wait_ms, (60.0 - (now - recent[-budget])) * 1000.0)
    # 3. metronome rhythm
    if len(times) > RHYTHM_WINDOW:
        tail = list(times)[-(RHYTHM_WINDOW + 1):]
        gaps = [b - a for a, b in zip(tail, tail[1:])]
        mean = statistics.fmean(gaps)
        if mean > 0 and statistics.pstdev(gaps) / mean < RHYTHM_MIN_CV:
            _state["tells"]["metronome"] += 1
            wait_ms = max(wait_ms, rng.uniform(*RHYTHM_BREAK_MS))
    if enforce and wait_ms > 0:
        _sleep(wait_ms)


def jitter_wait(seconds: float) -> float:
    """A person does not wait exactly 2.000 s. Returns the seconds to sleep:
    unchanged when off, jittered otherwise. Counts round waits."""
    with _lock:
        if not _state:
            reset()
        if _state["mode"] == "off" or seconds <= 0:
            return seconds
        if float(seconds).is_integer():
            _state["tells"]["round_wait"] += 1
        mult = _state["rng"].lognormvariate(0.0, WAIT_SIGMA)
        mult = min(max(mult, WAIT_CLIP[0]), WAIT_CLIP[1])
        return round(seconds * mult, 3)


# ---- measurement -----------------------------------------------------------
def report() -> dict:
    """Score the session so far: 100 = reads as a person, lower = scripted.
    Lists every tell seen, what was refused, and what to change."""
    with _lock:
        if not _state:
            reset()
        tells = dict(_state["tells"])
        penalty = sum(PENALTY.get(k, 1) * v for k, v in tells.items())
        score = max(0, 100 - penalty)
        times = list(_state["times"])
        now = _now()
        per_min = sum(1 for t in times if now - t < 60.0)
        cv = None
        if len(times) >= 3:
            gaps = [b - a for a, b in zip(times, times[1:])][-20:]
            mean = statistics.fmean(gaps)
            cv = round(statistics.pstdev(gaps) / mean, 3) if mean > 0 else 0.0
        title = _active_title()
        verdict = "human" if score >= 85 else "watch" if score >= 60 else "bot-like"
        return {
            "ok": True,
            "bot_safety": _state["mode"],
            "human_mode": _human_on(),
            "score": score,
            "verdict": verdict,
            "active_window": title,
            "sensitive_site": is_sensitive(title),
            "actions": _state["actions"],
            "sensitive_actions": _state["sensitive_actions"],
            "actions_last_minute": per_min,
            "interval_cv": cv,
            "paced_ms": round(_state["slept_ms"]),
            "tells": tells,
            "refused": dict(_state["refused"]),
            "advice": [ADVICE[k] for k in sorted(tells, key=lambda k: -tells[k]) if k in ADVICE],
        }


reset()
