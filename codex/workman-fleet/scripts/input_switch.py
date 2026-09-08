"""User-operated mouse/keyboard automation switches; never disable physical input.

Independent of the desktop lease, so STOP works while another process has input.
No listener, event hook, global shortcut, privacy change or background monitoring.
"""
import json
import os

import learning


class Disabled(Exception):
    pass


def path():
    return learning.journal_path().parent / "fleet-input-switch.json"


def state():
    try:
        r = json.loads(path().read_text())
        if type(r.get("mouse")) is bool and type(r.get("keyboard")) is bool:
            return {k: r[k] for k in ("mouse", "keyboard")}
    except FileNotFoundError:
        return {"mouse": True, "keyboard": True}
    except (OSError, ValueError, AttributeError):
        pass
    # Corrupt state must not silently enable automation.
    return {"mouse": False, "keyboard": False}


def update(**changes):
    if not changes or set(changes) - {"mouse", "keyboard"} or any(type(v) is not bool for v in changes.values()):
        raise ValueError("switches must be booleans")
    p = path()
    with learning.lock(p.with_suffix(".switch-lock")):
        current = dict(state(), **changes)
        temp = p.with_suffix(".switch-new")
        with temp.open("w") as f:
            os.chmod(temp, 0o600)
            json.dump(current, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, p)
    return current


def check(action):
    s = state()
    required = {"type": ("keyboard",), "paste": ("keyboard",), "key": ("keyboard",),
                "move": ("mouse",), "click": ("mouse",), "drag": ("mouse",),
                "scroll": ("mouse",), "focus": ("mouse", "keyboard"),
                "minimize": ("mouse", "keyboard")}.get(action, ())
    if any(not s[k] for k in required):
        raise Disabled()
    return s
