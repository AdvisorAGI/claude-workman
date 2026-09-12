"""macOS backend — Quartz first, `screencapture`/`osascript` as the floor.

Three macOS facts shape this file.

**1. Retina.** `screencapture` returns backing-store pixels (2x on a Retina
panel) while CGEvent coordinates are points. Handing that image up would make
every click land at double the distance, so a capture is normalised back to
point space here, before anyone can read a coordinate off it. `sips` ships with
macOS, so the normalisation works even without Pillow.

**2. Permissions are the usual failure, not the API.** Screen Recording gates
capture and Accessibility gates synthetic input, both per *calling* app — the
terminal or the MCP client, not Workman. When they are missing the OS returns a
black frame or silently drops the event, so errors here name the pane in System
Settings to open rather than reporting a generic failure.

**3. pyobjc is optional.** Quartz gives real event injection and a proper window
list; without it the backend still sees (`screencapture`) and can act through
`cliclick` or System Events, and says which path it used in `via`.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time

from . import base

PLATFORM = "macOS"

# ---- optional Quartz -------------------------------------------------------
_quartz_mod = None
_quartz_tried = False


def _quartz():
    """The Quartz module, or None. Imported once, lazily: the import is ~100ms
    and a capture-only session never needs it."""
    global _quartz_mod, _quartz_tried
    if not _quartz_tried:
        _quartz_tried = True
        try:
            import Quartz  # type: ignore

            _quartz_mod = Quartz
        except Exception:
            _quartz_mod = None
    return _quartz_mod


def _run(cmd: list[str], timeout: int = 20, **kw) -> subprocess.CompletedProcess:
    """Run a helper, treating "not installed" as a failed run rather than an
    exception. This module is imported and introspected on non-macOS hosts (the
    tests do exactly that), where none of these binaries exist."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, **kw)
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def _osa(script: str, timeout: int = 20) -> subprocess.CompletedProcess:
    return _run(["osascript", "-e", script], timeout=timeout)


def _has(binary: str) -> bool:
    return shutil.which(binary) is not None


def emit_cursor(kind: str, x: int, y: int) -> None:
    """No overlay daemon on macOS yet; kept so callers stay platform-blind."""
    return None


# ---- geometry --------------------------------------------------------------
def _main_bounds() -> tuple[int, int, int, int]:
    Q = _quartz()
    if Q is not None:
        b = Q.CGDisplayBounds(Q.CGMainDisplayID())
        return (int(b.origin.x), int(b.origin.y),
                int(b.size.width), int(b.size.height))
    if not _has("osascript"):
        return (0, 0, 0, 0)
    # Finder reports the desktop rectangle in points, which is exactly the
    # coordinate space clicks use, and needs no extra permission.
    res = _osa('tell application "Finder" to get bounds of window of desktop')
    try:
        x1, y1, x2, y2 = [int(v.strip()) for v in res.stdout.split(",")]
        return (x1, y1, x2 - x1, y2 - y1)
    except Exception:
        return (0, 0, 1440, 900)


def screen_size() -> tuple[int, int]:
    _, _, w, h = _main_bounds()
    return (w, h)


def monitors() -> list[dict]:
    Q = _quartz()
    if Q is None:
        x, y, w, h = _main_bounds()
        return [{"name": "main", "x": x, "y": y, "w": w, "h": h, "primary": True}]
    err, ids, _count = Q.CGGetActiveDisplayList(16, None, None)
    if err:
        x, y, w, h = _main_bounds()
        return [{"name": "main", "x": x, "y": y, "w": w, "h": h, "primary": True}]
    main = Q.CGMainDisplayID()
    out = []
    for did in ids:
        b = Q.CGDisplayBounds(did)
        out.append({
            "name": f"display-{did}",
            "x": int(b.origin.x), "y": int(b.origin.y),
            "w": int(b.size.width), "h": int(b.size.height),
            "primary": bool(did == main),
            "backing_scale": _backing_scale(did),
        })
    return out


def _backing_scale(display_id=None) -> float:
    """Pixels per point. 2.0 on Retina, 1.0 on an external 1080p panel."""
    Q = _quartz()
    if Q is None:
        return 1.0
    try:
        did = display_id if display_id is not None else Q.CGMainDisplayID()
        mode = Q.CGDisplayCopyDisplayMode(did)
        pw = Q.CGDisplayModeGetPixelWidth(mode)
        w = Q.CGDisplayModeGetWidth(mode)
        return round(pw / w, 4) if w else 1.0
    except Exception:
        return 1.0


