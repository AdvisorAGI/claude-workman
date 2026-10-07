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

from . import polite

DISPLAY = os.environ.get("WORKMAN_DISPLAY") or os.environ.get("DISPLAY") or ":0"


def _env() -> dict:
    e = dict(os.environ)
    e["DISPLAY"] = DISPLAY
    return e


def _run(cmd: list[str], timeout: int = 20, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, env=_env(), capture_output=True, text=True, timeout=timeout, input=input_text)


# ---- the persistent channel ------------------------------------------------
# Every primitive below asks `_xt()` first. With a channel, an action is one X
# request on an open connection; without one (no libXtst, WORKMAN_XTEST=0, the
# test suite) it is the original xdotool/ffmpeg subprocess. Both paths stamp
# the injection so the Escape listener can tell an agent's XTEST events from
# the owner's KVM-relayed ones.

def _xt():
    """The XTest channel for DISPLAY, or None to use the subprocess path."""
    from . import xtest
    return xtest.channel(DISPLAY)


def _stamp(escape: bool = False) -> None:
    from . import owner_pause
    polite.guard()
    if polite.current() is not None or DISPLAY.split(".")[0] == ":0" or os.environ.get("WORKMAN_POLITE") == "0":
        owner_pause.mark_agent_input(escape=escape)


def _is_escape(key: str) -> bool:
    from . import xtest
    names = xtest.parse_combo(key)
    return bool(names) and names[-1] == "Escape"


def keysym_for(ch: str) -> int:
    from . import xtest
    return xtest.keysym_for_char(ch)


def input_channel() -> str:
    """'xtest' when actions go over the persistent connection, else 'xdotool'."""
    return "xtest" if _xt() is not None else "xdotool"


# Where this process last put the pointer. Raw XInput2 events never see a
# warp (the Deskflow KVM moves the owner's pointer with XWarpPointer), so the
# one thing that does reveal him moving the mouse is the pointer not being
# where the agent left it.
_LAST_PLACED: dict = {"at": None}
_INPUT_HELD: dict = {"keys": set(), "buttons": set()}
FOREIGN_MOTION_PX = 3


def _placed(x: int, y: int) -> None:
    _LAST_PLACED["at"] = (int(x), int(y))
    polite.placed(x, y)


def foreign_pointer_motion(tolerance: int = FOREIGN_MOTION_PX) -> bool:
    """True when the pointer moved since the agent last placed it: somebody
    else has the mouse. Costs one XQueryPointer; nothing is polled."""
    ch = _xt()
    at = _LAST_PLACED["at"]
    if ch is None or at is None:
        return False
    try:
        p = ch.pointer_position()
    except Exception:
        return False
    if abs(p["x"] - at[0]) > tolerance or abs(p["y"] - at[1]) > tolerance:
        _LAST_PLACED["at"] = None
        return True
    return False


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
    ch = _xt()
    if ch is not None:
        return ch.screen_size()
    _require("xdotool")
    out = _run(["xdotool", "getdisplaygeometry"]).stdout.split()
    return (int(out[0]), int(out[1])) if len(out) == 2 else (1920, 1080)


def screenshot(max_dim: int | None = None, region: tuple[int, int, int, int] | None = None) -> bytes:
    """Grab the display as PNG bytes. region=(x,y,w,h) captures a sub-rect.
    max_dim downscales so no side exceeds it (needs Pillow; ignored if absent)."""
    ch = _xt()
    if ch is not None:
        try:
            return ch.screenshot(region=region, max_dim=max_dim)
        except Exception:
            pass  # fall through to ffmpeg for this one frame
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
    if polite.current() is not None:
        polite.guard("focus_window")
        polite.may_change_focus()
        ch = _xt()
        if ch is None:
            return {"ok": False, "error": "focus_restore_unavailable"}
        result = ch.restore_input_focus({"window": int(target), "revert_to": 1})
        if result.get("ok"):
            polite.set_target(ch.top_level_focus())
        polite.observe_focus()
        return dict(result, target=target)
    if minimize_blockers:
        active = _run(["xdotool", "getactivewindow"]).stdout.strip()
        if active and active != target:
            _run(["xdotool", "windowminimize", active])
    _run(["xdotool", "windowactivate", "--sync", target])
    _run(["xdotool", "windowraise", target])
    now = _run(["xdotool", "getactivewindow", "getwindowname"]).stdout.strip()
    return {"ok": True, "target": target, "frontmost_now": now, "verify": "screenshot before typing"}


