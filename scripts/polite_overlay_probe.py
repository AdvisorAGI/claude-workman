"""Draw-only :0 acceptance. No pointer, key, focus, or device mutation calls.

Choose empty desktop positions from a fresh screenshot before invoking. This
probe creates only the permitted overlay, queries X state, and captures PNGs.
"""
from __future__ import annotations

import argparse
import ctypes as C
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--positions", nargs="+", required=True, help="empty desktop x,y pairs")
    args = parser.parse_args()
    from workman import ghost_cursor, xtest
    ch = xtest.channel(":0")
    assert ch is not None and ch.display_name == ":0"
    args.out.mkdir(parents=True, exist_ok=True)
    shape = C.CDLL("libXext.so.6")
    shape.XShapeGetRectangles.argtypes = [C.c_void_p, C.c_ulong, C.c_int,
                                         C.POINTER(C.c_int), C.POINTER(C.c_int)]
    shape.XShapeGetRectangles.restype = C.c_void_p
    ch.x11.XGetWMHints.argtypes = [C.c_void_p, C.c_ulong]
    ch.x11.XGetWMHints.restype = C.POINTER(ghost_cursor.XWMHints)
    reports = []
    env = dict(os.environ, DISPLAY=":0", WORKMAN_DISPLAY=":0")
    for i, pair in enumerate(args.positions):
        px, py = map(int, pair.split(","))
        before_focus, before_pointer = ch.input_focus(), ch.pointer_position()
        proc = subprocess.Popen([sys.executable, "-m", "workman.ghost_cursor",
                                 "--display", ":0", "--demo", str(px), str(py), "--timeout", "2.6"],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            wid = None
            for _ in range(50):
                found = subprocess.run(["xdotool", "search", "--name", "^Workman steel ghost$"],
                                       env=env, capture_output=True, text=True)
                if found.returncode == 0 and found.stdout.strip():
                    wid = int(found.stdout.strip().splitlines()[0])
                    break
                if proc.poll() is not None:
                    break
                time.sleep(0.02)
            assert wid is not None, proc.communicate(timeout=5)
            time.sleep(0.10)
            n, ordering = C.c_int(), C.c_int()
            rectangles = shape.XShapeGetRectangles(ch.dpy, wid, 2, C.byref(n), C.byref(ordering))
            if rectangles:
                ch.x11.XFree(rectangles)
            hints = ch.x11.XGetWMHints(ch.dpy, wid)
            accepts_focus = bool(hints and hints.contents.input)
            if hints:
                ch.x11.XFree(hints)
            full = args.out / f"polite-ghost-{i+1}.png"
            full.write_bytes(ch.screenshot())
            crop = args.out / f"polite-ghost-{i+1}-crop.png"
            crop.write_bytes(ch.screenshot(region=(max(0, px-8), max(0, py-8), 50, 50)))
            details = subprocess.run(["xwininfo", "-id", str(wid)], env=env,
                                     capture_output=True, text=True)
            override = "Override Redirect State: yes" in details.stdout
            after_focus, after_pointer = ch.input_focus(), ch.pointer_position()
            clients = ch._property(ch.root, "_NET_CLIENT_LIST", "WINDOW") or b""
            stride = C.sizeof(C.c_ulong)
            listed = wid in [int.from_bytes(clients[k:k+stride], sys.byteorder)
                             for k in range(0, len(clients), stride)]
            assert n.value == 0 and not accepts_focus and override and not listed
            stdout, stderr = proc.communicate(timeout=5)
            assert proc.returncode == 0, stderr
            reports.append({"position": [px, py], "window": wid,
                            "shape_input_rectangles": n.value, "accepts_focus": accepts_focus,
                            "listed_as_managed_client": listed,
                            "override_redirect": override, "exit": proc.returncode,
                            "focus_before": before_focus, "focus_after": after_focus,
                            "pointer_before": before_pointer, "pointer_after": after_pointer,
                            "screenshot": str(full), "crop": str(crop)})
        finally:
            if proc.poll() is None:
                proc.terminate()  # only our own bounded overlay process
                proc.wait(timeout=5)
    print(json.dumps(reports, indent=2))


if __name__ == "__main__":
    main()
