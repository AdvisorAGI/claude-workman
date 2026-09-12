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

import importlib
import sys
import time

from mcp.server.fastmcp import FastMCP, Image

from . import desktop, human


class _NeverRaised(Exception):
    """Exception type handed out for a missing module's exception classes, so
    an `except mod.SomeError` clause stays a valid clause instead of a
    TypeError when the module never imported."""


class _Unavailable:
    """Stand-in for a module that failed to import. Its tools answer with the
    import error instead of the whole server failing to start."""

    def __init__(self, name: str, exc: BaseException):
        self._name = name
        self._why = f"{type(exc).__name__}: {exc}"

    def __getattr__(self, attr: str):
        # CapitalisedNames are exception classes by convention in this
        # package (VisionUnavailable, Unsupported, ...); everything else is a
        # function whose reply says what is missing.
        if attr[:1].isupper():
            return _NeverRaised

        def unavailable(*_args, **_kwargs) -> dict:
            return {"ok": False, "error": "module_unavailable",
                    "module": self._name, "detail": self._why}
        return unavailable


def _optional(name: str):
    try:
        return importlib.import_module(f".{name}", __package__)
    except Exception as exc:
        sys.stderr.write(f"workman: {name} unavailable, its tools will say so: {exc}\n")
        return _Unavailable(name, exc)


# `_remote` because the `remote` tool below would otherwise shadow the module.
(a11y, account, apps, bridge, chrome, esc_pause, handback, owner_pause, _remote,
 resume, session, shortcuts, shotlog, vision, workspace_layout) = (_optional(n) for n in (
    "a11y", "account", "apps", "bridge", "chrome", "esc_pause", "handback",
    "owner_pause", "remote", "resume", "session", "shortcuts", "shotlog", "vision",
    "workspace_layout"))

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


def _challenge_refusal(name: str) -> dict | None:
    """A bot challenge in the active browser window stops every input tool.
    Nothing here tries to pass it: the owner does, by hand."""
    try:
        active = chrome.challenge_active()
    except Exception:
        return None
    if not active:
        return None
    return {"ok": False, "error": "bot_challenge", "action": name,
            "instruction": getattr(chrome, "CHALLENGE_INSTRUCTION",
                                   "A bot challenge is on screen; leave it to the owner.")}


def _input(name: str, args: dict, run) -> dict:
    """Every input tool goes through here: the challenge check, the note of
    where the pointer was before the agent's first touch, and the memory of
    an interrupted result so resume_interrupted can finish it."""
    refused = _challenge_refusal(name)
    if refused is not None:
        return resume.remember(name, args, refused)
    handback.note_pointer_before()
    result = run()
    return resume.remember(name, args, result)


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
    raw = desktop.screenshot(max_dim=max_dim)
    meta = {"screen": list(desktop.screen_size()), "view": None, "scale": 1.0, "resized": False}
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
    shot = shotlog.archive_capture(data, "png", "screenshot")
    if shot:
        meta["shot"] = shot
    shotlog.journal("screenshot", meta)
    return [meta, Image(data=data, format="png")]


@mcp.tool()
def screenshot_region(x: int, y: int, w: int, h: int) -> Image:
    """Capture a sub-rectangle of the screen at native resolution (faster than a
    full grab). For reading small detail, prefer `zoom`, which also magnifies."""
    data = desktop.screenshot(region=(x, y, w, h))
    shot = shotlog.archive_capture(data, "png", "screenshot_region")
    shotlog.journal("screenshot_region", {"region": [x, y, w, h], "shot": shot})
    return Image(data=data, format="png")


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
        screen_w, screen_h = desktop.screen_size()
        if space == "view":
            scale = _LAST_VIEW.get("scale") or 1.0
            x1, y1, x2, y2 = (round(v / scale) for v in (x1, y1, x2, y2))
        elif space != "screen":
            return [{"ok": False, "error": f"space must be 'screen' or 'view', got {space!r}"}]
        rx, ry, rw, rh = vision.normalize_region(x1, y1, x2, y2, (screen_w, screen_h))
        raw = desktop.screenshot(region=(rx, ry, rw, rh))
        data, meta = vision.magnify(raw)
    except (ValueError, vision.VisionUnavailable) as exc:
        return [{"ok": False, "error": str(exc)}]
    # One capture, many targets. The caller reads several coordinates off this
    # one image and maps each back to the screen, so the mapping is returned
    # rather than left to be derived from `magnification`, which is the wrong
    # number to derive it from: magnification is the image measured against the
    # CROP, and a crop is whatever the backend grabbed. Measured here, the
    # macOS grab comes back in points (a 120x80 point region gives a 120x80
    # image), so the two happen to agree; a backend that grabs a 2x display in
    # device pixels makes them differ by exactly that 2, and every mapped click
    # then lands at half the intended offset. `factor` is the region's width in
    # screen points over the returned image's width, so it is right either way
    # and the caller never has to know which kind of grab it got.
    image_w, image_h = (meta.get("magnified") or [rw, rh])[:2]
    image_w, image_h = int(image_w) or rw, int(image_h) or rh
    factor_x, factor_y = rw / image_w, rh / image_h
    meta.update({
        "ok": True,
        "region_screen": [rx, ry, rx + rw, ry + rh],
        "origin": [rx, ry],
        "factor": round((factor_x + factor_y) / 2, 6),
        "factor_xy": [round(factor_x, 6), round(factor_y, 6)],
        "to_screen": "screen_x = origin[0] + image_x * factor, "
                     "screen_y = origin[1] + image_y * factor",
        "note": "coordinates read off this image are relative to the crop and "
                "magnified. Map each one with `to_screen` and click it with "
                "space='screen'; several targets can be mapped from this one "
                "capture, so plan the whole sequence before capturing again",
    })
    crop_w = int((meta.get("crop") or [0])[0] or 0)
    if crop_w and crop_w != rw:
        # A Retina grab comes back in device pixels while the region is in
        # points. Saying so beats leaving a caller to wonder why the numbers do
        # not divide.
        meta["device_scale"] = round(crop_w / rw, 3)
    if save_to_disk:
        meta["saved_to"] = vision.save(data, "jpg")
    shot = shotlog.archive_capture(data, "jpeg", "zoom")
    if shot:
        meta["shot"] = shot
    shotlog.journal("zoom", meta)
    return [meta, Image(data=data, format="jpeg")]


