"""Visual click cursor — a Codex-style overlay so a human can SEE what Workman
is doing on screen.

Codex's Computer Use shows a labelled cursor and a click animation ("ChatGPT is
using your computer", accent #339cff). This is the Linux/X11 equivalent: a
transparent, always-on-top, CLICK-THROUGH overlay window that draws a cursor ring
following the action point and an expanding ripple on each click. It exists for
oversight — you can watch, and interrupt, exactly where the automation acts.

Run the daemon:   python -m workman.cursor
It reads newline events from a FIFO (default ~/.cache/workman/cursor.fifo):
    move X Y
    click X Y
    label TEXT
    quit
The x11 backend writes those events; see x11.emit_cursor().
"""
from __future__ import annotations
import math
import os
import time

FIFO = os.environ.get("WORKMAN_CURSOR_FIFO", os.path.expanduser("~/.cache/workman/cursor.fifo"))
ACCENT = (0.20, 0.61, 1.0)  # #339cff, Codex-like cyan-blue


def fifo_path() -> str:
    """Create the control FIFO owner-only (0600). Anything that can write here can
    drive the overlay, so it must not be group- or world-writable."""
    d = os.path.dirname(FIFO)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    if not os.path.exists(FIFO):
        os.mkfifo(FIFO, 0o600)
    os.chmod(FIFO, 0o600)
    return FIFO


def run() -> None:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk  # noqa: F401  (Gdk pinned to 3.0)

    path = fifo_path()

    state = {"x": -100, "y": -100, "label": "Workman", "ripples": []}  # ripples: [(x,y,t0)]

    win = Gtk.Window(type=Gtk.WindowType.POPUP)
    win.set_app_paintable(True)
    win.set_decorated(False)
    win.set_keep_above(True)
    win.set_accept_focus(False)
    win.set_skip_taskbar_hint(True)
    win.set_skip_pager_hint(True)
    screen = win.get_screen()
    win.set_visual(screen.get_rgba_visual())
    win.set_default_size(screen.get_width(), screen.get_height())
    win.move(0, 0)

    def on_realize(w):
        # make the whole overlay click-through: empty input region
        try:
            region = w.get_window().create_region() if False else None
        except Exception:
            region = None
        try:
            import cairo
            empty = cairo.Region()
            w.get_window().input_shape_combine_region(empty, 0, 0)
        except Exception:
            pass
    win.connect("realize", on_realize)

    def draw(widget, cr):
        cr.set_operator(1)  # SOURCE — clear
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(2)  # OVER
        now = time.monotonic()
        # expanding click ripples
        alive = []
        for (rx, ry, t0) in state["ripples"]:
            age = now - t0
            if age > 0.6:
                continue
            alive.append((rx, ry, t0))
            r = 8 + age * 60
            a = max(0.0, 0.6 * (1 - age / 0.6))
            cr.set_source_rgba(*ACCENT, a)
            cr.set_line_width(3)
            cr.arc(rx, ry, r, 0, 2 * math.pi)
            cr.stroke()
        state["ripples"] = alive
        # cursor ring + dot at current point
        x, y = state["x"], state["y"]
        cr.set_source_rgba(*ACCENT, 0.95)
        cr.set_line_width(2.5)
        cr.arc(x, y, 11, 0, 2 * math.pi)
        cr.stroke()
        cr.set_source_rgba(*ACCENT, 0.9)
        cr.arc(x, y, 3, 0, 2 * math.pi)
        cr.fill()
        # label pill
        label = state["label"]
        if label:
            cr.select_font_face("sans-serif")
            cr.set_font_size(13)
            ext = cr.text_extents(label)
            pad, lx, ly = 7, x + 16, y - 6
            cr.set_source_rgba(0, 0, 0, 0.55)
            cr.rectangle(lx - pad, ly - ext.height - pad, ext.width + 2 * pad, ext.height + 2 * pad)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.95)
            cr.move_to(lx, ly)
            cr.show_text(label)
        return False
    win.connect("draw", draw)

    # non-blocking FIFO reader
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    buf = {"data": b""}

    def pump():
        try:
            chunk = os.read(fd, 4096)
            if chunk:
                buf["data"] += chunk
        except (BlockingIOError, OSError):
            pass
        while b"\n" in buf["data"]:
            line, buf["data"] = buf["data"].split(b"\n", 1)
            parts = line.decode("utf-8", "ignore").split()
            if not parts:
                continue
            cmd = parts[0]
            if cmd == "quit":
                Gtk.main_quit()
                return False
            if cmd in ("move", "click") and len(parts) >= 3:
                try:
                    state["x"], state["y"] = int(parts[1]), int(parts[2])
                except ValueError:
                    continue
                if cmd == "click":
                    state["ripples"].append((state["x"], state["y"], time.monotonic()))
            elif cmd == "label":
                state["label"] = " ".join(parts[1:])
        win.queue_draw()
        return True

    GLib.timeout_add(33, pump)   # ~30fps redraw/poll
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    run()
