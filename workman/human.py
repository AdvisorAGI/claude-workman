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

# ---- typing: log-normal intervals, faster common bigrams, word pauses --------
#: Clip bands for the per-character flight time (after the key goes up).
CHAR_DELAY_MS = (50, 600)
SPACE_DELAY_MS = (120, 1200)
ENTER_MIN_MS = 150
#: Log-normal parameters (natural-log mean, sigma) for the inter-key interval
#: and the key dwell, generic touch-typist figures rather than anyone's own.
IKI_LOGNORM = (math.log(170.0), 0.45)
DWELL_LOGNORM = (math.log(80.0), 0.30)
#: Key held down per keystroke, clip band.
KEY_DWELL_MS = (35, 180)
#: Frequent English bigrams are typed faster than the average pair.
FAST_BIGRAMS = frozenset((
    "th", "he", "in", "er", "an", "re", "on", "at", "en", "nd", "ti", "es",
    "or", "te", "of", "ed", "is", "it", "al", "ar", "st", "to", "nt", "ng",
    "se", "ha", "as", "ou", "io", "le", "ve", "co", "me", "de", "hi", "ri",
    "ro", "ic", "ne", "ea", "ra", "ce", "li", "ch", "ll", "be", "ma", "si",
    "om", "ur",
))
FAST_BIGRAM_FACTOR = 0.7
#: Word boundaries are slower, and now and then a person stops to think.
SPACE_FACTOR = 1.6
WORD_PAUSE_CHANCE = 0.08
WORD_PAUSE_MS = (300, 900)
SENTENCE_PAUSE_CHANCE = 0.5
SENTENCE_PAUSE_MS = (500, 1500)
CLICK_PRESS_MS = (60, 140)
#: A person settles on the target before pressing: always, 20-120 ms.
AIM_PAUSE_MS = (20, 120)
AIM_PAUSE_CHANCE = 1.0
PATH_WAYPOINTS = (4, 6)
#: Pointer samples per second along a path. Real mice report at 60-125 Hz
#: (and more), so a 600 ms move is tens of motion events, never eight.
MOTION_HZ = (60, 125)
PATH_MIN_STEPS = 8
#: Fitts' law: MT = a + b * log2(D / W + 1), in ms, generic mouse figures,
#: with log-normal noise, clipped to PATH_DURATION_MS. W is the target size;
#: a plain move to a point assumes DEFAULT_TARGET_PX.
FITTS_A_MS = 120.0
FITTS_B_MS = 140.0
FITTS_NOISE_SIGMA = 0.12
DEFAULT_TARGET_PX = 24.0
PATH_DURATION_MS = (120, 1400)
#: Hand tremor on the way: Gaussian pixel noise on intermediate samples.
TREMOR_PX = 0.6
ENDPOINT_JITTER_PX = (1, 3)
OVERSHOOT_CHANCE = 0.28
#: Long moves overshoot more often; this is the chance at LONG_MOVE_PX and up.
OVERSHOOT_CHANCE_LONG = 0.5
LONG_MOVE_PX = 400.0
OVERSHOOT_PX = (4, 12)
DISTANCE_FOR_MAX_MS = 1500.0
#: Gaussian aim inside an element: sigma as a fraction of its size, clipped
#: to the inner band so a click never lands on the border.
AIM_SIGMA = 0.18
AIM_INNER = 0.15
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


def apply_session_default() -> dict:
    """Human Mode is the default for a fresh server. WORKMAN_HUMAN_MODE=0
    opts out for a session that wants the fast, direct path."""
    import os

    raw = os.environ.get("WORKMAN_HUMAN_MODE", "1").strip().lower()
    on = raw not in ("0", "false", "no", "off")
    seed = os.environ.get("WORKMAN_HUMAN_SEED")
    try:
        seed_val = int(seed) if seed else None
    except ValueError:
        seed_val = None
    return set_mode(on, seed=seed_val)


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
    """Minimum-jerk position profile (10t^3 - 15t^4 + 6t^5): the bell-shaped
    speed curve of a real reaching movement, zero velocity and acceleration
    at both ends."""
    t = max(0.0, min(1.0, t))
    return t * t * t * (10.0 - 15.0 * t + 6.0 * t * t)


