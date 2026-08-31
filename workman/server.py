"""claude-workman — an MCP connector for human-mode desktop control on Linux/X11.

Exposes see / click / type / window / accessibility tools over MCP (stdio) so any
MCP client (Claude, etc.) can drive a real desktop the way a person does:
screenshot -> locate (by pixel OR accessibility element) -> act -> screenshot to
verify. The AT-SPI layer makes clicks element-accurate instead of pixel-guessed.

Coordinates: a screenshot is downscaled to the driving model's image budget,
because a model that silently receives a shrunken image reports coordinates in a
space the server never recorded — the classic "clicks land near, not on". Every
screenshot and zoom therefore reports its `scale`, and the pointer tools take a
`space` argument so view coordinates can be passed back verbatim.

Run:  python -m workman.server        (stdio MCP server)
"""
from __future__ import annotations

import time

from mcp.server.fastmcp import FastMCP, Image

from . import apps, atspi, chrome, gtkops, human, vision, x11

mcp = FastMCP("claude-workman")

# What the last screenshot/zoom showed, so "view" coordinates can be mapped back
# to screen pixels. Reset on every capture.
_LAST_VIEW: dict = {"scale": 1.0, "screen": None, "view": None}


def _to_screen(x: float, y: float, space: str) -> tuple[int, int]:
    """Map a coordinate into screen pixels. space='view' means it was read off
    the last capture, which may have been downscaled."""
    if space == "screen":
        return int(round(x)), int(round(y))
    if space != "view":
        raise ValueError(f"space must be 'screen' or 'view', got {space!r}")
    scale = _LAST_VIEW.get("scale") or 1.0
    return int(round(x / scale)), int(round(y / scale))


# ---- SEE -------------------------------------------------------------------
@mcp.tool()
def screenshot(max_dim: int | None = None, full_resolution: bool = False,
               save_to_disk: bool = False) -> list:
    """Capture the whole display. Take one before AND after any action.

    The image is downscaled to the model's image budget and the reply states the
    scale, because coordinates read off a downscaled image are not screen pixels.
    Pass those coordinates straight back with space='view', or multiply them by
    the reported factor yourself. full_resolution=True skips the resize (large;
    most clients will downscale it again on their side and lose the factor).
    save_to_disk writes a copy and returns the path — only worth it when a human
    is meant to look at the file.
    """
    raw = x11.screenshot(max_dim=max_dim)
    meta = {"screen": list(x11.screen_size()), "view": None, "scale": 1.0, "resized": False}
    data = raw
    if not full_resolution and max_dim is None:
        try:
            data, meta = vision.to_view(raw)
        except vision.VisionUnavailable as exc:
            meta["note"] = str(exc)
    _LAST_VIEW.update({"scale": meta.get("scale", 1.0), "screen": meta.get("screen"),
                       "view": meta.get("view")})
    if save_to_disk:
        meta["saved_to"] = vision.save(data, "png")
    if meta.get("resized"):
        factor = round(1 / meta["scale"], 3) if meta["scale"] else 1
        meta["coordinates"] = (
            f"image is {meta['view'][0]}x{meta['view'][1]} for a "
            f"{meta['screen'][0]}x{meta['screen'][1]} screen — pass coordinates read "
            f"from it with space='view', or multiply them by {factor}"
        )
    return [meta, Image(data=data, format="png")]


@mcp.tool()
def screenshot_region(x: int, y: int, w: int, h: int) -> Image:
    """Capture a sub-rectangle of the screen at native resolution (faster than a
    full grab). For reading small detail, prefer `zoom`, which also magnifies."""
    return Image(data=x11.screenshot(region=(x, y, w, h)), format="png")


