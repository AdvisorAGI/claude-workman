#!/usr/bin/env python3
"""wm.py: one Workman command line with the same verbs on every fleet machine (2026-09-12).

Per-device interpreter. Run it with any python: when the backend is not importable the
file re-execs itself with the first venv python below that exists (override: WM_PY=<python>;
WM_NO_REEXEC=1 disables the re-exec, the remote launcher sets it).
  dgx       ~/workman/.venv/bin/python                                  workman.server, linux_x11, DISPLAY :0
  air       ~/claude-atmos-workman/.venv/bin/python                     workman.server, darwin backend
  mac-mini  ~/workman/.venv/bin/python                                  workman.server, darwin
  mac-mini  ~/src/claude-atmos-workman/computer-mcp/.venv/bin/python    atmos_computer.server: every verb forwards to the
            Workman.app daemon (ai.atmosphere.workman), which holds the Screen Recording + Accessibility grants itself

Usage: wm.py [--host <device>] <verb> [args]
  shot <path> [full]          screen PNG to <path> (downscaled view by default, 'full' = native), prints meta + blank check
  region <path> x y w h       native-resolution crop of the screen (screen points)
  zoom <path> x1 y1 x2 y2     region re-grabbed at full resolution and magnified (screen points)
  windows                     list windows
  active                      active window
  focus <query>               focus a window by title / app / class
  click x y [--button 1|2|3] [--count N]
                              click at a screen point (human path when Human Mode is on, which is the default);
                              button 1=left 2=middle 3=right (default 1), count=2 double/3 triple (default 1)
  hover x y                   move the pointer to a screen point without clicking (no click posted)
  move x y                    move through the same owner gate
  scroll up|down|left|right [amount]
  drag from_x from_y to_x to_y
  burst '<JSON actions>'      one focus/click/type sequence; owner restoration is deferred
  type <text>                 type literal text into the focused window
  paste <text>                clipboard paste behind the owner gate (Command-V on macOS, Ctrl-V on X11)
  key <combo>                 press a key; xdotool syntax everywhere (ctrl+l, super+v, Return); cmd/command is mapped
  elements [app] [filter]     actionable accessibility elements (needs the Accessibility grant on macOS)
  human on|off|status         persistent Human Mode for later runs (state file ~/.claude/tools/.wm-human); WM_HUMAN=0 per run
  platform                    backend, display, permission hints, owner-pause state
  pause [status|resume|pause|release] [confirmed]
                              the owner-pause switch (physical Escape); resume needs the word 'confirmed'
  resume                      finish the last interrupted action from where it stopped
  handoff                     release every held key/button, close what this tool launched, park the pointer, verify
  ping                        liveness (host, pid, backend)
  jbl [cmd]                   JBL speaker (DGX): status|play|pause|next|prev|vol N|up|down|mute|unmute|connect|on|off|release|bt on/off|awake|app ... (`jbl help`)
  ac [cmd]                    office Gree AC (DGX): status|on|off|temp N|mode M|fan F|quiet on/off|cool-quiet [N]|lock N/off/status|log (`ac help`)
  serve                       JSON-lines server on stdin/stdout, one request per line: {"verb": "...", "args": [...]}
                              (what workman.remote runs on the far end; shot/region/zoom with path '-' return image_b64)
  status | stop               with --host: the channel's transport + RTT, or stop the relay
Remote: `--host mac-mini` (or WM_HOST=mac-mini) runs the verb on that machine over one persistent ssh channel
(workman.remote; path picked by ~/.ssh/fleet-connect.sh: cable, LAN, Headscale, Cloudflare). The remote executes
this same file, so nothing has to be installed over there beyond a Python and its backend venv.
Legacy aliases kept from the DGX original: tree [app] [filter], click_el <name> [role] [app], focused.
Polite X11 mode locks the intended top-level on focus/click. Keyboard calls refuse
focus_changed without input if focus differs; screenshot to verify the target and result.
Separate calls may continue inside the inactivity window. New actions reset restoration:
WORKMAN_POLITE_RESTORE_AFTER_S defaults to 5 seconds, followed by owner idle. handoff
requests restoration immediately. A focus/pointer readback mismatch reports restore_failed.
A blank capture on macOS means the Workman daemon is down or Workman.app lacks Screen Recording:
run `wm.py platform` (shows daemon + grants) or `python -m atmos_computer.install --status`.
"""
import base64
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