def motion_steps(duration_ms: int, rng: random.Random | None = None) -> int:
    """How many samples a path of this length gets: a mouse-report rate drawn
    from MOTION_HZ, never fewer than PATH_MIN_STEPS."""
    rng = resolve_rng(rng)
    hz = rng.uniform(*MOTION_HZ)
    return max(PATH_MIN_STEPS, int(round(duration_ms / 1000.0 * hz)))


def jitter_point(x: int, y: int, w: int, h: int,
                 rng: random.Random | None = None) -> tuple[int, int]:
    """A click point inside an element's box, Gaussian around the centre and
    clipped to the inner band: not the exact centre every time."""
    rng = resolve_rng(rng)
    w, h = max(1, int(w)), max(1, int(h))
    cx, cy = x + w / 2.0, y + h / 2.0
    dx = rng.gauss(0.0, w * AIM_SIGMA)
    dy = rng.gauss(0.0, h * AIM_SIGMA)
    lim_x = max(0.0, w / 2.0 - w * AIM_INNER)
    lim_y = max(0.0, h / 2.0 - h * AIM_INNER)
    dx = max(-lim_x, min(lim_x, dx))
    dy = max(-lim_y, min(lim_y, dy))
    return int(round(cx + dx)), int(round(cy + dy))


def human_click_element(el: dict, rng: random.Random | None = None,
                        button: int = 1, count: int = 1) -> dict:
    """Click an element found in an accessibility tree the way a person
    does: at a Gaussian point inside its box, with a Fitts-law move sized
    to the target. `el` needs x, y, w, h (screen pixels); cx/cy are used
    when the box is unknown."""
    rng = resolve_rng(rng)
    if el.get("w") and el.get("h") and el.get("x") is not None:
        px, py = jitter_point(int(el["x"]), int(el["y"]), int(el["w"]), int(el["h"]), rng)
        target = float(min(int(el["w"]), int(el["h"])))
    else:
        px, py = int(el.get("cx", 0)), int(el.get("cy", 0))
        target = None
    out = human_click(px, py, rng=rng, button=button, count=count, target_px=target)
    out["aimed"] = [px, py]
    return out


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


def path_duration_ms(x0: float, y0: float, x1: float, y1: float,
                     target_px: float | None = None,
                     rng: random.Random | None = None) -> int:
    """Movement time by Fitts' law for this distance and target size, with
    a little log-normal noise, clipped to PATH_DURATION_MS."""
    rng = resolve_rng(rng)
    dist = math.hypot(x1 - x0, y1 - y0)
    width = max(4.0, float(target_px or DEFAULT_TARGET_PX))
    index = math.log2(dist / width + 1.0)
    ms = (FITTS_A_MS + FITTS_B_MS * index) * math.exp(rng.gauss(0.0, FITTS_NOISE_SIGMA))
    lo, hi = PATH_DURATION_MS
    return int(round(max(lo, min(hi, ms))))


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
               rng: random.Random | None = None,
               target_px: float | None = None) -> list[tuple[int, int, int]]:
    """Pointer path as (x, y, t_ms).

    4–6 Bezier waypoints sampled at a mouse-report rate (MOTION_HZ) along a
    minimum-jerk speed profile whose duration follows Fitts' law for the
    distance and target size, so a 600 px move is tens of motion events.
    Intermediate samples carry a little tremor; endpoint jitter is 1–3 px.
    A small overshoot-and-correct happens sometimes, more often on long
    moves, and is folded into the same sample budget. Timing is monotonic.
    """
    rng = resolve_rng(rng)
    start = (float(x0), float(y0))
    jitter = rng.randint(*ENDPOINT_JITTER_PX)
    end = (float(int(round(x1)) + rng.randint(-jitter, jitter)),
           float(int(round(y1)) + rng.randint(-jitter, jitter)))
    duration = path_duration_ms(start[0], start[1], end[0], end[1],
                                target_px=target_px, rng=rng)
    n_steps = motion_steps(duration, rng)
    dist = math.hypot(end[0] - start[0], end[1] - start[1])
    chance = OVERSHOOT_CHANCE + (OVERSHOOT_CHANCE_LONG - OVERSHOOT_CHANCE) * min(
        1.0, dist / LONG_MOVE_PX)
    overshoot = rng.random() < chance and dist > 24
    n_correct = 0
    curve_end = end
    if overshoot:
        n_correct = max(2, int(round(n_steps * 0.15)))
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
    denom = max(n_steps - 1, 1)
    points: list[tuple[int, int, int]] = []

    def sample(i: int, x: float, y: float) -> None:
        t_ms = int(round(duration * (i / denom)))
        if points:
            t_ms = max(t_ms, points[-1][2])
            # Tremor on the way, not at the endpoints.
            x += rng.gauss(0.0, TREMOR_PX)
            y += rng.gauss(0.0, TREMOR_PX)
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


