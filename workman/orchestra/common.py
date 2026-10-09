"""Shared ground for the orchestra modules: the policy constants, paths, time
and the redaction every printed string passes through."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any



CTX_WINDOW = 1_000_000

STATES = ("working", "idle", "needs_input", "blocked", "silent", "limit_hit")

NEED_STATES = ("needs_input", "blocked", "limit_hit", "silent")

EVENT_KINDS = ("question", "permission", "blocked", "limit_hit", "silent",
               "compact_ready", "finished")

EFFORTS = ("low", "medium", "high", "xhigh", "max")

QUIET_S = 60                 # a turn counts as ended only after this much quiet

SILENT_MIN = 25              # minutes inside a tool call before it reads as stuck

SILENT_SPAN_MIN = 90         # silent fires once, in [SILENT_MIN, SILENT_MIN + span]

FINISHED_RECENT_S = 30 * 60  # a turn end older than this is history, not news

COMPACT_BAND = (300_000, 400_000)

TAIL_BYTES = 4 << 20         # the state is in the last records

HEAD_BYTES = 512 << 10       # the first prompt is in the first records

TITLE_LEN, TEXT_LEN = 90, 200

SELF_ENV = ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")

SECRET_RES = [
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [redacted]"),
    (re.compile(r"(?i)\b([A-Z0-9_]*(token|secret|passw(or)?d|api[_-]?key|auth|cookie)[A-Z0-9_]*)"
                r"\s*[:=]\s*\S+"), r"\1=[redacted]"),
    (re.compile(r"\b(sk-|sk_|pk_|rk_|ghp_|gho_|ghs_|github_pat_|xox[abprs]-|AKIA|AIza|eyJ)"
                r"[A-Za-z0-9_\-.]{6,}"), "[redacted]"),
    # bridge / remote session ids, plain or hyphenated (uuid-shaped)
    (re.compile(r"\b(cse|session)_(?=[A-Za-z0-9-]*\d)[A-Za-z0-9][A-Za-z0-9-]{15,}"),
     r"\1_[redacted]"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "[hex]"),
]

def home_dir(home: str | Path | None = None) -> Path:
    """Home as the shell sees it, so a test moves it with $HOME."""
    return Path(home) if home else Path(os.path.expanduser("~"))

def orchestra_dir(home: str | Path | None = None) -> Path:
    env = os.environ.get("WORKMAN_ORCHESTRA_DIR")
    return Path(env) if env and not home else home_dir(home) / "orchestra"

def iso(t: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if t is None else t,
                                  timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def parse_ts(s: Any) -> float | None:
    """A transcript timestamp ("2026-10-08T20:26:37.123Z") as epoch seconds."""
    if not isinstance(s, str) or len(s) < 19:
        return None
    try:
        return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None

def redact(s: Any, n: int = TEXT_LEN) -> str:
    """One line, secrets masked, at most n chars."""
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    for rx, rep in SECRET_RES:
        s = rx.sub(rep, s)
    return s[:n]

def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None

def _read_span(path: Path, start: int, size: int) -> list[str]:
    """Lines of path[start:start+size]; the cut line at either edge is dropped."""
    try:
        with open(path, "rb") as fh:
            total = os.fstat(fh.fileno()).st_size
            start = max(0, min(start, total))
            fh.seek(start)
            data = fh.read(size)
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    if start + size < total and lines:
        lines = lines[:-1]
    return lines

def config_dirs(home: str | Path | None = None) -> list[Path]:
    """~/.claude and every ~/.claude-*: one per account seat."""
    h = home_dir(home)
    out = []
    for p in sorted(h.glob(".claude*")):
        if p.is_dir() and (p / "projects").is_dir():
            out.append(p)
    return out
