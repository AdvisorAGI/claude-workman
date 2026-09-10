"""Shared human cadence and pointer motion for workman.

This is the OS-level stealth channel: real xdotool motion and keystrokes
with seedable timing. Chrome helpers and the generic input tools both call
here so there is one path generator and one typing cadence.

Human Mode itself is a session flag (see set_mode / get_mode). When it is
off, callers keep their fast/direct x11 behaviour. When it is on, they
route through the helpers below. Timing is behind a seedable Random so
tests are deterministic; in real use the seed varies per session.
"""
from __future__ import annotations

import inspect
import json
import math
import os
import pathlib
import random
import re
import time

from . import desktop

# Skilled-person cadence. These are a fast typist and a Fitts-like flick,
# not a cautious hunt-and-peck and not a teleport. Bounds stay jittered so
# a site watching inter-key or pointer velocity still sees a hand.
CHAR_DELAY_MS = (40, 140)
SPACE_DELAY_MS = (80, 200)
ENTER_MIN_MS = 90
CLICK_PRESS_MS = (60, 140)
AIM_PAUSE_MS = (20, 80)
AIM_PAUSE_CHANCE = 0.4
PATH_WAYPOINTS = (4, 6)
PATH_STEPS = (6, 14)
PATH_DURATION_MS = (160, 520)
ENDPOINT_JITTER_PX = (1, 3)
OVERSHOOT_CHANCE = 0.28
OVERSHOOT_PX = (4, 12)
DISTANCE_FOR_MAX_MS = 1500.0
TYPO_CHANCE = 0.03
SCROLL_BURST = (1, 3)
SCROLL_INTRA_MS = (15, 70)
SCROLL_PAUSE_MS = (40, 180)
DOUBLE_CLICK_GAP_MS = (50, 120)
SENSITIVE_FIELDS = {
    "password", "passwd", "secret", "url", "uri", "href",
    "money", "amount", "price", "currency", "card", "cvv", "iban",
}
_MONEY_RE = re.compile(
    r"^[$€£¥]?\s*\d{1,3}([,.]\d{3})*([,.]\d{1,2})?\s*$"
)
_NEIGHBORS = {
    "a": "sqwz", "b": "vghn", "c": "xdfv", "d": "sfcxe", "e": "wrsd",
    "f": "dgcrv", "g": "fthvb", "h": "gyjbn", "i": "uokj", "j": "huknm",
    "k": "jilm", "l": "kop", "m": "njk", "n": "bhjm", "o": "iplk",
    "p": "ol", "q": "wa", "r": "edft", "s": "awedxz", "t": "rfgy",
    "u": "yhji", "v": "cfgb", "w": "qase", "x": "zsdc", "y": "tghu",
    "z": "asx",
}

_state: dict = {"on": False, "seed": None, "rng": None, "persist": True}


def reset() -> None:
    """Return session state to the default (off, no seed). For tests.

    Also turns persistence off for the rest of the process, so a test that
    flips Human Mode never writes the real switch file. Nothing in the server
    calls this; it exists for the autouse fixtures.
    """
    _state["on"] = False
    _state["seed"] = None
    _state["rng"] = None
    _state["persist"] = False


# The Human Mode switch is one flag in one file, shared with the atmos_computer
# daemon (which reads the same "human" key). Two servers, one switch, so
# "turn human mode off" cannot half-land. WORKMAN_HUMAN_STATE redirects it.
STATE_FILENAME = "settings.json"


def state_path() -> pathlib.Path:
    """The one switch file, resolved exactly the way the daemon resolves it.

    ``atmos_computer.protocol`` uses ``WORKMAN_HOME`` or the literal
    ``~/Library/Application Support/Workman`` on every platform, with no
    darwin branch. Mirroring that verbatim is the whole point: a second
    opinion about where the file lives would silently split the switch in two.
    """
    override = os.environ.get("WORKMAN_HUMAN_STATE", "").strip()
    if override:
        return pathlib.Path(override).expanduser()
    home = os.environ.get("WORKMAN_HOME", "~/Library/Application Support/Workman")
    return pathlib.Path(home).expanduser() / STATE_FILENAME


