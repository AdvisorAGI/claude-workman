"""X11 backend — see/click/type via ffmpeg (x11grab) + xdotool.

The accuracy discipline baked in: see -> locate -> act -> see again. Callers
should screenshot-verify before any consequential Return. Synthetic keys via
`xdotool key --window` are ignored by Electron/VTE — always focus with a real
click + global type, which is what `type_text`/`press_key` here do.
"""
from __future__ import annotations
import os
import shutil
import subprocess
import tempfile

DISPLAY = os.environ.get("WORKMAN_DISPLAY") or os.environ.get("DISPLAY") or ":0"


def _env() -> dict:
    e = dict(os.environ)
    e["DISPLAY"] = DISPLAY
    return e


def _run(cmd: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, env=_env(), capture_output=True, text=True, timeout=timeout)


def _require(binary: str) -> None:
    if shutil.which(binary) is None:
        raise RuntimeError(f"'{binary}' not found on PATH — install it (Workman needs xdotool + ffmpeg)")


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
    _run(["xdotool", "mousemove", str(x), str(y), "click", "--repeat", str(count), str(button)])
    return {"ok": True, "clicked": [x, y], "button": button, "count": count}


def move(x: int, y: int) -> dict:
    _run(["xdotool", "mousemove", str(x), str(y)])
    return {"ok": True, "at": [x, y]}


def drag(from_x: int, from_y: int, to_x: int, to_y: int) -> dict:
    _run(["xdotool", "mousemove", str(from_x), str(from_y), "mousedown", "1",
          "mousemove", str(to_x), str(to_y), "mouseup", "1"])
    return {"ok": True, "from": [from_x, from_y], "to": [to_x, to_y]}


def scroll(direction: str, amount: int = 3) -> dict:
    button = {"up": 4, "down": 5, "left": 6, "right": 7}.get(direction)
    if not button:
        return {"ok": False, "error": "direction must be up|down|left|right"}
    _run(["xdotool", "click", "--repeat", str(amount), str(button)])
    return {"ok": True, "scrolled": direction, "amount": amount}


def type_text(text: str, delay_ms: int = 40) -> dict:
    _require("xdotool")
    _run(["xdotool", "type", "--delay", str(delay_ms), text])
    return {"ok": True, "typed_len": len(text)}


def press_key(key: str) -> dict:
    """xdotool key syntax: 'Return', 'Tab', 'ctrl+c', 'super+l', 'KP_0'."""
    _require("xdotool")
    _run(["xdotool", "key", key])
    return {"ok": True, "key": key}
