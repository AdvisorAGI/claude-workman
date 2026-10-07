"""Draw-only :0 acceptance. No input, focus or device mutation calls.

The parent selects empty positions from a fresh screenshot before running.
Draw directly through the overlay object, never through the ghost daemon or
its demo loop: a daemon may also service pending polite restores in v3.
"""
from __future__ import annotations

import argparse
import ctypes as C
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]


def require(condition, detail):
    if not condition:
        raise AssertionError(detail)


def draw_probe(args):
    from workman import ghost_cursor, xtest
    ch = xtest.channel(":0")
    require(ch is not None and ch.display_name == ":0", "draw-only :0 query channel unavailable")
    width, height = ch.screen_size()
    positions = []
    for pair in args.positions:
        try:
            px, py = map(int, pair.split(","))
        except ValueError:
            raise SystemExit(f"Invalid empty-desktop position: {pair!r}")
        require(0 <= px < width and 0 <= py < height, {"position_outside_screen": [px, py]})
        positions.append((px, py))
    args.out.mkdir(parents=True, exist_ok=True)
    shape = C.CDLL("libXext.so.6")
    shape.XShapeGetRectangles.argtypes = [C.c_void_p, C.c_ulong, C.c_int,
                                         C.POINTER(C.c_int), C.POINTER(C.c_int)]
    shape.XShapeGetRectangles.restype = C.c_void_p
    ch.x11.XGetWMHints.argtypes = [C.c_void_p, C.c_ulong]
    ch.x11.XGetWMHints.restype = C.POINTER(ghost_cursor.XWMHints)
    reports = []
    overlay = ghost_cursor._Overlay(":0")
    try:
        for i, (px, py) in enumerate(positions):
            before_focus, before_pointer = ch.input_focus(), ch.pointer_position()
            overlay.draw((px, py), 1.0)  # Paint/move only this overlay window.
            overlay.x.XSync(overlay.dpy, 0)
            time.sleep(0.15)
            wid = overlay.window
            count, ordering = C.c_int(), C.c_int()
            rectangles = shape.XShapeGetRectangles(ch.dpy, wid, 2, C.byref(count), C.byref(ordering))
            if rectangles:
                ch.x11.XFree(rectangles)
            hints = ch.x11.XGetWMHints(ch.dpy, wid)
            require(bool(hints), "overlay has no WM input hints")
            accepts_focus = bool(hints.contents.input)
            input_hint_present = bool(hints.contents.flags & 1)
            ch.x11.XFree(hints)
            full = args.out / f"polite-ghost-{i+1}.png"
            full.write_bytes(ch.screenshot())
            crop = args.out / f"polite-ghost-{i+1}-crop.png"
            crop_x, crop_y = max(0, px - 8), max(0, py - 8)
            crop.write_bytes(ch.screenshot(region=(crop_x, crop_y, min(50, width - crop_x), min(50, height - crop_y))))
            # This subprocess only queries the exact overlay window we own.
            details = subprocess.run(["xwininfo", "-display", ":0", "-id", str(wid)],
                                     env=os.environ.copy(), capture_output=True, text=True, timeout=5)
            require(details.returncode == 0, {"xwininfo_exit": details.returncode, "stderr": details.stderr})
            override = "Override Redirect State: yes" in details.stdout
            clients = ch._property(ch.root, "_NET_CLIENT_LIST", "WINDOW") or b""
            stride = C.sizeof(C.c_ulong)
            listed = wid in [int.from_bytes(clients[k:k+stride], sys.byteorder)
                             for k in range(0, len(clients), stride)]
            after_focus, after_pointer = ch.input_focus(), ch.pointer_position()
            unchanged = (before_focus.get("window") == after_focus.get("window")
                         and all(before_pointer.get(key) == after_pointer.get(key) for key in ("x", "y", "buttons")))
            require(count.value == 0 and input_hint_present and not accepts_focus and override and not listed,
                    {"overlay_shape_or_hints_failed": wid})
            require(unchanged, {"draw_only_observation_changed": True, "focus_before": before_focus,
                                "focus_after": after_focus, "pointer_before": before_pointer, "pointer_after": after_pointer,
                                "note": "Owner activity during capture can also change these observations; do not restore it."})
            reports.append({"position": [px, py], "window": wid, "draw_path": "direct _Overlay; no daemon/demo",
                            "shape_input_rectangles": count.value, "accepts_focus": accepts_focus,
                            "input_hint_present": input_hint_present, "listed_as_managed_client": listed,
                            "override_redirect": override, "state_unchanged": unchanged,
                            "focus_before": before_focus, "focus_after": after_focus,
                            "pointer_before": before_pointer, "pointer_after": after_pointer,
                            "screenshot": str(full), "crop": str(crop)})
            overlay.hide()
            overlay.x.XSync(overlay.dpy, 0)
    finally:
        overlay.close()  # Only this probe's overlay window and drawing resources.
        ch.close()
        print(json.dumps(reports, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--positions", nargs="+", required=True, help="parent-selected empty desktop x,y pairs")
    args = parser.parse_args()
    sys.path.insert(0, str(REPO))
    # Independent roots further protect any future drawing initialization from
    # consuming live owner snapshots. No polite action or scheduler is called.
    with tempfile.TemporaryDirectory(prefix="workman-overlay-probe-") as directory:
        root = Path(directory)
        for suffix in ("learn", "state", "runtime"):
            (root / suffix).mkdir(mode=0o700)
        os.environ.update(DISPLAY=":0", WORKMAN_DISPLAY=":0", WORKMAN_XTEST="1", WORKMAN_POLITE="0",
                          WORKMAN_LEARN_ROOT=str(root / "learn"), WORKMAN_STATE_DIR=str(root / "state"),
                          XDG_RUNTIME_DIR=str(root / "runtime"), WORKMAN_CURSOR_FIFO=str(root / "cursor.fifo"))
        draw_probe(args)


if __name__ == "__main__":
    main()