# ---- see -------------------------------------------------------------------
def _png_size(path: str) -> tuple[int, int]:
    """Width/height straight from the IHDR chunk, so no Pillow is needed."""
    with open(path, "rb") as fh:
        head = fh.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return (0, 0)
    return (int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big"))


def _resize_file(path: str, width: int, height: int) -> bool:
    """Resize a PNG in place. Pillow if present, else sips (always installed)."""
    try:
        from PIL import Image

        im = Image.open(path)
        im.resize((width, height), Image.LANCZOS if hasattr(Image, "LANCZOS")
                  else Image.Resampling.LANCZOS).save(path)
        return True
    except Exception:
        pass
    if _has("sips"):
        res = _run(["sips", "--resampleHeightWidth", str(height), str(width), path])
        return res.returncode == 0
    return False


def screenshot(max_dim: int | None = None,
               region: tuple[int, int, int, int] | None = None) -> bytes:
    """Grab the screen as PNG bytes, in point space.

    Raises RuntimeError when Screen Recording is denied, because a silently
    black frame is worse than a failure: the model would act on an empty desk.
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        path = tmp.name
    try:
        cmd = ["screencapture", "-x", "-t", "png"]
        if region:
            rx, ry, rw, rh = region
            cmd += ["-R", f"{rx},{ry},{rw},{rh}"]
        cmd.append(path)
        res = _run(cmd, timeout=30)
        if res.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) == 0:
            raise RuntimeError(
                "screencapture failed"
                + (f": {res.stderr.strip()}" if res.stderr.strip() else "")
                + " — grant Screen Recording to the app running Workman in "
                  "System Settings > Privacy & Security > Screen Recording, "
                  "then restart that app"
            )

        want_w, want_h = (region[2], region[3]) if region else screen_size()
        got_w, got_h = _png_size(path)
        if got_w and want_w and got_w != want_w:
            # Retina: the file is 2x the coordinate space. Normalise the image,
            # never the coordinates.
            _resize_file(path, want_w, want_h)

        data = open(path, "rb").read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return base.resize_png(data, max_dim) if max_dim else data


# ---- windows ---------------------------------------------------------------
def list_windows() -> list[dict]:
    """On-screen windows, frontmost first. Needs Quartz; System Events is the
    fallback and only reports the frontmost app's windows."""
    Q = _quartz()
    if Q is None:
        return _list_windows_osa()
    opts = (Q.kCGWindowListOptionOnScreenOnly | Q.kCGWindowListExcludeDesktopElements)
    info = Q.CGWindowListCopyWindowInfo(opts, Q.kCGNullWindowID) or []
    out = []
    for w in info:
        bounds = w.get("kCGWindowBounds") or {}
        owner = w.get("kCGWindowOwnerName") or ""
        title = w.get("kCGWindowName") or ""
        if int(w.get("kCGWindowLayer", 0)) != 0:
            continue  # menu bar, dock, overlays: not clickable app windows
        out.append({
            "id": str(w.get("kCGWindowNumber", "")),
            "name": title or owner,
            "app": owner,
            "title": title,
            "pid": str(w.get("kCGWindowOwnerPID", "")),
            "x": int(bounds.get("X", 0)), "y": int(bounds.get("Y", 0)),
            "w": int(bounds.get("Width", 0)), "h": int(bounds.get("Height", 0)),
            "minimized": False,
        })
    return out


def _list_windows_osa() -> list[dict]:
    script = (
        'tell application "System Events" to tell (first process whose frontmost is true) '
        'to return name & "\\n" & (name of windows as string)'
    )
    res = _osa(script)
    if res.returncode != 0:
        return []
    lines = [l for l in res.stdout.strip().splitlines() if l]
    if not lines:
        return []
    app = lines[0]
    return [{"id": "", "name": t, "app": app, "title": t, "pid": "",
             "x": 0, "y": 0, "w": 0, "h": 0, "minimized": False}
            for t in (lines[1].split(", ") if len(lines) > 1 else [])]


def _match_window(query: str) -> dict | None:
    q = query.lower()
    for w in list_windows():
        if q in (w.get("name") or "").lower() or q in (w.get("app") or "").lower():
            return w
        if query == w.get("id") or query == w.get("pid"):
            return w
    return None


def focus_window(query: str, minimize_blockers: bool = True) -> dict:
    """Bring a window's application to the front.

    macOS activates *applications*, so a multi-window app is raised to whichever
    window it had in front; AXRaise on the exact window follows when Quartz and
    the Accessibility grant are both present. Always screenshot to verify.
    """
    win = _match_window(query)
    if win is None:
        return {"ok": False, "error": f"no window matching {query!r}",
                "windows": [w["name"] for w in list_windows()][:40]}
    app = win.get("app") or win.get("name")
    res = _osa(f'tell application "System Events" to set frontmost of '
               f'process "{app}" to true')
    if res.returncode != 0:
        return {"ok": False, "error": res.stderr.strip() or "activate failed",
                "hint": "grant Accessibility to the app running Workman in "
                        "System Settings > Privacy & Security > Accessibility"}
    return {"ok": True, "target": win.get("id") or app, "frontmost_now": app,
            "window": win.get("name"), "verify": "screenshot before typing"}


def active_window() -> dict:
    res = _osa('tell application "System Events" to tell (first process whose '
               'frontmost is true) to return name')
    app = res.stdout.strip()
    if not app:
        return {"ok": False, "error": "no active window"}
    for w in list_windows():
        if w.get("app") == app:
            return {"ok": True, **w}
    return {"ok": True, "id": "", "name": app, "app": app, "pid": "",
            "x": 0, "y": 0, "w": 0, "h": 0}


def focused_window_identity() -> dict | None:
    """The frontmost app window's owner and title from one Quartz window-list
    read; None without Quartz (an osascript per keystroke is far too slow)."""
    if _quartz() is None:
        return None
    wins = list_windows()
    if not wins:
        return None
    return {"ok": True, "class": wins[0].get("app") or "", "app": wins[0].get("app") or "",
            "name": wins[0].get("title") or ""}


def window_at_point(x: int, y: int) -> dict | None:
    """Frontmost on-screen window whose frame contains (x, y), or None."""
    for w in list_windows():
        try:
            wx, wy, ww, wh = (int(w.get(k) or 0) for k in ("x", "y", "w", "h"))
        except (TypeError, ValueError):
            continue
        if ww <= 0 or wh <= 0:
            continue
        if wx <= x < wx + ww and wy <= y < wy + wh:
            return {"ok": True, "class": w.get("app") or "", "app": w.get("app") or "",
                    "name": w.get("title") or w.get("name") or ""}
    return None


def kill_window(query: str) -> dict:
    """Terminate the owning process, matching the Linux backend's semantics.

    macOS has no per-window kill: closing a window is an app-level action, so
    the honest equivalent of `xdotool windowkill` is to signal the process.
    """
    win = _match_window(query)
    if win is None:
        return {"ok": False, "error": f"no window matching {query!r}"}
    pid = win.get("pid")
    if not pid:
        return {"ok": False, "error": "window has no pid", "window": win.get("name")}
    try:
        os.kill(int(pid), 15)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "killed": pid, "app": win.get("app"),
            "warning": "whole application terminated; unsaved work is lost"}


