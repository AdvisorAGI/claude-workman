"""Front-end compaction: type /compact into a Claude desktop session the way the
owner would, then send it a resume line.

A desktop session cannot compact itself from the inside, and a session that
is mid-turn must never be typed into. So the runner waits until the target's
transcript says its turn ended (last assistant/user record is an assistant
record with stop_reason end_turn, and the file has been quiet for 20 s), fronts
it through the app's deep link, proves the right session is on screen by
hashing a strip of its header, types /compact, and counts compact_boundary
lines in the transcript to know it worked. Every step that could type into the
wrong place aborts instead, and every outcome is one JSON line in the log.

This is the flow of ~/orchestra/self-compact.sh, which compacted a live session
on 2026-09-28, step for step and with the same coordinates, crop and result
strings. Where it differs it is bounded where the script was not: the film
deferral stops after 4 h instead of waiting forever, a second runner for the same target refuses
instead of compacting twice, and the resume text is flattened to one line
(a newline typed into the prompt box is Enter, which would send half of it).

The coordinates are the 1920x1080 layout of :0 on the DGX. A header hash is
learned once per session with --learn and stored in
~/.workman/front-compact/headers.json; a run with
no --expect uses the stored one and refuses without it.

Env:
  WORKMAN_FRONT_COMPACT_LOG  the JSON-lines log (default ~/orchestra/compact-log.jsonl)
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

DISPLAY = ":0"
SCREEN = "1920x1080"
# The session title strip in the desktop app header. Gray so a theme's accent
# tint does not change the hash.
HEADER_CROP = "crop=260:24:360:276,format=gray"
PROMPT_XY = ("1000", "1011")
SEND_XY = ("1581", "1013")

# Shared with every other front-end driver on this machine (self-compact.sh
# today): only one of them may own the mouse at a time.
FRONTEND_LOCK = "/tmp/workman-frontend.lock"
FILM_PLAYERS = frozenset({"vlc", "mpv"})

QUIET_S = 20
IDLE_POLLS, IDLE_POLL_S = 180, 10          # 30 min
FILM_POLLS, FILM_POLL_S = 48, 300          # 4 h, then give up rather than wait forever
LOCK_WAIT_S = 180
BOUNDARY_POLLS, BOUNDARY_POLL_S = 72, 5    # 6 min
OPEN_SETTLE_S = 3
RESUME_SETTLE_S = 8
RESUME_POLLS, RESUME_POLL_S = 18, 10       # 3 min for the compacted session to read clean

# The last turn is at the end of the transcript; a 70 MB file is not re-read
# every 10 s to find it. A record longer than this reads as "not idle", which
# fails closed.
TAIL_BYTES = 2_000_000
MAX_RESUME = 2000

LOCAL_ID_RE = re.compile(r"^local_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
HASH_RE = re.compile(r"^[0-9a-f]{12}$")

# Exit codes. 2-6 are self-compact.sh's own.
OK, NEVER_IDLE, HEADER_MISMATCH, LOCK_BUSY, BUSY_AGAIN, NO_BOUNDARY = 0, 2, 3, 4, 5, 6
FILM_TOO_LONG, ALREADY_RUNNING, ERROR, NO_HEADER, REFUSED = 7, 8, 9, 10, 11


def _home() -> Path:
    """Home as the shell sees it, so a test can move it with $HOME."""
    return Path(os.path.expanduser("~"))


def state_dir() -> Path:
    """~/.workman/front-compact: header store, per-target run locks, stderr."""
    return _home() / ".workman" / "front-compact"


def header_store() -> Path:
    """Learned header hashes, {local_id: hash12}."""
    return state_dir() / "headers.json"


def log_path() -> Path:
    """Where every run's outcome lines go."""
    return Path(os.environ.get("WORKMAN_FRONT_COMPACT_LOG")
                or _home() / "orchestra" / "compact-log.jsonl")