@mcp.tool()
def screen_info() -> dict:
    """Screen size, monitor layout, image budget and the last capture's scale.
    Check this before trusting one screen size — a scaled or multi-head setup can
    make the X screen and a panel's native mode disagree."""
    width, height = desktop.screen_size()
    budget = vision.max_edge()
    info = desktop.platform_info()
    return {
        "ok": True,
        "screen": {"w": width, "h": height},
        "monitors": desktop.monitors(),
        "image_max_edge": budget,
        "view_size": list(vision.view_size(width, height)),
        "last_capture": dict(_LAST_VIEW),
        "display": desktop.display_name(),
        # Carried here as well as in workman_platform because it changes how a
        # caller must write a shortcut: 'super' is Command on macOS.
        "backend": info.get("backend"),
        "modifier_super": info.get("modifier_super"),
    }


@mcp.tool()
def workman_platform() -> dict:
    """Which OS backend is driving this desktop, and what it can and cannot do.

    Worth one call at the start of a session on an unfamiliar machine: it names
    the backend (linux_x11 | darwin | win32), what `super` maps to, which helper
    binaries are installed, the accessibility layer in use, and the permission
    or session gotcha that most often makes actions silently do nothing
    (Wayland on Linux, Screen Recording and Accessibility on macOS).
    """
    info = desktop.platform_info()
    info["accessibility"] = a11y.backend_name()
    info["human_mode"] = human.get_mode()
    # Owner priority: the pause switch, presence ages and the Escape listener.
    try:
        info["owner_pause"] = owner_pause.status()
        info["esc_listener_pid"] = esc_pause.listener_pid()
    except Exception as exc:
        info["owner_pause_error"] = str(exc)
    # Chrome's debug port is never used unless WORKMAN_CHROME_CDP=1; whether
    # something is listening on it is reported so nobody has to guess.
    info["chrome_cdp"] = {"enabled": bool(chrome.cdp_enabled()),
                          "port_listening": _port_open(9222)}
    return info


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.1) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ---- WINDOWS ---------------------------------------------------------------
@mcp.tool()
def list_windows() -> list[dict]:
    """List on-screen windows with id, name, pid, geometry and state
    (minimized/maximized/fullscreen/active/workspace)."""
    return desktop.list_windows_rich()


@mcp.tool()
def focus_window(query: str, minimize_blockers: bool = True,
                 learn_shortcuts: bool = True) -> dict:
    """Raise a window by id or name-substring. On focus-stealing WMs (e.g.
    mutter) plain activation can silently fail, so the frontmost blocker is
    minimized first. Always screenshot to verify focus before typing.

    When `learn_shortcuts` is true (default), the first focus of an app this
    week harvests its menu-bar chords into the durable learned-shortcuts store
    so later `shortcut()` calls never forget them. Harvest failure never fails
    the focus itself.
    """
    result = desktop.focus_window(query, minimize_blockers=minimize_blockers)
    if not learn_shortcuts:
        return result
    app = ""
    if isinstance(result, dict):
        app = str((result.get("app") or result.get("name")
                   or (result.get("window") or {}).get("app")
                   or "")).strip()
    if not app:
        try:
            active = desktop.active_window()
            if isinstance(active, dict):
                app = str(active.get("app") or active.get("name") or "").strip()
        except Exception:
            app = ""
    if app:
        try:
            from . import learned_shortcuts
            result = dict(result) if isinstance(result, dict) else {"focus": result}
            result["shortcuts_learned"] = learned_shortcuts.ensure_learned(app)
        except Exception as exc:
            if isinstance(result, dict):
                result["shortcuts_learned"] = {"ok": False, "error": str(exc)}
    return result


@mcp.tool()
def window(action: str, query: str) -> dict:
    """Change a window's state. action: activate | minimize | unminimize |
    maximize | unmaximize | fullscreen | unfullscreen | above | unabove | pin |
    unpin | close.

    `close` is the graceful route — the app can still prompt about unsaved work.
    Use kill_window only when close is ignored.
    """
    return desktop.window_action(query, action)


@mcp.tool()
def window_geometry(query: str, x: int | None = None, y: int | None = None,
                    w: int | None = None, h: int | None = None) -> dict:
    """Move and/or resize a window. Omitted values are left alone. A maximized
    window ignores geometry, so it is unmaximized first."""
    return desktop.window_geometry(query, x=x, y=y, w=w, h=h)


@mcp.tool()
def window_tile(action: str, query: str = "", display: int | str | None = None,
                prefer_menu: bool = True) -> dict:
    """Tile a window the way the operating system tiles it: halves, quarters,
    fill, center, and back again.

    action: tile_left | tile_right | tile_top | tile_bottom | tile_top_left |
    tile_top_right | tile_bottom_left | tile_bottom_right | fill | center |
    tile_restore. `query` names the window or its application and defaults to
    the active one; on a multi-window application the named window is made the
    app's main window first, so this moves the window asked for rather than
    whichever was in front.

    On macOS this clicks the app's own Window > Move & Resize menu, so the
    result is the system's rectangle, gap and menu bar inset included, rather
    than one computed here. There is no keyboard route to substitute for it:
    the built-in chords need the Globe/fn modifier, which macOS resolves below
    the event tap and strips from a synthetic event, and the menu items carry
    no key equivalent of their own. An application with no Window menu, a move
    to another display, and every non-macOS backend fall back to writing a
    computed rectangle. `route` says which one ran ('native_menu' or
    'geometry'), `honored` compares the result against the computed tile, and
    `activated` records that the menu route had to bring the app forward,
    because the click only lands on the frontmost application.
    """
    return workspace_layout.tile(action, query=query or None, display=display,
                                 prefer_menu=prefer_menu)