# ---- input -----------------------------------------------------------------
# Three routes, best first: Quartz events (no extra install, exact),
# cliclick (a real HID-level tool if the user has it), System Events (always
# there, but flakiest and needs the Accessibility grant).

_CG_BUTTON = {1: "left", 2: "center", 3: "right"}
_CG_FLAGS = {"super": 0x100000, "shift": 0x20000, "alt": 0x80000,
             "ctrl": 0x40000, "control": 0x40000, "meta": 0x100000}
_MOD_KEYCODE = {"super": 55, "shift": 56, "alt": 58, "ctrl": 59,
                "control": 59, "meta": 55}

# xdotool key names -> macOS virtual keycodes.
_KEYCODES = {
    "return": 36, "enter": 36, "kp_enter": 76, "tab": 48, "space": 49,
    "backspace": 51, "delete": 51, "forwarddelete": 117, "escape": 53, "esc": 53,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "prior": 116, "page_up": 116, "pageup": 116,
    "next": 121, "page_down": 121, "pagedown": 121,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
    "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "a": 0, "b": 11, "c": 8, "d": 2, "e": 14, "f": 3, "g": 5, "h": 4, "i": 34,
    "j": 38, "k": 40, "l": 37, "m": 46, "n": 45, "o": 31, "p": 35, "q": 12,
    "r": 15, "s": 1, "t": 17, "u": 32, "v": 9, "w": 13, "x": 7, "y": 16, "z": 6,
    "0": 29, "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26,
    "8": 28, "9": 25,
    "minus": 27, "equal": 24, "bracketleft": 33, "bracketright": 30,
    "backslash": 42, "semicolon": 41, "apostrophe": 39, "quote": 39,
    "grave": 50, "comma": 43, "period": 47, "slash": 44,
    "kp_0": 82, "kp_1": 83, "kp_2": 84, "kp_3": 85, "kp_4": 86, "kp_5": 87,
    "kp_6": 88, "kp_7": 89, "kp_8": 91, "kp_9": 92, "kp_decimal": 65,
    "kp_multiply": 67, "kp_add": 69, "kp_divide": 75, "kp_subtract": 78,
}
# Punctuation typed as a shifted key, so press_key("plus") does the right thing.
_SHIFTED = {"plus": "equal", "underscore": "minus", "colon": "semicolon",
            "question": "slash", "exclam": "1", "at": "2", "numbersign": "3",
            "dollar": "4", "percent": "5", "asciicircum": "6", "ampersand": "7",
            "asterisk": "8", "parenleft": "9", "parenright": "0",
            "braceleft": "bracketleft", "braceright": "bracketright",
            "less": "comma", "greater": "period", "bar": "backslash",
            "quotedbl": "quote", "asciitilde": "grave"}

# Characters -> the US-layout key a person presses for them; True means a real
# Shift key goes down around it, so a page sees shiftKey AND a Shift keydown.
_US_PLAIN = {" ": "space", "-": "minus", "=": "equal", "[": "bracketleft",
             "]": "bracketright", "\\": "backslash", ";": "semicolon",
             "'": "apostrophe", "`": "grave", ",": "comma", ".": "period",
             "/": "slash", "\t": "tab", "\n": "return", "\r": "return"}
