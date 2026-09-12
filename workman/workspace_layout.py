"""Workspace layout — arrange the screen instead of piling windows on it.

A person working on one thing puts that window across the screen; a person
comparing two things splits the screen and puts one on each side. An agent
driving a desktop does neither by default, so its monitor degrades into a stack
of overlapping windows: the human watching cannot tell what it is doing, and the
agent itself ends up screenshotting a window that is half-covered by another.

This module is the missing verb. It is deliberately thin — it owns arithmetic
and verification, and delegates every actual move to `desktop`/`a11y`, which
already know how to talk to three window managers.

Three ideas run through all of it.

**The working area, not the screen.** A window placed at the display's origin on
macOS lands under the menu bar, and on Linux under the top panel. Every layout
here is computed inside the *visible* frame (NSScreen.visibleFrame semantics,
`_NET_WORKAREA` on X11, `MONITORINFO.rcWork` on Windows), never the raw display
rectangle.

**Read back, never assume.** A window manager is free to ignore a geometry
request, and applications enforce their own minimum sizes — a browser's profile
chooser will refuse to go narrower than its content. So every placement re-reads
the geometry from the window manager afterwards, waits for it to stop changing,
and reports `honored` plus the `delta` rather than claiming the request landed.

**Verify the raise.** On some window managers activation silently fails. A
function that returns ok=True because it *sent* a raise is worse than one that
returns ok=False, because the caller then types into the wrong window.

Coordinates are global logical points across all displays, top-left origin, the
same space `screenshot` and `click` use. Nothing here assumes one display or a
particular resolution: the geometry is always read from `desktop.monitors()`.

    from workman import workspace_layout as layout

    layout.focus_and_verify("Editor")
    layout.fullscreen("Editor")
    layout.split("Editor", "Browser", ratio=0.6)
"""
from __future__ import annotations

import functools
import json
import shutil
import subprocess
import time

from . import desktop
from .platform import backend_name

#: Most tiles that stay readable side by side. Three across a 1080p-class
#: display leaves each window roughly a third of the width, which is about where
#: wrapped prose and a browser's own chrome stop being legible from normal
#: viewing distance; a fourth column buys nothing a second display would not buy
#: better. `stack` refuses above this rather than silently dropping windows.
STACK_CAP = 3

#: How long to wait for a window's geometry to stop changing after a move. Long
#: enough for an app to run its own resize logic, short enough that a failed
#: placement is still reported promptly.
_SETTLE_S = 0.6
_POLL_S = 0.06

#: A placement is "honored" within this many points. Window managers round to
#: their own increments, and a titlebar-less app can be a point or two off.
_TOLERANCE = 4

#: Consecutive identical reads that count as "this window has stopped moving",
#: for a placement that never reached the rectangle it was given.
_STABLE_READS = 3

#: A geometry write is not a promise: it is clamped against where the window
#: is at that instant, and an application may overwrite it during its own
#: layout pass. A placement is therefore re-asserted up to this many times,
#: with this pause after each write for it to be applied, before the window
#: is taken at its word that it will not go there. Measured: a window well
#: away from its target converges in two or three rounds.
_SET_ATTEMPTS = 5
_WRITE_BEAT_S = 0.15

#: Exiting native fullscreen animates the window out of its own Space; AX
#: geometry writes are ignored until that finishes.
_UNFULLSCREEN_S = 1.0

#: GTK often maps a 1x1 client-leader at (0,0) before the real dialog is
#: viewable. A window smaller than this is not a typing target.
MIN_MAPPED_SIDE = 20


# ---- failure as data -------------------------------------------------------
def _backend_label() -> str:
    """The backend name, or a placeholder — this must never be the thing that
    raises inside an error handler."""
    try:
        return backend_name()
    except Exception:
        return "unknown"