@mcp.tool()
def window_tile_available(query: str = "") -> dict:
    """Which tiling actions this window's app can do natively, right now.

    Reads the app's Window menu without touching it, so it is safe on a screen
    someone is using. `available` False is a normal answer for an application
    that has no standard Window menu; `window_tile` still works there through
    the computed route. `actions` lists only items that are enabled, so
    tile_restore shows up on a window that has been tiled and not on one that
    has not.
    """
    return workspace_layout.native_tiling(query or None)


@mcp.tool()
def kill_window(query: str) -> dict:
    """Force a window's client to die. Unsaved work is lost — try
    window(action='close') first."""
    return desktop.kill_window(query)


@mcp.tool()
def active_window() -> dict:
    """The window that currently has focus."""
    return desktop.active_window()


@mcp.tool()
def workspace(action: str = "list", index: int = 0, query: str = "") -> dict:
    """Virtual desktops. action: list | switch | move_window.
    switch needs `index`; move_window needs `index` and `query`."""
    if action == "list":
        return desktop.workspaces()
    if action == "switch":
        return desktop.set_workspace(index=index)
    if action == "move_window":
        return desktop.move_to_workspace(query, index=index)
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
    unknown = [m for m in mods if m not in desktop.MODIFIERS]
    if unknown:
        return {"ok": False, "error": f"unknown modifier(s) {unknown}",
                "supported": sorted(desktop.MODIFIERS)}
    rng = human.session_rng()
    moved = human.human_move(x, y, rng)
    if not moved.get("ok"):
        return moved
    held: list[str] = []
    try:
        for mod in mods:
            r = desktop.key_down(mod)
            if isinstance(r, dict) and r.get("ok") is False:
                return {"ok": False, "error": "interrupted", "reason": r,
                        "clicked": False, "modifiers": mods, "human": True}
            held.append(mod)
        press = human.human_press_click(button=button, count=count, rng=rng, aim=True)
        return {"ok": True, "clicked": [x, y], "button": button, "count": count,
                "modifiers": mods, "press_ms": press, "human": True}
    except human.Interrupted as exc:
        return {"ok": False, "error": "interrupted", "reason": exc.result,
                "clicked": False, "modifiers": mods, "human": True}
    finally:
        # Only what actually went down comes back up: a stray key_up could
        # release a key the owner is physically holding. One release failing
        # must not strand the modifiers behind it.
        for mod in reversed(held):
            try:
                desktop.key_up(mod)
            except Exception:
                pass


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

    def run():
        if human.enabled():
            if modifiers:
                return _click_with_human(sx, sy, button=button, count=count,
                                         modifiers=modifiers)
            return human.human_click(sx, sy, button=button, count=count)
        if modifiers:
            return desktop.click_with(sx, sy, button=button, count=count, modifiers=modifiers)
        return desktop.click(sx, sy, button=button, count=count)
    return _input("click", {"x": sx, "y": sy, "button": button, "count": count,
                            "modifiers": modifiers}, run)


@mcp.tool()
def move(x: int, y: int, space: str = "screen") -> dict:
    """Move the pointer without clicking. Human Mode follows a curved
    multi-waypoint path instead of teleporting."""
    try:
        sx, sy = _to_screen(x, y, space)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return _input("move", {"x": sx, "y": sy},
                  lambda: human.human_move(sx, sy) if human.enabled() else desktop.move(sx, sy))


@mcp.tool()
def hover(x: int, y: int, settle_ms: int = 350, space: str = "screen") -> dict:
    """Move the pointer and wait for hover-triggered UI — tooltips, submenus,
    hover states — to appear before you screenshot."""
    try:
        sx, sy = _to_screen(x, y, space)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return _input("hover", {"x": sx, "y": sy, "settle_ms": settle_ms},
                  lambda: human.human_hover(sx, sy, settle_ms=settle_ms) if human.enabled()
                  else desktop.hover(sx, sy, settle_ms=settle_ms))


@mcp.tool()
def drag(from_x: int, from_y: int, to_x: int, to_y: int, space: str = "screen") -> dict:
    """Press at (from_x, from_y), drag to (to_x, to_y), release."""
    try:
        fx, fy = _to_screen(from_x, from_y, space)
        tx, ty = _to_screen(to_x, to_y, space)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return _input("drag", {"from_x": fx, "from_y": fy, "to_x": tx, "to_y": ty},
                  lambda: human.human_drag(fx, fy, tx, ty) if human.enabled()
                  else desktop.drag(fx, fy, tx, ty))


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

    def run():
        if human.enabled():
            return human.human_scroll(direction, amount=amount, x=sx, y=sy)
        if sx is None or sy is None:
            return desktop.scroll(direction, amount=amount)
        return desktop.scroll_at(sx, sy, direction, amount=amount)
    return _input("scroll", {"direction": direction, "amount": amount, "x": sx, "y": sy}, run)


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

    def run():
        if human.enabled():
            out = human.human_mouse_button(button=button, press=press, x=sx, y=sy)
        else:
            out = (desktop.mouse_down if press else desktop.mouse_up)(button=button, x=sx, y=sy)
        if not (isinstance(out, dict) and out.get("ok") is False):
            handback.note_button(button, press)
        return out
    if not press:
        # A release is never refused or held back: nothing may stay stuck.
        return run()
    return _input("mouse_button", {"button": button, "press": press, "x": sx, "y": sy}, run)


@mcp.tool()
def pointer_position() -> dict:
    """Where the pointer is now, and which window is under it."""
    return desktop.pointer_position()


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
    return _input("type_text", {"text": text, "delay_ms": delay_ms, "typos": typos,
                                "field": field},
                  lambda: human.human_type(text, typos=typos, field=field) if human.enabled()
                  else desktop.type_text(text, delay_ms=delay_ms))


