#!/usr/bin/env python3
"""Measure what one human_move, one human_type and one screenshot cost.

Reports wall time, CPU time (this process plus every child it spawned) and
the number of processes spawned, per action. Run it twice, once with the
xdotool path forced (WORKMAN_XTEST=0) and once with the XTest channel, and
compare:

    WORKMAN_DISPLAY=:99 WORKMAN_XTEST=0 python scripts/bench_lite.py   # before
    WORKMAN_DISPLAY=:99 python scripts/bench_lite.py                   # after

Point it at an agent screen (Xvfb :99) so nothing a person is using moves.
Typing goes to whatever has focus on that display, so it is only run on a
display that is not the owner's.
"""
from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("WORKMAN_DISPLAY", ":99")
os.environ["DISPLAY"] = os.environ["WORKMAN_DISPLAY"]
os.environ.pop("XAUTHORITY", None)

from workman import desktop, human  # noqa: E402

#: Exactly 100 characters: letters, capitals, digits, punctuation, spaces.
TEXT_100 = ("The quick brown fox jumps over the lazy dog. Pack my box with five "
            "dozen liquor jugs, 0123456789 OK!")
assert len(TEXT_100) == 100, len(TEXT_100)

SPAWNS = {"n": 0}
_popen = subprocess.Popen


class _CountingPopen(_popen):
    def __init__(self, *a, **kw):
        SPAWNS["n"] += 1
        super().__init__(*a, **kw)


subprocess.Popen = _CountingPopen  # subprocess.run goes through Popen


def _cpu() -> float:
    me = resource.getrusage(resource.RUSAGE_SELF)
    kids = resource.getrusage(resource.RUSAGE_CHILDREN)
    return me.ru_utime + me.ru_stime + kids.ru_utime + kids.ru_stime


def measure(label: str, fn) -> dict:
    SPAWNS["n"] = 0
    cpu0, t0 = _cpu(), time.perf_counter()
    out = fn()
    wall, cpu = time.perf_counter() - t0, _cpu() - cpu0
    row = {"action": label, "wall_ms": round(wall * 1000, 1),
           "cpu_ms": round(cpu * 1000, 1), "spawns": SPAWNS["n"]}
    if isinstance(out, dict) and out.get("ok") is False:
        row["error"] = out.get("error")
    if isinstance(out, (bytes, bytearray)):
        row["bytes"] = len(out)
    return row


def main() -> int:
    human.set_mode(True, seed=7)
    x0, y0 = 200, 200
    desktop.move(x0, y0)
    rows = [
        measure("human_move 600px", lambda: human.human_move(x0 + 600, y0)),
        measure("human_type 100 chars", lambda: human.human_type(TEXT_100)),
        measure("screenshot", lambda: desktop.screenshot()),
        measure("screenshot view (max 1568)", lambda: desktop.screenshot(max_dim=1568)),
    ]
    info = desktop.platform_info()
    print(json.dumps({"channel": info.get("input_channel", "xdotool"),
                      "display": os.environ["DISPLAY"], "rows": rows}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