_US_SHIFT = {"~": "grave", "!": "1", "@": "2", "#": "3", "$": "4", "%": "5",
             "^": "6", "&": "7", "*": "8", "(": "9", ")": "0", "_": "minus",
             "+": "equal", "{": "bracketleft", "}": "bracketright", "|": "backslash",
             ":": "semicolon", '"': "apostrophe", "<": "comma", ">": "period",
             "?": "slash"}
US_CHARS: dict[str, tuple[int, bool]] = {
    **{c: (_KEYCODES[c], False) for c in "abcdefghijklmnopqrstuvwxyz0123456789"},
    **{c.upper(): (_KEYCODES[c], True) for c in "abcdefghijklmnopqrstuvwxyz"},
    **{c: (_KEYCODES[n], False) for c, n in _US_PLAIN.items()},
    **{c: (_KEYCODES[n], True) for c, n in _US_SHIFT.items()},
}
#: Longest a synthetic key may stay down, or a typing gap last (caller input).
MAX_KEY_MS = 1000
#: After pasting an off-layout character, wait this long then put the owner's
#: clipboard back. A page's paste handler reads the clipboard asynchronously.
PASTE_RESTORE_S = 0.3
#: App names behind which a web page or an Electron shell sits: there a page
#: can see how input arrived, so it must arrive as a person's would.
BROWSER_APPS = frozenset({"chrome", "chromium", "safari", "arc", "brave", "edge",
                          "firefox", "opera", "vivaldi", "orion", "electron", "claude",
                          "cursor", "slack", "code", "discord"})


def char_key(ch: str) -> tuple[int | None, bool]:
    """(virtual keycode, needs Shift) for one character on a US layout, or
    (None, False) when that layout has no key for it."""
    return US_CHARS.get(ch, (None, False))


def is_browser_app(name: str) -> bool:
    """True when an app name is a browser, a WebKit shell or an Electron app."""
    low = (name or "").lower()
    return "webkit" in low or any(w in BROWSER_APPS for w in re.split(r"[^a-z0-9]+", low))


def keycode_for(name: str) -> tuple[int | None, bool]:
    """(virtual keycode, needs_shift) for an xdotool-style key name."""
    key = (name or "").strip()
    lowered = key.lower()
    if lowered in _SHIFTED:
        base_name = _SHIFTED[lowered]
        return _KEYCODES.get(base_name), True
    if key in _KEYCODES:
        return _KEYCODES[key], False
    if lowered in _KEYCODES:
        # A bare capital letter is that letter with shift held.
        return _KEYCODES[lowered], len(key) == 1 and key.isupper()
    return None, False


def _flags_for(mods: list[str]) -> int:
    value = 0
    for m in mods:
        value |= _CG_FLAGS.get(m, 0)
    return value


def _post_mouse(kind: str, x: int, y: int, button: int = 1, clicks: int = 1,
                flags: int = 0) -> bool:
    Q = _quartz()
    if Q is None:
        return False
    down, up = {
        1: (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp),
        2: (Q.kCGEventOtherMouseDown, Q.kCGEventOtherMouseUp),
        3: (Q.kCGEventRightMouseDown, Q.kCGEventRightMouseUp),
    }.get(button, (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp))
    cg_button = {1: 0, 2: 2, 3: 1}.get(button, 0)
    point = Q.CGPointMake(float(x), float(y))

    def post(event_type, click_state=None):
        ev = Q.CGEventCreateMouseEvent(None, event_type, point, cg_button)
        if flags:
            Q.CGEventSetFlags(ev, flags)
        if click_state:
            Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventClickState, click_state)
        Q.CGEventPost(Q.kCGHIDEventTap, ev)

    if kind == "move":
        post(Q.kCGEventMouseMoved)
        return True
    if kind == "down":
        post(down, 1)
        return True
    if kind == "up":
        post(up, 1)
        return True
    post(Q.kCGEventMouseMoved)
    for n in range(1, max(1, clicks) + 1):
        # click_state is what turns two clicks into a double-click rather than
        # two singles; macOS reads it from the event, not from the timing.
        post(down, n)
        post(up, n)
    return True


def move(x: int, y: int) -> dict:
    emit_cursor("move", x, y)
    if _post_mouse("move", x, y):
        return {"ok": True, "at": [x, y], "via": "quartz"}
    if _has("cliclick"):
        _run(["cliclick", f"m:{x},{y}"])
        return {"ok": True, "at": [x, y], "via": "cliclick"}
    return base.unsupported("pointer move", PLATFORM,
                            "install pyobjc (pip install pyobjc-framework-Quartz) "
                            "or cliclick (brew install cliclick)")