@mcp.tool()
def press_key(key: str) -> dict:
    """Press a key/combo in xdotool syntax: 'Return', 'Tab', 'ctrl+c',
    'super+l', 'KP_0'."""
    def run():
        if human.enabled() and key.lower() in {"return", "enter", "kp_enter"}:
            time.sleep(human.enter_delay_ms() / 1000.0)
        if human.enabled():
            return desktop.press_key(key, hold_ms=human.key_dwell_ms())
        return desktop.press_key(key)
    return _input("press_key", {"key": key}, run)


@mcp.tool()
def key_hold(key: str, press: bool = True) -> dict:
    """Hold or release a key across other actions (e.g. hold 'ctrl', click
    several items, release). Always release what you press; hand_back
    releases whatever is still down."""
    def run():
        out = (desktop.key_down if press else desktop.key_up)(key)
        if not (isinstance(out, dict) and out.get("ok") is False):
            handback.note_key(key, press)
        return out
    if not press:
        return run()
    return _input("key_hold", {"key": key, "press": press}, run)


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
    return desktop.clipboard_get(selection=selection)


@mcp.tool()
def clipboard_set(text: str, selection: str = "clipboard") -> dict:
    """Put text on the clipboard — the reliable way to enter long or exact
    strings: set it, then press ctrl+v. Only selection='clipboard' can be
    written; PRIMARY is owned by whatever last selected text and no manager
    persists it. `stored` false means no clipboard manager is running and the
    text may not outlive this call."""
    return desktop.clipboard_set(text=text, selection=selection)


# ---- APPLICATIONS ----------------------------------------------------------
@mcp.tool()
def launch_app(command: str, args: list[str] | None = None,
               wait_for_window: float = 8.0) -> dict:
    """Start an application, detached so it outlives this server. Waits for its
    window to appear and returns it, so you can act without guessing a sleep.
    hand_back closes what was launched here and nothing else. A browser
    with a debug port, automation switch, headless flag or scratch profile
    is refused: that is not the owner's browser and a page can tell."""
    before = handback.window_ids()
    out = apps.launch(command, args=args, wait_for_window=wait_for_window)
    handback.note_launch(out, before=before)
    return out


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
    return a11y.ensure_a11y(enable_web=enable_web, silence=True)


@mcp.tool()
def accessibility_tree(app: str | None = None, actionable_only: bool = True) -> list[dict]:
    """Dump actionable UI elements as {app, role, name, x, y, w, h} with screen
    coordinates. Filter by app name substring. Call enable_accessibility first."""
    return a11y.tree(app=app, actionable_only=actionable_only)


@mcp.tool()
def click_element(name: str, role: str | None = None, app: str | None = None) -> dict:
    """Find an element by role+name in the accessibility tree and click inside
    its box with the real pointer: element-accurate, no pixel guessing, and
    the click lands at a slightly different point each time like a hand does."""
    return _input("click_element", {"name": name, "role": role, "app": app},
                  lambda: a11y.click_element(name, role=role, app=app))


@mcp.tool()
def perform_element_action(name: str, action: str = "click", role: str | None = None,
                           app: str | None = None) -> dict:
    """Invoke an element's own action instead of clicking at it. Works where a
    synthetic click cannot reach in a native (GTK) app: occluded, scrolled,
    or under a pointer grab. In a browser or Electron app the tree only FINDS
    the element and the real pointer presses it, because an AT-SPI action
    fires a click with no pointer events behind it, which a page can see.
    Use element_actions to see what an element declares."""
    return _input("perform_element_action",
                  {"name": name, "action": action, "role": role, "app": app},
                  lambda: a11y.perform_action(name, action=action, role=role, app=app))


@mcp.tool()
def element_actions(name: str, role: str | None = None, app: str | None = None) -> dict:
    """List the actions a toolkit declares on an element (press, activate, ...)."""
    return a11y.element_actions(name, role=role, app=app)


@mcp.tool()
def set_element_value(name: str, value: str, role: str | None = None,
                      app: str | None = None) -> dict:
    """Set a field's contents. In a native (GTK) app the value is written
    through the toolkit: no keystroke timing, no stray keybindings, no
    autocomplete corruption. In a browser or Electron app the field is
    clicked, select-all is pressed and the value is typed with human cadence,
    because a value that appears with no keys behind it is a tell."""
    return _input("set_element_value", {"name": name, "value": value, "role": role, "app": app},
                  lambda: a11y.set_value(name, value, role=role, app=app))


@mcp.tool()
def focused_element() -> dict:
    """What currently has keyboard focus — the reliable way to confirm a click
    landed before you start typing."""
    return a11y.focused_element()


@mcp.tool()
def wait_for_element(name: str, role: str | None = None, app: str | None = None,
                     timeout: float = 10.0) -> dict:
    """Block until an element appears. Better than sleeping: UI that is still
    animating in reports stale geometry."""
    return a11y.wait_for_element(name, role=role, app=app, timeout=timeout)


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
    return _input("chrome_click_text", {"text": text}, lambda: chrome.click_text(text))


@mcp.tool()
def chrome_type(text: str, human: bool = True) -> dict:
    """Type into the focused Chrome window. human=True uses per-character
    cadence (log-normal intervals, faster common bigrams, pauses between
    words); wraps the same type-text path as everywhere else."""
    return _input("chrome_type", {"text": text, "human": human},
                  lambda: chrome.type_text(text, human=human))


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


# ---- CHROME BRIDGE (workman-chrome extension) ------------------------------
# The extension is SIGHT: DOM query, locate, read, page info. workman is the
# OS-level HANDS. Prefer bridge_locate_and_click over the extension's own
# dom.click / dom.type when a site must not see synthetic events.