HOME = os.path.expanduser("~")
STATE = os.path.join(HOME, ".claude", "tools", ".wm-human")
VENVS = [
    os.environ.get("WM_PY"),
    os.path.join(HOME, "workman", ".venv", "bin", "python"),
    os.path.join(HOME, "claude-atmos-workman", ".venv", "bin", "python"),
    os.path.join(HOME, "src", "claude-atmos-workman", "computer-mcp", ".venv", "bin", "python"),
]
IS_MAC = sys.platform == "darwin"
if sys.platform.startswith("linux"):
    os.environ.setdefault("DISPLAY", ":0")
    os.environ.setdefault("XAUTHORITY", "/run/user/1000/gdm/Xauthority")

KIND = None
S = None


# ---- argv: --host / WM_HOST routing ---------------------------------------------------------
def _pop_host(argv):
    host = os.environ.get("WM_HOST") or None
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--host" and i + 1 < len(argv):
            host, i = argv[i + 1], i + 2
            continue
        if a.startswith("--host="):
            host, i = a[len("--host="):], i + 1
            continue
        rest.append(a)
        i += 1
    return (host or "").strip() or None, rest


def _parse_click_args(a):
    """click's argv after the verb: x y [--button 1|2|3] [--count N] in either order."""
    button, count, pos = 1, 1, []
    i = 0
    while i < len(a):
        tok = a[i]
        if tok == "--button" and i + 1 < len(a):
            button, i = int(a[i + 1]), i + 2
            continue
        if tok == "--count" and i + 1 < len(a):
            count, i = int(a[i + 1]), i + 2
            continue
        pos.append(tok)
        i += 1
    return pos, button, count


def _remote_module():
    try:
        from workman import remote  # noqa: F401
        return remote
    except ImportError:
        root = os.path.join(HOME, "workman")
        sys.path.insert(0, root)
        # The relay child (python -m workman.remote) inherits env, not sys.path.
        os.environ["PYTHONPATH"] = os.pathsep.join(p for p in (root, os.environ.get("PYTHONPATH")) if p)
        from workman import remote
        return remote


# ---- backend (imported lazily so --host and serve can decide first) -------------------------
def _import_backend():
    try:
        import workman.server as s  # noqa: F401
        return "workman", s
    except ImportError:
        pass
    try:
        import atmos_computer.server as s  # noqa: F401
        return "atmos", s
    except ImportError:
        return None, None


def _human_wanted():
    """Human Mode is the default everywhere. WM_HUMAN=0 turns it off for one
    run; `wm.py human off` turns it off for later runs on this machine."""
    env = os.environ.get("WM_HUMAN")
    if env is not None:
        return env.strip() not in ("0", "false", "no", "off")
    try:
        with open(STATE) as fh:
            return fh.read().strip() != "0"
    except OSError:
        return True


def _set_human(on):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as fh:
        fh.write("1\n" if on else "0\n")


HUMAN = _human_wanted()


def _backend():
    """Import the backend, re-exec into a venv that has one when needed."""
    global KIND, S
    if KIND is not None:
        return KIND
    KIND, S = _import_backend()
    if KIND is None:
        if os.environ.get("WM_NO_REEXEC") == "1" or not os.path.isabs(__file__) \
                or not os.path.exists(__file__):
            sys.exit("wm.py: no Workman backend importable (workman.server / atmos_computer.server); set WM_PY")
        # Compare literal paths, never realpaths: a venv python is a symlink to the base
        # interpreter (DGX: ~/workman/.venv/bin/python -> /usr/bin/python3.12), and the venv
        # only takes effect when launched through its own path.
        tried = [p for p in os.environ.get("WM_TRIED", "").split(":") if p]
        me = os.path.abspath(sys.executable)
        for py in VENVS:
            if py and os.path.exists(py) and os.path.abspath(py) not in tried + [me]:
                os.environ["WM_TRIED"] = ":".join(tried + [me])
                os.execv(py, [py, os.path.abspath(__file__)] + sys.argv[1:])
        sys.exit("wm.py: no Workman backend importable (workman.server / atmos_computer.server); set WM_PY")
    if KIND == "workman":
        S.human.apply_session_default()
        if not HUMAN:
            S.workman_set_human_mode(False)
        elif os.environ.get("WM_SEED"):
            S.workman_set_human_mode(True, seed=int(os.environ["WM_SEED"]))
    return KIND


