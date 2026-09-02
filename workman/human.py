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

import math
import random
import re
import time

from . import desktop

CHAR_DELAY_MS = (50, 300)
SPACE_DELAY_MS = (150, 300)
ENTER_MIN_MS = 150
CLICK_PRESS_MS = (60, 140)
AIM_PAUSE_MS = (20, 80)
AIM_PAUSE_CHANCE = 0.4
PATH_WAYPOINTS = (4, 6)
PATH_STEPS = (8, 20)
PATH_DURATION_MS = (300, 900)
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

_state: dict = {"on": False, "seed": None, "rng": None}


def reset() -> None:
    """Return session state to the default (off, no seed). For tests."""
    _state["on"] = False
    _state["seed"] = None
    _state["rng"] = None


def set_mode(on: bool, seed: int | None = None) -> dict:
    """Store the Human Mode flag and a seedable RNG in module state."""
    _state["on"] = bool(on)
    if seed is not None:
        _state["seed"] = int(seed)
    elif on:
        _state["seed"] = random.SystemRandom().randint(0, 2**31 - 1)
    if on:
        _state["rng"] = random.Random(_state["seed"])
    else:
        _state["rng"] = None
    return get_mode()


def get_mode() -> dict:
    return {"human_mode": bool(_state["on"]), "seed": _state["seed"]}


def enabled() -> bool:
    return bool(_state["on"])


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

    4–6 Bezier waypoints, sampled into 8–20 eased steps over 300–900 ms
    (scaled by distance). Endpoint jitter is 1–3 px. Occasional tiny
    overshoot-and-correct is folded into the same step budget so the
    returned path stays 8–20 points. Timing is monotonic.
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

    50–300 ms normally; space gets a longer gap (150–300 ms). Enter/newline
    is never faster than 150 ms after the previous character.
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


def human_type(text: str, rng: random.Random | None = None,
               typos: bool = False, field: str = "") -> dict:
    rng = resolve_rng(rng)
    allow_typos = bool(typos) and not sensitive_field(field, text)
    delays = typing_cadence(text, rng)
    typed = 0
    corrections = 0
    for ch, delay in zip(text, delays):
        if allow_typos and ch not in " \n\r" and rng.random() < TYPO_CHANCE:
            wrong = _typo_char(ch, rng)
            if wrong and wrong != ch:
                desktop.type_text(wrong, delay_ms=0)
                _sleep_ms(rng.randint(*CHAR_DELAY_MS))
                desktop.press_key("BackSpace")
                _sleep_ms(rng.randint(50, 150))
                corrections += 1
        if ch in "\n\r":
            _sleep_ms(max(delay, ENTER_MIN_MS))
            desktop.press_key("Return")
        else:
            desktop.type_text(ch, delay_ms=0)
            _sleep_ms(delay)
        typed += 1
    return {"ok": True, "typed_len": typed, "human": True,
            "typos": allow_typos, "corrections": corrections}