def click(x: int, y: int, button: int = 1, count: int = 1) -> dict:
    if polite.current() is not None:
        moved = move(x, y)
        if not moved.get("ok"):
            return moved
        completed = 0
        for i in range(max(1, int(count))):
            polite.guard("click")
            down = mouse_down(button)
            if not down.get("ok"):
                return down
            completed += 1
            polite.progress(clicked=[x, y], count=completed, requested_count=count,
                            remaining_clicks=max(0, count - completed))
            up = mouse_up(button)
            if not up.get("ok"):
                return up
            if i < count - 1:
                polite.sleep(0.012)
        return {"ok": True, "clicked": [x, y], "button": button, "count": completed}
    emit_cursor("click", x, y)
    _stamp()
    _placed(x, y)
    ch = _xt()
    if ch is not None:
        ch.click(x, y, button=button, count=count)
        return {"ok": True, "clicked": [x, y], "button": button, "count": count, "via": "xtest"}
    _require("xdotool")
    _run(["xdotool", "mousemove", str(x), str(y), "click", "--repeat", str(count), str(button)])
    return {"ok": True, "clicked": [x, y], "button": button, "count": count}


def move(x: int, y: int) -> dict:
    _stamp()
    emit_cursor("move", x, y)
    ch = _xt()
    if ch is not None:
        ch.move(x, y)
        _placed(x, y)
        return {"ok": True, "at": [x, y], "via": "xtest"}
    if polite.current() is not None:
        return {"ok": False, "error": "owned_state_unavailable"}
    result = _run(["xdotool", "mousemove", str(x), str(y)])
    if polite.current() is not None and result.returncode:
        return {"ok": False, "error": "move failed"}
    _placed(x, y)
    return {"ok": True, "at": [x, y]}


def drag(from_x: int, from_y: int, to_x: int, to_y: int) -> dict:
    if polite.current() is not None:
        moved = move(from_x, from_y)
        if not moved.get("ok"):
            return moved
        down = mouse_down(1)
        if not down.get("ok"):
            return down
        polite.progress(at=[from_x, from_y], dragged=False, **{"from": [from_x, from_y], "to": [to_x, to_y]})
        polite.sleep(0.08)
        moved = move(to_x, to_y)
        if not moved.get("ok"):
            return moved
        polite.progress(at=[to_x, to_y], dragged=True)
        polite.sleep(0.08)
        return mouse_up(1) | {"from": [from_x, from_y], "to": [to_x, to_y]}
    emit_cursor("move", from_x, from_y)
    _stamp()
    _placed(to_x, to_y)
    ch = _xt()
    if ch is not None:
        ch.move(from_x, from_y)
        ch.button(1, True)
        try:
            time.sleep(0.08)
            ch.move(to_x, to_y)
            time.sleep(0.08)
        finally:
            ch.button(1, False)
        emit_cursor("click", to_x, to_y)
        return {"ok": True, "from": [from_x, from_y], "to": [to_x, to_y], "via": "xtest"}
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
    if polite.current() is not None:
        completed = 0
        for _ in range(max(0, int(amount))):
            polite.guard("scroll")
            down = mouse_down(button)
            if not down.get("ok"):
                return down
            completed += 1
            polite.progress(scrolled=direction, amount=completed, remaining=amount-completed)
            up = mouse_up(button)
            if not up.get("ok"):
                return up
        return {"ok": True, "scrolled": direction, "amount": completed}
    _stamp()
    ch = _xt()
    if ch is not None:
        ch.click(None, None, button=button, count=amount, gap_ms=0)
        return {"ok": True, "scrolled": direction, "amount": amount, "via": "xtest"}
    _run(["xdotool", "click", "--repeat", str(amount), str(button)])
    return {"ok": True, "scrolled": direction, "amount": amount}


