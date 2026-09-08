"""Small per-call motion choices; reuse Workman's existing planners."""
import math
import time

import input_switch


def duration_ms(start, end, speed):
    distance = math.dist(start, end)
    seconds = max(.12, min(1.6, .10 + .075 * math.log2(distance / 24 + 1)))
    return round(max(.03, min(6.4, seconds / speed)) * 1000)


def linux_move(start, end, speed, post, planner=None):
    if planner is None:
        from workman.human import eased_path
        planner = eased_path
    path = planner(*start, *end)
    # Preserve a smooth route, but final accuracy takes precedence over jitter.
    path[-1] = (end[0], end[1], path[-1][2])
    t0 = time.monotonic()
    for x, y, ms in path:
        due = t0 + ms / (1000 * speed)
        while due > time.monotonic():
            input_switch.check("move")
            time.sleep(min(.02, due - time.monotonic()))
        input_switch.check("move")
        post(x, y)
    return {"at": list(end), "motion": "human", "speed": speed,
            "steps": len(path), "duration_ms": round((time.monotonic() - t0) * 1000)}
