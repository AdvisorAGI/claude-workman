"""X11 backend — see/click/type via ffmpeg (x11grab) + xdotool.

The accuracy discipline baked in: see -> locate -> act -> see again. Callers
should screenshot-verify before any consequential Return. Synthetic keys via
`xdotool key --window` are ignored by Electron/VTE — always focus with a real
click + global type, which is what `type_text`/`press_key` here do.
"""
from __future__ import annotations
import os
import re
import shutil
import subprocess
import tempfile
import time

DISPLAY = os.environ.get("WORKMAN_DISPLAY") or os.environ.get("DISPLAY") or ":0"


def _env() -> dict:
    e = dict(os.environ)
    e["DISPLAY"] = DISPLAY
    return e


def _run(cmd: list[str], timeout: int = 20, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, env=_env(), capture_output=True, text=True, timeout=timeout, input=input_text)


def _require(binary: str) -> None:
    if shutil.which(binary) is None:
        raise RuntimeError(f"'{binary}' not found on PATH — install it (Workman needs xdotool + ffmpeg)")


_CURSOR_FIFO = os.environ.get("WORKMAN_CURSOR_FIFO", os.path.expanduser("~/.cache/workman/cursor.fifo"))


def emit_cursor(kind: str, x: int, y: int) -> None:
    """Best-effort notify the visual cursor overlay (workman.cursor) of an action.
    Silent no-op if the overlay daemon isn't running."""
    try:
        if os.path.exists(_CURSOR_FIFO):
            fd = os.open(_CURSOR_FIFO, os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.write(fd, f"{kind} {x} {y}\n".encode())
            finally:
                os.close(fd)
    except OSError:
        pass


def screen_size() -> tuple[int, int]:
    _require("xdotool")
    out = _run(["xdotool", "getdisplaygeometry"]).stdout.split()
    return (int(out[0]), int(out[1])) if len(out) == 2 else (1920, 1080)


def screenshot(max_dim: int | None = None, region: tuple[int, int, int, int] | None = None) -> bytes:
    """Grab the display as PNG bytes. region=(x,y,w,h) captures a sub-rect.
    max_dim downscales so no side exceeds it (needs Pillow; ignored if absent)."""
    _require("ffmpeg")
    w, h = screen_size()
    vinput = DISPLAY
    size = f"{w}x{h}"
    if region:
        rx, ry, rw, rh = region
        vinput = f"{DISPLAY}+{rx},{ry}"
        size = f"{rw}x{rh}"
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        path = tmp.name
    try:
        _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "x11grab",
              "-video_size", size, "-i", vinput, "-frames:v", "1", path])
        data = open(path, "rb").read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if max_dim:
        try:
            import io
            from PIL import Image
            im = Image.open(io.BytesIO(data))
            scale = min(1.0, max_dim / max(im.size))
            if scale < 1.0:
                im = im.resize((int(im.width * scale), int(im.height * scale)))
                buf = io.BytesIO()
                im.save(buf, "PNG")
                data = buf.getvalue()
        except Exception:
            pass
    return data


def list_windows() -> list[dict]:
    _require("xdotool")
    ids = _run(["xdotool", "search", "--name", ""]).stdout.split()
    wins = []
    for wid in ids:
        name = _run(["xdotool", "getwindowname", wid]).stdout.strip()
        if not name:
            continue
        pid = _run(["xdotool", "getwindowpid", wid]).stdout.strip()
        geo = _run(["xdotool", "getwindowgeometry", "--shell", wid]).stdout
        g = dict(line.split("=", 1) for line in geo.splitlines() if "=" in line)
        wins.append({"id": wid, "name": name, "pid": pid,
                     "x": g.get("X"), "y": g.get("Y"), "w": g.get("WIDTH"), "h": g.get("HEIGHT")})
    return wins


def focus_window(query: str, minimize_blockers: bool = True) -> dict:
    """Raise a window by id or name-substring. On focus-stealing WMs (mutter),
    windowactivate can silently fail — optionally minimize the frontmost blocker
    first. ALWAYS verify with a screenshot before typing."""
    _require("xdotool")
    wins = list_windows()
    if query.isdigit():
        target = query
    else:
        m = [w for w in wins if query.lower() in w["name"].lower()]
        if not m:
            return {"ok": False, "error": f"no window matching {query!r}", "windows": [w["name"] for w in wins]}
        target = m[0]["id"]
    if minimize_blockers:
        active = _run(["xdotool", "getactivewindow"]).stdout.strip()
        if active and active != target:
            _run(["xdotool", "windowminimize", active])
    _run(["xdotool", "windowactivate", "--sync", target])
    _run(["xdotool", "windowraise", target])
    now = _run(["xdotool", "getactivewindow", "getwindowname"]).stdout.strip()
    return {"ok": True, "target": target, "frontmost_now": now, "verify": "screenshot before typing"}