def out(obj):
    """Print the result; exit status 1 when the backend reported an error dict."""
    print(json.dumps(obj, default=str, indent=1)[:20000])
    return 1 if isinstance(obj, dict) and (obj.get("ok") is False or "error" in obj) else 0


# ---- images ----------------------------------------------------------------------------------
def _write(path, data):
    with open(path, "wb") as fh:
        fh.write(data)


def _fmt(data):
    return "png" if data[:4] == b"\x89PNG" else "jpeg" if data[:2] == b"\xff\xd8" else "bin"


def _save_image(path, data):
    """Save image bytes; convert to PNG when the path says .png and the bytes are JPEG.
    path '-' returns the bytes inline (base64) instead: the serve/remote path."""
    fmt = _fmt(data)
    if path == "-":
        return {"image_b64": base64.b64encode(data).decode("ascii"), "format": fmt,
                "bytes": len(data), "blank_check": _blank_check(data)}
    if path.lower().endswith(".png") and fmt == "jpeg":
        try:
            from PIL import Image as PILImage
            buf = io.BytesIO()
            PILImage.open(io.BytesIO(data)).save(buf, format="PNG")
            data, fmt = buf.getvalue(), "png"
        except ImportError:
            if IS_MAC:
                tmp = path + ".jpg"
                _write(tmp, data)
                subprocess.run(["sips", "-s", "format", "png", tmp, "--out", path],
                               capture_output=True, timeout=30)
                os.unlink(tmp)
                return {"saved": path, "format": "png", "converted_by": "sips"}
    _write(path, data)
    return {"saved": path, "format": fmt, "bytes": len(data)}


def _blank_check(src):
    """Uniform image = nothing captured (macOS Screen Recording not granted to the launcher).
    `src` is a path or the image bytes."""
    try:
        from PIL import Image as PILImage
        im = PILImage.open(io.BytesIO(src) if isinstance(src, (bytes, bytearray)) else src).convert("L")
        lo, hi = im.getextrema()
        res = {"blank": lo == hi, "size": list(im.size), "extrema": [lo, hi]}
    except ImportError:
        n = len(src) if isinstance(src, (bytes, bytearray)) else os.path.getsize(src)
        res = {"blank": n < 25000, "bytes": n, "method": "size heuristic (no PIL)"}
    if res["blank"]:
        res["hint"] = ("capture is blank: on macOS grant Screen Recording to the launching process "
                       "(System Settings > Privacy & Security > Screen Recording); founder toggle")
    return res


# ---- key syntax: xdotool everywhere; atmos_computer wants cliclick names ------------------
_ATMOS_NAMED = {"return": "return", "kp_enter": "enter", "enter": "enter", "escape": "esc", "esc": "esc",
                "tab": "tab", "backspace": "delete", "delete": "fwd-delete", "space": "space",
                "up": "arrow-up", "down": "arrow-down", "left": "arrow-left", "right": "arrow-right",
                "home": "home", "end": "end", "prior": "page-up", "next": "page-down",
                "page_up": "page-up", "page_down": "page-down"}


def _key_for_backend(combo):
    parts = [p for p in combo.replace(" ", "").split("+") if p]
    if not parts:
        return combo
    mods, main = parts[:-1], parts[-1]
    if KIND == "workman":  # xdotool syntax; only alias cmd/command -> super
        mods = ["super" if m.lower() in ("cmd", "command") else m for m in mods]
        return "+".join(mods + [main])
    mods = ["cmd" if m.lower() in ("super", "cmd", "command") else m.lower() for m in mods]
    main = _ATMOS_NAMED.get(main.lower(), main.lower()) if len(main) > 1 else main
    return "+".join(mods + [main])