@mcp.tool()
def zoom(x1: int, y1: int, x2: int, y2: int, space: str = "view",
         save_to_disk: bool = False) -> list:
    """Magnify a region so small detail becomes legible — labels, status text,
    icons, line numbers, a chart's tick values.

    The region is (x1,y1) top-left to (x2,y2) bottom-right. It is re-grabbed from
    the live screen at FULL resolution and then enlarged to fill the image
    budget, so this recovers detail a downscaled screenshot threw away —
    enlarging the screenshot itself could not. Coordinates default to the space
    of the last screenshot.
    """
    try:
        screen_w, screen_h = x11.screen_size()
        if space == "view":
            scale = _LAST_VIEW.get("scale") or 1.0
            x1, y1, x2, y2 = (round(v / scale) for v in (x1, y1, x2, y2))
        elif space != "screen":
            return [{"ok": False, "error": f"space must be 'screen' or 'view', got {space!r}"}]
        rx, ry, rw, rh = vision.normalize_region(x1, y1, x2, y2, (screen_w, screen_h))
        raw = x11.screenshot(region=(rx, ry, rw, rh))
        data, meta = vision.magnify(raw)
    except (ValueError, vision.VisionUnavailable) as exc:
        return [{"ok": False, "error": str(exc)}]
    meta.update({"ok": True, "region_screen": [rx, ry, rx + rw, ry + rh],
                 "note": "coordinates inside this crop are relative to it — "
                         "add the region origin before clicking"})
    if save_to_disk:
        meta["saved_to"] = vision.save(data, "jpg")
    return [meta, Image(data=data, format="jpeg")]


@mcp.tool()
def screen_info() -> dict:
    """Screen size, monitor layout, image budget and the last capture's scale.
    Check this before trusting one screen size — a scaled or multi-head setup can
    make the X screen and a panel's native mode disagree."""
    width, height = x11.screen_size()
    budget = vision.max_edge()
    return {
        "ok": True,
        "screen": {"w": width, "h": height},
        "monitors": x11.monitors(),
        "image_max_edge": budget,
        "view_size": list(vision.view_size(width, height)),
        "last_capture": dict(_LAST_VIEW),
        "display": x11.DISPLAY,
    }


# ---- WINDOWS ---------------------------------------------------------------
@mcp.tool()
def list_windows() -> list[dict]:
    """List on-screen windows with id, name, pid, geometry and state
    (minimized/maximized/fullscreen/active/workspace)."""
    result = gtkops.call("windows")
    if result.get("ok"):
        return result["windows"]
    return x11.list_windows()  # Wnck unavailable — fall back to raw geometry


@mcp.tool()
def focus_window(query: str, minimize_blockers: bool = True) -> dict:
    """Raise a window by id or name-substring. On focus-stealing WMs (e.g.
    mutter) plain activation can silently fail, so the frontmost blocker is
    minimized first. Always screenshot to verify focus before typing."""
    return x11.focus_window(query, minimize_blockers=minimize_blockers)


@mcp.tool()
def window(action: str, query: str) -> dict:
    """Change a window's state. action: activate | minimize | unminimize |
    maximize | unmaximize | fullscreen | unfullscreen | above | unabove | pin |
    unpin | close.

    `close` is the graceful route — the app can still prompt about unsaved work.
    Use kill_window only when close is ignored.
    """
    return gtkops.call("window_action", query=query, action=action)


@mcp.tool()
def window_geometry(query: str, x: int | None = None, y: int | None = None,
                    w: int | None = None, h: int | None = None) -> dict:
    """Move and/or resize a window. Omitted values are left alone. A maximized
    window ignores geometry, so it is unmaximized first."""
    return gtkops.call("window_geometry", query=query, x=x, y=y, w=w, h=h)


@mcp.tool()
def kill_window(query: str) -> dict:
    """Force a window's client to die. Unsaved work is lost — try
    window(action='close') first."""
    return x11.kill_window(query)


@mcp.tool()
def active_window() -> dict:
    """The window that currently has focus."""
    return x11.active_window()


@mcp.tool()
def workspace(action: str = "list", index: int = 0, query: str = "") -> dict:
    """Virtual desktops. action: list | switch | move_window.
    switch needs `index`; move_window needs `index` and `query`."""
    if action == "list":
        return gtkops.call("workspaces")
    if action == "switch":
        return gtkops.call("set_workspace", index=index)
    if action == "move_window":
        return gtkops.call("move_to_workspace", query=query, index=index)
    return {"ok": False, "error": f"unknown action {action!r}",
            "actions": ["list", "switch", "move_window"]}


# ---- HUMAN MODE ------------------------------------------------------------
@mcp.tool()
def workman_set_human_mode(on: bool, seed: int | None = None) -> dict:
    """Turn Human Mode on or off for this session.

    When on, every input tool (move, click, type, scroll, mouse_button)
    uses OS-level human cadence so a site cannot tell the pointer is
    scripted. seed makes the cadence reproducible; omit it and a fresh
    seed is drawn for the session. Default is off (fast, unchanged).
    """
    return human.set_mode(on, seed=seed)


@mcp.tool()
def workman_get_human_mode() -> dict:
    """Current Human Mode flag and the RNG seed driving this session's cadence."""
    return human.get_mode()


