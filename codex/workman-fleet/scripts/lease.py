"""Short per-desktop input leases shared by independent Workman sessions."""
import contextlib
import fcntl
import json
import os
import time

import learning

INPUT = frozenset({"focus", "move", "click", "type", "paste", "key", "scroll", "drag", "minimize"})


class Busy(Exception):
    pass


def path():
    return learning.journal_path().parent / "fleet-input-lease.json"


def state():
    try:
        r = json.loads(path().read_text())
        if isinstance(r.get("owner"), str) and type(r.get("expires")) in (int, float) and r["expires"] > time.time():
            return r
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def status(owner):
    s = state()
    return {"available": not s or s["owner"] == owner,
            "owned_by_this_session": s.get("owner") == owner,
            "remaining_seconds": max(0, round(s.get("expires", 0) - time.time())),
            "scope": "Workman Fleet v1.1 clients; legacy direct tools do not participate"}


@contextlib.contextmanager
def hold(owner, action, seconds=120):
    """Hold operation lock through actual input; reserve the desktop between calls.

    Reads do not acquire input ownership. Separate devices use separate files.
    A release from another session is refused, even if its caller has full access.
    """
    if action not in INPUT | {"reserve", "release", "finish"}:
        yield status(owner)
        return
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.with_suffix(".lock").open("a") as f:
        os.chmod(f.name, 0o600)
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy() from None
        s = state()
        if s and s["owner"] != owner:
            raise Busy()
        updated = {} if action in ("release", "finish") else {"owner": owner, "expires": time.time() + seconds}
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(updated))
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
        yield status(owner)