def click(x: int, y: int, button: int = 1, count: int = 1) -> dict:
    emit_cursor("click", x, y)
    if _post_mouse("click", x, y, button=button, clicks=count):
        return {"ok": True, "clicked": [x, y], "button": button, "count": count,
                "via": "quartz"}
    if _has("cliclick"):
        verb = {1: "c", 2: "tc", 3: "rc"}.get(button, "c")
        for _ in range(max(1, count)):
            _run(["cliclick", f"{verb}:{x},{y}"])
        return {"ok": True, "clicked": [x, y], "button": button, "count": count,
                "via": "cliclick"}
    res = _osa(f'tell application "System Events" to click at {{{x}, {y}}}')
    if res.returncode == 0:
        return {"ok": True, "clicked": [x, y], "button": button, "count": count,
                "via": "system-events"}
    return base.unsupported("click", PLATFORM,
                            "grant Accessibility, or install pyobjc/cliclick")


def click_with(x: int, y: int, button: int = 1, count: int = 1,
               modifiers: list[str] | None = None) -> dict:
    mods, unknown = base.normalize_modifiers(modifiers)
    if unknown:
        return {"ok": False, "error": f"unknown modifier(s) {unknown}",
                "supported": sorted(base.MODIFIERS)}
    if not mods:
        return click(x, y, button=button, count=count)
    if _post_mouse("click", x, y, button=button, clicks=count,
                   flags=_flags_for(mods)):
        return {"ok": True, "clicked": [x, y], "button": button, "count": count,
                "modifiers": mods, "via": "quartz"}
    # Without Quartz there is no way to hold a modifier across a click here:
    # System Events cannot, and cliclick's key-down state does not survive
    # separate invocations. Refuse rather than send a plain click — a
    # shift-click that loses its shift does not extend a selection, it replaces
    # it, and the caller would never know.
    if _has("cliclick"):
        keys = ",".join({"super": "cmd", "ctrl": "ctrl", "alt": "alt",
                         "shift": "shift", "control": "ctrl",
                         "meta": "cmd"}[m] for m in mods)
        verb = {1: "c", 2: "tc", 3: "rc"}.get(button, "c")
        _run(["cliclick", f"kd:{keys}", f"{verb}:{x},{y}", f"ku:{keys}"])
        return {"ok": True, "clicked": [x, y], "button": button, "count": count,
                "modifiers": mods, "via": "cliclick"}
    return base.unsupported("modifier-qualified click", PLATFORM,
                            "install pyobjc-framework-Quartz or cliclick; a "
                            "plain click would be a different action, so this "
                            "does not fall back to one")


def mouse_down(button: int = 1, x: int | None = None, y: int | None = None) -> dict:
    if x is None or y is None:
        pos = pointer_position()
        x, y = pos.get("x", 0), pos.get("y", 0)
    emit_cursor("move", x, y)
    if _post_mouse("down", x, y, button=button):
        return {"ok": True, "held": button, "at": [x, y], "via": "quartz"}
    if _has("cliclick"):
        _run(["cliclick", f"dd:{x},{y}"])
        return {"ok": True, "held": button, "at": [x, y], "via": "cliclick"}
    return base.unsupported("mouse_down", PLATFORM, "install pyobjc or cliclick")


def mouse_up(button: int = 1, x: int | None = None, y: int | None = None) -> dict:
    if x is None or y is None:
        pos = pointer_position()
        x, y = pos.get("x", 0), pos.get("y", 0)
    if _post_mouse("up", x, y, button=button):
        return {"ok": True, "released": button, "via": "quartz"}
    if _has("cliclick"):
        _run(["cliclick", f"du:{x},{y}"])
        return {"ok": True, "released": button, "via": "cliclick"}
    return base.unsupported("mouse_up", PLATFORM, "install pyobjc or cliclick")


def drag(from_x: int, from_y: int, to_x: int, to_y: int) -> dict:
    Q = _quartz()
    if Q is not None:
        _post_mouse("move", from_x, from_y)
        _post_mouse("down", from_x, from_y)
        # A drag with no intermediate points is ignored by many AppKit views:
        # they track kCGEventLeftMouseDragged, not the endpoints.
        steps = 12
        for i in range(1, steps + 1):
            ix = int(from_x + (to_x - from_x) * i / steps)
            iy = int(from_y + (to_y - from_y) * i / steps)
            ev = Q.CGEventCreateMouseEvent(None, Q.kCGEventLeftMouseDragged,
                                           Q.CGPointMake(float(ix), float(iy)), 0)
            Q.CGEventPost(Q.kCGHIDEventTap, ev)
            time.sleep(0.008)
        _post_mouse("up", to_x, to_y)
        emit_cursor("click", to_x, to_y)
        return {"ok": True, "from": [from_x, from_y], "to": [to_x, to_y],
                "via": "quartz"}
    if _has("cliclick"):
        _run(["cliclick", f"dd:{from_x},{from_y}", f"du:{to_x},{to_y}"])
        return {"ok": True, "from": [from_x, from_y], "to": [to_x, to_y],
                "via": "cliclick"}
    return base.unsupported("drag", PLATFORM, "install pyobjc or cliclick")


