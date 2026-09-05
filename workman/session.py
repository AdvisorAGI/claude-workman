"""Session continuity: how full a Claude Code session's context is, and the
verdict on compacting it.

A session's context fills as it works. Below the FLOOR (250000 tokens) nobody
asks the question. Between the floor and the WINDOW (`autoCompactWindow` in
~/.claude/settings.json, 400000 by default) the question is a judgement, not a
threshold: a Sonnet lane looks at each turn end and writes whether compacting
now beats running on to /tmp/compact-verdict.<session_id>.json. At the window
the harness compacts whether or not the moment is clean.

That state already exists on disk, written and read by two shell hooks
(~/.claude/skills/context-autopilot/scripts/context-step.sh and
~/.claude/hooks/sonnet-oncall.sh). This module is the same state through
Workman, so a session (or the judging lane itself) can read and write it as
tool calls instead of ad-hoc shell.

The token count is deliberately the identical read: the LAST assistant turn's
`usage`, summed over input + cache_creation + cache_read + output, found by
scanning the transcript jsonl backwards from its tail. Two components that
disagreed about how full the context is would be worse than no number at all.

Paths are derived, never hardcoded. A project's memory directory is its cwd
with slashes turned into dashes under ~/.claude/projects/<slug>/memory, which
is how Claude Code names them; the ledger is turn-ledger.md and the handoff is
session-handoff-latest.md inside it. A transcript is <session_id>.jsonl under
one of those project directories.

Nothing here raises. Every entry point returns a dict, with an `error` key when
something went wrong: a tool that raises tells the model nothing it can act on.

Env:
  COMPACT_FLOOR        token floor below which nobody asks (default 250000)
  WORKMAN_VERDICT_DIR  directory holding the verdict files (default /tmp).
                       The hooks read /tmp; override it in tests only.
"""

from __future__ import annotations

import glob
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_FLOOR = 250_000
DEFAULT_WINDOW = 400_000

# The four usage counters that make up "what is in the context window right
# now". Cache reads count: they are re-sent on every turn.
USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens",
              "cache_read_input_tokens", "output_tokens")

# How much of the transcript tail to read. The last assistant turn is at the
# end; reading the whole file on a long session would cost more than the answer
# is worth. A half line at the seek point simply fails to parse and is skipped.
TAIL_BYTES = 600_000

LEDGER_NAME = "turn-ledger.md"
HANDOFF_NAME = "session-handoff-latest.md"

# Matches `date +%Y-%m-%dT%H:%M%z`, the stamp the on-call lane already writes.
STAMP_FORMAT = "%Y-%m-%dT%H:%M%z"


def _home() -> Path:
    """Home as the shell sees it, so a test can move it with $HOME."""
    return Path(os.path.expanduser("~"))


def projects_dir() -> Path:
    """~/.claude/projects: one directory per cwd Claude Code has run in."""
    return _home() / ".claude" / "projects"


def settings_path() -> Path:
    return _home() / ".claude" / "settings.json"


def window() -> int:
    """The auto-compact backstop: `autoCompactWindow` from settings, else 400000."""
    try:
        value = int(json.loads(settings_path().read_text(encoding="utf-8"))
                    .get("autoCompactWindow", DEFAULT_WINDOW))
        return value if value > 0 else DEFAULT_WINDOW
    except Exception:
        return DEFAULT_WINDOW


def floor() -> int:
    """The token floor below which compaction is nobody's question."""
    try:
        value = int(os.environ.get("COMPACT_FLOOR", DEFAULT_FLOOR))
        return value if value > 0 else DEFAULT_FLOOR
    except (TypeError, ValueError):
        return DEFAULT_FLOOR


def verdict_path(session_id: str) -> Path:
    """/tmp/compact-verdict.<session_id>.json, the file the hooks read."""
    directory = os.environ.get("WORKMAN_VERDICT_DIR") or "/tmp"
    return Path(directory) / f"compact-verdict.{session_id or 'nosession'}.json"


def memory_dir(cwd: str = "") -> Path:
    """This project's memory directory, derived from a cwd the way Claude Code
    derives it: /Users/x/Repo -> ~/.claude/projects/-Users-x-Repo/memory."""
    path = os.path.abspath(os.path.expanduser(cwd or os.getcwd()))
    return projects_dir() / path.replace("/", "-") / "memory"


def transcript_for(session_id: str) -> str:
    """The transcript of one session: <session_id>.jsonl under any project."""
    if not session_id:
        return ""
    hits = glob.glob(str(projects_dir() / "*" / f"{session_id}.jsonl"))
    return hits[0] if hits else ""


def latest_transcript() -> str:
    """The most recently modified transcript on this machine, the same
    `ls -t ~/.claude/projects/*/*.jsonl | head -1` fallback the hooks use."""
    try:
        hits = glob.glob(str(projects_dir() / "*" / "*.jsonl"))
        return max(hits, key=lambda p: os.path.getmtime(p)) if hits else ""
    except OSError:
        return ""


def resolve_transcript(transcript_path: str = "", session_id: str = "") -> str:
    """An explicit path wins; then the session's own transcript; then the
    newest one on the machine."""
    if transcript_path and os.path.isfile(os.path.expanduser(transcript_path)):
        return os.path.expanduser(transcript_path)
    return transcript_for(session_id) or latest_transcript()