def log(target: str, result: str) -> None:
    """Append one {"ts","target","result"} line: the only record of a detached run."""
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "target": target, "result": result}, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# ---- pure reads --------------------------------------------------------------
def transcript_idle(path: str, quiet_s: float = QUIET_S, now: float | None = None) -> bool:
    """True only when the file has been quiet for quiet_s and its last
    assistant/user record is an assistant turn that ended (stop_reason end_turn).

    Anything unreadable is busy: this answer decides whether we type into it.
    """
    try:
        if (time.time() if now is None else now) - os.path.getmtime(path) < quiet_s:
            return False
        with open(path, "rb") as fh:
            fh.seek(max(0, os.path.getsize(path) - TAIL_BYTES))
            lines = fh.read().decode("utf-8", "replace").splitlines()
        # The line cut at the seek point cannot parse (every record ends with
        # its own closing brace), so it is skipped like any other bad line.
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            # main chain only: a subagent's end_turn says nothing about the session
            if (isinstance(record, dict) and record.get("type") in ("assistant", "user")
                    and not record.get("isSidechain")):
                message = record.get("message") or {}
                return (record["type"] == "assistant" and isinstance(message, dict)
                        and message.get("stop_reason") == "end_turn")
    except OSError:
        pass
    return False


def count_boundaries(path: str) -> int:
    """Lines mentioning compact_boundary, as `grep -c compact_boundary` counts them.
    A missing file counts 0, so a compaction can never be read into it."""
    try:
        with open(path, "rb") as fh:
            return sum(1 for line in fh if b"compact_boundary" in line)
    except OSError:
        return 0


def hash_frame(raw: bytes) -> str:
    """12 hex of md5 over the raw gray crop, as `md5sum | cut -c1-12`. An empty
    frame (the grab failed) hashes to "" so it can never match."""
    return hashlib.md5(raw).hexdigest()[:12] if raw else ""


def header_matches(actual: str, expected: str) -> bool:
    """A well-formed expected hash, equal to what is on screen."""
    return bool(HASH_RE.match(expected or "")) and actual == expected


def film_playing(names: Iterable[str]) -> bool:
    """A film player is running; the owner may be watching on the TV output."""
    return any(name in FILM_PLAYERS for name in names)


def process_names() -> list[str]:
    """Every process's exact name, the set `pgrep -x` matches against."""
    names = []
    for comm in Path("/proc").glob("[0-9]*/comm"):
        try:
            names.append(comm.read_text().strip())
        except OSError:
            continue  # exited between the glob and the read
    return names


def clean_resume(text: str) -> str:
    """One line of text: whitespace runs, newlines included, become one space."""
    return " ".join((text or "").split())


def deep_link(local_id: str) -> str:
    """The URL that makes the desktop app show this session."""
    return f"claude://claude.ai/epitaxy/{local_id}"


# ---- header store ------------------------------------------------------------
def load_headers() -> dict[str, str]:
    """The store, keeping only well-formed hashes; unreadable reads empty."""
    try:
        data = json.loads(header_store().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str) and HASH_RE.match(v)}


def stored_header(local_id: str) -> str:
    """This session's learned hash, "" if never learned."""
    return load_headers().get(local_id, "")


