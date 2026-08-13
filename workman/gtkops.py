"""GTK/Wnck backend — true window-manager state and the clipboard.

xdotool can move and raise a window but cannot set EWMH *state* (maximize,
always-on-top, sticky, workspace placement), and `wmctrl` is not installed
everywhere. libwnck speaks EWMH properly, so window state lives here while
xdotool keeps the raw pointer/keyboard paths in `x11.py`.

Everything here runs in a SHORT-LIVED SUBPROCESS (`python -m workman.gtkops <op>`)
instead of inside the MCP server: GTK wants to own a main loop and the server
already runs asyncio. One-shot processes stop the two from fighting, at a cost of
roughly 200ms per call.

Clipboard writes survive the helper's exit only because `store()` hands the
content to the session clipboard manager (gnome-shell). With no manager running,
a written clipboard would die with the process — hence `clipboard_set` reports
`stored` so the caller can tell durable from ephemeral.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

DISPLAY = os.environ.get("WORKMAN_DISPLAY") or os.environ.get("DISPLAY") or ":0"

_MODULE = "workman.gtkops"
_TIMEOUT = 25

# How long to stay alive serving a clipboard transfer (see op_clipboard_set).
_CLIPBOARD_HANDOFF_MIN_S = 0.15
_CLIPBOARD_HANDOFF_MAX_S = 1.5


# ---- caller side (runs inside the MCP server) -------------------------------
def call(op: str, **kwargs) -> dict:
    """Run one GTK op in a fresh subprocess and return its JSON result."""
    env = dict(os.environ)
    env["DISPLAY"] = DISPLAY
    env.setdefault("GDK_BACKEND", "x11")
    # Keep the package importable even when it was never pip-installed.
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = os.pathsep.join(
        [p for p in (pkg_parent, env.get("PYTHONPATH", "")) if p]
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-m", _MODULE, op, json.dumps(kwargs)],
            env=env, capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"GTK op {op!r} timed out after {_TIMEOUT}s"}
    out = (proc.stdout or "").strip()
    if not out:
        err = (proc.stderr or "").strip().splitlines()
        return {"ok": False, "error": err[-1] if err else f"GTK op {op!r} produced no output"}
    try:
        return json.loads(out.splitlines()[-1])
    except ValueError:
        return {"ok": False, "error": f"unparseable GTK output: {out[:200]}"}


# ---- GTK side (runs in the helper subprocess) -------------------------------
def _gtk():
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("Wnck", "3.0")
    from gi.repository import Gdk, Gtk, Wnck
    Gtk.init([])
    return Gdk, Gtk, Wnck


def _pump(Gtk) -> None:
    """Let GTK process the X round-trips our calls just queued."""
    while Gtk.events_pending():
        Gtk.main_iteration()


def _screen(Gtk, Wnck):
    screen = Wnck.Screen.get_default()
    screen.force_update()
    _pump(Gtk)
    return screen


def _server_time(Gdk) -> int:
    """EWMH activation needs a real X timestamp; mutter ignores stale ones."""
    try:
        import gi
        gi.require_version("GdkX11", "3.0")
        from gi.repository import GdkX11
        return GdkX11.x11_get_server_time(Gdk.get_default_root_window())
    except Exception:
        return int(time.time())


def _describe(win) -> dict:
    x, y, w, h = win.get_geometry()
    workspace = win.get_workspace()
    app = win.get_application()
    return {
        "id": str(win.get_xid()),
        "name": win.get_name() or "",
        "app": app.get_name() if app else "",
        "pid": win.get_pid(),
        "x": x, "y": y, "w": w, "h": h,
        "minimized": win.is_minimized(),
        "maximized": win.is_maximized(),
        "fullscreen": win.is_fullscreen(),
        "above": win.is_above(),
        "active": win.is_active(),
        "workspace": workspace.get_name() if workspace else None,
        "workspace_index": workspace.get_number() if workspace else None,
    }


def _settle(screen, Gtk, xid: int, before: dict, timeout: float = 0.8) -> dict:
    """Re-read a window until the WM has actually applied the change.

    Window managers act asynchronously: one event pump after `maximize()` still
    reports the old geometry, so a caller trusting the reply would verify the
    state it had *before* acting. Poll until the description changes, then stop.
    """
    deadline = time.monotonic() + timeout
    latest = before
    stable = 0
    while time.monotonic() < deadline:
        time.sleep(0.05)
        screen.force_update()
        _pump(Gtk)
        current = _match(screen, str(xid))
        if current is None:  # window went away (closed mid-settle)
            return latest
        fresh = _describe(current)
        # Geometry and state flags land in separate round-trips, so the first
        # observed change is often half-applied. Wait for it to stop moving.
        stable = stable + 1 if fresh == latest else 0
        latest = fresh
        if stable >= 3:
            break
    return latest


def _match(screen, query: str):
    """Resolve a window by X id or case-insensitive name substring.

    Ids win over names so a numeric title can never shadow an explicit id.
    """
    windows = screen.get_windows()
    q = str(query)
    if q.isdigit():
        for win in windows:
            if str(win.get_xid()) == q or win.get_xid() == int(q):
                return win
    needle = q.lower()
    for win in windows:
        if needle in (win.get_name() or "").lower():
            return win
    return None


# ---- ops --------------------------------------------------------------------
def op_windows(**_) -> dict:
    Gdk, Gtk, Wnck = _gtk()
    screen = _screen(Gtk, Wnck)
    return {"ok": True, "windows": [_describe(w) for w in screen.get_windows()]}


def op_workspaces(**_) -> dict:
    Gdk, Gtk, Wnck = _gtk()
    screen = _screen(Gtk, Wnck)
    active = screen.get_active_workspace()
    return {
        "ok": True,
        "count": screen.get_workspace_count(),
        "active_index": active.get_number() if active else None,
        "workspaces": [
            {"index": w.get_number(), "name": w.get_name()} for w in screen.get_workspaces()
        ],
    }


def op_set_workspace(index: int = 0, **_) -> dict:
    Gdk, Gtk, Wnck = _gtk()
    screen = _screen(Gtk, Wnck)
    spaces = screen.get_workspaces()
    if not 0 <= index < len(spaces):
        return {"ok": False, "error": f"workspace {index} out of range (have {len(spaces)})"}
    spaces[index].activate(_server_time(Gdk))
    _pump(Gtk)
    return {"ok": True, "workspace_index": index, "name": spaces[index].get_name()}


_STATE_ACTIONS = {
    "activate", "minimize", "unminimize", "maximize", "unmaximize",
    "fullscreen", "unfullscreen", "above", "unabove", "pin", "unpin", "close",
}


def op_window_action(query: str = "", action: str = "activate", **_) -> dict:
    """Apply an EWMH state change to one window and report the state after it."""
    if action not in _STATE_ACTIONS:
        return {"ok": False, "error": f"unknown action {action!r}",
                "actions": sorted(_STATE_ACTIONS)}
    Gdk, Gtk, Wnck = _gtk()
    screen = _screen(Gtk, Wnck)
    win = _match(screen, query)
    if win is None:
        return {"ok": False, "error": f"no window matching {query!r}",
                "windows": [w.get_name() for w in screen.get_windows()]}
    before = _describe(win)
    stamp = _server_time(Gdk)
    if action == "activate":
        # A minimized window must be restored first or activation is a no-op.
        if win.is_minimized():
            win.unminimize(stamp)
        win.activate(stamp)
    elif action == "minimize":
        win.minimize()
    elif action == "unminimize":
        win.unminimize(stamp)
    elif action == "maximize":
        win.maximize()
    elif action == "unmaximize":
        win.unmaximize()
    elif action == "fullscreen":
        win.set_fullscreen(True)
    elif action == "unfullscreen":
        win.set_fullscreen(False)
    elif action == "above":
        win.make_above()
    elif action == "unabove":
        win.unmake_above()
    elif action == "pin":
        win.pin()
    elif action == "unpin":
        win.unpin()
    elif action == "close":
        win.close(stamp)
    _pump(Gtk)
    if action == "close":
        return {"ok": True, "action": action, "closed": query}
    return {"ok": True, "action": action,
            "window": _settle(screen, Gtk, win.get_xid(), before)}


def op_window_geometry(query: str = "", x: int | None = None, y: int | None = None,
                       w: int | None = None, h: int | None = None, **_) -> dict:
    """Move and/or resize a window. Unmaximizes first — a maximized window
    silently ignores geometry requests."""
    Gdk, Gtk, Wnck = _gtk()
    screen = _screen(Gtk, Wnck)
    win = _match(screen, query)
    if win is None:
        return {"ok": False, "error": f"no window matching {query!r}"}
    before = _describe(win)
    cur_x, cur_y, cur_w, cur_h = win.get_geometry()
    if win.is_maximized():
        win.unmaximize()
        _pump(Gtk)
    mask = 0
    if x is not None:
        mask |= Wnck.WindowMoveResizeMask.X
    if y is not None:
        mask |= Wnck.WindowMoveResizeMask.Y
    if w is not None:
        mask |= Wnck.WindowMoveResizeMask.WIDTH
    if h is not None:
        mask |= Wnck.WindowMoveResizeMask.HEIGHT
    if not mask:
        return {"ok": False, "error": "give at least one of x, y, w, h"}
    win.set_geometry(
        # OR-ing GI flag members yields a plain int; Wnck wants the flags type back.
        Wnck.WindowGravity.STATIC, Wnck.WindowMoveResizeMask(mask),
        cur_x if x is None else x, cur_y if y is None else y,
        cur_w if w is None else w, cur_h if h is None else h,
    )
    _pump(Gtk)
    return {"ok": True, "window": _settle(screen, Gtk, win.get_xid(), before)}


def op_move_to_workspace(query: str = "", index: int = 0, **_) -> dict:
    Gdk, Gtk, Wnck = _gtk()
    screen = _screen(Gtk, Wnck)
    win = _match(screen, query)
    if win is None:
        return {"ok": False, "error": f"no window matching {query!r}"}
    spaces = screen.get_workspaces()
    if not 0 <= index < len(spaces):
        return {"ok": False, "error": f"workspace {index} out of range (have {len(spaces)})"}
    win.move_to_workspace(spaces[index])
    _pump(Gtk)
    return {"ok": True, "moved": win.get_name(), "workspace_index": index}


def _clipboard(Gdk, Gtk, selection: str):
    atom = Gdk.SELECTION_PRIMARY if selection == "primary" else Gdk.SELECTION_CLIPBOARD
    return Gtk.Clipboard.get(atom)


def op_clipboard_get(selection: str = "clipboard", **_) -> dict:
    Gdk, Gtk, Wnck = _gtk()
    text = _clipboard(Gdk, Gtk, selection).wait_for_text()
    return {"ok": True, "selection": selection, "text": text, "empty": text is None}


def op_clipboard_set(text: str = "", selection: str = "clipboard", **_) -> dict:
    """Put text on the clipboard so it outlives this one-shot helper.

    X11 clipboard transfer is a conversation, not an assignment: the owner must
    still be alive to answer the manager's SelectionRequest. Exiting straight
    after `store()` drops the text on the floor — the call reports success and
    the clipboard stays empty. So we keep pumping events until the transfer has
    had a chance to happen. `get_owner()` is not usable as a done-signal here
    (GTK reports None even while we hold the selection), hence the bounded wait.
    """
    if selection == "primary":
        # Clipboard managers persist CLIPBOARD only. PRIMARY belongs to whoever
        # last selected text and would vanish the moment this helper exits, so a
        # write here can only ever be a silent no-op. Say so instead.
        return {"ok": False, "selection": selection,
                "error": "PRIMARY cannot be written durably — no clipboard manager persists it; "
                         "use selection='clipboard' and paste with ctrl+v"}
    Gdk, Gtk, Wnck = _gtk()
    board = _clipboard(Gdk, Gtk, selection)
    board.set_text(text, -1)
    stored = True
    try:
        board.store()
    except Exception:
        stored = False
    deadline = time.monotonic() + _CLIPBOARD_HANDOFF_MAX_S
    idle_polls = 0
    while time.monotonic() < deadline:
        _pump(Gtk)
        idle_polls = idle_polls + 1 if not Gtk.events_pending() else 0
        # Settled: nothing left to serve, and we gave the manager a real chance.
        if idle_polls >= 3 and time.monotonic() >= deadline - _CLIPBOARD_HANDOFF_MAX_S + _CLIPBOARD_HANDOFF_MIN_S:
            break
        time.sleep(0.02)
    return {"ok": True, "selection": selection, "length": len(text), "stored": stored,
            "note": None if stored else "no clipboard manager — text dies when this call returns"}


_OPS = {
    "windows": op_windows,
    "workspaces": op_workspaces,
    "set_workspace": op_set_workspace,
    "window_action": op_window_action,
    "window_geometry": op_window_geometry,
    "move_to_workspace": op_move_to_workspace,
    "clipboard_get": op_clipboard_get,
    "clipboard_set": op_clipboard_set,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in _OPS:
        print(json.dumps({"ok": False, "error": f"usage: {_MODULE} <op> [json]",
                          "ops": sorted(_OPS)}))
        return 2
    op = argv[0]
    try:
        kwargs = json.loads(argv[1]) if len(argv) > 1 else {}
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": f"bad json args: {exc}"}))
        return 2
    try:
        result = _OPS[op](**kwargs)
    except Exception as exc:  # a GTK/X failure must not look like a crash upstream
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