BROWSER_CLASSES = ("chrome", "chromium", "firefox", "brave", "vivaldi", "opera",
                   "edge", "electron", "claude", "code", "slack", "discord")


def active_window_is_browser() -> bool:
    """Chrome, Chromium, Firefox or an Electron app has focus. Cheap: three
    property reads on the open connection; False when nothing can be told."""
    ch = _xt()
    if ch is None:
        return False
    try:
        info = ch.active_window_info()
    except Exception:
        return False
    if not info.get("ok"):
        return False
    haystack = f"{info.get('class', '')} {info.get('instance', '')}".lower()
    return any(b in haystack for b in BROWSER_CLASSES)


#: After pasting an off-layout character, wait then put the owner's clipboard
#: back. A page's paste handler reads the clipboard asynchronously.
PASTE_RESTORE_S = 0.3


def _clipboard_snapshot() -> dict | None:
    """Current clipboard, or None when it cannot be read."""
    from .platform import linux_x11
    try:
        snap = linux_x11.clipboard_get()
    except Exception:
        return None
    return snap if isinstance(snap, dict) else None


def _clipboard_restore(snap: dict | None) -> None:
    if not snap or not snap.get("ok"):
        return
    # Clipboard repair is not input injection. It must complete even when
    # owner activity/Escape interrupted the paste or its settling delay.
    try:
        polite.sleep(PASTE_RESTORE_S)
    except polite.Interrupted:
        pass
    from .platform import linux_x11
    try:
        linux_x11.clipboard_set(text=snap.get("text") or "")
    except Exception:
        pass


def _paste(text: str) -> dict:
    """The way a person enters a character that is not on the keyboard."""
    from .platform import linux_x11
    polite.guard("type_text")
    setres = linux_x11.clipboard_set(text=text)
    if not setres.get("ok"):
        return setres
    return press_key("ctrl+v")


def _polite_type(text: str, delay_ms: int, dwell_ms: int) -> dict:
    ch = _xt()
    if ch is None:
        return {"ok": False, "error": "owned_state_unavailable", "typed_len": 0,
                "total_len": len(text), "remaining": text}
    typed = 0
    saved = None
    browser = ch is not None and active_window_is_browser()
    pasted = False
    polite.progress(typed_len=0, total_len=len(text), remaining=text)
    try:
        for char in text:
            polite.guard("type_text")
            _stamp()
            via_paste = browser and ch.needs_remap(keysym_for(char))
            polite.progress(typed_len=0, pressed=False)
            try:
                if via_paste:
                    if not pasted:
                        saved = _clipboard_snapshot()
                    pasted = True
                    r = _paste(char)
                    if not r.get("ok"):
                        return dict(r, typed_len=typed, total_len=len(text), remaining=text[typed:])
                elif ch is not None:
                    ch.type_text(char, delay_ms=0, dwell_ms=dwell_ms)
                else:
                    _require("xdotool")
                    r = _run(["xdotool", "type", "--delay", "0", "--file", "-"], input_text=char)
                    if r.returncode:
                        polite.progress(typed_len=typed, total_len=len(text), remaining=text[typed:])
                        return {"ok": False, "error": "typing command failed", "typed_len": typed,
                                "total_len": len(text), "remaining": text[typed:]}
            except polite.Interrupted as exc:
                # XTEST key-down already inserts the character, even if dwell is interrupted.
                sent = bool(exc.result.get("pressed")) if via_paste else bool(exc.result.get("typed_len"))
                typed += int(sent)
                polite.progress(typed_len=typed, total_len=len(text), remaining=text[typed:])
                raise
            except Exception as exc:
                # Preserve the acknowledged prefix if a later character/backend fails.
                sent = bool(polite.current().progress.get("pressed")) if via_paste else bool(
                    polite.current().progress.get("typed_len"))
                typed += int(sent)
                polite.progress(typed_len=typed, total_len=len(text), remaining=text[typed:])
                return {"ok": False, "error": "backend_error", "detail": f"{type(exc).__name__}: {exc}",
                        "typed_len": typed, "total_len": len(text), "remaining": text[typed:]}
            typed += 1
            polite.progress(typed_len=typed, total_len=len(text), remaining=text[typed:])
            if delay_ms > 0:
                polite.sleep(delay_ms / 1000.0)
        return {"ok": True, "typed_len": typed}
    finally:
        _clipboard_restore(saved)


