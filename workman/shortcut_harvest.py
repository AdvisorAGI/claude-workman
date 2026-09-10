"""Harvest an application's menu-bar keyboard shortcuts into learned rows.

macOS: walk the AX menu bar of a running app and read AXMenuItemCmdChar /
AXMenuItemCmdModifiers / AXMenuItemCmdVirtualKey. That is the same data a
person sees next to each menu item.

Linux / Windows: honest stubs for now — return ok=False with a route note so
a session falls back to the static table and can still ``computer_remember``
a chord it discovers by hand. The computer-use lab yellow zone can fill those
later from vendor docs without pretending a harvest happened.
"""
from __future__ import annotations

import re
import sys
from typing import Any

# AXMenuItemCmdModifiers bits (AppKit / HIServices).
_AX_SHIFT = 1
_AX_OPTION = 2
_AX_CONTROL = 4
_AX_NO_COMMAND = 8

_VIRTUAL = {
    36: "Return", 48: "Tab", 49: "space", 51: "BackSpace", 53: "Escape",
    76: "Return", 115: "Home", 116: "Page_Up", 117: "Delete", 119: "End",
    121: "Page_Down", 123: "Left", 124: "Right", 125: "Down", 126: "Up",
}

_SKIP_TITLES = {
    "", "apple", "emoji & symbols", "enter full screen", "exit full screen",
}


def _mods_to_prefix(modifiers: int) -> str:
    parts: list[str] = []
    # Command is ON unless NoCommand is set — that is how macOS menus work.
    if not (modifiers & _AX_NO_COMMAND):
        parts.append("super")
    if modifiers & _AX_CONTROL:
        parts.append("ctrl")
    if modifiers & _AX_OPTION:
        parts.append("alt")
    if modifiers & _AX_SHIFT:
        parts.append("shift")
    return "+".join(parts)


def _char_to_key(ch: str) -> str | None:
    if not ch:
        return None
    if len(ch) == 1:
        if ch.isalpha():
            return ch.lower()
        if ch.isdigit():
            return ch
        # Common unshifted punctuation on ANSI US.
        punct = {
            " ": "space", ",": "comma", ".": "period", "/": "slash",
            ";": "semicolon", "'": "apostrophe", "[": "bracketleft",
            "]": "bracketright", "\\": "backslash", "`": "grave",
            "-": "minus", "=": "equal",
        }
        return punct.get(ch) or (ch if ch.isalnum() else None)
    return None


def chord_from_ax(cmd_char: Any, modifiers: Any, virtual_key: Any) -> str | None:
    """Build an xdotool chord from AX menu attributes, or None if none."""
    try:
        mods = int(modifiers or 0)
    except (TypeError, ValueError):
        mods = 0
    prefix = _mods_to_prefix(mods)
    key = None
    try:
        vk = int(virtual_key) if virtual_key not in (None, "") else None
    except (TypeError, ValueError):
        vk = None
    if vk is not None and vk in _VIRTUAL:
        key = _VIRTUAL[vk]
    if key is None and cmd_char not in (None, ""):
        key = _char_to_key(str(cmd_char))
    if not key:
        return None
    return f"{prefix}+{key}" if prefix else key


def _slug_action(label: str, menu_path: list[str]) -> str:
    from .learned_shortcuts import slug

    # Prefer the leaf label; fall back to joined path.
    leaf = label or (menu_path[-1] if menu_path else "")
    # Strip trailing ellipsis / shortcut decoration.
    leaf = re.sub(r"[\.…]+$", "", leaf).strip()
    leaf = re.sub(r"\s+", " ", leaf)
    return slug(leaf)


def _daemon_harvest(app: str) -> dict | None:
    """Prefer Workman.app (holds Accessibility). None if the daemon is down."""
    import json
    import socket
    from pathlib import Path

    sock_path = Path.home() / "Library/Application Support/Workman/workman.sock"
    if not sock_path.exists():
        return None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(60.0)
        s.connect(str(sock_path))
        s.sendall(
            (json.dumps({"id": 1, "op": "harvest_menu_shortcuts",
                         "args": {"app": app}}) + "\n").encode("utf-8")
        )
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(1 << 20)
            if not chunk:
                break
            buf += chunk
        s.close()
    except OSError:
        return None
    if not buf:
        return None
    try:
        resp = json.loads(buf.decode("utf-8").split("\n", 1)[0])
    except ValueError:
        return None
    if not resp.get("ok"):
        # Unknown op on an old daemon — fall through to in-process AX.
        err = str(resp.get("error") or "")
        if "unknown op" in err:
            return None
        return {"ok": False, "error": err or "daemon harvest failed", "entries": []}
    result = resp.get("result") or {}
    if not isinstance(result, dict):
        return None
    # Enrich with app_key so the learned store stays stable.
    from . import learned_shortcuts as ls

    entries = []
    for row in result.get("entries") or []:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        item.setdefault("app_key", ls.app_key(str(item.get("app") or app)))
        entries.append(item)
    out = dict(result)
    out["entries"] = entries
    out["count"] = len(entries)
    out.setdefault("via", "workman-daemon")
    return out


