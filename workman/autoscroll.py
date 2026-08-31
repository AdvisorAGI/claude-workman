"""Pure autoscroll-and-read: stitch overlapping captures, stop, cap.

Chrome's live tools in `chrome.py` supply capture/scroll. This module has no
browser, no X11, and no sleeps — tests stub those functions.
"""
from __future__ import annotations

import re

TEXT_CAP = 200000
SCROLL_FRACTION = 0.85
DEFAULT_OVERLAP = 3
DEFAULT_MAX_SCROLLS = 40
PX_PER_WHEEL = 100

STOP_SCROLL_POSITION = "scroll_position"
STOP_NO_NEW_TEXT = "no_new_text"
STOP_MAX_SCROLLS = "max_scrolls"

SCROLL_ROLES = {
    "scroll pane", "scrollpane", "document web", "document", "viewport",
    "article", "main", "region", "list",
}
# Window chrome is tall but is not the page's scrollable region.
_EXCLUDE_ROLES = {
    "frame", "application", "window", "root pane", "glass pane",
    "page tab list", "tool bar", "toolbar", "menu bar", "push button",
}

_CSS_ID = re.compile(r"#([\w-]+)")
_CSS_CLASS = re.compile(r"\.([\w-]+)")
_CSS_ATTR = re.compile(
    r"\[(?:aria-label|name|id|data-testid)=['\"]?([^'\"]+)['\"]?\]", re.I
)


def nonempty_lines(text: str) -> list[str]:
    return [ln for ln in (text or "").splitlines() if ln.strip()]


def _join(lines: list[str]) -> str:
    return "\n".join(lines)


def find_subseq(haystack: list[str], needle: list[str]) -> int | None:
    """Index of `needle` as a contiguous slice of `haystack`, or None."""
    if not needle:
        return 0
    n = len(needle)
    last = len(haystack) - n
    if last < 0:
        return None
    for i in range(last + 1):
        if haystack[i:i + n] == needle:
            return i
    return None


def stitch_pair(prev: str, nxt: str, overlap_lines: int = DEFAULT_OVERLAP) -> str:
    """Append `nxt` onto `prev`, dropping the overlapping tail/head.

    Matching is on the last `overlap_lines` non-empty lines of `prev`. Identical
    chunks and a next-chunk fully contained in `prev` add nothing.
    """
    prev_ne = nonempty_lines(prev)
    next_ne = nonempty_lines(nxt)
    if not next_ne:
        return _join(prev_ne)
    if not prev_ne:
        return _join(next_ne)
    if prev_ne == next_ne:
        return _join(prev_ne)

    n = max(0, int(overlap_lines))
    if n > 0:
        needle = prev_ne[-n:] if len(prev_ne) >= n else prev_ne
        idx = find_subseq(next_ne, needle)
        if idx is not None:
            return _join(prev_ne + next_ne[idx + len(needle):])

    if find_subseq(prev_ne, next_ne) is not None:
        return _join(prev_ne)
    return _join(prev_ne + next_ne)


def stitch_chunks(chunks: list[str], overlap_lines: int = DEFAULT_OVERLAP) -> str:
    if not chunks:
        return ""
    acc = chunks[0]
    for chunk in chunks[1:]:
        acc = stitch_pair(acc, chunk, overlap_lines)
    return acc


def cap_text(text: str, cap: int = TEXT_CAP) -> tuple[str, bool]:
    """Return `(text, truncated)`. Truncation is a prefix cut at `cap` chars."""
    cap = max(0, int(cap))
    raw = text or ""
    if len(raw) > cap:
        return raw[:cap], True
    return raw, False


def wheel_ticks(visible_height: int, fraction: float = SCROLL_FRACTION,
                px_per_tick: int = PX_PER_WHEEL) -> int:
    """Wheel clicks covering ~`fraction` of the visible height (at least 1)."""
    px = max(0.0, float(visible_height or 0) * float(fraction))
    ticks = int(px / max(1, int(px_per_tick)) + 0.5)
    return max(1, ticks)


def added_new_lines(prev_text: str, nxt: str,
                    overlap_lines: int = DEFAULT_OVERLAP) -> bool:
    stitched = stitch_pair(prev_text, nxt, overlap_lines)
    return nonempty_lines(stitched) != nonempty_lines(prev_text)


def stop_after_step(*, prev_position, position, prev_text: str, text: str,
                    overlap_lines: int, scrolls: int,
                    max_scrolls: int) -> str | None:
    """Why to stop after a scroll+capture, or None to keep going.

    Order: scroll position did not advance, then no new lines, then max_scrolls.
    """
    if position == prev_position:
        return STOP_SCROLL_POSITION
    if not added_new_lines(prev_text, text, overlap_lines):
        return STOP_NO_NEW_TEXT
    if scrolls >= max_scrolls:
        return STOP_MAX_SCROLLS
    return None


def selector_needles(selector: str) -> list[str]:
    """Raw selector plus CSS id/class/attr tokens, longest first."""
    s = (selector or "").strip()
    if not s:
        return []
    found = [s]
    found.extend(_CSS_ID.findall(s))
    found.extend(_CSS_CLASS.findall(s))
    found.extend(_CSS_ATTR.findall(s))
    uniq: list[str] = []
    seen: set[str] = set()
    for token in found:
        key = token.lower()
        if key and key not in seen:
            seen.add(key)
            uniq.append(token)
    uniq.sort(key=lambda t: len(t), reverse=True)
    return uniq