def _never_raises(fn):
    """Turn any escaping exception into the module's normal failure dict.

    Every public function here promises a dict with `ok`, because a tool that
    raises tells a model nothing about what to do instead. Most failures are
    already returned as data, but two of the facade's own entry points —
    `monitors()` and `list_windows_rich()` — bypass its error conversion and
    raise outright when a backend's helper binaries are missing. Verified: on a
    host with no xdotool that is a bare RuntimeError, and a backend loaded for
    the wrong OS raises Unsupported. Neither is useful to a caller in that form.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "where": fn.__name__,
                "backend": _backend_label(),
                "hint": "the desktop backend could not be reached — check its "
                        "helper tools (xdotool and ffmpeg on X11), and that the "
                        "backend matches the machine it is running on",
            }

    return wrapper


# ---- window records --------------------------------------------------------
def _windows() -> list[dict]:
    return desktop.list_windows_rich()


def _box(win: dict) -> dict:
    """Geometry as plain ints.

    The X11 backend reports geometry as strings (it parses `xdotool
    getwindowgeometry --shell`), so arithmetic on a raw record silently
    concatenates instead of adding.
    """
    out: dict = {}
    for key in ("x", "y", "w", "h"):
        try:
            out[key] = int(win.get(key) or 0)
        except (TypeError, ValueError):
            out[key] = 0
    return out


def _brief(win: dict) -> dict:
    """The parts of a window record a caller of this module actually needs."""
    return {"id": win.get("id") or "", "name": win.get("name") or "",
            "app": win.get("app") or "", "pid": win.get("pid") or "",
            **_box(win)}


def _usable_window(win: dict | None) -> bool:
    """Mapped, viewable, large enough to be a real typing target."""
    if not win:
        return False
    if win.get("mapped") is False or win.get("viewable") is False:
        return False
    if win.get("minimized"):
        return False
    box = _box(win)
    return box["w"] >= MIN_MAPPED_SIDE and box["h"] >= MIN_MAPPED_SIDE


def _prefer_usable(matches: list[dict]) -> dict | None:
    """The largest mapped match, else the first record (still mapping)."""
    if not matches:
        return None
    usable = [w for w in matches if _usable_window(w)]
    if usable:
        return max(usable, key=lambda w: _box(w)["w"] * _box(w)["h"])
    return matches[0]


def _find_window(query: str, listing: list[dict] | None = None) -> dict | None:
    """Resolve a window by exact id/pid first, then by name or app substring.

    Ids are tried before substrings on purpose: a window whose *title* contains
    the digits of another window's id would otherwise shadow it. The backends do
    not agree on this ordering among themselves, so the ordering is settled here
    and the resolved id is what gets handed back down to them.

    When several windows match a substring (a GTK 1x1 client-leader plus the
    real dialog), the mapped one with real geometry wins.
    """
    windows = _windows() if listing is None else listing
    query = str(query or "")
    exact = [win for win in windows
             if query and (win.get("id") == query or win.get("pid") == query
                           or str(win.get("id") or "") == query
                           or str(win.get("pid") or "") == query)]
    if exact:
        return _prefer_usable(exact)
    needle = query.lower()
    if not needle:
        return None
    hits = []
    for win in windows:
        if (needle in (win.get("name") or "").lower()
                or needle in (win.get("app") or "").lower()
                or needle in (win.get("title") or "").lower()):
            hits.append(win)
    return _prefer_usable(hits)


def _relocate(win: dict, listing: list[dict] | None = None) -> dict | None:
    """Find the same window again in a fresh listing, by id then by identity."""
    windows = _windows() if listing is None else listing
    wid = win.get("id") or ""
    if wid:
        for candidate in windows:
            if candidate.get("id") == wid:
                return candidate
    pid = win.get("pid") or ""
    title = win.get("title") or win.get("name") or ""
    for candidate in windows:
        if pid and candidate.get("pid") == pid and (
                candidate.get("title") or candidate.get("name") or "") == title:
            return candidate
    return None


def _query_for(win: dict) -> str:
    """The query string that resolves back to this window on *this* backend.

    The backends do not agree on precedence: X11 and Windows try an id before a
    name, so an id is the unambiguous handle there. macOS tries the name first
    and only then the id, so handing it an id would let a window whose title
    happens to contain those digits win the match. Give each the key it looks at
    first rather than assuming a shared convention.
    """
    if backend_name() == "darwin":
        return (win.get("title") or win.get("name") or win.get("app")
                or win.get("id") or "")
    return win.get("id") or win.get("name") or win.get("app") or ""


def _known_windows(limit: int = 40) -> list[str]:
    return [w.get("name") or w.get("app") or "" for w in _windows()][:limit]


# ---- macOS accessibility ---------------------------------------------------
# Windows are moved through AXUIElement rather than by driving keyboard chords
# or shelling out to a tiling utility: a chord is whatever the frontmost app
# decided it means, and a third-party tiler is a dependency the package does not
# have. AX is also the only route that addresses one *specific* window of a
# multi-window application, which is exactly what a split of two browser windows
# needs.

_AX_RAISE = "AXRaise"
_AX_FULLSCREEN = "AXFullScreen"  # no pyobjc constant exists for this one


def _ax_api():
    """The AX module, or None. Same accessor `a11y` dispatches to on macOS."""
    if backend_name() != "darwin":
        return None
    from .platform import ax_darwin

    return ax_darwin._api()


def _ax_app(win: dict):
    api = _ax_api()
    if api is None:
        return None
    try:
        pid = int(win.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    return api.AXUIElementCreateApplication(pid) if pid else None


def _ax_window(win: dict):
    """The AXUIElement for one specific window record, or None.

    Matched on geometry before title, because the two titles genuinely differ: a
    browser's profile chooser reports its application name to the window server
    and its own heading to accessibility, so a title-first match picks the wrong
    window or none at all.
    """
    api = _ax_api()
    app = _ax_app(win)
    if api is None or app is None:
        return None
    err, elements = api.AXUIElementCopyAttributeValue(app, api.kAXWindowsAttribute,
                                                      None)
    elements = list(elements or [])
    if err or not elements:
        return None
    # The windows attribute is not always all windows: a file manager reports its
    # desktop as a scroll area in the same list, and that element accepts no size.
    windowish = []
    for element in elements:
        err2, role = api.AXUIElementCopyAttributeValue(
            element, api.kAXRoleAttribute, None)
        if err2 or str(role or "") == "AXWindow":
            windowish.append(element)
    elements = windowish or elements
    if len(elements) == 1:
        return elements[0]
    from .platform import ax_darwin

    box = _box(win)
    wanted = (box["x"], box["y"], box["w"], box["h"])
    for element in elements:
        if ax_darwin._point_size(element) == wanted:
            return element
    title = (win.get("title") or win.get("name") or "").lower()
    if title:
        for element in elements:
            err2, value = api.AXUIElementCopyAttributeValue(
                element, api.kAXTitleAttribute, None)
            if not err2 and title in str(value or "").lower():
                return element
    return elements[0]


def _ax_frame(element) -> tuple[int, int, int, int] | None:
    if element is None:
        return None
    from .platform import ax_darwin

    return ax_darwin._point_size(element)


# ---- displays and their working areas --------------------------------------
def _rect_contains(rect: dict, x: int, y: int) -> bool:
    return (rect["x"] <= x < rect["x"] + rect["w"]
            and rect["y"] <= y < rect["y"] + rect["h"])


def _rect_inside(inner: dict, outer: dict) -> bool:
    return (inner["x"] >= outer["x"] and inner["y"] >= outer["y"]
            and inner["x"] + inner["w"] <= outer["x"] + outer["w"]
            and inner["y"] + inner["h"] <= outer["y"] + outer["h"])


def _intersect(a: dict, b: dict) -> dict | None:
    x = max(a["x"], b["x"])
    y = max(a["y"], b["y"])
    right = min(a["x"] + a["w"], b["x"] + b["w"])
    bottom = min(a["y"] + a["h"], b["y"] + b["h"])
    if right <= x or bottom <= y:
        return None
    return {"x": x, "y": y, "w": right - x, "h": bottom - y}


def _monitor_rect(monitor: dict) -> dict:
    return {"x": int(monitor.get("x") or 0), "y": int(monitor.get("y") or 0),
            "w": int(monitor.get("w") or 0), "h": int(monitor.get("h") or 0)}


def _pick_display(display: int | str | None,
                  near: dict | None = None) -> tuple[dict | None, dict | None]:
    """(monitor, error). `display` is an index into `desktop.monitors()` or a
    name substring; None means the display holding `near`, else the primary.

    The index deliberately matches the ordering `screen_info` reports, so a
    caller that read "monitor 1" off that reply lands on the same panel here.
    """
    monitors = desktop.monitors()
    if not monitors:
        return None, {"ok": False, "error": "no displays reported"}
    if display is None:
        if near is not None:
            box = _box(near)
            cx, cy = box["x"] + box["w"] // 2, box["y"] + box["h"] // 2
            for monitor in monitors:
                if _rect_contains(_monitor_rect(monitor), cx, cy):
                    return monitor, None
        for monitor in monitors:
            if monitor.get("primary"):
                return monitor, None
        return monitors[0], None
    if isinstance(display, bool):  # bool is an int subclass; never an index
        return None, {"ok": False, "error": "display must be an index or a name"}
    if isinstance(display, int):
        if 0 <= display < len(monitors):
            return monitors[display], None
        return None, {"ok": False,
                      "error": f"no display {display}; there are {len(monitors)}",
                      "displays": [m.get("name") for m in monitors]}
    needle = str(display).lower()
    for monitor in monitors:
        if needle in str(monitor.get("name") or "").lower():
            return monitor, None
    return None, {"ok": False, "error": f"no display matching {display!r}",
                  "displays": [m.get("name") for m in monitors]}


def _visible_frame_darwin(monitor: dict) -> dict:
    """NSScreen.visibleFrame for this display, in top-left global points.

    AppKit's origin is the *bottom* left of the zero-origin screen with y
    growing upward, while every coordinate in this package is a top-left global
    point, so each rect is flipped through the height of that zero-origin
    screen. Getting the flip wrong is invisible on a single display whose Dock
    is hidden and catastrophic everywhere else, which is why the result is
    checked against the display rectangle before it is trusted.
    """
    whole = _monitor_rect(monitor)
    try:
        from AppKit import NSScreen  # type: ignore
    except Exception:
        return {**whole, "via": "display bounds", "respects_chrome": False,
                "note": "AppKit is unavailable, so the menu bar and Dock could "
                        "not be measured; a window may land underneath them"}
    screens = list(NSScreen.screens() or [])
    if not screens:
        return {**whole, "via": "display bounds", "respects_chrome": False,
                "note": "NSScreen reported no screens"}
    flip_h = float(screens[0].frame().size.height)

    def flip(rect) -> dict:
        return {"x": int(round(rect.origin.x)),
                "y": int(round(flip_h - (rect.origin.y + rect.size.height))),
                "w": int(round(rect.size.width)),
                "h": int(round(rect.size.height))}

    chosen = None
    for screen in screens:
        frame = flip(screen.frame())
        if frame["x"] == whole["x"] and frame["y"] == whole["y"]:
            chosen = screen
            break
        cx, cy = whole["x"] + whole["w"] // 2, whole["y"] + whole["h"] // 2
        if chosen is None and _rect_contains(frame, cx, cy):
            chosen = screen
    if chosen is None:
        return {**whole, "via": "display bounds", "respects_chrome": False,
                "note": "no NSScreen matched this display"}
    visible = flip(chosen.visibleFrame())
    if not _rect_inside(visible, whole):
        return {**whole, "via": "display bounds", "respects_chrome": False,
                "note": f"visibleFrame {visible} fell outside the display "
                        f"rectangle {whole}; the flip is not trustworthy here"}
    return {**visible, "via": "NSScreen.visibleFrame", "respects_chrome": True}


def _visible_frame_linux(monitor: dict) -> dict:
    """The EWMH work area, intersected with this display.

    `_NET_WORKAREA` is a property of the root window and describes the whole X
    screen, not one output, so on a multi-head desktop it has to be clipped to
    the monitor being laid out. Panels advertise themselves through struts,
    which is what the work area already accounts for.
    """
    from . import x11

    whole = _monitor_rect(monitor)
    if shutil.which("xprop"):
        try:
            out = x11._run(["xprop", "-root", "_NET_WORKAREA"]).stdout
            digits = [int(v) for v in out.replace("=", " ").replace(",", " ").split()
                      if v.lstrip("-").isdigit()]
            if len(digits) >= 4:
                area = {"x": digits[0], "y": digits[1],
                        "w": digits[2], "h": digits[3]}
                clipped = _intersect(area, whole)
                if clipped:
                    return {**clipped, "via": "_NET_WORKAREA",
                            "respects_chrome": True}
        except Exception:
            pass
    if shutil.which("wmctrl"):
        try:
            for line in x11._run(["wmctrl", "-d"]).stdout.splitlines():
                if "WA:" not in line:
                    continue
                tail = line.split("WA:", 1)[1].split()
                origin = tail[0].split(",")
                size = tail[1].split("x")
                area = {"x": int(origin[0]), "y": int(origin[1]),
                        "w": int(size[0]), "h": int(size[1])}
                clipped = _intersect(area, whole)
                if clipped:
                    return {**clipped, "via": "wmctrl -d", "respects_chrome": True}
        except Exception:
            pass
    return {**whole, "via": "display bounds", "respects_chrome": False,
            "note": "neither xprop nor wmctrl could report the work area, so "
                    "panel struts are not accounted for"}


def _visible_frame_win32(monitor: dict) -> dict:
    """`MONITORINFO.rcWork` for this display — the screen minus the taskbar."""
    whole = _monitor_rect(monitor)
    try:
        import ctypes

        from .platform import win32

        u32, _g32, _k32 = win32._dll()
        found: list[dict] = []
        proto = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                                   ctypes.c_void_p,
                                   ctypes.POINTER(win32.RECT), ctypes.c_ssize_t)

        def collect(hmon, _hdc, _lprc, _data):
            info = win32.MONITORINFO()
            info.cbSize = ctypes.sizeof(win32.MONITORINFO)
            if u32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                work = info.rcWork
                x, y = win32._to_contract(work.left, work.top)
                found.append({"x": x, "y": y,
                              "w": work.right - work.left,
                              "h": work.bottom - work.top})
            return 1

        u32.EnumDisplayMonitors(None, None, proto(collect), 0)
        for area in found:
            if _rect_inside(area, whole):
                return {**area, "via": "MONITORINFO.rcWork",
                        "respects_chrome": True}
    except Exception:
        pass
    return {**whole, "via": "display bounds", "respects_chrome": False,
            "note": "the work area could not be read, so the taskbar is not "
                    "accounted for"}


@_never_raises
def visible_frame(display: int | str | None = None) -> dict:
    """The working area of one display: the screen minus menu bar, Dock,
    panels or taskbar. Every layout in this module is computed inside it.

    `respects_chrome` says whether that subtraction actually happened; when it
    is False the frame is the whole display and a window placed at its origin
    may sit under the system's own furniture.
    """
    monitor, error = _pick_display(display)
    if error is not None:
        return error
    backend = backend_name()
    if backend == "darwin":
        frame = _visible_frame_darwin(monitor)
    elif backend == "win32":
        frame = _visible_frame_win32(monitor)
    else:
        frame = _visible_frame_linux(monitor)
    return {"ok": True, "backend": backend,
            "display": {"name": monitor.get("name"), "index": _index_of(monitor),
                        "primary": bool(monitor.get("primary")),
                        **_monitor_rect(monitor)},
            "frame": {k: frame[k] for k in ("x", "y", "w", "h")},
            "via": frame.get("via"),
            "respects_chrome": frame.get("respects_chrome", False),
            **({"note": frame["note"]} if frame.get("note") else {})}


def _index_of(monitor: dict) -> int:
    for i, candidate in enumerate(desktop.monitors()):
        if candidate.get("name") == monitor.get("name"):
            return i
    return 0


# ---- placement -------------------------------------------------------------
def _unblock(win: dict) -> list[str]:
    """Undo the states in which a window ignores a geometry request.

    A minimized window has no on-screen frame to set, a maximized one is pinned
    by the window manager, and a natively fullscreen macOS window lives in its
    own Space where AX position and size writes are accepted and discarded.
    """
    undone: list[str] = []
    api = _ax_api()
    if api is not None:
        element = _ax_window(win)
        if element is not None:
            err, minimized = api.AXUIElementCopyAttributeValue(
                element, api.kAXMinimizedAttribute, None)
            if not err and bool(minimized):
                api.AXUIElementSetAttributeValue(
                    element, api.kAXMinimizedAttribute, False)
                undone.append("unminimize")
            err, full = api.AXUIElementCopyAttributeValue(
                element, _AX_FULLSCREEN, None)
            if not err and bool(full):
                api.AXUIElementSetAttributeValue(element, _AX_FULLSCREEN, False)
                undone.append("unfullscreen")
                time.sleep(_UNFULLSCREEN_S)
        return undone
    query = _query_for(win)
    if win.get("minimized"):
        desktop.window_action(query, "unminimize")
        undone.append("unminimize")
    if win.get("fullscreen"):
        # The X11 path unmaximizes inside window_geometry but does not leave
        # fullscreen, and a fullscreen window swallows geometry silently.
        desktop.window_action(query, "unfullscreen")
        undone.append("unfullscreen")
    if win.get("maximized"):
        desktop.window_action(query, "unmaximize")
        undone.append("unmaximize")
    return undone


def _set_frame(win: dict, x: int, y: int, w: int, h: int) -> dict:
    """Drive a window to a rectangle, re-asserting until it gets there.

    A single pair of accessibility writes does not reliably land, for a reason
    that took measuring to see: **macOS clamps a resize against where the window
    is at that instant**, not against where it has just been told to go. A
    window sitting low on the screen and asked to become full height is trimmed
    so its bottom stays clear of the Dock — a 960-point request came back as 528
    here — and because the writes are asynchronous, a size sent immediately
    after a move is clamped against the *old* position. Applications add their
    own layout pass on top: a browser restoring a remembered window size will
    accept every write, return success for each, and then quietly undo them.

    So this converges rather than commanding. Position first (that is what makes
    room to grow into), then size, with a beat after each for the write to be
    applied, then re-read and go round again until the frame matches or the
    attempts run out. The frame is read back through accessibility, not through
    the window server, because the window server's copy lags by a frame or two
    and reports a successful move as refused.

    A window that never converges is not an error — a fixed-size panel and an
    application minimum are both legitimate answers — so the caller's `honored`
    and `delta` carry that, not an exception.
    """
    api = _ax_api()
    element = _ax_window(win) if api is not None else None
    if api is None or element is None:
        result = desktop.window_geometry(_query_for(win), x=x, y=y, w=w, h=h)
        result.setdefault("via", f"desktop.window_geometry/{backend_name()}")
        return result
    wanted = {"x": x, "y": y, "w": w, "h": h}
    point = api.AXValueCreate(api.kAXValueCGPointType, api.CGPoint(x, y))
    size = api.AXValueCreate(api.kAXValueCGSizeType, api.CGSize(w, h))
    rejected: int | None = None
    for attempt in range(_SET_ATTEMPTS):
        frame = _ax_frame(element)
        if frame is not None and _within(_as_box(frame), wanted):
            return {"ok": True, "via": "AXUIElement", "attempts": attempt}
        for attribute, value in ((api.kAXPositionAttribute, point),
                                 (api.kAXSizeAttribute, size)):
            code = api.AXUIElementSetAttributeValue(element, attribute, value)
            if code and rejected is None:
                rejected = code
            time.sleep(_WRITE_BEAT_S)
    frame = _ax_frame(element)
    if frame is not None and _within(_as_box(frame), wanted):
        return {"ok": True, "via": "AXUIElement", "attempts": _SET_ATTEMPTS}
    if rejected:
        return {"ok": False, "via": "AXUIElement", "attempts": _SET_ATTEMPTS,
                "error": f"accessibility rejected the frame (AX error {rejected})",
                "hint": "the window may be a fixed-size panel, or the "
                        "Accessibility grant may have been revoked"}
    return {"ok": True, "via": "AXUIElement", "attempts": _SET_ATTEMPTS,
            "converged": False}


def _as_box(frame: tuple[int, int, int, int]) -> dict:
    return dict(zip(("x", "y", "w", "h"), frame))


def _settle(win: dict, tile: dict) -> tuple[dict, bool]:
    """Re-read geometry from the window manager until it lands or stops moving.

    A geometry write is asynchronous, and the window manager's view lags the
    application's: measured here, an accepted resize still reports the *old*
    width for a frame or two and then animates to the new one. So "the last two
    reads agree" is not enough on its own — taken alone it returns the stale
    value and reports a successful move as refused.

    Matching the request therefore wins immediately, and only a window that has
    *not* landed is watched for the full window before its final, stable
    geometry is reported. Fast when it worked, patient when it did not.
    """
    deadline = time.monotonic() + _SETTLE_S
    previous: dict | None = None
    agreements = 0
    latest = win
    while True:
        found = _relocate(win)
        if found is not None:
            latest = found
            box = _box(found)
            if _within(box, tile):
                return latest, True
            if previous is not None and box == previous:
                agreements += 1
            else:
                agreements = 1
            previous = box
        if time.monotonic() >= deadline:
            return latest, agreements >= _STABLE_READS
        time.sleep(_POLL_S)


def _within(box: dict, tile: dict) -> bool:
    return all(abs(box[key] - tile[key]) <= _TOLERANCE
               for key in ("x", "y", "w", "h"))


def _place(win: dict, tile: dict) -> dict:
    """Move one window to one rectangle and report what actually happened."""
    before = _box(win)
    undone = _unblock(win)
    if undone:
        win = _relocate(win) or win
    asked = _set_frame(win, tile["x"], tile["y"], tile["w"], tile["h"])
    after_win, settled = _settle(win, tile)
    after = _box(after_win)
    delta = {key: after[key] - tile[key] for key in ("x", "y", "w", "h")}
    honored = all(abs(value) <= _TOLERANCE for value in delta.values())
    out = {
        "ok": bool(asked.get("ok")),
        "window": _brief(after_win),
        "requested": dict(tile),
        "geometry": after,
        "before": before,
        "honored": honored,
        "delta": delta,
        "settled": settled,
        "via": asked.get("via"),
    }
    if undone:
        out["undone"] = undone
    if asked.get("attempts") is not None:
        # How many rounds it took to converge. A steadily rising number across
        # calls is the signal that an application is fighting the placement.
        out["attempts"] = asked["attempts"]
    if not asked.get("ok"):
        out["error"] = asked.get("error") or "the window manager refused the move"
        if asked.get("hint"):
            out["hint"] = asked["hint"]
    elif not honored:
        out["note"] = ("the window manager or the application overrode the "
                       "request; applications enforce their own minimum sizes")
    return out


def _raise_window(win: dict) -> dict:
    """Bring one specific window forward.

    macOS activates *applications*, so activating alone leaves a multi-window
    app showing whichever window it had in front; the AXRaise that follows is
    what selects the window actually asked for.
    """
    if backend_name() == "darwin":
        result = desktop.focus_window(win.get("app") or win.get("name") or "")
        api = _ax_api()
        element = _ax_window(win) if api is not None else None
        if api is not None and element is not None:
            api.AXUIElementPerformAction(element, _AX_RAISE)
            result["ax_raise"] = True
        return result
    return desktop.focus_window(_query_for(win))


def _frontmost_verdict(win: dict) -> dict:
    """Is `win` the window a keystroke would reach right now, and how do we know?"""
    if not _usable_window(win):
        return {"verified": False, "matched_on": "", "active": None,
                "reason": "not_mapped"}
    active = desktop.active_window()
    if not active.get("ok"):
        return {"verified": False, "matched_on": "", "active": active}
    if active.get("id") and str(active.get("id")) == str(win.get("id")):
        return {"verified": True, "matched_on": "id", "active": active}
    api = _ax_api()
    if api is not None and active.get("pid") == win.get("pid"):
        # The macOS active-window record is only the frontmost app's first
        # window, so it cannot distinguish two windows of one app. AX can: ask
        # the application which of its windows holds focus.
        app = _ax_app(win)
        if app is not None:
            err, focused = api.AXUIElementCopyAttributeValue(
                app, api.kAXFocusedWindowAttribute, None)
            target = _ax_frame(_ax_window(win))
            if not err and focused is not None and target is not None:
                if _ax_frame(focused) == target:
                    return {"verified": True, "matched_on": "ax_focused_window",
                            "active": active}
    if active.get("pid") and active.get("pid") == win.get("pid"):
        return {"verified": True, "matched_on": "pid", "active": active}
    app_name = (win.get("app") or "").lower()
    if app_name and (active.get("app") or "").lower() == app_name:
        return {"verified": True, "matched_on": "app", "active": active}
    return {"verified": False, "matched_on": "", "active": active}


# ---- native tiling: the macOS Window > Move & Resize menu -------------------
# macOS ships its own tiler. Its key chords cannot be driven from here, but its
# menu can, and three measurements on this machine settle the design.
#
# **The chords are unreachable, and not for lack of trying.** The built-in
# tiling shortcuts are Globe(fn) plus an arrow. `kCGEventFlagMaskSecondaryFn`
# exists and equals 0x800000, but macOS resolves fn *below* the event tap, so
# the flag is dropped from a synthetic CGEvent before any application sees it:
# posting keycode 51 (Delete) with that flag, caret at the start of a document
# reading "abcdef", left the text untouched, while plain ForwardDelete
# (keycode 117) produced "bcdef". The menu items carry no key equivalent
# either (AXMenuItemCmdChar on "Left" is missing value), so there is no chord
# to send even in principle. The menu is therefore not a workaround for a
# missing keystroke; it is the interface this feature has.
#
# **The menu gives the OS's own rectangle.** Clicking Window > Move & Resize >
# Left moved a TextEdit window from (147,76) 673x439 to (0,30) 641x600: the
# system's left half, with its gap and its menu bar inset already applied.
# The computed path below arrives at 640 wide from the visible frame, so the
# two agree to a point here, but the OS's number is the one that tracks the
# "tiled windows have margins" setting and any future change to it.
#
# **The click only lands on the frontmost app.** With the same window visible
# on the same Space but Finder in front, the identical click returned success
# and moved nothing. So a tile activates the target application first, and
# makes the named window that application's main window, because a Window menu
# item acts on the main window rather than on the one the caller named.
_WINDOW_MENU = "Window"
_MOVE_RESIZE_MENU = "Move & Resize"

#: How far a native tile may sit from the rectangle this module would have
#: computed and still count as honored. The OS applies its own gap between
#: tiles and its own inset under the menu bar, and both move with a System
#: Settings toggle, so an exact match is the wrong test for this route.
_NATIVE_TOLERANCE = 24

#: action -> the menu item that performs it, and the same tile expressed as
#: fractions of the visible frame for the fallback. `rect=None` means the
#: fallback cannot compute the answer from the frame alone: "center" keeps the
#: window's own size, and "tile_restore" needs the remembered rectangle.
_TILE_ACTIONS: dict[str, dict] = {
    "tile_left": {"item": "Left", "submenu": _MOVE_RESIZE_MENU,
                  "rect": (0.0, 0.0, 0.5, 1.0)},
    "tile_right": {"item": "Right", "submenu": _MOVE_RESIZE_MENU,
                   "rect": (0.5, 0.0, 0.5, 1.0)},
    "tile_top": {"item": "Top", "submenu": _MOVE_RESIZE_MENU,
                 "rect": (0.0, 0.0, 1.0, 0.5)},
    "tile_bottom": {"item": "Bottom", "submenu": _MOVE_RESIZE_MENU,
                    "rect": (0.0, 0.5, 1.0, 0.5)},
    "tile_top_left": {"item": "Top Left", "submenu": _MOVE_RESIZE_MENU,
                      "rect": (0.0, 0.0, 0.5, 0.5)},
    "tile_top_right": {"item": "Top Right", "submenu": _MOVE_RESIZE_MENU,
                       "rect": (0.5, 0.0, 0.5, 0.5)},
    "tile_bottom_left": {"item": "Bottom Left", "submenu": _MOVE_RESIZE_MENU,
                         "rect": (0.0, 0.5, 0.5, 0.5)},
    "tile_bottom_right": {"item": "Bottom Right", "submenu": _MOVE_RESIZE_MENU,
                          "rect": (0.5, 0.5, 0.5, 0.5)},
    "fill": {"item": "Fill", "submenu": None, "rect": (0.0, 0.0, 1.0, 1.0)},
    "center": {"item": "Center", "submenu": None, "rect": None},
    "tile_restore": {"item": "Return to Previous Size",
                     "submenu": _MOVE_RESIZE_MENU, "rect": None},
}

_TILE_ALIASES = {
    "left": "tile_left", "right": "tile_right", "top": "tile_top",
    "bottom": "tile_bottom", "left_half": "tile_left",
    "right_half": "tile_right", "top_half": "tile_top",
    "bottom_half": "tile_bottom", "top_left": "tile_top_left",
    "top_right": "tile_top_right", "bottom_left": "tile_bottom_left",
    "bottom_right": "tile_bottom_right", "tile_fill": "fill",
    "maximise": "fill", "maximize": "fill", "tile_center": "center",
    "centre": "center", "restore": "tile_restore",
    "previous_size": "tile_restore", "return_to_previous_size": "tile_restore",
}

#: Where each window was before its first tile, keyed by window id. macOS
#: remembers this itself for "Return to Previous Size", but the fallback route
#: has nowhere else to read it from, and a caller that tiles a window on one
#: app and restores it on another needs the same answer either way.
_TILE_PREVIOUS: dict[str, dict] = {}


def tile_actions() -> list[str]:
    """Every tiling action name, in menu order."""
    return list(_TILE_ACTIONS)


def _norm_tile_action(action: str) -> str:
    key = str(action or "").strip().lower().replace(" ", "_").replace("-", "_")
    key = _TILE_ALIASES.get(key, key)
    return key


def _osa(script: str, timeout: float = 10.0) -> tuple[bool, str]:
    """Run one AppleScript. Returns (ok, stdout-or-error), never raises.

    System Events is the only route to another application's menu bar, and it
    is a separate process: a hung app makes it hang too, so the timeout is part
    of the contract rather than a nicety.
    """
    try:
        done = subprocess.run(["osascript", "-e", script], capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"System Events did not answer within {timeout}s"
    except OSError as exc:
        return False, f"osascript could not be run: {exc}"
    if done.returncode != 0:
        return False, (done.stderr or "").strip() or "System Events refused"
    return True, done.stdout.strip()


def _osa_str(value: str) -> str:
    """Quote a value for an AppleScript string literal."""
    return '"' + str(value or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _process_name(win: dict) -> str:
    """The name System Events knows this window's application by.

    `app` is the window server's owner name, which is what the accessibility
    process list uses too, so the two agree without a lookup table.
    """
    return str(win.get("app") or win.get("name") or "")


def _menu_state(process: str) -> dict:
    """What this application's Window menu offers right now.

    Both halves matter. `exists` says whether the app has a standard Window
    menu at all, which is what separates an application that can be tiled
    natively from one that cannot. `enabled` says whether a particular item
    would do anything if clicked, which is not the same question: "Return to
    Previous Size" is present but greyed out on a window that has never been
    tiled, and System Events reports a click on a greyed-out item as a success.
    Reading the flag is what keeps that from being reported as a tile that
    happened.
    """
    name = _osa_str(process)
    script = (
        f'tell application "System Events" to tell process {name}\n'
        f'  if not (exists menu bar item {_osa_str(_WINDOW_MENU)} of menu bar 1) '
        f'then return "NO_WINDOW_MENU"\n'
        f'  set out to ""\n'
        f'  repeat with mi in menu items of menu 1 of menu bar item '
        f'{_osa_str(_WINDOW_MENU)} of menu bar 1\n'
        f'    set out to out & "T\t" & (name of mi as string) & "\t" & '
        f'(enabled of mi as string) & linefeed\n'
        f'  end repeat\n'
        f'  if exists menu item {_osa_str(_MOVE_RESIZE_MENU)} of menu 1 of menu '
        f'bar item {_osa_str(_WINDOW_MENU)} of menu bar 1 then\n'
        f'    repeat with mi in menu items of menu 1 of menu item '
        f'{_osa_str(_MOVE_RESIZE_MENU)} of menu 1 of menu bar item '
        f'{_osa_str(_WINDOW_MENU)} of menu bar 1\n'
        f'      set out to out & "M\t" & (name of mi as string) & "\t" & '
        f'(enabled of mi as string) & linefeed\n'
        f'    end repeat\n'
        f'  end if\n'
        f'  return out\n'
        f'end tell'
    )
    ok, payload = _osa(script)
    if not ok:
        return {"ok": False, "window_menu": False, "move_resize": False,
                "top": {}, "items": {}, "error": payload,
                "hint": "System Events needs the Accessibility grant; without "
                        "it every menu read fails with AppleScript error -1719"}
    if payload == "NO_WINDOW_MENU":
        return {"ok": True, "window_menu": False, "move_resize": False,
                "top": {}, "items": {}}
    top: dict[str, bool] = {}
    items: dict[str, bool] = {}
    for line in payload.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        where, label, enabled = parts
        if label == "missing value":  # a separator, not an item
            continue
        (top if where == "T" else items)[label] = enabled.strip() == "true"
    return {"ok": True, "window_menu": True, "move_resize": bool(items),
            "top": top, "items": items}


def _menu_lookup(state: dict, spec: dict) -> tuple[bool, bool]:
    """(present, enabled) for one action's menu item."""
    table = state["items"] if spec["submenu"] else state["top"]
    if spec["item"] not in table:
        return False, False
    return True, bool(table[spec["item"]])