def type_text(text: str, delay_ms: int = 40, dwell_ms: int = 0) -> dict:
    if polite.current() is not None:
        return _polite_type(text, delay_ms, dwell_ms)
    _stamp()
    ch = _xt()
    if ch is not None:
        saved = None
        try:
            pasted: list[str] = []
            if any(ch.needs_remap(keysym_for(c)) for c in text) and active_window_is_browser():
                # A scratch-keycode remap gives a page an odd or empty
                # event.code, which is a tell. Type what is on the keyboard,
                # paste what is not.
                run = ""
                for c in text:
                    if ch.needs_remap(keysym_for(c)):
                        if run:
                            ch.type_text(run, delay_ms=delay_ms, dwell_ms=dwell_ms)
                            run = ""
                        if saved is None:
                            saved = _clipboard_snapshot()
                        r = _paste(c)
                        if not r.get("ok"):
                            return {"ok": False, "error": f"paste failed: {r.get('error')}",
                                    "typed_len": len(pasted), "via": "xtest+paste"}
                        pasted.append(c)
                    else:
                        run += c
                if run:
                    ch.type_text(run, delay_ms=delay_ms, dwell_ms=dwell_ms)
                return {"ok": True, "typed_len": len(text), "via": "xtest+paste",
                        "pasted": pasted}
            n = ch.type_text(text, delay_ms=delay_ms, dwell_ms=dwell_ms)
        except Exception as exc:
            return {"ok": False, "error": f"typing failed: {exc}", "via": "xtest"}
        else:
            return {"ok": True, "typed_len": n, "via": "xtest"}
        finally:
            _clipboard_restore(saved)
    _require("xdotool")
    result = _run(["xdotool", "type", "--delay", str(delay_ms), "--file", "-"], input_text=text)
    if result.returncode:
        return {"ok": False, "error": "typing command failed"}
    return {"ok": True, "typed_len": len(text)}


def press_key(key: str, hold_ms: int = 0) -> dict:
    """xdotool key syntax: 'Return', 'Tab', 'ctrl+c', 'super+l', 'KP_0'.

    hold_ms is how long the base key stays down on the XTest path (Human
    Mode dwell). The xdotool fallback has no hold and ignores it.
    """
    polite.progress(pressed=False)
    _stamp(escape=_is_escape(key))
    ch = _xt()
    if ch is not None:
        try:
            ch.press_combo(key, hold_ms=hold_ms)
        except polite.Interrupted:
            raise
        except Exception as exc:
            return {"ok": False, "error": f"key failed: {exc}", "key": key, "via": "xtest"}
        return {"ok": True, "key": key, "via": "xtest"}
    if polite.current() is not None:
        return {"ok": False, "error": "owned_state_unavailable", "key": key}
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
    ch = _xt()
    if ch is not None:
        p = ch.pointer_position()
        return {"ok": True, "x": p["x"], "y": p["y"], "screen": "0",
                "window": str(p["window"] or 0), "buttons": p["buttons"], "via": "xtest"}
    _require("xdotool")
    result = _run(["xdotool", "getmouselocation", "--shell"])
    vals = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode or "X" not in vals or "Y" not in vals:
        return {"ok": False, "error": "pointer_unreadable"}
    return {"ok": True, "x": int(vals.get("X", 0)), "y": int(vals.get("Y", 0)),
            "screen": vals.get("SCREEN"), "window": vals.get("WINDOW")}