def _el_matches(el: dict, needles: list[str]) -> bool:
    blob = f"{el.get('name') or ''} {el.get('role') or ''}".lower()
    return any(n.lower() in blob for n in needles)


def _as_box(el: dict, via: str) -> dict:
    x = int(el.get("x") or 0)
    y = int(el.get("y") or 0)
    w = int(el.get("w") or 0)
    h = int(el.get("h") or 0)
    return {
        "role": el.get("role") or "",
        "name": el.get("name") or "",
        "x": x, "y": y, "w": w, "h": h,
        "cx": x + w // 2, "cy": y + h // 2,
        "via": via,
    }


def pick_container(nodes: list[dict], selector: str | None = None,
                   fallback: dict | None = None, min_h: int = 120,
                   min_w: int = 120) -> dict:
    """Selector match if given, else the tallest scrollable-looking node.

    Falls back to `fallback` (the document / window content area) when nothing
    qualifies. `selector` is an AT-SPI name/role substring; CSS id/class/attr
    tokens (`#foo`, `.bar`, `[aria-label=...]`) are accepted as hints.
    """
    fallback = dict(fallback or {})
    if "via" not in fallback:
        fallback["via"] = "document"
    if "cx" not in fallback or "cy" not in fallback:
        fallback = _as_box(fallback, fallback.get("via") or "document")

    def tallest(group: list[dict], via: str) -> dict:
        hit = max(group, key=lambda e: (e.get("h") or 0, e.get("w") or 0))
        return _as_box(hit, via)

    if selector and (selector or "").strip():
        needles = selector_needles(selector)
        hits = [el for el in nodes
                if (el.get("h") or 0) > 0 and _el_matches(el, needles)]
        if hits:
            return tallest(hits, "selector")

    cands: list[dict] = []
    fallback_h = fallback.get("h") or 0
    for el in nodes:
        h, w = el.get("h") or 0, el.get("w") or 0
        if h < min_h or w < min_w:
            continue
        role = (el.get("role") or "").lower()
        if role in _EXCLUDE_ROLES:
            continue
        if role in SCROLL_ROLES or (fallback_h and h >= fallback_h * 0.45):
            cands.append(el)
    if cands:
        return tallest(cands, "tallest")
    return fallback


def lines_from_nodes(nodes: list[dict], container: dict | None,
                     text_roles: set[str]) -> str:
    """Named text-role nodes whose center sits inside `container`."""
    texts: list[str] = []
    seen: set[str] = set()
    box = container or {}
    bx, by = box.get("x") or 0, box.get("y") or 0
    bw, bh = box.get("w") or 0, box.get("h") or 0
    clip = bw > 0 and bh > 0
    roles = {r.lower() for r in text_roles}
    for el in nodes:
        name = (el.get("name") or "").strip()
        if not name or name in seen:
            continue
        if (el.get("role") or "").lower() not in roles:
            continue
        if clip:
            cx = (el.get("x") or 0) + (el.get("w") or 0) // 2
            cy = (el.get("y") or 0) + (el.get("h") or 0) // 2
            if not (bx <= cx <= bx + bw and by <= cy <= by + bh):
                continue
        seen.add(name)
        texts.append(name)
    return "\n".join(texts)


def run_autoscroll(capture, scroll, *, max_scrolls: int = DEFAULT_MAX_SCROLLS,
                   overlap_lines: int = DEFAULT_OVERLAP, text_cap: int = TEXT_CAP,
                   settle=None, screenshot=None) -> dict:
    """Walk a region: capture, scroll, stitch. `capture`/`scroll` are injected.

    `capture()` → `{"text": str, "position": comparable}`. Missing `position`
    defaults to the captured text. `scroll()` advances. `settle()` is an
    optional bounded wait after each scroll. `screenshot()` optional path.
    """
    max_scrolls = max(0, int(max_scrolls))
    overlap_lines = max(0, int(overlap_lines))
    chunks: list[str] = []
    shots: list[str] = []
    scrolls = 0

    def snap() -> tuple[str, object]:
        cap = capture() or {}
        text = cap.get("text") or ""
        pos = cap["position"] if "position" in cap else text
        if screenshot is not None:
            path = screenshot()
            if path:
                shots.append(path)
        return text, pos

    text, pos = snap()
    chunks.append(text)
    stitched = text
    stopped_because = STOP_MAX_SCROLLS

    while scrolls < max_scrolls:
        scroll()
        scrolls += 1
        if settle is not None:
            settle()
        new_text, new_pos = snap()
        reason = stop_after_step(
            prev_position=pos, position=new_pos,
            prev_text=stitched, text=new_text,
            overlap_lines=overlap_lines, scrolls=scrolls,
            max_scrolls=max_scrolls,
        )
        if reason in (STOP_SCROLL_POSITION, STOP_NO_NEW_TEXT):
            stopped_because = reason
            break
        chunks.append(new_text)
        stitched = stitch_pair(stitched, new_text, overlap_lines)
        pos = new_pos
        if reason == STOP_MAX_SCROLLS:
            stopped_because = reason
            break

    out, truncated = cap_text(stitched, text_cap)
    result = {
        "text": out,
        "chunks": chunks,
        "scrolls": scrolls,
        "stopped_because": stopped_because,
        "truncated": truncated,
    }
    if screenshot is not None:
        result["screenshots"] = shots
    return result