def _click_with_human(x: int, y: int, button: int, count: int,
                      modifiers: list[str]) -> dict:
    """Human-move to the target, then click while holding modifiers in place."""
    mods = [m.strip().lower() for m in modifiers if m.strip()]
    unknown = [m for m in mods if m not in x11._MODIFIERS]
    if unknown:
        return {"ok": False, "error": f"unknown modifier(s) {unknown}",
                "supported": sorted(x11._MODIFIERS)}
    rng = human.session_rng()
    human.human_move(x, y, rng)
    for mod in mods:
        x11.key_down(mod)
    try:
        press = human.human_press_click(button=button, count=count, rng=rng, aim=True)
        return {"ok": True, "clicked": [x, y], "button": button, "count": count,
                "modifiers": mods, "press_ms": press, "human": True}
    finally:
        for mod in reversed(mods):
            x11.key_up(mod)


# ---- MOUSE -----------------------------------------------------------------
@mcp.tool()
def click(x: int, y: int, button: int = 1, count: int = 1,
          modifiers: list[str] | None = None, space: str = "screen") -> dict:
    """Click at (x, y). button 1=left 2=middle 3=right. count=2 double, 3 triple.
    modifiers hold keys during the click (e.g. ['shift'] to extend a selection).
    space='view' if the coordinates came from a downscaled screenshot.

    Aim for the centre of the target, not its edge. Human Mode routes this
    through an eased pointer path and a 60–140 ms press."""
    try:
        sx, sy = _to_screen(x, y, space)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if human.enabled():
        if modifiers:
            return _click_with_human(sx, sy, button=button, count=count,
                                     modifiers=modifiers)
        return human.human_click(sx, sy, button=button, count=count)
    if modifiers:
        return x11.click_with(sx, sy, button=button, count=count, modifiers=modifiers)
    return x11.click(sx, sy, button=button, count=count)


@mcp.tool()
def move(x: int, y: int, space: str = "screen") -> dict:
    """Move the pointer without clicking. Human Mode follows a curved
    multi-waypoint path instead of teleporting."""
    try:
        sx, sy = _to_screen(x, y, space)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if human.enabled():
        return human.human_move(sx, sy)
    return x11.move(sx, sy)


@mcp.tool()
def hover(x: int, y: int, settle_ms: int = 350, space: str = "screen") -> dict:
    """Move the pointer and wait for hover-triggered UI — tooltips, submenus,
    hover states — to appear before you screenshot."""
    try:
        sx, sy = _to_screen(x, y, space)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if human.enabled():
        return human.human_hover(sx, sy, settle_ms=settle_ms)
    return x11.hover(sx, sy, settle_ms=settle_ms)


@mcp.tool()
def drag(from_x: int, from_y: int, to_x: int, to_y: int, space: str = "screen") -> dict:
    """Press at (from_x, from_y), drag to (to_x, to_y), release."""
    try:
        fx, fy = _to_screen(from_x, from_y, space)
        tx, ty = _to_screen(to_x, to_y, space)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if human.enabled():
        return human.human_drag(fx, fy, tx, ty)
    return x11.drag(fx, fy, tx, ty)


@mcp.tool()
def scroll(direction: str, amount: int = 3, x: int | None = None, y: int | None = None,
           space: str = "screen") -> dict:
    """Scroll up|down|left|right by `amount` wheel steps. Give x and y to scroll
    over a specific pane — without them it scrolls wherever the pointer happens
    to be, which is rarely what you meant. Human Mode uses erratic bursts."""
    sx = sy = None
    if x is not None and y is not None:
        try:
            sx, sy = _to_screen(x, y, space)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
    if human.enabled():
        return human.human_scroll(direction, amount=amount, x=sx, y=sy)
    if sx is None or sy is None:
        return x11.scroll(direction, amount=amount)
    return x11.scroll_at(sx, sy, direction, amount=amount)


@mcp.tool()
def mouse_button(button: int = 1, press: bool = True, x: int | None = None,
                 y: int | None = None, space: str = "screen") -> dict:
    """Hold or release a mouse button for gestures a single click can't express
    (rubber-band selection, drag with a pause). Always release what you press."""
    sx = sy = None
    if x is not None and y is not None:
        try:
            sx, sy = _to_screen(x, y, space)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
    if human.enabled():
        return human.human_mouse_button(button=button, press=press, x=sx, y=sy)
    return (x11.mouse_down if press else x11.mouse_up)(button=button, x=sx, y=sy)