def _lognormal_ms(params: tuple[float, float], rng: random.Random,
                  clip: tuple[int, int]) -> int:
    mu, sigma = params
    value = math.exp(rng.gauss(mu, sigma))
    return int(round(max(clip[0], min(clip[1], value))))


def typing_cadence(text: str, rng: random.Random | None = None) -> list[int]:
    """Per-character flight times in milliseconds (the gap after each key).

    Log-normal inter-key intervals; a frequent bigram ("th", "in", ...) is
    typed faster; a space is slower and now and then carries a thinking
    pause; the end of a sentence often does. Enter/newline is never faster
    than ENTER_MIN_MS after the previous character.
    """
    rng = resolve_rng(rng)
    delays: list[int] = []
    prev = ""
    for i, ch in enumerate(text):
        base = _lognormal_ms(IKI_LOGNORM, rng, (1, 10_000))
        if ch in "\n\r":
            delay = max(ENTER_MIN_MS, base)
            delay = max(CHAR_DELAY_MS[0], min(CHAR_DELAY_MS[1], delay))
        elif ch == " ":
            delay = base * SPACE_FACTOR
            if prev in ".!?" and rng.random() < SENTENCE_PAUSE_CHANCE:
                delay += rng.randint(*SENTENCE_PAUSE_MS)
            elif rng.random() < WORD_PAUSE_CHANCE:
                delay += rng.randint(*WORD_PAUSE_MS)
            delay = int(round(max(SPACE_DELAY_MS[0], min(SPACE_DELAY_MS[1], delay))))
        else:
            nxt = text[i + 1] if i + 1 < len(text) else ""
            if (ch + nxt).lower() in FAST_BIGRAMS:
                base *= FAST_BIGRAM_FACTOR
            delay = int(round(max(CHAR_DELAY_MS[0], min(CHAR_DELAY_MS[1], base))))
        delays.append(int(delay))
        prev = ch
    return delays


def enter_delay_ms(rng: random.Random | None = None) -> int:
    """Pause after the last typed character before pressing Return (>= 150 ms)."""
    rng = resolve_rng(rng)
    return max(ENTER_MIN_MS, rng.randint(*CHAR_DELAY_MS))


def key_dwell_ms(rng: random.Random | None = None) -> int:
    """How long one key stays down: log-normal, clipped to KEY_DWELL_MS."""
    return _lognormal_ms(DWELL_LOGNORM, resolve_rng(rng), KEY_DWELL_MS)


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