def click(x: int, y: int, button: int = 1, count: int = 1) -> dict:
    _require("xdotool")
    emit_cursor("click", x, y)
    _run(["xdotool", "mousemove", str(x), str(y), "click", "--repeat", str(count), str(button)])
    return {"ok": True, "clicked": [x, y], "button": button, "count": count}


def move(x: int, y: int) -> dict:
    emit_cursor("move", x, y)
    _run(["xdotool", "mousemove", str(x), str(y)])
    return {"ok": True, "at": [x, y]}


def drag(from_x: int, from_y: int, to_x: int, to_y: int) -> dict:
    emit_cursor("move", from_x, from_y)
    _run(["xdotool", "mousemove", str(from_x), str(from_y)])
    _run(["xdotool", "mousedown", "1"])
    try:
        time.sleep(0.08)  # Let the window manager start the drag before motion.
        _run(["xdotool", "mousemove", str(to_x), str(to_y)])
        time.sleep(0.08)
    finally:
        _run(["xdotool", "mouseup", "1"])
    emit_cursor("click", to_x, to_y)
    return {"ok": True, "from": [from_x, from_y], "to": [to_x, to_y]}


def scroll(direction: str, amount: int = 3) -> dict:
    button = {"up": 4, "down": 5, "left": 6, "right": 7}.get(direction)
    if not button:
        return {"ok": False, "error": "direction must be up|down|left|right"}
    _run(["xdotool", "click", "--repeat", str(amount), str(button)])
    return {"ok": True, "scrolled": direction, "amount": amount}


def type_text(text: str, delay_ms: int = 40) -> dict:
    _require("xdotool")
    result = _run(["xdotool", "type", "--delay", str(delay_ms), "--file", "-"], input_text=text)
    if result.returncode:
        return {"ok": False, "error": "typing command failed"}
    return {"ok": True, "typed_len": len(text)}


def press_key(key: str) -> dict:
    """xdotool key syntax: 'Return', 'Tab', 'ctrl+c', 'super+l', 'KP_0'."""
    _require("xdotool")
    _run(["xdotool", "key", key])
    return {"ok": True, "key": key}


# ---- fine-grained input -----------------------------------------------------
# Held state is the reason these exist separately from click/press_key: a
# modifier-qualified click, a slow drag with a pause mid-flight, or a key held
# across several actions all need the press and the release to be separate calls.

_MODIFIERS = {"ctrl", "control", "alt", "shift", "super", "meta"}


def pointer_position() -> dict:
    """Where the pointer is now, and which window is under it."""
    _require("xdotool")
    out = _run(["xdotool", "getmouselocation", "--shell"]).stdout
    vals = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return {"ok": True, "x": int(vals.get("X", 0)), "y": int(vals.get("Y", 0)),
            "screen": vals.get("SCREEN"), "window": vals.get("WINDOW")}


def mouse_down(button: int = 1, x: int | None = None, y: int | None = None) -> dict:
    """Press and HOLD a mouse button. Pair with mouse_up or the desktop is left
    with a stuck button."""
    _require("xdotool")
    cmd = ["xdotool"]
    if x is not None and y is not None:
        cmd += ["mousemove", str(x), str(y)]
        emit_cursor("move", x, y)
    cmd += ["mousedown", str(button)]
    _run(cmd)
    return {"ok": True, "held": button, "at": [x, y] if x is not None else None}


def mouse_up(button: int = 1, x: int | None = None, y: int | None = None) -> dict:
    _require("xdotool")
    cmd = ["xdotool"]
    if x is not None and y is not None:
        cmd += ["mousemove", str(x), str(y)]
    cmd += ["mouseup", str(button)]
    _run(cmd)
    if x is not None and y is not None:
        emit_cursor("click", x, y)
    return {"ok": True, "released": button}


def key_down(key: str) -> dict:
    """Hold a key down (modifiers especially). Pair with key_up."""
    _require("xdotool")
    _run(["xdotool", "keydown", key])
    return {"ok": True, "held": key}