@mcp.tool()
def pointer_position() -> dict:
    """Where the pointer is now, and which window is under it."""
    return x11.pointer_position()


# ---- KEYBOARD --------------------------------------------------------------
@mcp.tool()
def type_text(text: str, delay_ms: int = 40, typos: bool = False,
              field: str = "") -> dict:
    """Type literal text into the focused window. For long or exact strings,
    clipboard_set + a paste keystroke is faster and cannot be mangled by
    autocomplete.

    When Human Mode is on, inter-key cadence (50–300 ms, longer after
    spaces) is used instead of delay_ms. typos=True opts into a rare
    wrong-char-then-backspace; it stays off for password, URL, and money
    fields even when requested."""
    if human.enabled():
        return human.human_type(text, typos=typos, field=field)
    return x11.type_text(text, delay_ms=delay_ms)


@mcp.tool()
def press_key(key: str) -> dict:
    """Press a key/combo in xdotool syntax: 'Return', 'Tab', 'ctrl+c',
    'super+l', 'KP_0'."""
    if human.enabled() and key.lower() in {"return", "enter", "kp_enter"}:
        time.sleep(human.enter_delay_ms() / 1000.0)
    return x11.press_key(key)


@mcp.tool()
def key_hold(key: str, press: bool = True) -> dict:
    """Hold or release a key across other actions (e.g. hold 'ctrl', click
    several items, release). Always release what you press."""
    return (x11.key_down if press else x11.key_up)(key)


@mcp.tool()
def wait(seconds: float = 1.0) -> dict:
    """Pause for the UI to settle. Cap 30s — for anything longer, wait on a
    specific element with wait_for_element instead of guessing."""
    seconds = max(0.0, min(float(seconds), 30.0))
    time.sleep(seconds)
    return {"ok": True, "waited_s": seconds}


# ---- CLIPBOARD -------------------------------------------------------------
@mcp.tool()
def clipboard_get(selection: str = "clipboard") -> dict:
    """Read the clipboard. selection: clipboard | primary (primary is the X
    middle-click selection, which is a different buffer)."""
    return gtkops.call("clipboard_get", selection=selection)


@mcp.tool()
def clipboard_set(text: str, selection: str = "clipboard") -> dict:
    """Put text on the clipboard — the reliable way to enter long or exact
    strings: set it, then press ctrl+v. Only selection='clipboard' can be
    written; PRIMARY is owned by whatever last selected text and no manager
    persists it. `stored` false means no clipboard manager is running and the
    text may not outlive this call."""
    return gtkops.call("clipboard_set", text=text, selection=selection)


# ---- APPLICATIONS ----------------------------------------------------------
@mcp.tool()
def launch_app(command: str, args: list[str] | None = None,
               wait_for_window: float = 8.0) -> dict:
    """Start an application, detached so it outlives this server. Waits for its
    window to appear and returns it, so you can act without guessing a sleep."""
    return apps.launch(command, args=args, wait_for_window=wait_for_window)


@mcp.tool()
def list_apps(launchable: bool = False, query: str = "") -> list[dict]:
    """Applications with a window right now, or — with launchable=True — the
    installed desktop entries you could start, filtered by `query`."""
    return apps.list_launchable(query=query) if launchable else apps.list_apps()


@mcp.tool()
def terminate_app(pid: int, force: bool = False) -> dict:
    """Signal a process to exit. Prefer window(action='close'): a signal gives
    the app no chance to prompt about unsaved work."""
    return apps.terminate(pid, force=force)


# ---- ACCESSIBILITY (the accuracy layer) ------------------------------------
@mcp.tool()
def enable_accessibility(enable_web: bool = True) -> dict:
    """Turn on AT-SPI tree export. enable_web also lets Chromium/Electron export
    their web content. Orca is killed afterwards so nothing is spoken aloud."""
    return atspi.ensure_a11y(enable_web=enable_web, silence=True)


@mcp.tool()
def accessibility_tree(app: str | None = None, actionable_only: bool = True) -> list[dict]:
    """Dump actionable UI elements as {app, role, name, x, y, w, h} with screen
    coordinates. Filter by app name substring. Call enable_accessibility first."""
    return atspi.tree(app=app, actionable_only=actionable_only)


@mcp.tool()
def click_element(name: str, role: str | None = None, app: str | None = None) -> dict:
    """Find an element by role+name in the accessibility tree and click its
    center — element-accurate, no pixel guessing."""
    return atspi.click_element(name, role=role, app=app)