# ---- macOS helpers for the atmos_computer backend -------------------------------------------
def _cg_windows():
    import Quartz
    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    rows = []
    for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
        if int(w.get("kCGWindowLayer", 0)) != 0:
            continue
        b = w.get("kCGWindowBounds", {}) or {}
        rows.append({"id": int(w.get("kCGWindowNumber", 0)), "pid": int(w.get("kCGWindowOwnerPID", 0)),
                     "app": str(w.get("kCGWindowOwnerName", "")), "title": str(w.get("kCGWindowName", "")),
                     "x": int(b.get("X", 0)), "y": int(b.get("Y", 0)),
                     "w": int(b.get("Width", 0)), "h": int(b.get("Height", 0))})
    return rows


def _frontmost():
    from AppKit import NSWorkspace
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return {"app": str(app.localizedName()), "pid": int(app.processIdentifier()),
            "bundle": str(app.bundleIdentifier() or "")}


def _pid_for_app(name):
    from AppKit import NSWorkspace
    q = name.lower()
    for a in NSWorkspace.sharedWorkspace().runningApplications():
        if q in str(a.localizedName() or "").lower() or q in str(a.bundleIdentifier() or "").lower():
            return int(a.processIdentifier()), str(a.localizedName())
    return None, None


def _mac_focus(query):
    from AppKit import NSRunningApplication, NSApplicationActivateIgnoringOtherApps
    q = query.lower()
    hit = next((w for w in _cg_windows() if q in w["app"].lower() or q in w["title"].lower()), None)
    pid, app = (hit["pid"], hit["app"]) if hit else _pid_for_app(query)
    if pid is None:
        return {"ok": False, "error": f"no window or app matches {query!r}"}
    ra = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    ok = bool(ra and ra.activateWithOptions_(NSApplicationActivateIgnoringOtherApps))
    if hit and hit["title"]:
        t = hit["title"].replace('"', '\\"')
        script = (f'tell application "System Events" to tell process "{app}" to perform action "AXRaise" '
                  f'of (first window whose name contains "{t}")')
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    return {"ok": ok, "focused": hit or {"app": app, "pid": pid}}


_AX_ROLES = {"AXButton", "AXTextField", "AXTextArea", "AXCheckBox", "AXRadioButton", "AXPopUpButton",
             "AXMenuButton", "AXLink", "AXComboBox", "AXTab", "AXMenuItem", "AXSlider", "AXIncrementor",
             "AXDisclosureTriangle", "AXToolbar", "AXSearchField"}


def _mac_elements(app=None, limit=200):
    import ApplicationServices as AX
    if app:
        pid, name = _pid_for_app(app)
        if pid is None:
            return {"ok": False, "error": f"no running app matches {app!r}"}
    else:
        fm = _frontmost()
        pid, name = fm["pid"], fm["app"]
    root = AX.AXUIElementCreateApplication(pid)
    err, wins = AX.AXUIElementCopyAttributeValue(root, "AXWindows", None)
    if err != 0:
        return {"ok": False, "error": f"AX error {err} (-25211 = Accessibility not granted to the launching "
                                      "process: System Settings > Privacy & Security > Accessibility)"}

    def attr(el, name):
        e, v = AX.AXUIElementCopyAttributeValue(el, name, None)
        return v if e == 0 else None

    def geom(el):
        p, s = attr(el, "AXPosition"), attr(el, "AXSize")
        try:
            _, pt = AX.AXValueGetValue(p, 1, None)
            _, sz = AX.AXValueGetValue(s, 2, None)
            return [int(pt.x), int(pt.y), int(sz.width), int(sz.height)]
        except Exception:
            return None

    found, seen = [], 0
    stack = [(w, 0) for w in (wins or [])]
    while stack and len(found) < limit and seen < 4000:
        el, depth = stack.pop()
        seen += 1
        role = attr(el, "AXRole") or ""
        if role in _AX_ROLES:
            found.append({"role": role, "name": attr(el, "AXTitle") or attr(el, "AXDescription")
                          or attr(el, "AXValue") or "", "geom": geom(el)})
        if depth < 14:
            kids = attr(el, "AXChildren") or []
            stack.extend((k, depth + 1) for k in reversed(list(kids)))
    return {"ok": True, "app": name, "pid": pid, "count": len(found), "elements": found}