def scroll(direction: str, amount: int = 3) -> dict:
    delta = {"up": (1, 0), "down": (-1, 0), "left": (0, 1), "right": (0, -1)}.get(direction)
    if delta is None:
        return {"ok": False, "error": "direction must be up|down|left|right"}
    Q = _quartz()
    if Q is None:
        return base.unsupported("scroll", PLATFORM, "install pyobjc-framework-Quartz")
    vy, vx = delta
    for _ in range(max(1, amount)):
        ev = Q.CGEventCreateScrollWheelEvent(None, Q.kCGScrollEventUnitLine,
                                             2, vy * 3, vx * 3)
        Q.CGEventPost(Q.kCGHIDEventTap, ev)
        time.sleep(0.01)
    return {"ok": True, "scrolled": direction, "amount": amount, "via": "quartz"}


def scroll_at(x: int, y: int, direction: str, amount: int = 3) -> dict:
    move(x, y)
    out = scroll(direction, amount)
    if out.get("ok"):
        out["at"] = [x, y]
    return out


def hover(x: int, y: int, settle_ms: int = 350) -> dict:
    move(x, y)
    time.sleep(max(0, settle_ms) / 1000.0)
    return {"ok": True, "at": [x, y], "settled_ms": settle_ms}


def pointer_position() -> dict:
    Q = _quartz()
    if Q is not None:
        loc = Q.CGEventGetLocation(Q.CGEventCreate(None))
        return {"ok": True, "x": int(loc.x), "y": int(loc.y), "via": "quartz"}
    if _has("cliclick"):
        out = _run(["cliclick", "p"]).stdout.strip()
        if "," in out:
            x, y = out.split(",")[:2]
            return {"ok": True, "x": int(float(x)), "y": int(float(y)),
                    "via": "cliclick"}
    return base.unsupported("pointer_position", PLATFORM,
                            "install pyobjc or cliclick")


def _ms(value) -> int:
    """A caller's hold or gap in ms, bounded so no key stays down for long."""
    try:
        return max(0, min(int(value or 0), MAX_KEY_MS))
    except (TypeError, ValueError):
        return 0