def save_header(local_id: str, value: str) -> None:
    """Atomic: a runner reading the store must never see half of it."""
    headers = load_headers()
    headers[local_id] = value
    path = header_store()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(headers, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


# ---- validation (the trust boundary: the tool and the CLI both call it) ------
def validate(local_id: str, transcript: str = "", expect: str = "", resume: str = "",
             need_transcript: bool = True) -> str:
    """"" when the arguments are safe to act on, else why not."""
    if not LOCAL_ID_RE.match(local_id or ""):
        return f"local_id must be local_<uuid>, got {local_id!r}"
    if need_transcript and not os.path.isfile(os.path.expanduser(transcript or "")):
        return f"transcript not found: {transcript!r}"
    if expect and not HASH_RE.match(expect):
        return f"expect must be 12 lowercase hex, got {expect!r}"
    if len(resume or "") > MAX_RESUME:
        return f"resume is {len(resume)} chars, max {MAX_RESUME}"
    return ""


# ---- display side effects ----------------------------------------------------
def display_env() -> dict[str, str]:
    """The environment that reaches the owner's :0, whatever this process has."""
    return {**os.environ, "DISPLAY": DISPLAY,
            "XAUTHORITY": f"/run/user/{os.getuid()}/gdm/Xauthority"}


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Every subprocess this module starts goes through here (tests replace it)."""
    return subprocess.run(argv, env=display_env(), stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=timeout, check=False)


def header_hash() -> str:
    """Hash of the header strip on :0 right now, "" if the grab failed."""
    out = _run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "x11grab",
                "-video_size", SCREEN, "-i", DISPLAY, "-frames:v", "1",
                "-vf", HEADER_CROP, "-f", "rawvideo", "-"], timeout=20)
    return hash_frame(out.stdout) if out.returncode == 0 else ""


def open_session(local_id: str) -> None:
    """Front the session in the desktop app."""
    _run(["xdg-open", deep_link(local_id)], timeout=15)


def click(xy: tuple[str, str]) -> None:
    """Left click at a screen point on :0."""
    _run(["xdotool", "mousemove", xy[0], xy[1], "click", "1"], timeout=10)


def type_text(text: str, delay_ms: int) -> None:
    """Type text into whatever has focus on :0, delay_ms per key."""
    # `--` so a resume line that starts with "-" is typed, not parsed as a flag.
    _run(["xdotool", "type", "--delay", str(delay_ms), "--", text],
         timeout=15 + len(text) * delay_ms / 1000)


def try_lock(path: str | Path, wait_s: float,
             sleep: Callable[[float], None] = time.sleep) -> int | None:
    """An open fd holding an exclusive flock on path, or None after wait_s.
    flock(2), so it excludes the shell's `flock` on the same file."""
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    for attempt in range(int(wait_s) + 1):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if attempt < int(wait_s):
                sleep(1)
    os.close(fd)
    return None


# ---- the runs ----------------------------------------------------------------
def full_ready(transcript: str, self_id: str = "", after_compact: bool = False) -> tuple[bool, str]:
    """The orchestra turn guards on the main chain (caller known and not the
    target, end_turn, quiet >= 60 s, no open question, registry neither busy
    nor waiting). after_compact also accepts /compact's own echo as clean."""
    from . import orchestra_tools
    return orchestra_tools.turn_ready(transcript, self_id=self_id, after_compact=after_compact)


def wait_ready(ready: Callable[[], tuple[bool, str]], sleep: Callable[[float], None],
               polls: int = RESUME_POLLS, poll_s: float = RESUME_POLL_S) -> tuple[bool, str]:
    """Ask `ready` up to `polls` times; the last answer when it never says yes."""
    why = ""
    for attempt in range(polls):
        ok, why = ready()
        if ok:
            return True, ""
        if attempt < polls - 1:
            sleep(poll_s)
    return False, why


class FrontCompact:
    """One compaction of one session; run() returns the exit code and logs why.

    Readiness, both while waiting and right before the first click, is the full
    turn check (`full_ready`), not only the transcript's last record."""

    def __init__(self, local_id: str, transcript: str, expect: str, resume: str = "",
                 sleep: Callable[[float], None] = time.sleep,
                 procs: Callable[[], list[str]] = process_names, self_id: str = "",
                 ready: Callable[[str], tuple[bool, str]] | None = None,
                 resume_ready: Callable[[str], tuple[bool, str]] | None = None):
        self.local_id = local_id
        self.transcript = os.path.expanduser(transcript)
        self.expect = expect
        self.resume = clean_resume(resume)
        self.sleep = sleep
        self.procs = procs
        self.ready = ready or (lambda path: full_ready(path, self_id))
        # the resume line is typed only into a session that is still clean
        self.resume_ready = resume_ready or (lambda path: full_ready(path, self_id, True))

    def _end(self, code: int, result: str) -> int:
        log(self.local_id, result)
        return code

    def run(self) -> int:
        held: list[int] = []
        try:
            return self._run(held)
        except Exception as exc:  # the log is the only reader of a detached run
            return self._end(ERROR, f"abort: {type(exc).__name__}: {exc}")
        finally:
            for fd in held:
                os.close(fd)

    def _run(self, held: list[int]) -> int:
        code = self._wait_and_lock(held)
        if code is not None:
            return code
        before = count_boundaries(self.transcript)
        open_session(self.local_id)
        self.sleep(OPEN_SETTLE_S)
        seen = header_hash()
        if not header_matches(seen, self.expect):
            return self._end(HEADER_MISMATCH, f"abort: header {seen} != {self.expect}")
        ok, why = self.ready(self.transcript)
        if not transcript_idle(self.transcript) or not ok:
            return self._end(BUSY_AGAIN, "abort: target busy again" + (f": {why}" if why else ""))
        self._send("/compact", 70)
        for _ in range(BOUNDARY_POLLS):
            if count_boundaries(self.transcript) > before:
                break
            self.sleep(BOUNDARY_POLL_S)
        else:
            return self._end(NO_BOUNDARY, "fail: no compact_boundary within 6 min")
        log(self.local_id, "compacted")
        if not self.resume:
            return OK
        self.sleep(RESUME_SETTLE_S)
        ok, why = wait_ready(lambda: self.resume_ready(self.transcript), self.sleep)
        if not ok:
            return self._end(OK, f"resume skipped: {why}")
        seen = header_hash()
        if not header_matches(seen, self.expect):
            return self._end(OK, f"resume skipped: header {seen}")
        self._send(self.resume, 25, settle=0.8)
        return self._end(OK, "resume sent")

    def _wait_and_lock(self, held: list[int]) -> int | None:
        """The run lock, the ready wait, the film deferral and the frontend
        lock, in that order; an exit code when any of them says stop."""
        state_dir().mkdir(parents=True, exist_ok=True)
        # Held for the whole run: a second runner for the same target would
        # otherwise wait out the same idle window and compact it twice.
        mine = try_lock(state_dir() / f"{self.local_id}.run.lock", 0, self.sleep)
        if mine is None:
            return self._end(ALREADY_RUNNING, "abort: another run for this target is active")
        held.append(mine)
        for _ in range(IDLE_POLLS):
            if transcript_idle(self.transcript) and self.ready(self.transcript)[0]:
                break
            self.sleep(IDLE_POLL_S)
        else:
            return self._end(NEVER_IDLE, "abort: never idle in 30 min")
        for attempt in range(FILM_POLLS + 1):
            if not film_playing(self.procs()):
                break
            if attempt == FILM_POLLS:
                return self._end(FILM_TOO_LONG, "abort: film player still running after 4 h")
            self.sleep(FILM_POLL_S)
        front = try_lock(FRONTEND_LOCK, LOCK_WAIT_S, self.sleep)
        if front is None:
            return self._end(LOCK_BUSY, "abort: frontend lock busy")
        held.append(front)
        return None

    def _send(self, text: str, delay_ms: int, settle: float = 1.2) -> None:
        """Click the prompt box, type, click send: the only keystrokes a run makes."""
        click(PROMPT_XY)
        self.sleep(0.6)
        type_text(text, delay_ms)
        self.sleep(settle)
        click(SEND_XY)


# ---- detached launch and CLI: workman/front_compact_cli.py ------------------
# Delegates, so `front_compact.spawn` / `.main` / `.parse_args` / `.learn_header`
# and `python -m workman.front_compact` keep working (looked up at call time:
# the two modules import each other).
from . import front_compact_cli as _cli  # noqa: E402


def spawn(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return _cli.spawn(*args, **kwargs)


def learn_header(local_id: str, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    return _cli.learn_header(local_id, sleep)


def parse_args(argv: list[str] | None = None) -> Any:
    return _cli.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return _cli.main(argv)


if __name__ == "__main__":
    sys.exit(main())