@mcp.tool()
def bridge_start(port: int = 8765) -> dict:
    """Listen on 127.0.0.1 for the workman-chrome extension (ws://.../workman).

    Bind is loopback only. Empty WORKMAN_BRIDGE_HOST is treated as 127.0.0.1,
    never 0.0.0.0. A second extension connect drops the older socket so a
    browser restart recovers. Runs on a daemon thread; the MCP server is not
    blocked.
    """
    return bridge.start(port=port)


@mcp.tool()
def bridge_stop() -> dict:
    """Stop the workman-chrome WebSocket listener and drop any live connection."""
    return bridge.stop()


@mcp.tool()
def bridge_status() -> dict:
    """Bridge listener state: running, connected, ext_version, tabs, last_seen, pending."""
    return bridge.status()


@mcp.tool()
def bridge_call(op: str, args: dict | None = None, timeout_s: float = 15) -> dict:
    """Generic escape hatch: send {id, op, args} and return the extension result.

    Known ops: tabs.list/activate/open/close, dom.query/locate/read/click/type,
    page.info/scroll/await, shot.visible. On timeout or when nothing is
    connected this returns {ok: false, error} rather than raising. For stealth
    input prefer bridge_locate_and_click: the extension locates, workman clicks.
    """
    return bridge.call(op, args or {}, timeout_s=timeout_s)


@mcp.tool()
def bridge_tabs_list() -> dict:
    """List Chrome tabs the extension can see ({tabs, count})."""
    return bridge.call("tabs.list", {})


@mcp.tool()
def bridge_tabs_activate(tabId: int) -> dict:
    """Activate a tab by id and focus its window."""
    return bridge.call("tabs.activate", {"tabId": tabId})


@mcp.tool()
def bridge_tabs_open(url: str, active: bool = True) -> dict:
    """Open a tab. url must be http(s) or file, matching the extension's allowlist."""
    return bridge.call("tabs.open", {"url": url, "active": active})


@mcp.tool()
def bridge_tabs_close(tabId: int) -> dict:
    """Close a tab by id."""
    return bridge.call("tabs.close", {"tabId": tabId})


@mcp.tool()
def bridge_dom_query(tabId: int, selector: str, all_matches: bool = False) -> dict:
    """Query the tab's DOM. Each match includes screen-space boundingRect.

    all_matches=True returns up to 500 matches; otherwise the first match.
    """
    return bridge.call("dom.query",
                       {"tabId": tabId, "selector": selector, "all": all_matches})


@mcp.tool()
def bridge_dom_locate(tabId: int, selector: str | None = None,
                      text: str | None = None) -> dict:
    """Best-matching element's centre in screen pixels. Purely informational.

    Returns center (device pixels), centerCss, and boundingRect. No input is
    fired. Feed the centre to workman's OS-level click, or use
    bridge_locate_and_click which does that in one step.
    """
    args: dict = {"tabId": tabId}
    if selector:
        args["selector"] = selector
    if text:
        args["text"] = text
    return bridge.call("dom.locate", args)


@mcp.tool()
def bridge_dom_read(tabId: int, selector: str | None = None) -> dict:
    """innerText of the selector, or of the whole document. Capped at 30000 chars."""
    args: dict = {"tabId": tabId}
    if selector:
        args["selector"] = selector
    return bridge.call("dom.read", args)


@mcp.tool()
def bridge_dom_click(tabId: int, selector: str | None = None,
                     text: str | None = None) -> dict:
    """RELIABLE extension click (synthetic DOM events, stealth: false).

    Detectable by anti-bot systems. For stealth, use bridge_locate_and_click
    so workman presses the real pointer at the located centre.
    """
    args: dict = {"tabId": tabId}
    if selector:
        args["selector"] = selector
    if text:
        args["text"] = text
    return bridge.call("dom.click", args)


@mcp.tool()
def bridge_dom_type(tabId: int, selector: str, text: str,
                    clear: bool = False) -> dict:
    """RELIABLE extension typing (synthetic key events, stealth: false).

    Detectable. For stealth, bridge_dom_locate then type_text with Human Mode.
    """
    return bridge.call("dom.type",
                       {"tabId": tabId, "selector": selector, "text": text,
                        "clear": clear})


@mcp.tool()
def bridge_page_info(tabId: int) -> dict:
    """url, title, readyState, viewport, scroll, dpr, chrome offsets."""
    return bridge.call("page.info", {"tabId": tabId})


@mcp.tool()
def bridge_page_scroll(tabId: int, x: int | None = None, y: int | None = None,
                       dx: int | None = None, dy: int | None = None,
                       selector: str | None = None) -> dict:
    """Scroll the page. Absolute (x, y), relative (dx, dy), or selector into view."""
    args: dict = {"tabId": tabId}
    if x is not None:
        args["x"] = x
    if y is not None:
        args["y"] = y
    if dx is not None:
        args["dx"] = dx
    if dy is not None:
        args["dy"] = dy
    if selector:
        args["selector"] = selector
    return bridge.call("page.scroll", args)


@mcp.tool()
def bridge_page_await(tabId: int, selector: str, timeout_ms: int = 10000) -> dict:
    """Block until selector matches in the tab (default 10s)."""
    timeout_s = max(15.0, float(timeout_ms) / 1000.0 + 2.0)
    return bridge.call("page.await",
                       {"tabId": tabId, "selector": selector,
                        "timeout_ms": timeout_ms},
                       timeout_s=timeout_s)


@mcp.tool()
def bridge_shot_visible(tabId: int | None = None) -> dict:
    """PNG data URL of the visible tab. If tabId is given it must already be active."""
    args: dict = {}
    if tabId is not None:
        args["tabId"] = tabId
    return bridge.call("shot.visible", args)


@mcp.tool()
def bridge_locate_and_click(selector_or_text: str, tabId: int,
                            timeout_s: float = 15) -> dict:
    """Headline tool: extension sees, workman clicks.

    Calls dom.locate for the element's screen-space centre, then clicks that
    point with workman's OS-level pointer (Human Mode if it is on). Synthetic
    DOM events are not used. Returns the located rect and the click result so
    a caller can see what it aimed at.
    """
    return bridge.locate_and_click(selector_or_text, tabId, timeout_s=timeout_s)