def _menu_path(spec: dict) -> list[str]:
    return [_WINDOW_MENU] + ([spec["submenu"]] if spec["submenu"] else []) + \
           [spec["item"]]


def _click_menu(process: str, spec: dict) -> tuple[bool, str]:
    """Click one Window menu item on an application, through accessibility."""
    tail = (f'menu 1 of menu bar item {_osa_str(_WINDOW_MENU)} of menu bar 1')
    if spec["submenu"]:
        reference = (f'menu item {_osa_str(spec["item"])} of menu 1 of menu item '
                     f'{_osa_str(spec["submenu"])} of {tail}')
    else:
        reference = f'menu item {_osa_str(spec["item"])} of {tail}'
    return _osa(f'tell application "System Events" to tell process '
                f'{_osa_str(process)} to click {reference}')


def _make_main(win: dict) -> bool:
    """Make one window its application's main window, without a click.

    A Window menu item acts on the application's main window, so tiling a
    named window means naming it to the app first. AXMain is the attribute that
    says so, and setting it is what makes this address a specific window of a
    multi-window application rather than whichever one happened to be in front.
    """
    api = _ax_api()
    element = _ax_window(win) if api is not None else None
    if api is None or element is None:
        return False
    api.AXUIElementPerformAction(element, _AX_RAISE)
    return not api.AXUIElementSetAttributeValue(element, "AXMain", True)