def key_up(key: str) -> dict:
    _require("xdotool")
    _run(["xdotool", "keyup", key])
    return {"ok": True, "released": key}


def click_with(x: int, y: int, button: int = 1, count: int = 1,
               modifiers: list[str] | None = None) -> dict:
    """Click while holding modifiers, e.g. shift+click to extend a selection.

    The modifiers are released even if the click fails, so a crash mid-sequence
    cannot leave ctrl stuck down for the human using the machine afterwards.
    """
    _require("xdotool")
    mods = [m.strip().lower() for m in (modifiers or []) if m.strip()]
    unknown = [m for m in mods if m not in _MODIFIERS]
    if unknown:
        return {"ok": False, "error": f"unknown modifier(s) {unknown}",
                "supported": sorted(_MODIFIERS)}
    for mod in mods:
        _run(["xdotool", "keydown", mod])
    try:
        return click(x, y, button=button, count=count)
    finally:
        for mod in reversed(mods):
            _run(["xdotool", "keyup", mod])


def scroll_at(x: int, y: int, direction: str, amount: int = 3) -> dict:
    """Scroll over a specific point. Plain `scroll` hits whatever the pointer
    happens to be over, which is rarely the pane you meant."""
    button = {"up": 4, "down": 5, "left": 6, "right": 7}.get(direction)
    if not button:
        return {"ok": False, "error": "direction must be up|down|left|right"}
    _require("xdotool")
    emit_cursor("move", x, y)
    _run(["xdotool", "mousemove", str(x), str(y), "click", "--repeat", str(amount), str(button)])
    return {"ok": True, "at": [x, y], "scrolled": direction, "amount": amount}


def hover(x: int, y: int, settle_ms: int = 350) -> dict:
    """Move the pointer and wait for hover-triggered UI (tooltips, submenus) to
    appear before the caller screenshots."""
    move(x, y)
    time.sleep(max(0, settle_ms) / 1000.0)
    return {"ok": True, "at": [x, y], "settled_ms": settle_ms}


# ---- display + window facts -------------------------------------------------
def monitors() -> list[dict]:
    """Physical outputs and their positions in the X screen.

    Worth checking before trusting a single screen size: a scaled or multi-head
    setup can make the X screen and the panel's native mode disagree.
    """
    if shutil.which("xrandr") is None:
        w, h = screen_size()
        return [{"name": "screen", "x": 0, "y": 0, "w": w, "h": h, "primary": True}]
    out = _run(["xrandr", "--listmonitors"]).stdout
    found = []
    for line in out.splitlines()[1:]:
        m = re.match(r"\s*(\d+):\s+([+*]*)(\S+)\s+(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)", line)
        if m:
            _, flags, name, w, h, x, y = m.groups()
            found.append({"name": name, "x": int(x), "y": int(y), "w": int(w), "h": int(h),
                          "primary": "*" in flags})
    if not found:
        w, h = screen_size()
        found = [{"name": "screen", "x": 0, "y": 0, "w": w, "h": h, "primary": True}]
    return found


def active_window() -> dict:
    """The window that currently has focus."""
    _require("xdotool")
    wid = _run(["xdotool", "getactivewindow"]).stdout.strip()
    if not wid:
        return {"ok": False, "error": "no active window"}
    name = _run(["xdotool", "getwindowname", wid]).stdout.strip()
    pid = _run(["xdotool", "getwindowpid", wid]).stdout.strip()
    geo = _run(["xdotool", "getwindowgeometry", "--shell", wid]).stdout
    g = dict(line.split("=", 1) for line in geo.splitlines() if "=" in line)
    return {"ok": True, "id": wid, "name": name, "pid": pid,
            "x": g.get("X"), "y": g.get("Y"), "w": g.get("WIDTH"), "h": g.get("HEIGHT")}


def kill_window(query: str) -> dict:
    """Force a window's client to die. Unsaved work goes with it — prefer the
    graceful close in gtkops."""
    _require("xdotool")
    target = query
    if not query.isdigit():
        matches = [w for w in list_windows() if query.lower() in w["name"].lower()]
        if not matches:
            return {"ok": False, "error": f"no window matching {query!r}"}
        target = matches[0]["id"]
    _run(["xdotool", "windowkill", target])
    return {"ok": True, "killed": target, "warning": "client terminated without saving"}