# ---- SESSION CONTINUITY ----------------------------------------------------
# The context-pressure state a session and its on-call judging lane share:
# how full the window is, the compact-or-run-on verdict, and the two memory
# files that survive a compaction. Same files the ~/.claude hooks use, so a
# tool call and a hook can never report different numbers.

@mcp.tool()
def session_context_status(transcript_path: str = "", session_id: str = "",
                           cwd: str = "") -> dict:
    """How full this session's context is, and what the on-call lane says about it.

    Returns the token count (the last assistant turn's usage: input +
    cache_creation + cache_read + output), the auto-compact `window`, the
    `floor` below which nobody asks, the `zone` (quiet | judged | backstop),
    percent of the window, and the current verdict if one has been written.

    zone is the whole point: `quiet` means carry on, `judged` means compacting
    is a judgement call at each turn end, `backstop` means the harness compacts
    next turn whether or not the moment is clean. Leave transcript_path empty
    and the session's own transcript is used, or the most recently modified one
    on this machine. `cwd` only selects which project's memory_dir is reported.
    """
    return session.context_status(transcript_path=transcript_path,
                                  session_id=session_id, cwd=cwd)


@mcp.tool()
def session_compact_verdict(session_id: str, compact_now: bool | None = None,
                            reason: str = "", blockers: list[str] | None = None,
                            handoff_current: bool | None = None) -> dict:
    """Read the compact-or-run-on verdict for a session, or write it.

    With only session_id it reads /tmp/compact-verdict.<session_id>.json and
    reports `exists` false when nothing has judged this session yet, which the
    hooks read as "keep running". Supply compact_now to write the verdict; the
    object written is returned. Give a concrete one-sentence `reason` and put
    anything mid-flight in `blockers`: the session is told both when it is
    asked to wrap up. handoff_current left unset keeps the previous value
    rather than silently claiming the handoff is stale.
    """
    return session.compact_verdict(session_id, compact_now=compact_now,
                                   reason=reason, blockers=blockers,
                                   handoff_current=handoff_current)


@mcp.tool()
def session_ledger_append(cwd: str, entries: list[str]) -> dict:
    """Append one dated block of bullets to this project's turn-ledger.md.

    The ledger is what survives a compaction, so each entry should be a
    concrete fact with the path, command, branch, id or number in it, not a
    summary of the conversation. `cwd` picks the project: its memory directory
    is derived from it, and is created if missing.
    """
    return session.ledger_append(cwd, entries)


@mcp.tool()
def session_handoff(cwd: str, content: str = "") -> dict:
    """Read this project's session-handoff-latest.md, or overwrite it.

    Empty `content` reads; anything else replaces the file wholesale. Keep it
    true right now and under 40 lines: DONE / IN FLIGHT / NEXT (exact next
    command) / BLOCKERS / RUNNING LANES. This is the file a session resumes
    from when the context it was holding is gone.
    """
    return session.handoff(cwd, content=content)


# ---- ACCOUNT --------------------------------------------------------------
# Which account this machine is signed in as, on the Claude lane and the Grok
# lane. The plugin ships to whatever machine will have it, so nothing here may
# assume an email address: a hook that launches a child `claude -p` has to ask
# who the PARENT session is before it can hand the child the same identity.
# Identity fields only, by allowlist. No token, key or credential value is read
# by these tools, and the Grok seat is a presence check that never touches
# XAI_API_KEY and never calls api.x.ai.

@mcp.tool()
def account_lanes(config_dir: str = "", transcript_path: str = "") -> dict:
    """Every account lane on this machine, with the active one marked.

    `claude.accounts` is one entry per config dir ($CLAUDE_CONFIG_DIR, then
    ~/.claude, then every ~/.claude-* sibling), each carrying its config_dir,
    the account_file it actually keeps its identity in, `logged_in`, and the
    identity fields (emailAddress, displayName, organizationName,
    organizationUuid, accountUuid, seatTier, billingType). `grok` reports the
    CLI seat: installed, logged_in, auth_method, and whether an xAI key
    variable is exported. No credential value is ever returned.

    Use it to pick a lane on an unfamiliar machine: the DGX carries seven
    config dirs and five distinct signed-in identities, so "the account" is not
    a thing that can be assumed.
    """
    return account.lanes(config_dir=config_dir, transcript_path=transcript_path)


@mcp.tool()
def account_active(config_dir: str = "", transcript_path: str = "") -> dict:
    """The one account lane THIS session is running on, and how that was decided.

    `source` says which input won: argument, env ($CLAUDE_CONFIG_DIR),
    transcript (the config dir is three levels above
    <config_dir>/projects/<slug>/<session_id>.jsonl) or default (~/.claude).
    Pass the hook payload's transcript_path when there is one; it is exact by
    construction where a rebuilt cwd slug is not. When the env and the
    transcript disagree the env wins and the other answer is reported as
    `transcript_config_dir` rather than being dropped silently.

    A child process launched with this config_dir runs as the same account as
    the session that launched it, which is the whole point.
    """
    return account.active(config_dir=config_dir, transcript_path=transcript_path)


# ---- OWNER PRIORITY, RESUME, HAND-BACK -------------------------------------
# The owner's mouse and keyboard outrank the agent's, always. A physical
# Escape pauses every input tool (the listener in workman.esc_pause flips
# the switch and releases what the agent holds); while he is typing or
# moving the mouse the tools wait a moment and then refuse rather than
# fight him. Nothing here grabs a device. When he says resume, the last
# interrupted action carries on from where it stopped.