@mcp.tool()
def perform_element_action(name: str, action: str = "click", role: str | None = None,
                           app: str | None = None) -> dict:
    """Invoke an element's own action instead of clicking at it. Works where a
    synthetic click cannot reach — occluded, scrolled, or under a pointer grab.
    Use element_actions to see what an element declares."""
    return atspi.perform_action(name, action=action, role=role, app=app)


@mcp.tool()
def element_actions(name: str, role: str | None = None, app: str | None = None) -> dict:
    """List the actions a toolkit declares on an element (press, activate, ...)."""
    return atspi.element_actions(name, role=role, app=app)


@mcp.tool()
def set_element_value(name: str, value: str, role: str | None = None,
                      app: str | None = None) -> dict:
    """Set a field's contents directly. Beats click + select-all + type: no
    keystroke timing, no stray keybindings, no autocomplete corruption."""
    return atspi.set_value(name, value, role=role, app=app)


@mcp.tool()
def focused_element() -> dict:
    """What currently has keyboard focus — the reliable way to confirm a click
    landed before you start typing."""
    return atspi.focused_element()


@mcp.tool()
def wait_for_element(name: str, role: str | None = None, app: str | None = None,
                     timeout: float = 10.0) -> dict:
    """Block until an element appears. Better than sleeping: UI that is still
    animating in reports stale geometry."""
    return atspi.wait_for_element(name, role=role, app=app, timeout=timeout)


