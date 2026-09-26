"""Verified, reversible Cocoa notification fix for the existing macOS helper.

Run with the configured helper Python and source path. Never rebuilds Workman.app,
changes grants or restarts a service itself. Backups remain beside fleet support.
"""
import hashlib
import json
import os
import sys
import time
from pathlib import Path

MARKER = "# Workman fleet v1.1: pump Cocoa activation notifications."
BEFORE = "        while not _stop_flag.is_set():\n            try:\n"
AFTER = """        while not _stop_flag.is_set():
            # Workman fleet v1.1: pump Cocoa activation notifications.
            from Foundation import NSDate, NSRunLoop
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.02))
            try:
"""
BEFORE_REPLY = "                    conn.sendall(_handle(line))"
AFTER_REPLY = """                    reply = _handle(line)
                    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
                    conn.sendall(reply)"""


def patched(text):
    if MARKER in text:
        return text
    if text.count(BEFORE) != 1 or text.count(BEFORE_REPLY) != 1:
        raise ValueError("unrecognized helper source; inspect before patching")
    return text.replace(BEFORE, AFTER).replace(BEFORE_REPLY, AFTER_REPLY)


def main():
    path = Path(sys.argv[1]).resolve() / "atmos_computer/daemon.py"
    old = path.read_bytes(); new = patched(old.decode()).encode()
    if new == old:
        print(json.dumps({"path": str(path), "unchanged": True})); return
    compile(new, str(path), "exec")
    backup = Path.home() / ".claude/tools/workman-fleet-v1.1/backups" / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) / "daemon.py"
    backup.parent.mkdir(parents=True, exist_ok=True)
    with backup.open("xb") as f:
        os.chmod(backup, 0o600); f.write(old)
    if backup.read_bytes() != old: raise ValueError("backup verification failed")
    tmp = path.with_suffix(".fleet-new"); tmp.write_bytes(new); os.chmod(tmp, path.stat().st_mode & 0o777); os.replace(tmp, path)
    if path.read_bytes() != new: raise ValueError("installed verification failed")
    print(json.dumps({"path": str(path), "backup": str(backup), "previous_sha256": hashlib.sha256(old).hexdigest(), "sha256": hashlib.sha256(new).hexdigest()}))


if __name__ == "__main__": main()