def _atmos_image(res):
    """atmos_computer image tools return [Image, meta] or [error]. -> (meta, bytes|None)."""
    if not res:
        return {"ok": False, "error": "empty result"}, None
    if isinstance(res[0], dict):
        return res[0], None
    meta = dict(res[1]) if len(res) > 1 else {}
    return meta, res[0].data


# ---- verbs ----------------------------------------------------------------------------------
def v_shot(path, full=False):
    if KIND == "workman":
        meta, img = S.screenshot(full_resolution=full)
        meta = dict(meta)
        meta.update(_save_image(path, img.data))
    else:
        meta, data = _atmos_image(S.computer_screenshot(full=full))
        if data is None:
            return meta
        meta.pop("coordinate_contract", None)
        meta.update(_save_image(path, data))
    if path != "-":
        meta["blank_check"] = _blank_check(path)
    return meta


def v_region(path, x, y, w, h):
    if KIND == "workman":
        return _save_image(path, S.screenshot_region(x, y, w, h).data)
    meta, data = _atmos_image(S.computer_region(x=x, y=y, w=w, h=h))
    if data is None:
        return meta
    meta.update(_save_image(path, data))
    return meta


def v_zoom(path, x1, y1, x2, y2):
    if KIND == "workman":
        res = S.zoom(x1, y1, x2, y2, space="screen")
        if len(res) < 2:
            return res[0]
        meta = dict(res[0])
        meta.update(_save_image(path, res[1].data))
        return meta
    meta, data = _atmos_image(S.computer_zoom(x1=x1, y1=y1, x2=x2, y2=y2))
    if data is None:
        return meta
    meta["ok"] = True
    meta.update(_save_image(path, data))
    return meta


def v_windows():
    return S.list_windows() if KIND == "workman" else S.computer_windows()


def v_active():
    if KIND == "workman":
        return S.active_window()
    return S.computer_frontmost()


def v_focus(query):
    return S.focus_window(query) if KIND == "workman" else S.computer_focus(query=query)


def v_click(x, y, button=1, count=1):
    if KIND == "workman":
        return S.click(x, y, button=button, count=count)
    # atmos_computer.computer_click takes button as a string with no separate
    # count: 1/3 -> left/right, count>=2 on left folds into its 'double'.
    b = {2: "left", 3: "right"}.get(button, "left")
    if count >= 2 and b == "left":
        b = "double"
    return S.computer_click(x=x, y=y, button=b)


def v_hover(x, y):
    if KIND == "workman":
        return S.hover(x, y)
    return S.computer_move(x=x, y=y)


def v_type(text):
    return S.type_text(text, delay_ms=20) if KIND == "workman" else S.computer_type(text=text)


