#!/usr/bin/env python3
"""atspi-dump.py — enumerate top-level windows and actionable elements via AT-SPI2.

Prints:  role | name | x,y wxh   for buttons, text fields, links, etc.

Backends (auto-selected):
  1. pyatspi            (apt: python3-pyatspi)
  2. gi.repository.Atspi (apt: gir1.2-atspi-2.0 + python3-gi)  <- present on this DGX

Requires the AT-SPI bus to be running (at-spi-bus-launcher / at-spi2-registryd)
and, for full app coverage, GSETTING toolkit-accessibility=true:
  gsettings set org.gnome.desktop.interface toolkit-accessibility true
Electron/Chromium apps additionally need ACCESSIBILITY_ENABLED=1 /
--force-renderer-accessibility to export their tree.
"""
import sys

ACTIONABLE = {
    "push button", "toggle button", "check box", "radio button", "combo box",
    "text", "entry", "password text", "link", "menu item", "check menu item",
    "radio menu item", "spin button", "slider", "tab", "list item",
    "tree item", "icon", "button",
}

MAX_DEPTH = 14


def use_gi():
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    def get_extents(acc):
        try:
            comp = acc.get_component_iface() if hasattr(acc, "get_component_iface") else None
            if comp is None:
                comp = acc  # Atspi.Accessible implements component calls directly
            r = Atspi.Component.get_extents(acc, Atspi.CoordType.SCREEN)
            return r.x, r.y, r.width, r.height
        except Exception:
            return None

    def walk(acc, depth, out):
        if depth > MAX_DEPTH:
            return
        try:
            n = acc.get_child_count()
        except Exception:
            return
        for i in range(n):
            try:
                child = acc.get_child_at_index(i)
                if child is None:
                    continue
                role = child.get_role_name()
                name = child.get_name() or ""
                if role in ACTIONABLE:
                    ext = get_extents(child)
                    if ext and ext[2] > 0 and ext[3] > 0:
                        x, y, w, h = ext
                        out.append(f"    {role} | {name!r} | {x},{y} {w}x{h}")
                walk(child, depth + 1, out)
            except Exception:
                continue

    desktop = Atspi.get_desktop(0)
    n_apps = desktop.get_child_count()
    print(f"[gi.Atspi backend] desktop has {n_apps} registered application(s)\n")
    for i in range(n_apps):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue
        try:
            app_name = app.get_name() or "<unnamed app>"
        except Exception:
            continue
        for j in range(app.get_child_count()):
            try:
                win = app.get_child_at_index(j)
                if win is None:
                    continue
                wrole = win.get_role_name()
                wname = win.get_name() or "<untitled>"
            except Exception:
                continue
            print(f"WINDOW [{app_name}] {wrole} | {wname!r}")
            items = []
            walk(win, 0, items)
            if items:
                print("\n".join(items))
            else:
                print("    (no actionable elements exported)")
            print()


def use_pyatspi():
    import pyatspi

    def walk(acc, depth, out):
        if depth > MAX_DEPTH:
            return
        for child in acc:
            if child is None:
                continue
            try:
                role = child.getRoleName()
                name = child.name or ""
                if role in ACTIONABLE:
                    comp = child.queryComponent()
                    x, y = comp.getPosition(pyatspi.DESKTOP_COORDS)
                    w, h = comp.getSize()
                    if w > 0 and h > 0:
                        out.append(f"    {role} | {name!r} | {x},{y} {w}x{h}")
                walk(child, depth + 1, out)
            except Exception:
                continue

    desktop = pyatspi.Registry.getDesktop(0)
    print(f"[pyatspi backend] desktop has {desktop.childCount} registered application(s)\n")
    for app in desktop:
        if app is None:
            continue
        for win in app:
            if win is None:
                continue
            try:
                print(f"WINDOW [{app.name}] {win.getRoleName()} | {win.name!r}")
            except Exception:
                continue
            items = []
            walk(win, 0, items)
            print("\n".join(items) if items else "    (no actionable elements exported)")
            print()


def main():
    try:
        use_pyatspi()
        return
    except ImportError:
        pass
    try:
        use_gi()
        return
    except ImportError:
        pass
    sys.exit(
        "No AT-SPI Python binding available.\n"
        "Install one of:\n"
        "  sudo apt install python3-pyatspi          # pyatspi backend\n"
        "  sudo apt install python3-gi gir1.2-atspi-2.0   # gi backend\n"
        "Also enable app-side a11y export:\n"
        "  gsettings set org.gnome.desktop.interface toolkit-accessibility true"
    )


if __name__ == "__main__":
    main()
