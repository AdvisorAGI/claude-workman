"""The front_compact entry points: `spawn` (the detached launch the tools use),
`learn_header`, and the CLI `python -m workman.front_compact`.

A run started from here goes through the same guard chain as the MCP tool
(orchestra.compact.runner_check: caller known and not the target, clean
main-chain turn, quiet >= 60 s, no question, registry neither busy nor waiting,
the transcript is this local_id's session, and the 300k-400k policy band). The
band is skipped only with --force, which needs a --reason that is logged.

Every name the tests replace (`_run`, `FRONTEND_LOCK`, `process_names`,
`FrontCompact`, ...) is looked up on the front_compact module at call time.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import front_compact as fc

POLICY_BAND = (300_000, 400_000)


def learn_header(local_id: str, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Front the session, hash its header and store it for later runs.

    Refuses a hash another session already owns: two sessions the header strip
    cannot tell apart would make the guard meaningless, and it is also what a
    deep link that fronted nothing looks like.
    """
    why = fc.validate(local_id, need_transcript=False)
    if why:
        return {"ok": False, "error": why}
    if fc.film_playing(fc.process_names()):
        return {"ok": False, "error": "a film player is running; not fronting anything"}
    lock = fc.try_lock(fc.FRONTEND_LOCK, fc.LOCK_WAIT_S, sleep)
    if lock is None:
        return {"ok": False, "error": f"frontend lock busy for {fc.LOCK_WAIT_S} s"}
    try:
        fc.open_session(local_id)
        sleep(fc.OPEN_SETTLE_S)
        value = fc.header_hash()
    finally:
        os.close(lock)
    if not value:
        return {"ok": False, "error": "screen grab of :0 failed"}
    others = sorted(k for k, v in fc.load_headers().items() if v == value and k != local_id)
    if others:
        return {"ok": False, "error": f"header {value} is already {others[0]}'s: the deep "
                                      "link may not have fronted this session", "hash": value}
    fc.save_header(local_id, value)
    fc.log(local_id, f"learned header {value}")
    return {"ok": True, "hash": value, "store": str(fc.header_store())}


def band_args(min_tokens: int = POLICY_BAND[0], max_tokens: int = POLICY_BAND[1],
              force: bool = False, reason: str = "") -> list[str]:
    """The runner flags for a band or a force; nothing for the default band."""
    argv = []
    if int(min_tokens) != POLICY_BAND[0]:
        argv.append(f"--min-tokens={int(min_tokens)}")
    if int(max_tokens) != POLICY_BAND[1]:
        argv.append(f"--max-tokens={int(max_tokens)}")
    if force:
        argv += ["--force", f"--reason={reason or 'forced by the caller'}"]
    return argv


def spawn(local_id: str, transcript: str, resume: str = "", expect: str = "",
          self_id: str = "", min_tokens: int = POLICY_BAND[0], max_tokens: int = POLICY_BAND[1],
          force: bool = False, reason: str = "") -> dict[str, Any]:
    """Start a run in its own session and return at once; the runner checks the
    guard chain again and then waits for the target's turn to end."""
    try:
        why = fc.validate(local_id, transcript, expect, resume)
        if why:
            return {"ok": False, "error": why}
        if not expect and not fc.stored_header(local_id):
            return {"ok": False, "error": f"no header stored for {local_id}: run "
                                          f"`python -m workman.front_compact {local_id} --learn` first"}
        # `--opt=value`: a resume line starting with "-" must not read as a flag.
        argv = [sys.executable, "-m", "workman.front_compact", local_id,
                f"--transcript={os.path.expanduser(transcript)}"]
        if expect:
            argv.append(f"--expect={expect}")
        if resume:
            argv.append(f"--resume={resume}")
        if self_id:
            argv.append(f"--self-id={self_id}")
        argv += band_args(min_tokens, max_tokens, force, reason)
        fc.state_dir().mkdir(parents=True, exist_ok=True)
        err = fc.state_dir() / f"{local_id}.err"
        with err.open("w", encoding="utf-8") as stderr:
            proc = subprocess.Popen(argv, cwd=str(Path(fc.__file__).resolve().parent.parent),
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=stderr, start_new_session=True)
        return {"ok": True, "pid": proc.pid, "target": local_id,
                "header": "given" if expect else "stored",
                "log": str(fc.log_path()), "stderr": str(err)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def add_band_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--min-tokens", type=int, default=POLICY_BAND[0])
    parser.add_argument("--max-tokens", type=int, default=POLICY_BAND[1])
    parser.add_argument("--force", action="store_true",
                        help="skip the token band; needs --reason, which is logged")
    parser.add_argument("--reason", default="", help="why the band is skipped (with --force)")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m workman.front_compact",
        description="Compact a Claude desktop session from its front end on :0.")
    parser.add_argument("local_id", help="local_<uuid> of the desktop session")
    parser.add_argument("--transcript", default="", help="the session's .jsonl")
    parser.add_argument("--expect", default="", help="12-hex header hash (default: stored)")
    parser.add_argument("--resume", default="", help="line to send after compaction")
    parser.add_argument("--learn", action="store_true",
                        help="front the session, store its header hash, and exit")
    parser.add_argument("--self-id", default="",
                        help="the calling session's id(s), comma separated, each a session "
                             "id (>= 8 hex); without one (and none in the env or the process "
                             "tree) the run refuses")
    add_band_flags(parser)
    args = parser.parse_args(argv)
    why = fc.validate(args.local_id, args.transcript, args.expect, args.resume,
                      need_transcript=not args.learn)
    if args.force and not args.reason.strip():
        why = why or "--force needs --reason (it is logged)"
    if why:
        parser.error(why)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.learn:
        out = learn_header(args.local_id)
        print(json.dumps(out))
        return fc.OK if out["ok"] else fc.NO_HEADER
    from . import orchestra_tools
    if not orchestra_tools.caller_ids(args.self_id):
        why = orchestra_tools.bad_self_id(args.self_id) or "the caller's session id is unknown"
        fc.log(args.local_id, f"abort: {why}")
        print(f"refused: {why}; pass --self-id=<session id>", file=sys.stderr)
        return fc.ERROR
    expect = args.expect or fc.stored_header(args.local_id)
    if not expect:
        fc.log(args.local_id, "abort: no header hash stored, run --learn first")
        print(f"no header stored for {args.local_id}: run --learn first", file=sys.stderr)
        return fc.NO_HEADER
    from .orchestra.compact import runner_check
    ok, why = runner_check(args.transcript, "desktop", local_id=args.local_id,
                           self_id=args.self_id, min_tokens=args.min_tokens,
                           max_tokens=args.max_tokens, force=args.force, reason=args.reason,
                           expect=expect)
    if not ok:
        print(f"refused: {why}", file=sys.stderr)
        return fc.REFUSED
    return fc.FrontCompact(args.local_id, args.transcript, expect, args.resume,
                           self_id=args.self_id).run()