def harvest_darwin(app: str) -> dict:
    via_daemon = _daemon_harvest(app)
    if via_daemon is not None:
        return via_daemon
    try:
        from .platform import ax_darwin
    except Exception as exc:
        return {"ok": False, "error": f"ax_darwin unavailable: {exc}", "entries": []}
    state = ax_darwin.available()
    if not state.get("ok"):
        return {"ok": False, "error": state.get("error") or "AX unavailable",
                "hint": "grant Accessibility to Workman.app (daemon), then "
                        "python -m atmos_computer.install --restart",
                "entries": []}
    api = ax_darwin._api()
    element, name = ax_darwin._app_element(app)
    if element is None:
        return {"ok": False, "error": f"no running app matching {app!r}",
                "entries": []}

    menu_bar = ax_darwin._attr(element, api.kAXMenuBarAttribute)
    if menu_bar is None:
        # Some apps expose the menu bar as a child role.
        for child in ax_darwin._attr(element, api.kAXChildrenAttribute) or []:
            if str(ax_darwin._attr(child, api.kAXRoleAttribute) or "") == "AXMenuBar":
                menu_bar = child
                break
    if menu_bar is None:
        return {"ok": False, "error": f"{name or app} has no AX menu bar",
                "entries": [], "app": name or app}

    entries: list[dict] = []
    seen: set[str] = set()

    def walk(node, path: list[str], depth: int = 0) -> None:
        if depth > 8 or len(entries) >= 400:
            return
        children = ax_darwin._attr(node, api.kAXChildrenAttribute) or []
        for child in children:
            role = str(ax_darwin._attr(child, api.kAXRoleAttribute) or "")
            title = str(
                ax_darwin._attr(child, api.kAXTitleAttribute)
                or ax_darwin._attr(child, api.kAXDescriptionAttribute)
                or ""
            ).strip()
            if role == "AXMenuBarItem":
                if title.lower() in _SKIP_TITLES or title == "Apple":
                    # Still walk Apple menu? Skip — system stuff.
                    continue
                menu = ax_darwin._attr(child, api.kAXChildrenAttribute) or []
                # Menu bar item's child is usually the AXMenu.
                for m in menu:
                    walk(m, path + [title], depth + 1)
                continue
            if role == "AXMenu":
                walk(child, path, depth + 1)
                continue
            if role != "AXMenuItem":
                walk(child, path, depth + 1)
                continue
            if not title or title.lower() in _SKIP_TITLES or title == "-":
                continue
            cmd_char = ax_darwin._attr(child, "AXMenuItemCmdChar")
            cmd_mods = ax_darwin._attr(child, "AXMenuItemCmdModifiers")
            cmd_vk = ax_darwin._attr(child, "AXMenuItemCmdVirtualKey")
            keys = chord_from_ax(cmd_char, cmd_mods, cmd_vk)
            if not keys:
                # Descend into submenus even when the parent has no chord.
                walk(child, path + [title], depth + 1)
                continue
            menu_path = path + [title]
            action = _slug_action(title, menu_path)
            if action in seen:
                walk(child, menu_path, depth + 1)
                continue
            seen.add(action)
            from . import learned_shortcuts as ls

            entries.append({
                "app": name or app,
                "app_key": ls.app_key(name or app),
                "platform": "darwin",
                "action": action,
                "label": title,
                "menu_path": menu_path,
                "keys": keys,
                "source": "harvest-ax",
            })
            walk(child, menu_path, depth + 1)

    walk(menu_bar, [])
    return {
        "ok": True,
        "app": name or app,
        "platform": "darwin",
        "entries": entries,
        "count": len(entries),
        "via": "ax-menu-bar",
        "source": "harvest-ax",
    }


def harvest_linux(app: str) -> dict:
    return {
        "ok": False,
        "error": "menu-bar harvest is not implemented on Linux yet",
        "hint": "teach chords with shortcut_learn after discovering them, "
                "or let the computer-use lab yellow zone propose from docs",
        "app": app,
        "platform": "linux",
        "entries": [],
    }


def harvest_win32(app: str) -> dict:
    return {
        "ok": False,
        "error": "menu-bar harvest is not implemented on Windows yet",
        "hint": "teach chords with shortcut_learn after discovering them, "
                "or let the computer-use lab yellow zone propose from docs",
        "app": app,
        "platform": "win32",
        "entries": [],
    }


def harvest(app: str, platform: str | None = None) -> dict:
    from . import shortcuts as sc

    where = sc._norm(platform, sc._PLATFORM_ALIASES) or sc.detect_platform()
    if where == "darwin":
        return harvest_darwin(app)
    if where == "linux":
        return harvest_linux(app)
    if where == "win32":
        return harvest_win32(app)
    return {"ok": False, "error": f"unknown platform {where!r}", "entries": []}