class Interrupted(Exception):
    """A desktop action was refused (owner pause) or failed partway through."""

    def __init__(self, result: dict):
        super().__init__(result.get("error", "interrupted"))
        self.result = result


def _act(fn, *args, **kwargs):
    """Run one desktop action. A refusal or a backend error raises Interrupted
    instead of letting the sequence carry on and report work it never did."""
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:
        raise Interrupted({"ok": False, "error": "backend_error",
                           "detail": f"{type(exc).__name__}: {exc}"}) from exc
    if isinstance(result, dict) and result.get("ok") is False:
        raise Interrupted(result)
    return result


def _stopped(exc: Interrupted, **progress) -> dict:
    """Where a sequence stopped and why, so the caller can resume from there."""
    out = {"ok": False, "error": "interrupted", "reason": exc.result, "human": True}
    out.update(progress)
    return out


def _let_go(fn, *args, **kwargs) -> bool:
    """Release a button or key from a finally block. Never raises: a release
    that fails must not replace the result being returned, so the outcome is
    reported as a bool instead."""
    try:
        result = fn(*args, **kwargs)
    except Exception:
        return False
    return not (isinstance(result, dict) and result.get("ok") is False)


def human_move(x: int, y: int, rng: random.Random | None = None,
               target_px: float | None = None) -> dict:
    rng = resolve_rng(rng)
    x0, y0 = _pointer()
    path = eased_path(x0, y0, x, y, rng, target_px=target_px)
    at = [x0, y0]
    try:
        for i, (px, py, t_ms) in enumerate(path):
            if i > 0:
                _sleep_ms(t_ms - path[i - 1][2])
            _act(desktop.move, px, py)
            at = [px, py]
    except Interrupted as exc:
        return _stopped(exc, at=at, target=[x, y])
    return {"ok": True, "at": at, "target": [x, y], "points": len(path),
            "duration_ms": path[-1][2], "human": True}


def human_press_click(button: int = 1, count: int = 1,
                      rng: random.Random | None = None, aim: bool = True) -> int:
    """Down/up at the current pointer. Returns the press duration used.

    Raises Interrupted when a press is refused. A button that went down always
    comes back up, whatever happens while it is held."""
    rng = resolve_rng(rng)
    if aim:
        _sleep_ms(aim_pause_ms(rng))
    press = click_press_ms(rng)
    for i in range(max(1, int(count))):
        _act(desktop.mouse_down, button)
        try:
            _sleep_ms(press)
        finally:
            released = _let_go(desktop.mouse_up, button)
        if not released:
            raise Interrupted({"ok": False, "error": "release_failed",
                               "button": button})
        if i < count - 1:
            _sleep_ms(rng.randint(*DOUBLE_CLICK_GAP_MS))
    return press


def human_click(x: int, y: int, rng: random.Random | None = None,
                button: int = 1, count: int = 1,
                target_px: float | None = None) -> dict:
    rng = resolve_rng(rng)
    moved = human_move(x, y, rng, target_px=target_px)
    if not moved.get("ok"):
        return moved
    try:
        press = human_press_click(button=button, count=count, rng=rng, aim=True)
    except Interrupted as exc:
        return _stopped(exc, at=moved["at"], target=[x, y], clicked=False)
    moved.update({"ok": True, "press_ms": press, "button": button, "count": count,
                  "human": True})
    return moved


def human_hover(x: int, y: int, settle_ms: int = 350,
                rng: random.Random | None = None) -> dict:
    moved = human_move(x, y, rng)
    if not moved.get("ok"):
        return moved
    _sleep_ms(max(0, settle_ms))
    moved.update({"settled_ms": settle_ms, "human": True})
    return moved