def mouse_down(button: int = 1, x: int | None = None, y: int | None = None) -> dict:
    _stamp()
    if polite.current() is not None:
        if x is not None and y is not None:
            result = move(x, y)
            if not result.get("ok"):
                return result
        ch = _xt()
        if ch is None:
            return {"ok": False, "error": "owned_state_unavailable",
                    "detail": "polite button holds need the persistent XTEST channel"}
        if ch.button(button, True) is False:
            return {"ok": False, "error": "already_held", "button": button}
        return {"ok": True, "held": button}
    if x is not None and y is not None:
        _placed(x, y)
    ch = _xt()
    if ch is not None:
        if x is not None and y is not None:
            emit_cursor("move", x, y)
            ch.move(x, y)
        ch.button(button, True)
        return {"ok": True, "held": button, "at": [x, y] if x is not None else None, "via": "xtest"}
    _require("xdotool")
    cmd = ["xdotool"]
    if x is not None and y is not None:
        cmd += ["mousemove", str(x), str(y)]
        emit_cursor("move", x, y)
    cmd += ["mousedown", str(button)]
    r = _run(cmd)
    if r.returncode == 0:
        _INPUT_HELD["buttons"].add(button)
    return {"ok": True, "held": button, "at": [x, y] if x is not None else None}


def mouse_up(button: int = 1, x: int | None = None, y: int | None = None) -> dict:
    if polite.current() is not None:
        # Releasing is allowed during an interruption; coordinate motion is not.
        x = y = None
    ch = _xt()
    if ch is not None:
        if x is not None and y is not None:
            ch.move(x, y)
        changed = ch.button(button, False)
        return {"ok": True, "released": button if changed is not False else None, "via": "xtest"}
    if polite.current() is not None and button not in _INPUT_HELD["buttons"]:
        return {"ok": True, "released": None}
    _require("xdotool")
    cmd = ["xdotool"]
    if x is not None and y is not None:
        cmd += ["mousemove", str(x), str(y)]
    cmd += ["mouseup", str(button)]
    result = _run(cmd)
    if result.returncode:
        return {"ok": False, "error": "release_failed", "button": button}
    _INPUT_HELD["buttons"].discard(button)
    if x is not None and y is not None:
        emit_cursor("click", x, y)
    return {"ok": True, "released": button}


def key_down(key: str) -> dict:
    _stamp(escape=_is_escape(key))
    ch = _xt()
    if ch is not None:
        try:
            changed = ch.key(key, True)
        except polite.Interrupted:
            raise
        except Exception as exc:
            return {"ok": False, "error": f"key_down failed: {exc}", "key": key, "via": "xtest"}
        if polite.current() is not None and changed is False:
            return {"ok": False, "error": "already_held", "key": key}
        return {"ok": True, "held": key, "via": "xtest"}
    if polite.current() is not None:
        return {"ok": False, "error": "owned_state_unavailable",
                "detail": "polite key holds need the persistent XTEST channel"}
    _require("xdotool")
    r = _run(["xdotool", "keydown", key])
    if r.returncode == 0:
        _INPUT_HELD["keys"].add(key)
    return {"ok": True, "held": key}