def v_paste(text):
    if KIND != "workman":
        return S.computer_paste(text=text)
    from workman import desktop, owner_pause, polite

    def paste():
        module = desktop.active()
        # macOS and polite-off routes have no Action. Check the keyboard gate
        # before touching the clipboard, even on a private X11 display.
        if polite.current() is None:
            if owner_pause.blocked("press_key"):
                return owner_pause.refusal("paste")
            if polite.presence_enabled(module) and owner_pause.human_active():
                busy = owner_pause.wait_for_human_quiet()
                if owner_pause.blocked("press_key"):
                    return owner_pause.refusal("paste")
                if busy:
                    return owner_pause.refusal("paste", error="human_active")
        polite.guard("paste")
        platform = getattr(module, "PLATFORM", "")
        if platform == "macOS":
            try:
                copied = subprocess.run(["pbcopy"], input=text, text=True,
                                        capture_output=True, timeout=5)
            except (OSError, subprocess.SubprocessError) as exc:
                return {"ok": False, "error": "clipboard_failed", "via": "pbcopy",
                        "detail": type(exc).__name__}
            if copied.returncode:
                return {"ok": False, "error": "clipboard_failed", "via": "pbcopy"}
            key = "super+v"
        elif platform == "Linux/X11":
            # xclip forks its selection owner. Detach output pipes so that
            # owner can outlive this CLI, without keeping the relay open.
            copied = None
            try:
                payload = text.encode("utf-8")
                copied = subprocess.Popen(
                    ["xclip", "-display", module.display_name(), "-selection", "clipboard", "-in"],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
                copied.communicate(input=payload, timeout=5)
            except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
                if copied is not None:
                    try:
                        if copied.poll() is None:
                            copied.kill()
                        copied.communicate(timeout=1)
                    except (OSError, subprocess.SubprocessError):
                        pass
                return {"ok": False, "error": "clipboard_failed", "via": "xclip",
                        "detail": type(exc).__name__}
            if copied.returncode:
                return {"ok": False, "error": "clipboard_failed", "via": "xclip"}
            key = "ctrl+v"
        else:
            copied = desktop.clipboard_set(text=text)
            if not copied.get("ok"):
                return copied
            key = "ctrl+v"
        # The desktop route rechecks the intended focus immediately before
        # key injection. Never inject through a raw key helper.
        return desktop.press_key(key)

    return polite.run("paste", paste)


def v_key(combo):
    k = _key_for_backend(combo)
    return S.press_key(k) if KIND == "workman" else S.computer_key(keys=k)


def v_elements(app=None, flt=None):
    if KIND == "workman":
        els = S.accessibility_tree(app=app)
        if flt:
            q = flt.lower()
            els = [e for e in els if q in json.dumps(e).lower()]
        return els[:200]
    return S.computer_elements(app=app, filter=flt)


def v_human(arg):
    a = (arg or "status").lower()
    if a in ("on", "off"):
        _set_human(a == "on")
        if KIND == "workman":
            S.workman_set_human_mode(a == "on")
    st = {"human": _human_wanted(), "state_file": STATE, "backend": KIND}
    if KIND == "workman":
        st["backend_state"] = S.workman_get_human_mode()
    else:
        # The daemon's persisted Human Mode is the one that counts on the Mac.
        st["backend_state"] = S.computer_human_mode(on=(a == "on") if a in ("on", "off") else None)
        st["human"] = st["backend_state"].get("human", st["human"])
    return st


def v_platform():
    if KIND == "workman":
        p = S.workman_platform()
        p["cli_backend"] = "workman.server"
        return p
    st = S.computer_status()
    perms = st.get("live_permissions") or st.get("this_process_permissions") or {}
    return {"cli_backend": "atmos_computer.server", "os": "macos", "display": S.computer_display_info(),
            "daemon_reachable": st.get("daemon_reachable"), "daemon": st.get("daemon"),
            "permissions": perms}


_PREDATES_PAUSE_FIX = (
    "daemon predates pause, update and restart: "
    "land feat/mac-daemon-parity in the host computer-mcp checkout, then "
    "launchctl kickstart -k gui/$(id -u)/ai.atmosphere.workman  (owner action)"
)


def _atmos_predates(missing=None, detail=None):
    """Clean error when the installed atmos_computer.server (or its daemon)
    predates the pause/hand_back wrappers. Never an AttributeError traceback."""
    out = {"ok": False, "error": "daemon_predates_pause", "fix": _PREDATES_PAUSE_FIX}
    if missing:
        out["missing"] = missing
    if detail is not None:
        out["detail"] = str(detail)
    return out


def _atmos_call(name, **kw):
    """Call an atmos_computer.server wrapper by presence, not by catching
    AttributeError. A missing attribute is a stale install; an AttributeError
    raised *inside* a live wrapper is a real bug and must propagate."""
    fn = getattr(S, name, None)
    if not callable(fn):
        return _atmos_predates(missing=name)
    try:
        return fn(**kw)
    except Exception as exc:
        if "unknown op" in str(exc) or type(exc).__name__ == "KeyError":
            return _atmos_predates(missing=name, detail=exc)
        raise


def v_pause(args):
    action = (args[0] if args else "status").lower()
    confirmed = any(a.lower() in ("confirmed", "owner_confirmed", "yes") for a in args[1:])
    if KIND == "workman":
        return S.input_control(action=action, owner_confirmed=confirmed)
    return _atmos_call("computer_input_control", action=action, confirmed=confirmed)


def v_resume():
    if KIND == "workman":
        return S.resume_interrupted()
    # No interrupted-action store on the daemon. Refuse while paused (ask the
    # owner, then `pause resume confirmed`); otherwise there is nothing to replay.
    st = _atmos_call("computer_input_control", action="status")
    if isinstance(st, dict) and st.get("error") == "daemon_predates_pause":
        return st
    switches = (st or {}).get("input_switches") or {}
    if switches.get("mouse") is False or switches.get("keyboard") is False:
        out = {"ok": False, "error": "input_disabled",
               "instruction": "Ask the owner, then: wm.py pause resume confirmed"}
        if isinstance(st, dict):
            out.update({k: st[k] for k in st if k not in out})
        return out
    out = {"ok": True, "resumed": False,
           "note": "the daemon does not replay cut-short actions; input is already enabled"}
    if isinstance(st, dict):
        out.update({k: st[k] for k in st if k not in out})
    return out


def v_handoff():
    if KIND == "workman":
        return S.hand_back()
    return _atmos_call("computer_hand_back")


def v_ping():
    return {"ok": True, "host": socket.gethostname().split(".")[0], "pid": os.getpid(),
            "backend": KIND, "human": HUMAN, "platform": sys.platform, "t": time.time()}


def run(cmd, a):
    """Dispatch one verb; returns the result object (raises on bad arguments)."""
    if cmd == "ping":
        return v_ping()
    if cmd == "jbl":  # JBL speaker via the Mi TV + DGX Bluetooth (2026-10-07); driver ~/bin/jbl lives on the DGX
        import subprocess
        drv = os.path.expanduser("~/bin/jbl")
        if not os.path.exists(drv):
            return {"ok": False, "verb": "jbl", "error": "no ~/bin/jbl on this host; run it with --host dgx"}
        r = subprocess.run([drv, *(a or ["status"])], capture_output=True, text=True, timeout=300)
        return {"ok": r.returncode == 0, "verb": "jbl", "args": a, "output": (r.stdout + r.stderr).strip()}
    if cmd == "ac":  # office Gree AC over the LAN (2026-10-07); driver ~/bin/ac and its lock live on the DGX
        import subprocess  # the jbl branch's local import makes `subprocess` a local of run(); bind it here too
        drv = os.path.expanduser("~/bin/ac")
        if socket.gethostname().split(".")[0] != "amg-dgx" or not os.path.exists(drv):
            return {"ok": False, "verb": "ac", "error": "the office AC driver ~/bin/ac is on the DGX; run it with --host dgx"}
        try:  # worst case is 5 UDP exchanges x 2 tries x 4 s inside the driver
            r = subprocess.run([drv, *(a or ["status"])], capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            return {"ok": False, "verb": "ac", "args": a, "error": "~/bin/ac timed out after 60 s"}
        return {"ok": r.returncode == 0, "verb": "ac", "args": a, "output": (r.stdout + r.stderr).strip()}
    if cmd == "shot":
        return v_shot(a[0], full=(len(a) > 1 and a[1] == "full"))
    if cmd == "region":
        return v_region(a[0], *map(int, a[1:5]))
    if cmd == "zoom":
        return v_zoom(a[0], *map(int, a[1:5]))
    if cmd == "windows":
        return v_windows()
    if cmd == "active":
        return v_active()
    if cmd == "focus":
        return v_focus(a[0])
    if cmd == "burst" and KIND == "workman":
        actions = json.loads(" ".join(a))
        if not isinstance(actions, list):
            return {"ok": False, "error": "burst expects a JSON array of actions"}
        return S.batch(actions)
    if cmd == "click":
        pos, button, count = _parse_click_args(a)
        return v_click(int(pos[0]), int(pos[1]), button=button, count=count)
    if cmd == "hover":
        return v_hover(int(a[0]), int(a[1]))
    if cmd == "move" and KIND == "workman":
        return S.move(int(a[0]), int(a[1]))
    if cmd == "scroll" and KIND == "workman":
        return S.scroll(a[0], amount=int(a[1]) if len(a) > 1 else 3)
    if cmd == "drag" and KIND == "workman":
        return S.drag(*map(int, a[:4]))
    if cmd == "type":
        return v_type(" ".join(a))
    if cmd == "paste":
        return v_paste(" ".join(a))
    if cmd == "key":
        return v_key(a[0])
    if cmd in ("elements", "tree"):
        return v_elements(a[0] if a else None, a[1] if len(a) > 1 else None)
    if cmd == "human":
        return v_human(a[0] if a else None)
    if cmd in ("platform", "status"):
        return v_platform()
    if cmd == "pause":
        return v_pause(a)
    if cmd == "resume":
        return v_resume()
    if cmd == "handoff":
        return v_handoff()
    if cmd == "click_el" and KIND == "workman":
        return S.click_element(a[0], role=(a[1] if len(a) > 1 and a[1] != "-" else None),
                               app=(a[2] if len(a) > 2 else None))
    if cmd == "focused" and KIND == "workman":
        return S.focused_element()
    return {"ok": False, "error": f"unknown verb {cmd!r}", "usage": __doc__}


# ---- serve: JSON lines on stdin/stdout ---------------------------------------------------------
def serve():
    proto = sys.stdout
    sys.stdout = sys.stderr  # backend chatter must never land on the protocol channel
    try:
        _backend()
    except SystemExit as exc:
        proto.write(json.dumps({"ready": False, "error": str(exc)}) + "\n")
        proto.flush()
        return 2
    ready = v_ping()
    ready["ready"] = True
    proto.write(json.dumps(ready, default=str) + "\n")
    proto.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        verb = str(req.get("verb", ""))
        args = [str(x) for x in (req.get("args") or [])]
        try:
            result = run(verb, args)
        except (IndexError, ValueError) as exc:
            result = {"ok": False, "verb": verb, "error": f"bad arguments ({exc})"}
        except SystemExit as exc:
            result = {"ok": False, "verb": verb, "error": f"exit {exc.code}"}
        except Exception as exc:
            result = {"ok": False, "verb": verb, "error": f"{type(exc).__name__}: {exc}"}
        proto.write(json.dumps({"result": result}, default=str) + "\n")
        proto.flush()
    return 0


# ---- remote: the same verbs on another machine ------------------------------------------------
def remote_main(host, argv):
    remote = _remote_module()
    if not argv:
        print(__doc__)
        return 2
    cmd, a = argv[0], argv[1:]
    try:
        if cmd == "status":
            return out(remote.status(host))
        if cmd == "stop":
            return out(remote.stop(host))
        if cmd in ("shot", "region", "zoom"):
            path = a[0] if a else "-"
            meta, data = remote.image(host, cmd, ["-", *a[1:]])
            if data is not None and path != "-":
                meta.update(_save_image(path, data))
                meta["blank_check"] = _blank_check(path)
            elif data is not None:
                meta["bytes"] = len(data)
            meta["host"] = host
            return out(meta)
        res = remote.call(host, cmd, a)
        if isinstance(res, dict):
            res.setdefault("host", host)
        return out(res)
    except Exception as exc:
        return out({"ok": False, "host": host, "verb": cmd, "error": f"{type(exc).__name__}: {exc}"})


def _is_local(host):
    try:
        return _remote_module().is_local(host)
    except Exception:
        return False


def main(argv):
    host, argv = _pop_host(argv)
    if not argv:
        print(__doc__)
        return 2
    if host and not _is_local(host):
        return remote_main(host, argv)
    cmd, a = argv[0], argv[1:]
    if cmd == "serve":
        return serve()
    _backend()
    try:
        return out(run(cmd, a))
    except (IndexError, ValueError) as exc:
        print(f"wm.py {cmd}: bad arguments ({exc})\n{__doc__}")
        return 2
    except Exception as exc:  # report, never trace-dump onto the orchestrator
        out({"ok": False, "verb": cmd, "error": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