@mcp.tool()
def input_control(action: str = "status", owner_confirmed: bool = False) -> dict:
    """The owner-pause switch. action: status | resume | pause | release.

    `status` reports the mouse/keyboard switches, who paused them and when,
    how long ago the owner last touched a physical device, what the agent's
    XTEST devices are holding, and the Escape listener's pid. `pause` turns
    the switches off now and releases every held key and button. `release`
    only releases. `resume` turns the switches back on and needs
    owner_confirmed=True: an agent does not un-pause itself after the owner
    pressed Escape; it asks him and passes his answer.
    """
    act = (action or "status").strip().lower()
    if act == "status":
        return {"ok": True, **esc_pause.status()}
    if act == "release":
        return esc_pause.release_agent_holds()
    if act == "pause":
        state = owner_pause.pause_from_escape()
        released = esc_pause.release_agent_holds()
        return {"ok": True, "state": state, "released": released}
    if act == "resume":
        if not owner_confirmed:
            return {"ok": False, "error": "owner_confirmation_required",
                    "instruction": ("Ask the owner whether agent input may resume, then call "
                                    "input_control(action='resume', owner_confirmed=True). "
                                    "resume_interrupted() then finishes what was cut short.")}
        state = owner_pause.resume()
        return {"ok": True, "state": state, "interrupted": resume.last()}
    return {"ok": False, "error": f"unknown action {action!r}",
            "actions": ["status", "resume", "pause", "release"]}


@mcp.tool()
def resume_interrupted(dry_run: bool = False) -> dict:
    """Finish the last input action that was interrupted (Escape, the owner
    using the machine, or a bot challenge): the untyped rest of the text,
    the drag from where it dropped, the wheel clicks not yet sent, the
    click that never landed. dry_run lists the steps without doing them.
    Refuses while the switches are still off; call input_control(resume,
    owner_confirmed=True) first."""
    entry = resume.last()
    if not entry:
        return {"ok": True, "resumed": False, "note": "nothing was interrupted"}
    steps = resume.plan(entry)
    if dry_run:
        return {"ok": True, "resumed": False, "interrupted": entry, "steps": steps}
    if owner_pause.blocked("click") or owner_pause.blocked("type_text"):
        return owner_pause.refusal("resume_interrupted")
    result = batch(steps, stop_on_error=True)
    if result.get("ok"):
        resume.clear()
    return {"ok": result.get("ok", False), "resumed": result.get("ok", False),
            "interrupted": entry, "steps": steps, "result": result}


@mcp.tool()
def hand_back(close_launched: bool = True, park: bool = True) -> dict:
    """Leave the desk the way a person leaves it, and verify it.

    Releases every key and button this server holds and everything the XTEST
    devices still report as down (checked with the X server afterwards),
    closes only the windows launch_app opened (WM_DELETE_WINDOW, so unsaved
    work can prompt), moves the pointer back where the owner left it unless
    he is using it, makes sure the screen is on, and reports what is still
    not clean. Call it at the end of every session.
    """
    return handback.hand_back(close_launched_windows=close_launched, park=park)


# ---- REMOTE ------------------------------------------------------------------
# Another fleet machine's desktop, over one persistent ssh channel per host
# (workman.remote): the remote runs its own wm.py verbs, so its own human
# synthesis, pause switch and TCC grants apply. The ssh path is picked by the
# fleet ladder (cable, LAN, Headscale, Cloudflare) on first connect.

@mcp.tool()
def remote(host: str, verb: str, args: list[str] | None = None,
           timeout_s: float = 60.0) -> dict:
    """Run one wm.py verb on another machine: windows | active | focus <q> |
    click x y | type <text> | key <combo> | elements [app] | human on|off |
    platform | pause status|resume|release | handoff | ping. host: mac-mini |
    md | air | dgx | wind1. Same code both ends, one warm channel."""
    try:
        return _remote.call(host, verb, list(args or []), timeout=timeout_s)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "host": host}


@mcp.tool()
def remote_screenshot(host: str, full: bool = False) -> list:
    """Another machine's screen as an image plus meta (size, scale, blank
    check), through the persistent channel."""
    try:
        meta, data = _remote.image(host, "shot", ["-", "full"] if full else ["-"])
    except Exception as exc:
        return [{"ok": False, "error": f"{type(exc).__name__}: {exc}", "host": host}]
    if data is None:
        return [meta]
    fmt = "png" if data[:4] == b"\x89PNG" else "jpeg"
    return [meta, Image(data=data, format=fmt)]


@mcp.tool()
def remote_status(host: str) -> dict:
    """Is the channel to `host` up, which transport it took (cable, LAN,
    tailnet, Cloudflare), the remote backend, and one measured round trip."""
    try:
        return _remote.status(host)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "host": host}


# ---- BATCH -----------------------------------------------------------------
# Non-visual actions only: interleaving images inside one result is awkward for
# most clients, and the point here is to cut round-trips on action sequences.
# One shape does not fit: a step names its tool in the key "action", so a tool
# that takes its own `action` argument cannot be reached through here at all.
# window_tile is deliberately absent for that reason rather than listed and
# broken; call it directly.
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
    "input_control": input_control, "hand_back": hand_back, "remote": remote,
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


# --------------------------------------------------------------- human mode
# Working the screen the way a person does: know the machine's own key chords
# instead of clicking through menus, and put windows where a person would --
# one task filling the screen, two side by side, three at the hard ceiling.


@mcp.tool()
def shortcut(action: str, app: str | None = None,
             platform: str | None = None) -> dict:
    """The native key chord for an action on this machine, in xdotool syntax.

    Reach for this before clicking through a menu. `app` narrows to that app's
    own bindings ('chrome', 'files', 'terminal', 'vscode'); `platform` overrides
    detection ('darwin', 'linux', 'win32') to look up another machine's chord.

    Never raises and never guesses. An action a platform genuinely has no chord
    for comes back with keys=None plus a note saying what to do instead, and
    with a `route` naming the tool that does the job. macOS window-halving is
    the example: its chords are Globe(fn) plus an arrow, and macOS resolves fn
    below the event tap, so a synthetic event arrives with the flag stripped
    and nothing happens. Measured, so that it is not an excuse: posting Delete
    (keycode 51) with kCGEventFlagMaskSecondaryFn set left the text untouched,
    while plain ForwardDelete removed a character. Those rows therefore point
    at window_tile, which drives the same feature through the Window > Move &
    Resize menu. A misspelling comes back with suggestions.
    """
    return shortcuts.resolve(action, app=app, platform=platform)


