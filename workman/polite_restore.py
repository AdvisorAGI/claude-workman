"""One isolated debounce timer per display, independent of the drawing overlay."""
from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time

from . import polite_state

_CHILDREN = []


def ensure(display: str) -> dict:
    """On-demand timer. Does not open X or inject in the calling process."""
    _, action_lock = polite_state.paths(display)
    fd = os.open(str(action_lock) + ".timer", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"ok": True, "timer": "running"}
        root = Path(__file__).resolve().parent.parent
        env = os.environ.copy()
        env["WORKMAN_DISPLAY"] = env["DISPLAY"] = display
        code = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
                "from workman.polite_restore import main; raise SystemExit(main())")
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(root), "--display", display, "--lock-fd", str(fd)],
            cwd=str(root), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, pass_fds=(fd,), start_new_session=True)
        _CHILDREN[:] = [p for p in _CHILDREN if p.poll() is None]
        _CHILDREN.append(child)
        return {"ok": True, "timer": "started", "pid": child.pid}
    finally:
        # The child inherits this locked open-file description.
        os.close(fd)


def serve(display: str, timer_fd: int | None = None) -> None:
    from . import polite, x11
    from .platform import linux_x11
    x11.DISPLAY = display  # This timer process owns only the explicitly named display.
    idle_since = time.monotonic()
    while True:
        with polite_state.locked(display, blocking=False) as store:
            if store is None:
                wait = 0.1
            elif store.data.get("snapshot"):
                due = store.data.get("due")
                wait = max(0.01, min(1.0, (due or time.monotonic()) - time.monotonic()))
                idle_since = time.monotonic()
            else:
                wait = 1.0
                if time.monotonic() - idle_since >= 60:
                    # ensure() runs under this same action lock. Publish the
                    # timer's exit before a new action can arm another snapshot.
                    if timer_fd is not None:
                        fcntl.flock(timer_fd, fcntl.LOCK_UN)
                    return
        # restore_pending takes the lock anew and checks the latest deadline.
        # No X reads before the deadline, and no restore while an action owns it.
        if store is not None and store.data.get("snapshot"):
            if due is not None and time.monotonic() >= due:
                polite.restore_pending(linux_x11)
                wait = 0.2
            elif due is None:
                # An action that crashed left an unarmed snapshot. Discard it
                # through the same verified stale-state path.
                polite.restore_pending(linux_x11)
                wait = 0.2
        time.sleep(wait)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Private Workman polite restore timer")
    parser.add_argument("--display", required=True)
    parser.add_argument("--lock-fd", required=True, type=int)
    args = parser.parse_args(argv)
    _, action_lock = polite_state.paths(args.display)
    try:
        held = os.fstat(args.lock_fd)
        entry = os.stat(str(action_lock) + ".timer", follow_symlinks=False)
        if (held.st_dev, held.st_ino, held.st_uid) != (entry.st_dev, entry.st_ino, os.getuid()):
            return 1
        fcntl.flock(args.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        serve(args.display, args.lock_fd)
        return 0
    finally:
        os.close(args.lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
