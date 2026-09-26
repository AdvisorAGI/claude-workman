"""Persist every capture on the machine that took it.

The workman package has no learner of its own, so this module carries the two
pieces the fleet learner needs, in the same on-disk shape the room-1 mini's
`atmos_computer.learn` writes:

  ~/.grok/workman-learn/shots/<UTC-date>/<HHMMSSZ>-<pid>-<tool>.<ext>
  ~/.grok/workman-learn/journal.jsonl   append-only action log

Both entry points are best effort: an archiving or logging failure must never
break a capture, so nothing here raises.

Env:
  WORKMAN_LEARN_ROOT         store root (default ~/.grok/workman-learn)
  WORKMAN_SHOT_ARCHIVE       "0"/"false"/"no" turns archiving off (default on)
  WORKMAN_SHOT_RETAIN_DAYS   whole day-directories older than this are pruned
                             (default 14; pruning runs only when a new day
                             directory is created)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

LEARN_ROOT = Path(os.path.expanduser(os.environ.get("WORKMAN_LEARN_ROOT", "~/.grok/workman-learn")))
JOURNAL = LEARN_ROOT / "journal.jsonl"
SHOTS = LEARN_ROOT / "shots"

_SECRETISH = re.compile(r"(password|secret|token|cookie|authorization|api[_-]?key)", re.I)

_ARCHIVE = os.environ.get("WORKMAN_SHOT_ARCHIVE", "1").lower() not in ("0", "false", "no")
try:
    _RETAIN_DAYS = int(os.environ.get("WORKMAN_SHOT_RETAIN_DAYS", "14"))
except ValueError:
    _RETAIN_DAYS = 14


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure() -> None:
    LEARN_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(LEARN_ROOT, 0o700)
    except OSError:
        pass


def redact(payload: Any) -> Any:
    """Drop values whose keys look like secrets. Never store typed text."""
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for k, v in payload.items():
            if _SECRETISH.search(str(k)) or k in {"text", "typed"}:
                out[k] = f"<redacted:{type(v).__name__}>"
            else:
                out[k] = redact(v)
        return out
    if isinstance(payload, list):
        return [redact(x) for x in payload]
    return payload


def journal(tool: str, payload: dict[str, Any] | None = None) -> None:
    """Append one action row to journal.jsonl. Failures never break a capture."""
    try:
        _ensure()
        row = {
            "ts": _now(),
            "kind": "action",
            "tool": tool,
            "payload": redact(payload or {}),
        }
        try:
            from . import episode as _episode
            row = _episode.stamp(row)
        except Exception:
            uid = os.environ.get("WORKMAN_EPISODE_UID")
            if uid:
                row["episode_uid"] = uid
                row["task"] = os.environ.get("WORKMAN_TASK") or None
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str)
        with JOURNAL.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        return


def _prune_shots() -> None:
    """Drop whole day-directories older than the retention window."""
    try:
        cutoff = time.time() - _RETAIN_DAYS * 86400
        for day in SHOTS.iterdir():
            if day.is_dir() and day.stat().st_mtime < cutoff:
                for f in day.iterdir():
                    f.unlink()
                day.rmdir()
    except Exception:
        return


def archive_capture(data: bytes, fmt: str, tool: str) -> str | None:
    """Persist one capture. Returns the path, or None if archiving is off/failed."""
    if not _ARCHIVE or not data:
        return None
    try:
        _ensure()
        day = SHOTS / time.strftime("%Y-%m-%d", time.gmtime())
        fresh = not day.exists()
        day.mkdir(parents=True, exist_ok=True)
        if fresh:
            _prune_shots()
        ext = "jpg" if fmt == "jpeg" else (fmt or "png")
        name = "%s-%d-%s.%s" % (time.strftime("%H%M%SZ", time.gmtime()), os.getpid(), tool, ext)
        path = day / name
        path.write_bytes(data)
        try:
            from . import working
            working.observe(hashlib.sha256(data).hexdigest()[:16], root=LEARN_ROOT)
        except Exception:
            pass
        return str(path)
    except Exception:
        return None