@mcp.tool()
def shortcut_many(actions: list[str], app: str | None = None,
                  platform: str | None = None) -> list[dict]:
    """Resolve several chords in one round-trip. Same rules as `shortcut`."""
    return shortcuts.lookup_many(actions, app=app, platform=platform)


@mcp.tool()
def shortcut_list(app: str | None = None, platform: str | None = None,
                  group: str | None = None) -> list[dict]:
    """Every action that has a chord here, for finding out what is available.

    Filter with `group`: window, tabs, navigation, editing, text_motion, system,
    app. Read this before deciding a task needs the mouse. Includes chords
    harvested into the learned store for this app.
    """
    return shortcuts.list_actions(app=app, platform=platform, group=group)


@mcp.tool()
def shortcut_learn(app: str, force: bool = False) -> dict:
    """Harvest an app's menu-bar keyboard shortcuts and store them forever.

    Call on first use of an unfamiliar app (also runs automatically from
    focus_window). macOS reads AX menu key equivalents; Linux/Windows return
    a clear note until those harvesters land. Re-harvest is skipped for a week
    unless force=True.
    """
    from . import learned_shortcuts
    return learned_shortcuts.ensure_learned(app, force=force)


@mcp.tool()
def shortcut_learned(app: str | None = None,
                     platform: str | None = None) -> dict:
    """List durable learned chords (local + fleet mirrors)."""
    from . import learned_shortcuts
    rows = learned_shortcuts.list_for(app, platform)
    return {
        "ok": True,
        "count": len(rows),
        "path": str(learned_shortcuts.shortcuts_path()),
        "shortcuts": rows,
    }


@mcp.tool()
def cu_skill_recall(query: str, app: str = "", limit: int = 3) -> dict:
    """Short taught skills. Returns {ok,n,lines}. Call before inventing clicks."""
    from . import cu_memory, cu_skills
    return cu_memory.from_skills(cu_skills.recall(query, app=app, limit=limit, compact=True))


@mcp.tool()
def cu_skill_teach(title: str, steps: list[dict] | str, app: str = "",
                   platform: str = "", notes: str = "",
                   source: str = "session") -> dict:
    """Save one verified skill. steps=[{action,value}]. No secrets."""
    from . import cu_skills
    return cu_skills.teach(title, steps, app=app, platform=platform,
                           notes=notes, source=source)


@mcp.tool()
def cu_memory(op: str, q: str = "", items: list[str] | None = None) -> dict:
    """Tiny computer-use memory. Always {ok,op,n,lines}. Same shape for Qwen and frontier.

    op: status | working | tick | recall | fact | forget | history | board
    working: pass items=[...] checklist. tick: q=item text. fact: q='subj | pred | obj'.
    recall: q=query (skills + valid facts). forget: q=fact id or text.
    """
    from . import cu_memory as mem
    return mem.handle(op, q=q, items=items)


@mcp.tool()
def layout_focus(query: str, timeout_s: float = 6.0) -> dict:
    """Bring a window forward and CONFIRM it is frontmost before you type.

    Raising a window is a request, not a guarantee, so this polls the frontmost
    window until it matches or the timeout passes. Typing into whatever happened
    to be in front is the standard way this goes wrong, and `verified` in the
    reply is what tells you it did not.
    """
    return workspace_layout.focus_and_verify(query, timeout_s=timeout_s)


@mcp.tool()
def layout_full(query: str | None = None, native: bool = False) -> dict:
    """Give one window the whole working area -- the layout for a single task.

    Fills the visible frame, so the menu bar and Dock are respected and the task
    board stays reachable. `native=True` asks for real fullscreen instead, which
    hides both and hides the board with them; leave it off unless the task is
    watching video.
    """
    return workspace_layout.fullscreen(query, native=native)


@mcp.tool()
def layout_split(left: str, right: str, ratio: float = 0.5,
                 display: int | str | None = None) -> dict:
    """Two windows side by side -- the layout for comparing or referencing.

    `ratio` is the left window's share (0.6 gives it three fifths). Geometry is
    read back after the write rather than assumed: an app that enforces a
    minimum size comes back honored=False with the per-window delta, because a
    window that silently refused to shrink is something you need to know about.
    """
    return workspace_layout.split(left, right, ratio=ratio, display=display)


@mcp.tool()
def layout_stack(queries: list[str], display: int | str | None = None) -> dict:
    """Up to three windows in equal columns. Three is a hard cap, not a default.

    Narrower than a third of the working area stops being readable, which is the
    same reason a person stops at three. A fourth window is refused rather than
    squeezed in.
    """
    return workspace_layout.stack(queries, display=display)


@mcp.tool()
def layout_arrange(spec: dict) -> dict:
    """Apply a layout from one spec: {"layout": "full"|"split"|"stack", ...}.

    The declarative form of the three above -- describe the arrangement the task
    needs and let it place the windows.
    """
    return workspace_layout.arrange(spec)


def main() -> None:
    # Human Mode is the default, not an opt-in. A session that forgets to turn
    # it on drives a teleporting pointer across a screen someone is watching.
    # WORKMAN_HUMAN_MODE=0 opts out.
    human.apply_session_default()
    # Extension retries ws://127.0.0.1:8765/workman forever; be there on boot.
    # A bind failure must not take down the MCP server.
    try:
        bridge.start(port=8765)
    except Exception:
        pass
    mcp.run()


if __name__ == "__main__":
    main()