@_never_raises
def native_tiling(query: str | None = None) -> dict:
    """Whether this window's application can be tiled by the OS itself.

    An application without a standard Window menu (a single-window utility, a
    game, anything that builds its own menu bar) has no Move & Resize submenu,
    and there is nothing to click. That is a normal answer, not a failure:
    `available` comes back False with `reason`, and `tile` then computes the
    rectangle itself.

    `actions` lists only what would actually do something right now, so
    tile_restore appears on a window that has been tiled and not on one that
    has not.

    This reads the menu where it stands and does not activate anything, which
    makes it safe to call on a screen someone is using and gives it one blind
    spot: an application that has not been brought forward since it launched
    reports an empty accessibility tree, so it looks menu-less until something
    activates it. `tile` activates before it probes for exactly that reason,
    and can therefore succeed on a window this call calls unavailable.
    """
    if backend_name() != "darwin":
        return {"ok": True, "available": False, "backend": backend_name(),
                "reason": "the Window > Move & Resize menu is macOS only",
                "actions": [], "route": "geometry"}
    if query is None:
        active = desktop.active_window()
        if not active.get("ok"):
            return {"ok": False, "available": False,
                    "error": "no active window to inspect"}
        win = _relocate(active) or active
    else:
        win = _find_window(query)
    if win is None:
        return {"ok": False, "available": False,
                "error": f"no window matching {query!r}",
                "windows": _known_windows()}
    process = _process_name(win)
    state = _menu_state(process)
    out = {"ok": bool(state.get("ok")), "process": process,
           "window": _brief(win), "backend": "darwin"}
    if not state.get("ok"):
        return {**out, "available": False, "actions": [],
                "reason": state.get("error"), "route": "geometry",
                **({"hint": state["hint"]} if state.get("hint") else {})}
    available = []
    for name, spec in _TILE_ACTIONS.items():
        present, enabled = _menu_lookup(state, spec)
        if present and enabled:
            available.append(name)
    out["available"] = bool(state.get("move_resize"))
    out["actions"] = available
    out["window_menu"] = bool(state.get("window_menu"))
    if not out["available"]:
        out["route"] = "geometry"
        out["reason"] = (
            f"{process} has no Window > {_MOVE_RESIZE_MENU} submenu"
            if state.get("window_menu") else
            f"{process} has no standard {_WINDOW_MENU} menu")
        if not state.get("window_menu"):
            out["hint"] = ("an application that has never been brought forward "
                           "reports no menus at all; if this one has not been "
                           "activated yet, ask `tile` directly, which activates "
                           "before it probes")
    else:
        out["route"] = "native_menu"
    return out