def _post_key(code: int, down: bool, flags: int = 0, text: str | None = None) -> bool:
    Q = _quartz()
    if Q is None:
        return False
    ev = Q.CGEventCreateKeyboardEvent(None, code, down)
    if flags:
        Q.CGEventSetFlags(ev, flags)
    if text:
        # The characters a real event carries, so the text is right whatever
        # the layout. The count is UTF-16 units: an emoji is two.
        Q.CGEventKeyboardSetUnicodeString(ev, len(text.encode("utf-16-le")) // 2, text)
    Q.CGEventPost(Q.kCGHIDEventTap, ev)
    return True


def _stroke(code: int, mods: list[str], hold_ms: int = 0, text: str | None = None) -> None:
    """One keystroke as a person makes it: each modifier is a real key going
    down first (not only a flag on the key), the key is held for hold_ms,
    then everything comes back up in reverse, even when interrupted."""
    held = 0
    down: list[tuple[int, int]] = []
    try:
        for m in mods:
            mcode = _MOD_KEYCODE.get(m)
            if mcode is None or any(c == mcode for c, _ in down):
                continue
            held |= _CG_FLAGS.get(m, 0)
            _post_key(mcode, True, held)
            down.append((mcode, _CG_FLAGS.get(m, 0)))
        _post_key(code, True, held, text)
        try:
            if hold_ms > 0:
                time.sleep(hold_ms / 1000.0)
        finally:
            _post_key(code, False, held, text)
    finally:
        for mcode, bit in reversed(down):
            held &= ~bit
            _post_key(mcode, False, held)


def press_key(key: str, hold_ms: int = 0) -> dict:
    """xdotool key syntax, translated: 'Return', 'ctrl+c', 'super+l', 'KP_0'.
    `super` is Command here, so a model can write one combo for every OS.
    hold_ms keeps the key down that long (Human Mode's dwell), with real
    modifier key events around it."""
    mods, name = base.split_combo(key)
    code, needs_shift = keycode_for(name)
    if code is None:
        return {"ok": False, "error": f"unknown key {name!r}",
                "hint": "use xdotool names: Return, Tab, Escape, ctrl+c, super+space"}
    if needs_shift and "shift" not in mods:
        mods = mods + ["shift"]
    if _quartz() is not None:
        _stroke(code, mods, _ms(hold_ms))
        return {"ok": True, "key": key, "via": "quartz"}
    using = " using {" + ", ".join(
        {"super": "command down", "ctrl": "control down", "alt": "option down",
         "shift": "shift down", "control": "control down",
         "meta": "command down"}[m] for m in mods) + "}" if mods else ""
    res = _osa(f'tell application "System Events" to key code {code}{using}')
    if res.returncode == 0:
        return {"ok": True, "key": key, "via": "system-events"}
    return base.unsupported("press_key", PLATFORM,
                            "grant Accessibility or install pyobjc")


def key_down(key: str) -> dict:
    code, _ = keycode_for(key)
    if code is None:
        code = _MOD_KEYCODE.get((key or "").lower())
    if code is None:
        return {"ok": False, "error": f"unknown key {key!r}"}
    if _post_key(code, True):
        return {"ok": True, "held": key, "via": "quartz"}
    return base.unsupported("key_down", PLATFORM,
                            "held keys need pyobjc-framework-Quartz")


def key_up(key: str) -> dict:
    code, _ = keycode_for(key)
    if code is None:
        code = _MOD_KEYCODE.get((key or "").lower())
    if code is None:
        return {"ok": False, "error": f"unknown key {key!r}"}
    if _post_key(code, False):
        return {"ok": True, "released": key, "via": "quartz"}
    return base.unsupported("key_up", PLATFORM,
                            "held keys need pyobjc-framework-Quartz")


def release_all() -> dict:
    """hand_back's release beyond the agent's tracked holds: on macOS that is
    nothing, and the result says it cannot be verified."""
    # XTEST keeps a per-device held state that can be queried and cleared.
    # Quartz does not: posted and physical input share one state table, so a
    # key-up for a modifier the agent never pressed can cancel one the owner
    # is physically holding. hand_back has already released the tracked holds
    # through key_up / mouse_up (Quartz) before it asks this.
    return {"ok": True, "verified": None, "released_blind": False,
            "note": "macOS: tracked holds released one by one; no per-source held "
                    "state to verify against, so nothing is released blind"}


def _frontmost_is_browser() -> bool:
    return is_browser_app((focused_window_identity() or {}).get("class") or "")


def _paste(text: str, hold_ms: int = 0) -> dict:
    """The way a person enters a character their keyboard has no key for."""
    setres = clipboard_set(text=text)
    if not setres.get("ok"):
        return setres
    return press_key("super+v", hold_ms=hold_ms)


def type_text(text: str, delay_ms: int = 40, dwell_ms: int = 0) -> dict:
    """Type a literal string as the keys a person presses for it.

    Each character a US layout has is sent on its real virtual keycode, so a
    page reads a real event.code, with a real Shift press around capitals and
    shifted symbols; dwell_ms holds each key down, delay_ms is the gap after.
    A character the layout lacks is pasted when a browser is in front (there
    a bare unicode payload arrives with an empty event.code) and sent as a
    unicode payload in a native app, where it arrives intact.
    """
    # Fixed 2026-09-12: every character went out as a unicode payload on
    # keycode 0: an empty event.code and no Shift on capitals, a page's tell.
    Q = _quartz()
    if Q is not None:
        dwell, gap = _ms(dwell_ms), _ms(delay_ms)
        browser: bool | None = None
        saved = None
        try:
            for i, ch in enumerate(text):
                code, shift = char_key(ch)
                if code is not None:
                    _stroke(code, ["shift"] if shift else [], dwell,
                            ch if ch.isprintable() else None)
                else:
                    if browser is None:
                        browser = _frontmost_is_browser()
                    if browser:
                        if saved is None:
                            saved = clipboard_get()
                        pasted = _paste(ch, dwell)
                        if not pasted.get("ok"):
                            return {"ok": False, "error": f"paste failed: {pasted.get('error')}",
                                    "typed_len": i, "via": "quartz+paste"}
                    else:
                        _stroke(0, [], dwell, ch)
                if gap:
                    time.sleep(gap / 1000.0)
            return {"ok": True, "typed_len": len(text), "via": "quartz"}
        finally:
            if saved is not None and saved.get("ok"):
                time.sleep(PASTE_RESTORE_S)
                clipboard_set(text=saved.get("text") or "")
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    res = _osa(f'tell application "System Events" to keystroke "{escaped}"')
    if res.returncode == 0:
        return {"ok": True, "typed_len": len(text), "via": "system-events"}
    return base.unsupported("type_text", PLATFORM,
                            "grant Accessibility or install pyobjc")


# ---- environment -----------------------------------------------------------
def clipboard_get(selection: str = "clipboard") -> dict:
    if selection == "primary":
        return {"ok": False, "error": "macOS has no PRIMARY selection",
                "hint": "use selection='clipboard'"}
    res = _run(["pbpaste"])
    if res.returncode != 0:
        return {"ok": False, "error": res.stderr.strip() or "pbpaste failed"}
    return {"ok": True, "text": res.stdout, "selection": "clipboard"}


def clipboard_set(text: str = "", selection: str = "clipboard") -> dict:
    if selection == "primary":
        return {"ok": False, "error": "macOS has no PRIMARY selection",
                "hint": "use selection='clipboard'"}
    res = subprocess.run(["pbcopy"], input=text, text=True, capture_output=True,
                         timeout=20)
    if res.returncode != 0:
        return {"ok": False, "error": res.stderr.strip() or "pbcopy failed"}
    return {"ok": True, "set_len": len(text), "selection": "clipboard"}


# ---- optional capabilities -------------------------------------------------
# Window state and geometry go through System Events' accessibility bridge,
# which is the only supported way to move another app's window on macOS. It
# needs the Accessibility grant; without it osascript returns error -1719 and
# these report that instead of failing silently.

_AX_ACTIONS = {
    "minimize": ('set value of attribute "AXMinimized" of {ref} to true', None),
    "unminimize": ('set value of attribute "AXMinimized" of {ref} to false', None),
    "maximize": ('set value of attribute "AXFullScreen" of {ref} to true',
                 "macOS has no 'maximize'; this goes full screen"),
    "unmaximize": ('set value of attribute "AXFullScreen" of {ref} to false', None),
    "fullscreen": ('set value of attribute "AXFullScreen" of {ref} to true', None),
    "unfullscreen": ('set value of attribute "AXFullScreen" of {ref} to false', None),
    "close": ('perform action "AXPress" of (first button of {ref} whose '
              'subrole is "AXCloseButton")', None),
}


def _escape(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def _window_ref(win: dict) -> tuple[str, str]:
    """(process name, AppleScript reference to the window)."""
    app = _escape(win.get("app") or win.get("name") or "")
    title = win.get("title") or win.get("name") or ""
    if title:
        ref = f'(first window of process "{app}" whose name contains "{_escape(title)}")'
    else:
        ref = f'window 1 of process "{app}"'
    return app, ref


def list_windows_rich() -> list[dict]:
    """Same as list_windows: the Quartz window list already carries geometry
    and ownership, and macOS exposes no extra WM state to layer on top."""
    return list_windows()


def window_action(query: str, action: str) -> dict:
    win = _match_window(query)
    if win is None:
        return {"ok": False, "error": f"no window matching {query!r}"}
    if action == "activate":
        return focus_window(query)
    if action in ("above", "unabove", "pin", "unpin"):
        return base.unsupported(f"window action {action!r}", PLATFORM,
                                "macOS has no always-on-top or pin for other apps")
    spec = _AX_ACTIONS.get(action)
    if spec is None:
        return {"ok": False, "error": f"unknown action {action!r}",
                "actions": ["activate", *sorted(_AX_ACTIONS)]}
    body, note = spec
    _app, ref = _window_ref(win)
    res = _osa(f'tell application "System Events" to {body.format(ref=ref)}')
    if res.returncode != 0:
        return {"ok": False, "error": res.stderr.strip(),
                "hint": "grant Accessibility in System Settings > Privacy & Security"}
    out = {"ok": True, "action": action, "window": win.get("name")}
    if note:
        out["note"] = note
    return out


def window_geometry(query: str, x: int | None = None, y: int | None = None,
                    w: int | None = None, h: int | None = None) -> dict:
    win = _match_window(query)
    if win is None:
        return {"ok": False, "error": f"no window matching {query!r}"}
    _app, ref = _window_ref(win)
    statements = []
    if x is not None or y is not None:
        statements.append(f'set position of {ref} to '
                          f'{{{x if x is not None else win["x"]}, '
                          f'{y if y is not None else win["y"]}}}')
    if w is not None or h is not None:
        statements.append(f'set size of {ref} to '
                          f'{{{w if w is not None else win["w"]}, '
                          f'{h if h is not None else win["h"]}}}')
    if not statements:
        return {"ok": True, "window": win.get("name"), "unchanged": True,
                "geometry": {k: win[k] for k in ("x", "y", "w", "h")}}
    for statement in statements:
        res = _osa(f'tell application "System Events" to {statement}')
        if res.returncode != 0:
            return {"ok": False, "error": res.stderr.strip(),
                    "hint": "grant Accessibility in System Settings > Privacy & Security"}
    after = _match_window(query) or win
    return {"ok": True, "window": win.get("name"),
            "geometry": {k: after.get(k) for k in ("x", "y", "w", "h")}}


def workspaces() -> dict:
    return base.unsupported("virtual desktops", PLATFORM,
                            "macOS Spaces have no public API; switch with "
                            "press_key('ctrl+Right') or Mission Control")


def set_workspace(index: int = 0) -> dict:
    return workspaces()


def move_to_workspace(query: str, index: int = 0) -> dict:
    return workspaces()


def display_name() -> str:
    _x, _y, w, h = _main_bounds()
    return f"quartz:{w}x{h}" if w else "quartz:unavailable"


def platform_info() -> dict:
    ver = _run(["sw_vers", "-productVersion"]).stdout.strip() if _has("sw_vers") else ""
    return {
        "backend": "darwin",
        "os": "macos",
        "os_version": ver,
        "display": display_name(),
        "backing_scale": _backing_scale(),
        "quartz": _quartz() is not None,
        "tools": {name: _has(name) for name in
                  ("screencapture", "osascript", "sips", "cliclick", "pbcopy")},
        "modifier_super": "Command",
        "permissions": {
            "screen_recording": "System Settings > Privacy & Security > Screen Recording",
            "accessibility": "System Settings > Privacy & Security > Accessibility",
            "granted_to": "the app that launched Workman (Terminal, iTerm, Claude), "
                          "not Workman itself",
        },
        "notes": ("Screenshots are normalised from backing pixels to points, so "
                  "coordinates read off an image are click-ready."),
        "contract": list(base.CONTRACT),
    }