def key_up(key: str) -> dict:
    ch = _xt()
    if ch is not None:
        try:
            changed = ch.key(key, False)
        except Exception as exc:
            return {"ok": False, "error": f"key_up failed: {exc}", "key": key, "via": "xtest"}
        return {"ok": True, "released": key if changed is not False else None, "via": "xtest"}
    if polite.current() is not None and key not in _INPUT_HELD["keys"]:
        return {"ok": True, "released": None}
    _require("xdotool")
    r = _run(["xdotool", "keyup", key])
    if r.returncode:
        return {"ok": False, "error": "release_failed", "key": key}
    _INPUT_HELD["keys"].discard(key)
    return {"ok": True, "released": key}


def click_with(x: int, y: int, button: int = 1, count: int = 1,
               modifiers: list[str] | None = None) -> dict:
    """Click while holding modifiers, e.g. shift+click to extend a selection.

    The modifiers are released even if the click fails, so a crash mid-sequence
    cannot leave ctrl stuck down for the human using the machine afterwards.
    """
    mods = [m.strip().lower() for m in (modifiers or []) if m.strip()]
    unknown = [m for m in mods if m not in _MODIFIERS]
    if unknown:
        return {"ok": False, "error": f"unknown modifier(s) {unknown}",
                "supported": sorted(_MODIFIERS)}
    if polite.current() is not None:
        held = []
        try:
            for mod in dict.fromkeys(mods):
                r = key_down(mod)
                if not r.get("ok"):
                    return r
                held.append(mod)
            return click(x, y, button=button, count=count)
        finally:
            for mod in reversed(held):
                key_up(mod)
    ch = _xt()
    if ch is not None:
        held = []
        try:
            for mod in mods:
                ch.key(mod, True)
                held.append(mod)
            return click(x, y, button=button, count=count)
        finally:
            for mod in reversed(held):
                try:
                    ch.key(mod, False)
                except Exception:
                    pass
    _require("xdotool")
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
    if polite.current() is not None:
        moved = move(x, y)
        if not moved.get("ok"):
            return moved
        return scroll(direction, amount) | {"at": [x, y]}
    emit_cursor("move", x, y)
    _stamp()
    _placed(x, y)
    ch = _xt()
    if ch is not None:
        ch.click(x, y, button=button, count=amount, gap_ms=0)
        return {"ok": True, "at": [x, y], "scrolled": direction, "amount": amount, "via": "xtest"}
    _require("xdotool")
    _run(["xdotool", "mousemove", str(x), str(y), "click", "--repeat", str(amount), str(button)])
    return {"ok": True, "at": [x, y], "scrolled": direction, "amount": amount}


def hover(x: int, y: int, settle_ms: int = 350) -> dict:
    """Move the pointer and wait for hover-triggered UI (tooltips, submenus) to
    appear before the caller screenshots."""
    moved = move(x, y)
    if not moved.get("ok"):
        return moved
    polite.sleep(max(0, settle_ms) / 1000.0)
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
    ch = _xt()
    if ch is not None:
        try:
            info = ch.active_window_info()
        except Exception:
            info = {"ok": False}
        if info.get("ok"):
            # Geometry still needs one xdotool call; identity does not, and
            # identity is what callers ask for most (focus checks, browser
            # detection, the bot-challenge check on every human click).
            return {"ok": True, "id": str(info["id"]), "name": info["name"],
                    "pid": str(info.get("pid") or ""), "class": info.get("class"),
                    "via": "xtest"}
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


def input_focus() -> dict:
    ch = _xt()
    return ch.top_level_focus() if ch is not None else {"ok": False, "error": "focus_unavailable"}


def focused_window_identity() -> dict:
    ch = _xt()
    return ch.focused_window_identity() if ch is not None else {"ok": False, "error": "focus_unavailable"}


def restore_input_focus(focus: dict) -> dict:
    ch = _xt()
    return ch.restore_input_focus(focus) if ch is not None else {"ok": False, "error": "focus_restore_unavailable"}


def foreign_holds() -> dict:
    ch = _xt()
    return ch.foreign_holds() if ch is not None else {}


def polite_input_ready() -> bool:
    return _xt() is not None