def read_persisted() -> bool | None:
    """The persisted flag, or None when nothing has been switched yet."""
    try:
        with open(state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    value = data.get("human")
    return None if value is None else bool(value)


def write_persisted(on: bool) -> bool:
    """Store the flag beside the daemon's own settings. Never raises.

    Merges rather than overwrites: the same file carries the learner DSN.
    """
    path = pathlib.Path(state_path())
    try:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data["human"] = bool(on)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def set_mode(on: bool, seed: int | None = None, persist: bool | None = None) -> dict:
    """Store the Human Mode flag and a seedable RNG, and remember the choice.

    Persisting is the point of the switch: when the owner says turn it off, it
    stays off across restarts and across both workman surfaces. ``persist``
    forces the behaviour either way; the default is to persist unless
    ``reset()`` has marked this process as a test.
    """
    _state["on"] = bool(on)
    if seed is not None:
        _state["seed"] = int(seed)
    elif on:
        _state["seed"] = random.SystemRandom().randint(0, 2**31 - 1)
    if on:
        _state["rng"] = random.Random(_state["seed"])
    else:
        _state["rng"] = None
    should = _state["persist"] if persist is None else bool(persist)
    if should:
        write_persisted(bool(on))
    return get_mode()


def get_mode() -> dict:
    return {"human_mode": bool(_state["on"]), "seed": _state["seed"]}


def enabled() -> bool:
    return bool(_state["on"])


def apply_session_default() -> dict:
    """Turn Human Mode on for a fresh session unless the environment says not to.

    Human Mode is the default because the failure mode of forgetting it is
    invisible and expensive: the pointer teleports, hover-driven UI never
    fires, and anything watching input timing sees a machine. `reset()` stays
    the way it was, off, because tests depend on that being the neutral state;
    this is the product default, applied once when the server starts.

    The switch beats the default. ``workman-switch human off`` (or the
    ``workman_set_human_mode`` tool) writes the flag to the shared state file,
    and this reads it back, so an off survives a restart. Only when nothing
    has ever been switched does the on-by-default apply.

    Set ``WORKMAN_HUMAN_MODE=0`` for a fast, non-humanised session; the
    environment still wins over both, for one process.
    """
    raw = os.environ.get("WORKMAN_HUMAN_MODE", "").strip().lower()
    if raw in {"0", "off", "false", "no"}:
        return get_mode()
    if raw in {"1", "on", "true", "yes"}:
        if not _state["on"]:
            set_mode(True, persist=False)
        return get_mode()
    persisted = read_persisted()
    want = True if persisted is None else persisted
    if want and not _state["on"]:
        set_mode(True, persist=False)
    elif not want and _state["on"]:
        set_mode(False, persist=False)
    return get_mode()


def resolve_rng(rng: random.Random | None = None) -> random.Random:
    if rng is not None:
        return rng
    if _state["rng"] is not None:
        return _state["rng"]
    return random.Random()


def session_rng() -> random.Random:
    return resolve_rng(None)


def _sleep_ms(ms: float) -> None:
    if ms > 0:
        time.sleep(ms / 1000.0)


def _ease_in_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _bezier(ctrl: list[tuple[float, float]], t: float) -> tuple[float, float]:
    pts = list(ctrl)
    if not pts:
        return (0.0, 0.0)
    t = max(0.0, min(1.0, t))
    u = 1.0 - t
    while len(pts) > 1:
        pts = [(u * a[0] + t * b[0], u * a[1] + t * b[1])
               for a, b in zip(pts, pts[1:])]
    return pts[0]


def path_duration_ms(x0: float, y0: float, x1: float, y1: float) -> int:
    dist = math.hypot(x1 - x0, y1 - y0)
    lo, hi = PATH_DURATION_MS
    t = min(1.0, dist / DISTANCE_FOR_MAX_MS)
    return int(round(lo + (hi - lo) * t))


def _waypoints(start: tuple[float, float], end: tuple[float, float],
               rng: random.Random) -> list[tuple[float, float]]:
    """4–6 Bezier control points: start, offset mids, end. Never colinear."""
    n = rng.randint(*PATH_WAYPOINTS)
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    dist = math.hypot(dx, dy)
    if dist < 1.0:
        dist = 1.0
        px, py = 0.0, 1.0
    else:
        px, py = -dy / dist, dx / dist
    n_mid = max(0, n - 2)
    pts = [start]
    sign = rng.choice((-1.0, 1.0))
    for i in range(n_mid):
        t = (i + 1) / (n_mid + 1)
        t = max(0.12, min(0.88, t + rng.uniform(-0.08, 0.08)))
        along = 0.08 + 0.18 * rng.random()
        # Alternate side so the curve is not a single bow.
        side = sign if i % 2 == 0 else -sign
        offset = along * dist * side
        pts.append((start[0] + dx * t + px * offset,
                    start[1] + dy * t + py * offset))
    pts.append(end)
    return pts


def eased_path(x0: float, y0: float, x1: float, y1: float,
               rng: random.Random | None = None) -> list[tuple[int, int, int]]:
    """Pointer path as (x, y, t_ms).

    4–6 Bezier waypoints, sampled into 6–14 eased steps over 160–520 ms
    (scaled by distance). Endpoint jitter is 1–3 px. Occasional tiny
    overshoot-and-correct is folded into the same step budget so the
    returned path stays 6–14 points. Timing is monotonic.
    """
    rng = resolve_rng(rng)
    start = (float(x0), float(y0))
    jitter = rng.randint(*ENDPOINT_JITTER_PX)
    end = (float(int(round(x1)) + rng.randint(-jitter, jitter)),
           float(int(round(y1)) + rng.randint(-jitter, jitter)))
    n_steps = rng.randint(*PATH_STEPS)
    dist = math.hypot(end[0] - start[0], end[1] - start[1])
    overshoot = rng.random() < OVERSHOOT_CHANCE and dist > 24
    n_correct = 0
    curve_end = end
    if overshoot:
        n_correct = 2 if n_steps < 12 else 3
        n_correct = min(n_correct, n_steps - 6)
        if n_correct < 2:
            overshoot = False
            n_correct = 0
        else:
            mag = rng.randint(*OVERSHOOT_PX)
            ux = (end[0] - start[0]) / dist
            uy = (end[1] - start[1]) / dist
            curve_end = (end[0] + ux * mag, end[1] + uy * mag)
    n_curve = n_steps - n_correct
    ctrl = _waypoints(start, curve_end, rng)
    duration = path_duration_ms(start[0], start[1], end[0], end[1])
    denom = max(n_steps - 1, 1)
    points: list[tuple[int, int, int]] = []

    def sample(i: int, x: float, y: float) -> None:
        t_ms = int(round(duration * (i / denom)))
        if points:
            t_ms = max(t_ms, points[-1][2])
        points.append((int(round(x)), int(round(y)), t_ms))

    for i in range(n_curve):
        t = 0.0 if n_curve == 1 else i / (n_curve - 1)
        x, y = _bezier(ctrl, _ease_in_out(t))
        sample(i, x, y)
    if overshoot:
        sx, sy = float(points[-1][0]), float(points[-1][1])
        for j in range(1, n_correct + 1):
            t = j / n_correct
            e = _ease_in_out(t)
            sample(n_curve - 1 + j, sx + (end[0] - sx) * e, sy + (end[1] - sy) * e)
    if not points:
        points.append((int(round(start[0])), int(round(start[1])), 0))
    points[0] = (int(round(start[0])), int(round(start[1])), 0)
    points[-1] = (int(round(end[0])), int(round(end[1])),
                  max(duration, points[-1][2]))
    return points


def typing_cadence(text: str, rng: random.Random | None = None) -> list[int]:
    """Per-character delays in milliseconds.

    40–140 ms normally (~80–120 WPM); space gets a longer gap (80–200 ms).
    Enter/newline is never faster than 90 ms after the previous character.
    """
    rng = resolve_rng(rng)
    delays: list[int] = []
    for ch in text:
        if ch in "\n\r":
            delay = max(ENTER_MIN_MS, rng.randint(*CHAR_DELAY_MS))
        elif ch == " ":
            delay = rng.randint(*SPACE_DELAY_MS)
        else:
            delay = rng.randint(*CHAR_DELAY_MS)
        delays.append(delay)
    return delays


def enter_delay_ms(rng: random.Random | None = None) -> int:
    """Pause after the last typed character before pressing Return (>= 150 ms)."""
    rng = resolve_rng(rng)
    return max(ENTER_MIN_MS, rng.randint(*CHAR_DELAY_MS))


def click_press_ms(rng: random.Random | None = None) -> int:
    return resolve_rng(rng).randint(*CLICK_PRESS_MS)


def aim_pause_ms(rng: random.Random | None = None) -> int:
    rng = resolve_rng(rng)
    if rng.random() < AIM_PAUSE_CHANCE:
        return rng.randint(*AIM_PAUSE_MS)
    return 0


def scroll_plan(amount: int, rng: random.Random | None = None) -> list[tuple[int, int]]:
    """Erratic wheel bursts: (clicks, pause_ms_after). Sum of clicks == amount."""
    rng = resolve_rng(rng)
    remaining = max(0, int(amount))
    bursts: list[tuple[int, int]] = []
    while remaining > 0:
        n = rng.randint(SCROLL_BURST[0], min(SCROLL_BURST[1], remaining))
        pause = rng.randint(*SCROLL_PAUSE_MS)
        bursts.append((n, pause))
        remaining -= n
    return bursts


def sensitive_field(field: str = "", text: str = "") -> bool:
    f = (field or "").strip().lower()
    if f in SENSITIVE_FIELDS or any(s in f for s in SENSITIVE_FIELDS):
        return True
    t = (text or "").strip()
    tl = t.lower()
    if "://" in tl or tl.startswith("www."):
        return True
    if _MONEY_RE.match(t):
        return True
    return False


def _typo_char(ch: str, rng: random.Random) -> str | None:
    lower = ch.lower()
    n = _NEIGHBORS.get(lower)
    if not n:
        return None
    pick = n[rng.randint(0, len(n) - 1)]
    return pick.upper() if ch.isupper() else pick


def _pointer() -> tuple[int, int]:
    info = desktop.pointer_position()
    try:
        return int(info.get("x", 0) or 0), int(info.get("y", 0) or 0)
    except (TypeError, ValueError):
        return 0, 0


def human_move(x: int, y: int, rng: random.Random | None = None) -> dict:
    rng = resolve_rng(rng)
    x0, y0 = _pointer()
    path = eased_path(x0, y0, x, y, rng)
    for i, (px, py, t_ms) in enumerate(path):
        if i > 0:
            _sleep_ms(t_ms - path[i - 1][2])
        desktop.move(px, py)
    ax, ay = path[-1][0], path[-1][1]
    return {"ok": True, "at": [ax, ay], "target": [x, y], "points": len(path),
            "duration_ms": path[-1][2], "human": True}


def human_press_click(button: int = 1, count: int = 1,
                      rng: random.Random | None = None, aim: bool = True) -> int:
    """Down/up at the current pointer. Returns the press duration used."""
    rng = resolve_rng(rng)
    if aim:
        _sleep_ms(aim_pause_ms(rng))
    press = click_press_ms(rng)
    for i in range(max(1, int(count))):
        desktop.mouse_down(button)
        _sleep_ms(press)
        desktop.mouse_up(button)
        if i < count - 1:
            _sleep_ms(rng.randint(*DOUBLE_CLICK_GAP_MS))
    return press


def human_click(x: int, y: int, rng: random.Random | None = None,
                button: int = 1, count: int = 1) -> dict:
    rng = resolve_rng(rng)
    moved = human_move(x, y, rng)
    press = human_press_click(button=button, count=count, rng=rng, aim=True)
    moved.update({"ok": True, "press_ms": press, "button": button, "count": count,
                  "human": True})
    return moved


def human_hover(x: int, y: int, settle_ms: int = 350,
                rng: random.Random | None = None) -> dict:
    moved = human_move(x, y, rng)
    _sleep_ms(max(0, settle_ms))
    moved.update({"settled_ms": settle_ms, "human": True})
    return moved


def human_drag(from_x: int, from_y: int, to_x: int, to_y: int,
               rng: random.Random | None = None) -> dict:
    rng = resolve_rng(rng)
    human_move(from_x, from_y, rng)
    desktop.mouse_down(1)
    path = eased_path(from_x, from_y, to_x, to_y, rng)
    for i, (px, py, t_ms) in enumerate(path):
        if i == 0:
            continue
        _sleep_ms(t_ms - path[i - 1][2])
        desktop.move(px, py)
    desktop.mouse_up(1)
    return {"ok": True, "from": [from_x, from_y], "to": [to_x, to_y],
            "points": len(path), "human": True}


def human_scroll(direction: str, amount: int = 3,
                 x: int | None = None, y: int | None = None,
                 rng: random.Random | None = None) -> dict:
    button = {"up": 4, "down": 5, "left": 6, "right": 7}.get(direction)
    if not button:
        return {"ok": False, "error": "direction must be up|down|left|right"}
    rng = resolve_rng(rng)
    if x is not None and y is not None:
        human_move(int(x), int(y), rng)
    plan = scroll_plan(amount, rng)
    clicks = 0
    for n, pause in plan:
        for i in range(n):
            desktop.scroll(direction, amount=1)
            clicks += 1
            if i < n - 1:
                _sleep_ms(rng.randint(*SCROLL_INTRA_MS))
        _sleep_ms(pause)
    out: dict = {"ok": True, "scrolled": direction, "amount": clicks,
                 "bursts": len(plan), "human": True}
    if x is not None and y is not None:
        out["at"] = [int(x), int(y)]
    return out


def human_mouse_button(button: int = 1, press: bool = True,
                       x: int | None = None, y: int | None = None,
                       rng: random.Random | None = None) -> dict:
    rng = resolve_rng(rng)
    if x is not None and y is not None:
        human_move(int(x), int(y), rng)
    if press:
        _sleep_ms(aim_pause_ms(rng))
        return desktop.mouse_down(button=button)
    return desktop.mouse_up(button=button)


# Typing one character at a time is what makes the cadence human, and Human
# Mode wants those characters to be real virtual keycodes rather than unicode
# payloads on keycode 0, because a payload event carries no keyCode and anything
# watching the keyboard can see that. desktop.type_text is a fixed two argument
# dispatcher, so the flag has to reach the backend itself. The dispatcher is
# captured at import: while it is still the one we captured, going straight to
# the backend is the same call it would have made, and once a caller has
# replaced it (a test double, a recorder) that replacement is what they meant to
# run, so it is used as it is.
_DISPATCH_TYPE_TEXT = desktop.type_text
_TYPER_CACHE: dict = {"for": None, "typer": None, "real": False}


def _accepts_real_keys(fn) -> bool:
    try:
        return "real_keys" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _typer() -> tuple:
    """(function that types one character, whether it sends real keycodes).

    Probed once per typer rather than per character: the linux and win32
    backends take no real_keys argument, and asking them every keystroke would
    turn a missing feature into a stream of exceptions.
    """
    fn = desktop.type_text
    if _TYPER_CACHE["for"] is fn:
        return _TYPER_CACHE["typer"], _TYPER_CACHE["real"]
    keyed = None
    if fn is _DISPATCH_TYPE_TEXT:
        try:
            candidate = getattr(desktop.backend(), "type_text", None)
        except Exception:
            candidate = None
        if candidate is not None and _accepts_real_keys(candidate):
            keyed = candidate

    if keyed is None:
        def typer(ch: str) -> dict:
            return fn(ch, delay_ms=0)
    else:
        def typer(ch: str) -> dict:
            return keyed(ch, delay_ms=0, real_keys=True)

    real = keyed is not None
    _TYPER_CACHE.update({"for": fn, "typer": typer, "real": real})
    return typer, real


def _run_via(vias: list[str]) -> str | None:
    """One via for a whole typing run.

    A keycode anywhere in the run means the run was typed on real keys, since
    only the characters that have no key on this layout fall back to unicode.
    """
    for via in vias:
        if via and "keycode" in via:
            return via
    return vias[-1] if vias else None


def human_type(text: str, rng: random.Random | None = None,
               typos: bool = False, field: str = "") -> dict:
    rng = resolve_rng(rng)
    allow_typos = bool(typos) and not sensitive_field(field, text)
    delays = typing_cadence(text, rng)
    type_one, real_keys = _typer()
    typed = 0
    corrections = 0
    vias: list[str] = []
    fallbacks = 0

    def send(ch: str) -> None:
        nonlocal fallbacks
        result = type_one(ch)
        if not isinstance(result, dict):
            return
        via = result.get("via")
        if via:
            vias.append(via)
        try:
            fallbacks += int(result.get("unicode_fallbacks") or 0)
        except (TypeError, ValueError):
            pass

    for ch, delay in zip(text, delays):
        if allow_typos and ch not in " \n\r" and rng.random() < TYPO_CHANCE:
            wrong = _typo_char(ch, rng)
            if wrong and wrong != ch:
                send(wrong)
                _sleep_ms(rng.randint(*CHAR_DELAY_MS))
                desktop.press_key("BackSpace")
                _sleep_ms(rng.randint(50, 150))
                corrections += 1
        if ch in "\n\r":
            _sleep_ms(max(delay, ENTER_MIN_MS))
            desktop.press_key("Return")
        else:
            send(ch)
            _sleep_ms(delay)
        typed += 1
    return {"ok": True, "typed_len": typed, "human": True,
            "typos": allow_typos, "corrections": corrections,
            "real_keys": real_keys, "via": _run_via(vias),
            "unicode_fallbacks": fallbacks}