def human_drag(from_x: int, from_y: int, to_x: int, to_y: int,
               rng: random.Random | None = None) -> dict:
    rng = resolve_rng(rng)
    moved = human_move(from_x, from_y, rng)
    if not moved.get("ok"):
        return moved
    try:
        _act(desktop.mouse_down, 1)
    except Interrupted as exc:
        return _stopped(exc, at=moved["at"], dragged=False)
    path = eased_path(from_x, from_y, to_x, to_y, rng)
    at = [from_x, from_y]
    stopped: Interrupted | None = None
    try:
        for i, (px, py, t_ms) in enumerate(path):
            if i == 0:
                continue
            _sleep_ms(t_ms - path[i - 1][2])
            _act(desktop.move, px, py)
            at = [px, py]
    except Interrupted as exc:
        stopped = exc
    finally:
        released = _let_go(desktop.mouse_up, 1)
    if stopped is not None:
        return _stopped(stopped, at=at, to=[to_x, to_y], released=released,
                        **{"from": [from_x, from_y]})
    if not released:
        return {"ok": False, "error": "release_failed", "button": 1,
                "from": [from_x, from_y], "to": [to_x, to_y], "at": at,
                "released": False, "human": True}
    return {"ok": True, "from": [from_x, from_y], "to": [to_x, to_y],
            "points": len(path), "released": True, "human": True}


def human_scroll(direction: str, amount: int = 3,
                 x: int | None = None, y: int | None = None,
                 rng: random.Random | None = None) -> dict:
    button = {"up": 4, "down": 5, "left": 6, "right": 7}.get(direction)
    if not button:
        return {"ok": False, "error": "direction must be up|down|left|right"}
    rng = resolve_rng(rng)
    if x is not None and y is not None:
        moved = human_move(int(x), int(y), rng)
        if not moved.get("ok"):
            return moved
    plan = scroll_plan(amount, rng)
    clicks = 0
    try:
        for n, pause in plan:
            for i in range(n):
                _act(desktop.scroll, direction, amount=1)
                clicks += 1
                if i < n - 1:
                    _sleep_ms(rng.randint(*SCROLL_INTRA_MS))
            _sleep_ms(pause)
    except Interrupted as exc:
        return _stopped(exc, scrolled=direction, amount=clicks,
                        remaining=sum(n for n, _ in plan) - clicks)
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
        moved = human_move(int(x), int(y), rng)
        if not moved.get("ok"):
            # Pressing where the pointer actually stopped would act on the
            # wrong thing; a release is still allowed so nothing stays held.
            if press:
                moved["pressed"] = False
                return moved
            out = desktop.mouse_up(button=button)
            if isinstance(out, dict):
                out["move"] = moved
            return out
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
    stray = False
    try:
        for ch, delay in zip(text, delays):
            if allow_typos and ch not in " \n\r" and rng.random() < TYPO_CHANCE:
                wrong = _typo_char(ch, rng)
                if wrong and wrong != ch:
                    _act(desktop.type_text, wrong, delay_ms=0, dwell_ms=key_dwell_ms(rng))
                    stray = True
                    _sleep_ms(rng.randint(*CHAR_DELAY_MS))
                    _act(desktop.press_key, "BackSpace")
                    stray = False
                    _sleep_ms(rng.randint(50, 150))
                    corrections += 1
            if ch in "\n\r":
                _sleep_ms(max(delay, ENTER_MIN_MS))
                _act(desktop.press_key, "Return")
            else:
                # The key is held for `dwell`; the rest of the cadence gap is
                # flight time to the next key.
                dwell = key_dwell_ms(rng)
                _act(desktop.type_text, ch, delay_ms=0, dwell_ms=dwell)
                _sleep_ms(max(20, delay - dwell))
            typed += 1
    except Interrupted as exc:
        # `remaining` is exactly what to type to finish; `uncorrected_typo`
        # means one wrong character is still in the field before it.
        return _stopped(exc, typed_len=typed, remaining=text[typed:],
                        uncorrected_typo=stray, corrections=corrections)
    return {"ok": True, "typed_len": typed, "human": True,
            "typos": allow_typos, "corrections": corrections}