def _tile_rect(spec: dict, frame: dict, win: dict, action: str) -> tuple[dict | None, str]:
    """The rectangle the fallback would place, or (None, why not)."""
    fractions = spec["rect"]
    if fractions is not None:
        fx, fy, fw, fh = fractions
        return {"x": frame["x"] + int(round(frame["w"] * fx)),
                "y": frame["y"] + int(round(frame["h"] * fy)),
                "w": int(round(frame["w"] * fw)),
                "h": int(round(frame["h"] * fh))}, ""
    if action == "center":
        box = _box(win)
        return {"x": frame["x"] + max(0, (frame["w"] - box["w"]) // 2),
                "y": frame["y"] + max(0, (frame["h"] - box["h"]) // 2),
                "w": box["w"], "h": box["h"]}, ""
    remembered = _TILE_PREVIOUS.get(str(win.get("id") or ""))
    if remembered:
        return dict(remembered), ""
    return None, ("nothing was recorded for this window before it was tiled, "
                  "so there is no rectangle to return it to")


@_never_raises
def tile(action: str, query: str | None = None,
         display: int | str | None = None, prefer_menu: bool = True) -> dict:
    """Tile one window the way the operating system tiles it.

    Actions: tile_left, tile_right, tile_top, tile_bottom, the four quarters
    (tile_top_left and friends), fill, center, and tile_restore for Return to
    Previous Size. `query` names the window or its application, and defaults to
    the active window; on a multi-window application the named window is made
    the application's main window first, so this addresses the window asked for
    rather than whichever one was in front.

    Two routes, and the reply always says which one ran in `route` and `via`.
    `native_menu` clicks the app's own Window > Move & Resize item, so the
    result is the OS's rectangle including its gap and its inset. `geometry`
    computes the tile from the display's visible frame and writes it through
    accessibility, which is what happens for an application with no Window
    menu, for a platform that is not macOS, for a `display` other than the one
    the window is on (the menu tiles in place, it cannot move a window between
    displays), and for `prefer_menu=False`.

    Because the menu route only takes effect on the frontmost application, this
    activates the target app. That is the same thing a person does before
    tiling a window, but it is a focus change the caller did not separately ask
    for, so `activated` records it.
    """
    name = _norm_tile_action(action)
    spec = _TILE_ACTIONS.get(name)
    if spec is None:
        return {"ok": False, "error": f"unknown tile action {action!r}",
                "actions": tile_actions()}
    if query is None:
        active = desktop.active_window()
        if not active.get("ok"):
            return {"ok": False, "action": name,
                    "error": "no active window to tile",
                    "hint": "pass a query naming the window"}
        win = _relocate(active) or active
    else:
        win = _find_window(query)
    if win is None:
        return {"ok": False, "action": name,
                "error": f"no window matching {query!r}",
                "windows": _known_windows()}

    return _tile_resolved(win, name, spec, display=display,
                          prefer_menu=prefer_menu)


def _tile_resolved(win: dict, name: str, spec: dict,
                   display: int | str | None = None,
                   prefer_menu: bool = True) -> dict:
    """`tile` for a window that is already resolved.

    `split` and `fullscreen` have their window in hand and must not resolve it
    again: on macOS a query is matched against window titles, and two windows
    of one application can share a title, so a second lookup is a chance to
    tile the wrong one.
    """
    target = _brief(win)
    before = _box(win)
    window_id = str(win.get("id") or "")
    monitor, error = _pick_display(display, near=win)
    if error is not None:
        return {**error, "action": name, "target": target}
    on_display, _ = _pick_display(None, near=win)
    frame_reply = visible_frame(_index_of(monitor))
    if not frame_reply.get("ok"):
        return {**frame_reply, "action": name, "target": target}
    frame = frame_reply["frame"]
    computed, why_not = _tile_rect(spec, frame, win, name)

    # Remember where the window started, once, so the fallback can undo a tile
    # even though it is the OS that owns the real memory of it.
    if name != "tile_restore" and window_id and window_id not in _TILE_PREVIOUS:
        _TILE_PREVIOUS[window_id] = dict(before)

    moving_display = (display is not None and monitor is not None
                      and on_display is not None
                      and monitor.get("name") != on_display.get("name"))
    reason = ""
    probe: dict = {}
    if backend_name() != "darwin":
        reason = "the Window > Move & Resize menu is macOS only"
    elif not prefer_menu:
        reason = "prefer_menu=False"
    elif moving_display:
        reason = ("the menu tiles a window inside the display it is already "
                  "on, so a move to another display goes through geometry")

    activated = False
    if not reason:
        # Activating first serves two purposes: the click only lands on the
        # frontmost app, and an application that has never been activated
        # reports an empty accessibility tree, which would make its Window menu
        # look absent when it is merely unbuilt.
        raised = _raise_window(win)
        activated = bool(raised.get("ok"))
        _make_main(win)
        process = _process_name(win)
        probe = _menu_state(process)
        present, enabled = _menu_lookup(probe, spec) if probe.get("ok") else (False, False)
        if not probe.get("ok"):
            reason = probe.get("error") or "the Window menu could not be read"
        elif not present:
            reason = (f"{process} has no standard {_WINDOW_MENU} menu"
                      if not probe.get("window_menu") else
                      f"{process} has no {' > '.join(_menu_path(spec))} item")
        elif not enabled:
            reason = (f"{' > '.join(_menu_path(spec))} is greyed out for this "
                      f"window")
        else:
            ok, detail = _click_menu(process, spec)
            if not ok:
                reason = detail
            else:
                after_win, settled = _settle_native(win, computed, before)
                after = _box(after_win)
                result = _tile_result(
                    name=name, spec=spec, route="native_menu",
                    via=f"menu {' > '.join(_menu_path(spec))}",
                    target=target, before=before, after=after,
                    window=after_win, computed=computed, settled=settled,
                    tolerance=_NATIVE_TOLERANCE, frame_reply=frame_reply,
                    activated=activated, window_id=window_id)
                _forget_previous(name, result, window_id, "native_menu")
                return result

    if computed is None:
        return {"ok": False, "action": name, "route": "geometry",
                "target": target, "before": before,
                "error": why_not or "this action has no computed equivalent",
                "native_reason": reason,
                "hint": "tile the window first, or place it with a rectangle "
                        "through window_geometry"}
    placed = _place(win, computed)
    after_win = _relocate(win) or win
    result = _tile_result(
        name=name, spec=spec, route="geometry",
        via=placed.get("via") or f"geometry/{backend_name()}",
        target=target, before=before, after=_box(after_win), window=after_win,
        computed=computed, settled=bool(placed.get("settled")),
        tolerance=_TOLERANCE, frame_reply=frame_reply, activated=activated,
        window_id=window_id)
    if reason:
        result["fell_back"] = True
        result["native_reason"] = reason
    if placed.get("error"):
        result["ok"] = False
        result["error"] = placed["error"]
    _forget_previous(name, result, window_id, "geometry")
    return result


def _forget_previous(action: str, result: dict, window_id: str,
                     route: str) -> None:
    """Drop the remembered rectangle once the window stops being tiled.

    macOS drops its own at the same moment: after Return to Previous Size the
    menu item goes grey again, and measured, Center untiles a window too. A
    record kept past that point aims the next restore at a rectangle from two
    tiles ago, and makes a correct native restore read as not honored against
    a stale number.
    """
    if not window_id or not result.get("ok"):
        return
    if action == "tile_restore" or (action == "center" and route == "native_menu"):
        _TILE_PREVIOUS.pop(window_id, None)
        result.pop("restore_to", None)


def _settle_native(win: dict, computed: dict | None,
                   before: dict) -> tuple[dict, bool]:
    """Wait for a menu-driven tile to land.

    `_settle` watches for the rectangle this module asked for, which is the
    wrong target here: the OS is placing the window, and its answer is its own
    to decide. So this waits for the geometry to stop changing instead, and
    lets the caller judge the result against the computed tile with the wider
    native tolerance.

    `before` is what keeps the fast path honest. The click is asynchronous, so
    the first read back is usually still the pre-click rectangle, and where the
    computed tile happens to equal that rectangle (centering a window that is
    already centred, tiling one that is already tiled) an early match would
    report a move that has not happened yet. Measured: Center on a filled
    window returned instantly with the filled rectangle, while the OS was in
    the middle of restoring the window's pre-tile size. So a match only counts
    once the geometry has actually changed; a window that genuinely does not
    move falls through to the stability test below and is reported as not
    moved, which is the truth.
    """
    deadline = time.monotonic() + _SETTLE_S
    previous: dict | None = None
    agreements = 0
    latest = win
    while True:
        found = _relocate(win)
        if found is not None:
            latest = found
            box = _box(found)
            if (box != before and computed is not None
                    and _near(box, computed, _NATIVE_TOLERANCE)):
                return latest, True
            if previous is not None and box == previous:
                agreements += 1
                if agreements >= _STABLE_READS:
                    return latest, True
            else:
                agreements = 1
            previous = box
        if time.monotonic() >= deadline:
            return latest, agreements >= _STABLE_READS
        time.sleep(_POLL_S)


def _near(box: dict, tile_rect: dict, tolerance: int) -> bool:
    return all(abs(box[key] - tile_rect[key]) <= tolerance
               for key in ("x", "y", "w", "h"))


def _tile_result(*, name: str, spec: dict, route: str, via: str, target: dict,
                 before: dict, after: dict, window: dict,
                 computed: dict | None, settled: bool, tolerance: int,
                 frame_reply: dict, activated: bool, window_id: str) -> dict:
    """One shape for both routes, so a caller can read either without branching."""
    moved = after != before
    expected = computed
    note = ""
    if name == "center" and route == "native_menu":
        # Measured: macOS Center does not only move the window. On a window
        # that is tiled it also restores the size the window had before it was
        # tiled, then centres that. A filled 1280x600 window came back 602x401
        # at (338,130). The computed fallback centres the size the window
        # actually has, which is a different and simpler promise, so the two
        # are not compared: `honored` here says the OS moved it.
        expected = None
        note = ("macOS Center restores the window's pre-tile size before it "
                "centres it, so this is the OS's rectangle, not the computed "
                "one; `honored` only says the window moved")
    elif name == "tile_restore" and route == "native_menu":
        remembered = _TILE_PREVIOUS.get(window_id)
        expected = dict(remembered) if remembered else None
        if expected is None:
            note = ("nothing was recorded for this window before it was tiled, "
                    "so `honored` only says the window moved")
    if expected is None:
        honored = moved
        delta = None
    else:
        delta = {key: after[key] - expected[key] for key in ("x", "y", "w", "h")}
        honored = all(abs(value) <= tolerance for value in delta.values())
    out = {
        "ok": True,
        "action": name,
        "route": route,
        "via": via,
        "target": target,
        "window": _brief(window),
        "before": before,
        "geometry": after,
        "moved": moved,
        "honored": honored,
        "settled": settled,
        "tolerance": tolerance,
        "display": frame_reply["display"],
        "frame": frame_reply["frame"],
        "respects_chrome": frame_reply["respects_chrome"],
        "activated": activated,
    }
    # `requested` is what every other placement in this module reports, and
    # split and fullscreen hand these dicts straight back to callers that read
    # it, so both routes carry it. On the native route it is the rectangle this
    # module WOULD have asked for, which is exactly what `native_frame` should
    # be compared against.
    out["requested"] = computed
    if route == "native_menu":
        out["menu_path"] = _menu_path(spec)
        out["computed_frame"] = computed
        out["native_frame"] = after
    if delta is not None:
        out["delta"] = delta
    if note:
        out["note"] = note
    elif not honored and name == "tile_restore":
        # Measured: macOS returns a window to the size it had before its FIRST
        # tile, not to the tile before this one, and it remembers that across
        # processes. This module's own memory starts when it first tiles the
        # window, so the two answers differ whenever the window was already
        # tiled by hand before a session began. The OS's answer is the right
        # one; the delta is reported so the gap is visible rather than silent.
        out["note"] = ("the OS restored the size it remembers from before this "
                       "window was first tiled, which is not the rectangle "
                       "recorded here")
    elif not honored:
        out["note"] = ("the window did not reach the tile; applications enforce "
                       "their own minimum sizes, and the OS applies its own gap "
                       "and inset on the native route")
    if name != "tile_restore" and window_id in _TILE_PREVIOUS:
        out["restore_to"] = dict(_TILE_PREVIOUS[window_id])
    return out


# ---- public API ------------------------------------------------------------
@_never_raises
def focus_and_verify(query: str, timeout_s: float = 6.0) -> dict:
    """Raise the window matching `query` and confirm it actually came forward.

    Sending a raise and reporting success is the bug this exists to catch: on a
    focus-stealing window manager activation can fail silently, and on macOS a
    multi-window application can come forward on the wrong window. So the
    frontmost window is polled until it matches or `timeout_s` expires, and
    `verified` — not the raise having been sent — is what sets `ok`.

    `matched_on` names the evidence: 'id' is exact, 'ax_focused_window' is exact
    on macOS, and 'pid'/'app' mean the right application is in front but the
    specific window could not be distinguished.
    """
    started = time.monotonic()
    deadline = started + max(0.0, float(timeout_s))
    win = None
    raised: dict = {}
    verdict: dict = {"verified": False, "matched_on": "", "active": None}
    while True:
        found = _find_window(query)
        if found is not None:
            win = found
            if _usable_window(win):
                raised = _raise_window(win)
                verdict = _frontmost_verdict(win)
                if verdict["verified"]:
                    break
        if time.monotonic() >= deadline:
            break
        time.sleep(_POLL_S)
    waited_ms = int(round((time.monotonic() - started) * 1000))
    if win is None:
        return {"ok": False, "verified": False, "waited_ms": waited_ms,
                "window": None, "app": None,
                "error": f"no window matching {query!r}",
                "windows": _known_windows()}
    out = {
        "ok": bool(verdict["verified"]),
        "verified": bool(verdict["verified"]),
        "window": _brief(_relocate(win) or win),
        "app": win.get("app") or win.get("name") or "",
        "waited_ms": waited_ms,
        "matched_on": verdict.get("matched_on") or "",
        "raise_sent": bool(raised.get("ok")),
    }
    if not verdict["verified"]:
        if not _usable_window(_relocate(win) or win):
            out["error"] = (f"{query!r} did not map a viewable window "
                            f"within {timeout_s}s")
            out["hint"] = ("the window was still 1x1 or unmapped; wait for it "
                           "to finish mapping before typing")
        else:
            out["error"] = (f"{query!r} did not come forward within {timeout_s}s")
            out["hint"] = ("some window managers refuse programmatic activation; "
                           "screenshot before typing, and on macOS check the "
                           "Accessibility grant")
        out["frontmost"] = verdict.get("active")
    return out


@_never_raises
def fullscreen(query: str | None = None, native: bool = False,
               prefer_menu: bool = True) -> dict:
    """Put one window across the whole working area of its display.

    The default is a **maximise** — fill the visible frame, staying clear of the
    menu bar, Dock, panels and taskbar — not true native fullscreen. That is a
    deliberate choice, not a limitation. On macOS the real fullscreen chord
    moves the window into a Space of its own, and every other window then
    vanishes from the window list and from screenshots; an agent that just put
    its own target into a private Space has blinded itself half way through the
    perceive-act-verify loop, and cannot even see the thing it is working on
    next to anything else. A maximised window looks the same to the human
    watching and keeps the desktop legible to the agent.

    `native=True` opts into the real thing for the cases that want it — a video,
    a presentation, a screenshot with no furniture in it — and says so in the
    reply so the caller knows why the other windows disappeared.

    `query=None` targets the currently active window, which is convenient
    interactively and dangerous in a script: pass a query when it matters, and
    check `target` in the reply, which names what was resolved.

    On macOS the maximise is asked of the system first, through the
    application's own Window > Fill menu item, and only computed here when the
    application has no such menu. `route` in the reply says which one ran.
    `prefer_menu=False` forces the computed rectangle.
    """
    if query is None:
        active = desktop.active_window()
        if not active.get("ok"):
            return {"ok": False, "error": "no active window to maximise",
                    "hint": "pass a query naming the window"}
        win = _relocate(active) or active
    else:
        win = _find_window(query)
    if win is None:
        return {"ok": False, "error": f"no window matching {query!r}",
                "windows": _known_windows()}
    target = _brief(win)
    if native:
        api = _ax_api()
        element = _ax_window(win) if api is not None else None
        if element is not None:
            code = api.AXUIElementSetAttributeValue(element, _AX_FULLSCREEN, True)
            result = ({"ok": True, "via": "AXUIElement"} if not code else
                      {"ok": False, "via": "AXUIElement",
                       "error": f"accessibility refused fullscreen (AX error "
                                f"{code})"})
        else:
            result = desktop.window_action(_query_for(win), "fullscreen")
        result.setdefault("ok", False)
        result["target"] = target
        result["mode"] = "native"
        result["note"] = ("native fullscreen moves this window to its own "
                          "Space on macOS; other windows will not appear in "
                          "screenshots or in list_windows until it exits")
        return result
    if prefer_menu and backend_name() == "darwin":
        # Ask the OS for its own Fill before computing one. The rectangle is
        # the same idea, but the system is the authority on where its working
        # area ends, and its answer tracks a Dock or menu bar setting this
        # module would otherwise have to infer. An application with no Window
        # menu falls through to the computed path inside _tile_resolved and
        # comes back with route='geometry', so this is a preference, not a
        # requirement.
        filled = _tile_resolved(win, "fill", _TILE_ACTIONS["fill"],
                                display=None, prefer_menu=True)
        if filled.get("ok"):
            return {**filled, "mode": "maximise",
                    "raised": bool(filled.get("activated"))}
    monitor, error = _pick_display(None, near=win)
    if error is not None:
        return {**error, "target": target}
    frame = visible_frame(_index_of(monitor))
    if not frame.get("ok"):
        return {**frame, "target": target}
    state: dict = {}
    if backend_name() != "darwin":
        # Ask the window manager for its own maximised state rather than writing
        # a rectangle over it. That sets _NET_WM_STATE_MAXIMIZED_VERT/HORZ, which
        # is what a panel or a dock reserving space actually reads, so the result
        # tracks the struts even if they change afterwards. Geometry below is
        # still applied and still read back, so a window manager that ignores the
        # request is reported honestly rather than assumed. Never on macOS: the
        # darwin backend maps "maximize" onto the native fullscreen chord, which
        # is the exact thing this default exists to avoid.
        asked = desktop.window_action(_query_for(win), "maximize")
        state = {"maximize_state": bool(asked.get("ok")),
                 "via_state": "_NET_WM_STATE_MAXIMIZED_*"}
        if not asked.get("ok") and asked.get("error"):
            state["maximize_state_error"] = asked["error"]
        current = _relocate(win)
        if current is not None:
            if current.get("maximized") is not None:
                state["maximized"] = bool(current.get("maximized"))
            if _within(_box(current), frame["frame"]):
                # The window manager already put it where we would have.
                win = current
    placed = _place(win, frame["frame"])
    # Re-read before raising: the record still holds the pre-move rectangle, and
    # the accessibility element is matched on geometry.
    raised = _raise_window(_relocate(win) or win)
    return {**placed, "mode": "maximise", "target": target,
            "display": frame["display"], "frame": frame["frame"],
            "respects_chrome": frame["respects_chrome"],
            "raised": bool(raised.get("ok")), **state,
            **({"frame_note": frame["note"]} if frame.get("note") else {})}


@_never_raises
def split(left: str, right: str, ratio: float = 0.5,
          display: int | str | None = None, prefer_menu: bool = True) -> dict:
    """Two windows side by side on one display, both raised.

    `ratio` is the left window's share of the visible frame's width; 0.5 is an
    even split, 0.66 gives the left window two thirds. The reply carries each
    window's geometry as the window manager reports it *afterwards*, not the
    rectangle that was asked for, because an application with a minimum width
    will quietly refuse a half-width tile and the caller needs to know that its
    tidy split is actually two overlapping windows.

    `display=None` uses whichever display currently holds the left window.
    The first window named ends frontmost, since that is the one being worked
    on; both are raised above everything else.

    An even split on macOS is asked of the system, through each application's
    own Window > Move & Resize > Left and Right, so the two windows get the
    OS's halves with its gap between them instead of two rectangles computed
    here. That route needs both applications to offer the menu, both windows to
    be on one display, and `ratio` to be 0.5; anything else, including
    `prefer_menu=False`, computes the tiles. `route` in the reply says which
    one ran.
    """
    try:
        ratio = float(ratio)
    except (TypeError, ValueError):
        return {"ok": False, "error": f"ratio must be a number, got {ratio!r}"}
    if not 0.0 < ratio < 1.0:
        return {"ok": False, "error": f"ratio must be between 0 and 1, got {ratio}"}
    return _tile([left, right], display=display, ratios=[ratio, 1.0 - ratio],
                 layout="split", prefer_menu=prefer_menu)


@_never_raises
def stack(queries: list[str], display: int | str | None = None) -> dict:
    """Tile n windows left to right across one display, in the order given.

    Capped at three. Below roughly a third of a 1080p-class display each column
    is too narrow to read from normal viewing distance — wrapped prose turns
    into a ladder and a browser's own furniture eats what is left — so a fourth
    column produces a screenshot the agent cannot act on and a monitor the human
    cannot read. Above the cap this refuses rather than silently dropping the
    extra windows; use a second display, or two calls.

    Each tile's width is reported in points and in device pixels, since a
    high-density display doubles the pixels behind the same point width.
    """
    if isinstance(queries, str):
        queries = [queries]
    queries = [q for q in (queries or []) if str(q).strip()]
    if not queries:
        return {"ok": False, "error": "stack needs at least one window"}
    if len(queries) > STACK_CAP:
        return {"ok": False,
                "error": f"stack is capped at {STACK_CAP} windows, got "
                         f"{len(queries)}",
                "cap": STACK_CAP,
                "hint": "narrower than a third of the working area stops being "
                        "readable; use a second display or two calls"}
    share = 1.0 / len(queries)
    return _tile(queries, display=display, ratios=[share] * len(queries),
                 layout="stack")


def _tile(queries: list[str], display: int | str | None,
          ratios: list[float], layout: str, prefer_menu: bool = False) -> dict:
    """Shared engine for split and stack: resolve, compute, place, read back."""
    listing = _windows()
    resolved: list[dict] = []
    for query in queries:
        win = _find_window(query, listing=listing)
        if win is None:
            return {"ok": False, "error": f"no window matching {query!r}",
                    "layout": layout, "windows": _known_windows()}
        if any(win.get("id") == other.get("id") and win.get("id")
               for other in resolved):
            return {"ok": False, "layout": layout,
                    "error": f"{query!r} resolved to a window already placed by "
                             f"an earlier query; name them more precisely",
                    "resolved": [_brief(w) for w in resolved]}
        resolved.append(win)

    monitor, error = _pick_display(display, near=resolved[0])
    if error is not None:
        return {**error, "layout": layout}
    frame_reply = visible_frame(_index_of(monitor))
    if not frame_reply.get("ok"):
        return {**frame_reply, "layout": layout}
    frame = frame_reply["frame"]

    if (prefer_menu and layout == "split" and len(ratios) == 2
            and abs(ratios[0] - 0.5) <= 0.01 and backend_name() == "darwin"):
        native = _native_split(resolved, frame_reply, monitor)
        if native is not None:
            return native

    tiles: list[dict] = []
    cursor = frame["x"]
    for i, share in enumerate(ratios):
        last = i == len(ratios) - 1
        # The final tile takes the remainder rather than its own rounded share,
        # so rounding never leaves a one-point gutter down the middle.
        width = (frame["x"] + frame["w"] - cursor) if last else int(
            round(frame["w"] * share))
        tiles.append({"x": cursor, "y": frame["y"], "w": width, "h": frame["h"]})
        cursor += width

    scale = float(monitor.get("backing_scale") or 1.0)
    placed = [_place(win, tile) for win, tile in zip(resolved, tiles)]
    # Raised in reverse so the first window named ends up frontmost, and each is
    # re-read first because the records still carry their pre-move rectangles.
    raised = [bool(_raise_window(_relocate(win) or win).get("ok"))
              for win in reversed(resolved)]
    raised.reverse()

    slots = []
    for i, (win, result) in enumerate(zip(resolved, placed)):
        slots.append({**result, "slot": i, "raised": raised[i],
                      "tile_w_points": tiles[i]["w"],
                      "tile_w_pixels": int(round(tiles[i]["w"] * scale))})
    out = {
        "ok": all(item.get("ok") for item in placed),
        "layout": layout,
        "route": "geometry",
        "display": frame_reply["display"],
        "frame": frame,
        "respects_chrome": frame_reply["respects_chrome"],
        "honored": all(item.get("honored") for item in placed),
        "windows": slots,
    }
    if layout == "split":
        out["left"], out["right"] = slots[0], slots[1]
    if not out["honored"]:
        out["note"] = ("at least one window did not take its tile; check each "
                       "window's `delta` — applications enforce minimum sizes")
    if frame_reply.get("note"):
        out["frame_note"] = frame_reply["note"]
    return out


def _native_split(resolved: list[dict], frame_reply: dict,
                  monitor: dict) -> dict | None:
    """The OS's own left and right halves, or None if that route is not open.

    Returns None rather than a failure so the caller can fall back: every
    reason this route is unavailable (an application with no Window menu, two
    windows on two displays, a menu item greyed out) is a normal state of the
    desktop, not an error.

    Both applications are probed before either window is moved. A split that
    tiled one window natively and computed the other would leave the pair
    misaligned by the system gap, which looks like a bug in the arithmetic
    rather than what it is.
    """
    actions = ("tile_left", "tile_right")
    for win in resolved:
        near, _ = _pick_display(None, near=win)
        if near is None or near.get("name") != monitor.get("name"):
            return None
    for win, action in zip(resolved, actions):
        # Raise before reading the menu. An application that has not been
        # brought forward reports an empty accessibility tree, menu bar and
        # all, so probing a cold app answers "no Window menu" about an
        # application that has one. Measured on a freshly launched editor: no
        # windows and no menus until it was activated once, both present
        # immediately afterwards.
        _raise_window(win)
        state = _menu_state(_process_name(win))
        if not state.get("ok"):
            return None
        present, enabled = _menu_lookup(state, _TILE_ACTIONS[action])
        if not (present and enabled):
            return None
    results = [_tile_resolved(win, action, _TILE_ACTIONS[action], display=None,
                             prefer_menu=True)
               for win, action in zip(resolved, actions)]
    if not all(item.get("route") == "native_menu" for item in results):
        # One of them slipped to geometry between the probe and the click. Let
        # the caller compute both, which is at least consistent.
        return None
    raised = [bool(_raise_window(_relocate(win) or win).get("ok"))
              for win in reversed(resolved)]
    raised.reverse()
    scale = float(monitor.get("backing_scale") or 1.0)
    slots = []
    for i, item in enumerate(results):
        width = int(item["geometry"]["w"])
        slots.append({**item, "slot": i, "raised": raised[i],
                      "tile_w_points": width,
                      "tile_w_pixels": int(round(width * scale))})
    out = {
        "ok": all(item.get("ok") for item in results),
        "layout": "split",
        "route": "native_menu",
        "via": "menu Window > Move & Resize > Left/Right",
        "display": frame_reply["display"],
        "frame": frame_reply["frame"],
        "respects_chrome": frame_reply["respects_chrome"],
        "honored": all(item.get("honored") for item in results),
        "windows": slots,
        "left": slots[0],
        "right": slots[1],
    }
    if not out["honored"]:
        out["note"] = ("at least one window is not where the OS's own half "
                       "would be; check each window's `delta` against "
                       "`computed_frame`")
    return out


@_never_raises
def arrange(spec) -> dict:
    """One entry point a tool layer can expose. Dispatches on `spec['layout']`.

        {"layout": "full",  "query": "Editor", "native": false}
        {"layout": "split", "left": "Editor", "right": "Browser", "ratio": 0.6}
        {"layout": "stack", "queries": ["Editor", "Browser", "Terminal"]}

    `display` is accepted by split and stack. A JSON string is accepted as well
    as a dict, because tool layers routinely hand structured arguments through
    as text.
    """
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError as exc:
            return {"ok": False, "error": f"spec is not valid JSON: {exc}"}
    if not isinstance(spec, dict):
        return {"ok": False,
                "error": f"spec must be an object, got {type(spec).__name__}"}
    layout = str(spec.get("layout") or "").strip().lower()
    aliases = {"full": "full", "fullscreen": "full", "maximise": "full",
               "maximize": "full", "split": "split", "half": "split",
               "stack": "stack", "tile": "stack"}
    layout = aliases.get(layout, layout)
    if layout == "full":
        return fullscreen(query=spec.get("query") or spec.get("window"),
                          native=bool(spec.get("native")))
    if layout == "split":
        left, right = spec.get("left"), spec.get("right")
        if not left or not right:
            return {"ok": False, "error": "split needs 'left' and 'right'"}
        return split(left, right, ratio=spec.get("ratio", 0.5),
                     display=spec.get("display"))
    if layout == "stack":
        queries = spec.get("queries") or spec.get("windows") or []
        return stack(queries, display=spec.get("display"))
    return {"ok": False, "error": f"unknown layout {spec.get('layout')!r}",
            "layouts": ["full", "split", "stack"]}


__all__ = ["STACK_CAP", "arrange", "focus_and_verify", "fullscreen",
           "native_tiling", "split", "stack", "tile", "tile_actions",
           "visible_frame"]
