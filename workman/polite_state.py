"""Per-display polite transactions. No X connection or input on import/read."""
from __future__ import annotations

from contextlib import contextmanager
try:
    import fcntl
except ImportError:  # Non-X11 imports remain usable.
    fcntl = None
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


def display_key(display: str) -> str:
    display = re.sub(r"^unix/?(?=:\d)", "", display)
    return re.sub(r"(:\d+)\.0$", r"\1", display)


def paths(display: str) -> tuple[Path, Path]:
    root = Path(os.environ.get("WORKMAN_STATE_DIR", "~/.local/state/workman")).expanduser()
    key = hashlib.sha256(display_key(display).encode()).hexdigest()[:20]
    return root / f"polite-{key}.json", root / f"polite-{key}.lock"


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


class Store:
    def __init__(self, display: str):
        self.display = display_key(display)
        self.path, self.lock_path = paths(display)
        self.data = {}

    def read(self):
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            data = {}
        if not isinstance(data, dict):
            raise ValueError("invalid polite state")
        if data and (data.get("version") != 1 or data.get("display") != self.display):
            raise ValueError("invalid polite display state")
        # A monotonic deadline/snapshot from a previous boot is never reusable.
        if data.get("boot_id") != boot_id():
            data = {}
        self.data = data
        return data

    def write(self):
        self.data.update(version=1, display=self.display, boot_id=boot_id())
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".polite-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(self.data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


@contextmanager
def locked(display: str, *, blocking: bool = True):
    """All action injections and deferred restores hold this same flock."""
    if fcntl is None:
        raise OSError("polite transactions require Linux flock")
    store = Store(display)
    store.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(store.lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield None
            return
        store.read()
        yield store
    finally:
        os.close(fd)