def context_tokens(transcript_path: str) -> int:
    """Tokens in the context window right now: the last assistant turn's usage.

    Byte-for-byte the read context-step.sh does, so the two can never disagree.
    Returns 0 when the file is missing, empty, or has no usage yet.
    """
    ctx = 0
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(max(0, os.path.getsize(transcript_path) - TAIL_BYTES))
            tail = fh.read().decode("utf-8", "replace")
        for line in reversed(tail.splitlines()):
            try:
                d = json.loads(line)
            except Exception:
                continue
            usage = (d.get("message") or {}).get("usage") if isinstance(d, dict) else None
            if usage:
                ctx = sum(int(usage.get(k) or 0) for k in USAGE_KEYS)
                break
    except Exception:
        pass
    return ctx


def zone_for(context: int, win: int, flr: int) -> str:
    """quiet below the floor, judged in the band, backstop at the window."""
    if context >= win:
        return "backstop"
    if context >= flr:
        return "judged"
    return "quiet"


def read_verdict(session_id: str) -> dict[str, Any] | None:
    """The verdict written for this session, or None if there is not one yet."""
    try:
        data = json.loads(verdict_path(session_id).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


# ---- what the tools call ---------------------------------------------------
def context_status(transcript_path: str = "", session_id: str = "",
                   cwd: str = "") -> dict[str, Any]:
    """Where this session sits against the floor and the window, plus any verdict."""
    win, flr = window(), floor()
    try:
        path = resolve_transcript(transcript_path, session_id)
        if not path:
            return {"ok": False, "error": "no transcript found: pass transcript_path, "
                                          f"or a session_id with a jsonl under {projects_dir()}",
                    "window": win, "floor": flr}
        # A transcript is named after its session, so the id comes free.
        sid = session_id or Path(path).stem
        context = context_tokens(path)
        return {
            "ok": True,
            "session_id": sid,
            "transcript": path,
            "context": context,
            "window": win,
            "floor": flr,
            "zone": zone_for(context, win, flr),
            "percent": round(100.0 * context / win, 1) if win else 0.0,
            "memory_dir": str(memory_dir(cwd)),
            "verdict_path": str(verdict_path(sid)),
            "verdict": read_verdict(sid),
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "window": win, "floor": flr}


def compact_verdict(session_id: str, compact_now: bool | None = None,
                    reason: str = "", blockers: list[str] | None = None,
                    handoff_current: bool | None = None) -> dict[str, Any]:
    """Read the verdict for a session, or write it when compact_now is given.

    Writing is atomic (write then replace) because context-step.sh reads this
    file on every tool call and must never see half of it.
    """
    if not session_id:
        return {"ok": False, "error": "session_id is required"}
    path = verdict_path(session_id)
    if compact_now is None:
        existing = read_verdict(session_id)
        return {"ok": True, "session_id": session_id, "path": str(path),
                "exists": existing is not None, "verdict": existing,
                **({} if existing is not None
                   else {"note": "no verdict written for this session yet, "
                                 "which the hooks read as 'keep running'"})}
    try:
        previous = read_verdict(session_id) or {}
        status = context_status(session_id=session_id)
        record = {
            "compact_now": bool(compact_now),
            "context": int(status.get("context") or previous.get("context") or 0),
            "window": int(status.get("window") or window()),
            "reason": str(reason or ""),
            "blockers": [str(b) for b in (blockers or [])],
            # Left alone rather than silently flipped to false when the caller
            # is only updating the decision.
            "handoff_current": bool(previous.get("handoff_current", False)
                                    if handoff_current is None else handoff_current),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record), encoding="utf-8")
        os.replace(tmp, path)
        return {"ok": True, "session_id": session_id, "path": str(path),
                "written": True, "verdict": record}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "path": str(path)}


def ledger_append(cwd: str, entries: list[str]) -> dict[str, Any]:
    """Append one dated block of bullets to this project's turn-ledger.md."""
    try:
        bullets = [str(e).replace("\n", " ").strip() for e in (entries or [])]
        bullets = [b for b in bullets if b]
        if not bullets:
            return {"ok": False, "error": "entries is empty: nothing to append"}
        directory = memory_dir(cwd)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / LEDGER_NAME
        created = not path.exists()
        separator = ""
        if not created and path.stat().st_size:
            with path.open("rb") as fh:
                fh.seek(-1, os.SEEK_END)
                separator = "\n" if fh.read(1) == b"\n" else "\n\n"
        stamp = datetime.now().astimezone().strftime(STAMP_FORMAT)
        block = separator + f"## {stamp}\n" + "".join(f"- {b}\n" for b in bullets)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(block)
        return {"ok": True, "path": str(path), "bullets": len(bullets),
                "created": created, "stamp": stamp}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def handoff(cwd: str, content: str = "") -> dict[str, Any]:
    """Read this project's session-handoff-latest.md, or overwrite it."""
    try:
        directory = memory_dir(cwd)
        path = directory / HANDOFF_NAME
        if not content:
            if not path.exists():
                return {"ok": True, "path": str(path), "exists": False,
                        "content": "", "bytes": 0,
                        "note": "no handoff written for this project yet"}
            text = path.read_text(encoding="utf-8", errors="replace")
            return {"ok": True, "path": str(path), "exists": True,
                    "content": text, "bytes": len(text.encode("utf-8"))}
        directory.mkdir(parents=True, exist_ok=True)
        created = not path.exists()
        body = content if content.endswith("\n") else content + "\n"
        path.write_text(body, encoding="utf-8")
        return {"ok": True, "path": str(path), "written": True,
                "created": created, "bytes": len(body.encode("utf-8"))}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