@mcp.tool()
def show_cursor(on: bool = True) -> dict:
    """Start/stop the visual click cursor overlay — a Codex-style ring + click
    ripple that shows on the monitor exactly where Workman is acting (for human
    oversight). Requires GTK (python3-gi). Actions auto-notify it once running."""
    import subprocess
    import sys
    from . import cursor as _cursor
    if on:
        _cursor.fifo_path()
        subprocess.Popen([sys.executable, "-m", "workman.cursor"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "overlay": "starting", "fifo": _cursor.FIFO}
    try:
        with open(_cursor.FIFO, "w") as f:
            f.write("quit\n")
    except OSError:
        pass
    return {"ok": True, "overlay": "stopped"}


# ---- CHROME ----------------------------------------------------------------
@mcp.tool()
def chrome_focus() -> dict:
    """Find, raise and focus a Chrome/Chromium window. Returns its id and
    title. Screenshot to verify before typing into the page."""
    return chrome.focus()


@mcp.tool()
def chrome_open_url(url: str, new_tab: bool = True) -> dict:
    """Open a URL the way a person does: focus Chrome, ctrl+t (or ctrl+l to
    reuse the tab), type the URL with human cadence, press Return. Waits for
    the tab title to change before returning."""
    return chrome.open_url(url, new_tab=new_tab)


@mcp.tool()
def chrome_list_tabs() -> dict:
    """List Chrome tabs via CDP http://127.0.0.1:9222/json (id, title, url,
    active). If remote debugging is off, returns a 'no CDP' result with the
    focused window title instead of failing hard."""
    return chrome.list_tabs()


@mcp.tool()
def chrome_activate_tab(match: str) -> dict:
    """Activate the first tab whose title or URL contains `match`. Uses
    CDP /json/activate when available; otherwise walks ctrl+Tab reading the
    window title."""
    return chrome.activate_tab(match)


@mcp.tool()
def chrome_read_page() -> dict:
    """Visible text of the active tab via the Chrome window's AT-SPI tree
    (capped at 20000 chars), plus role/name of interactive elements.
    Call enable_accessibility first so Chromium exports the web content."""
    return chrome.read_page()


@mcp.tool()
def chrome_click_text(text: str) -> dict:
    """Find a clickable element by visible name in Chrome's AT-SPI tree,
    scroll it into view if needed, and human-click it (eased pointer path,
    endpoint jitter)."""
    return chrome.click_text(text)


@mcp.tool()
def chrome_type(text: str, human: bool = True) -> dict:
    """Type into the focused Chrome window. human=True uses per-character
    cadence (50–300 ms, longer after spaces); wraps the same type-text
    path as everywhere else."""
    return chrome.type_text(text, human=human)


@mcp.tool()
def chrome_wait_load(timeout_s: float = 15) -> dict:
    """Wait until the page looks loaded: the window title stops changing,
    and — when CDP is up — the active tab stops reporting a loading state."""
    return chrome.wait_load(timeout_s=timeout_s)


@mcp.tool()
def chrome_autoscroll_read(selector: str | None = None, max_scrolls: int = 40,
                           overlap_lines: int = 3, screenshots: bool = False,
                           human: bool | None = None) -> dict:
    """Walk a scrollable region and return the complete visible text.

    Finds the container matching `selector` (AT-SPI name/role; CSS id/class
    accepted as a hint) or the tallest scrollable element, then captures,
    scrolls ~85% of the viewport, and stitches overlapping captures so the
    result has no duplicated blocks and no gaps. Stops when the scroll
    position stops advancing, the captured text stops adding new lines, or
    `max_scrolls` is hit — `stopped_because` says which. Text is capped at
    200000 chars (`truncated` true when cut). `human` defaults to the
    global Human Mode flag; when on, scrolling is erratic bursts with
    variable acceleration and small pauses. `screenshots=True` saves one
    PNG per step and returns the paths.
    """
    return chrome.autoscroll_read(selector=selector, max_scrolls=max_scrolls,
                                  overlap_lines=overlap_lines,
                                  screenshots=screenshots, human=human)


@mcp.tool()
def chrome_autoscroll_to_top(selector: str | None = None) -> dict:
    """Scroll the same container `chrome_autoscroll_read` would use back to
    the top, so a read can start from the beginning."""
    return chrome.autoscroll_to_top(selector=selector)


# ---- BATCH -----------------------------------------------------------------
# Non-visual actions only: interleaving images inside one result is awkward for
# most clients, and the point here is to cut round-trips on action sequences.
_BATCH_OPS = {
    "click": click, "move": move, "hover": hover, "drag": drag, "scroll": scroll,
    "mouse_button": mouse_button, "type_text": type_text, "press_key": press_key,
    "key_hold": key_hold, "wait": wait, "focus_window": focus_window,
    "window": window, "window_geometry": window_geometry, "workspace": workspace,
    "clipboard_set": clipboard_set, "clipboard_get": clipboard_get,
    "click_element": click_element, "perform_element_action": perform_element_action,
    "set_element_value": set_element_value, "wait_for_element": wait_for_element,
    "focused_element": focused_element, "pointer_position": pointer_position,
    "chrome_focus": chrome_focus, "chrome_open_url": chrome_open_url,
    "chrome_list_tabs": chrome_list_tabs, "chrome_activate_tab": chrome_activate_tab,
    "chrome_read_page": chrome_read_page, "chrome_click_text": chrome_click_text,
    "chrome_type": chrome_type, "chrome_wait_load": chrome_wait_load,
    "chrome_autoscroll_read": chrome_autoscroll_read,
    "chrome_autoscroll_to_top": chrome_autoscroll_to_top,
    "workman_set_human_mode": workman_set_human_mode,
    "workman_get_human_mode": workman_get_human_mode,
}


@mcp.tool()
def batch(actions: list[dict], stop_on_error: bool = True) -> dict:
    """Run several actions in one round-trip. Each entry is
    {"action": "<tool name>", ...that tool's arguments}.

    For sequences whose outcome you already know how to verify — focus a window,
    set a field, press Return. Screenshot separately afterwards; batch returns no
    images. Stops at the first failure unless stop_on_error=False.
    """
    results = []
    for step, spec in enumerate(actions):
        if not isinstance(spec, dict):
            results.append({"ok": False, "error": f"step {step} is not an object"})
            if stop_on_error:
                break
            continue
        params = dict(spec)
        name = params.pop("action", None)
        handler = _BATCH_OPS.get(name)
        if handler is None:
            results.append({"ok": False, "error": f"step {step}: unknown action {name!r}",
                            "actions": sorted(_BATCH_OPS)})
            if stop_on_error:
                break
            continue
        try:
            outcome = handler(**params)
        except TypeError as exc:
            outcome = {"ok": False, "error": f"step {step}: bad arguments for {name!r}: {exc}"}
        except Exception as exc:
            outcome = {"ok": False, "error": f"step {step}: {type(exc).__name__}: {exc}"}
        entry = outcome if isinstance(outcome, dict) else {"ok": True, "result": outcome}
        entry["action"] = name
        results.append(entry)
        if stop_on_error and not entry.get("ok", True):
            break
    completed = sum(1 for r in results if r.get("ok", True))
    return {"ok": completed == len(actions), "completed": completed,
            "requested": len(actions), "results": results}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
