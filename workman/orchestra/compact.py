"""session_compact: compact ANOTHER session from its front end. Desktop goes
to the front_compact runner; Remote Control to `RemoteCompact` (claude.ai/code
in Chrome); headless is refused."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .. import front_compact as fc
from .. import front_compact_cli as fcli
from . import registry as rg
from . import state as st
from .drivers import ChromeDriver, is_editable, page_shows
from .guards import (COMPACT_POLICY, _guard, _public, band_guard, expected_titles, find_session,
                     headless_refusal, log_line, rc_url, turn_guards)


def session_compact(session: str, mode: str = "dry_run", min_tokens: int = 300_000,
                    max_tokens: int = 400_000, force: bool = False, resume: str = "",
                    self_id: str = "", home: Any = None, now: float | None = None,
                    proc_root: Path = Path("/proc"),
                    procs: Callable[[], list[str]] | None = None,
                    spawn_rc: Callable[..., dict] | None = None, expect: str = "",
                    local_id: str = "") -> dict[str, Any]:
    """Compact ANOTHER session from its front end, only at a clean turn and
    inside the policy band (300k-400k; a caller may narrow it) unless forced.
    Dry run by default. Refuses when the caller's own session id is unknown."""
    if mode not in ("dry_run", "run"):
        return {"ok": False, "error": "mode must be 'dry_run' or 'run'"}
    row, err = find_session(session, home, now, proc_root)
    if not row:
        fc.log(session or "?", f"{mode}: refused: {err}")
        return {"ok": False, "mode": mode, "error": err}
    target = row.get("local_id") or row["session_id"]
    out: dict[str, Any] = {"mode": mode, "session": _public(row)}
    if row.get("front_end") == "headless":
        why = headless_refusal(row, "compact")
        log_line(fc.log_path(), target=target, tool="session_compact", mode=mode,
                 result=f"refused: {why}")
        return {**out, "ok": False, "refused": why}
    guards = compact_guards(row, self_id, procs, min_tokens, max_tokens, force,
                            "session_compact force=True" if force else "", expect, local_id)
    ctx = int(row.get("ctx_tokens") or 0)
    resume_line = fc.clean_resume(resume)
    if len(resume_line) > fc.MAX_RESUME:
        guards.append(_guard("resume_len", False, f"{len(resume_line)} > {fc.MAX_RESUME}"))
    failed = [g for g in guards if not g["ok"]]
    out["guards"] = guards
    out["would"] = compact_steps(row, resume_line)
    if failed:
        why = "; ".join(f"{g['name']}: {g['detail']}" for g in failed)
        log_line(fc.log_path(), target=target, tool="session_compact", mode=mode,
                 result=f"refused: {why}")
        return {**out, "ok": False, "refused": why}
    if mode == "dry_run":
        log_line(fc.log_path(), target=target, tool="session_compact", mode=mode,
                 result=f"dry_run: would compact via {row['front_end']} at ctx {ctx}")
        return {**out, "ok": True, "result": "dry_run: every guard passed; nothing was touched"}
    ids = ",".join(rg.caller_ids(self_id, row["_scan"].home, row["_scan"].proc_root))
    band = {"min_tokens": min_tokens, "max_tokens": max_tokens, "force": force,
            "reason": "session_compact force=True" if force else ""}
    if row["front_end"] == "desktop":
        launched = fc.spawn(row["local_id"], row["path"], resume=resume_line, expect=expect,
                            self_id=ids, **band)
    else:
        launched = (spawn_rc or spawn_remote)(row["session_id"], row["path"], resume_line, ids,
                                              **band)
    log_line(fc.log_path(), target=target, tool="session_compact", mode=mode,
             result=("runner started pid %s" % launched.get("pid")) if launched.get("ok")
             else f"refused: {launched.get('error')}")
    return {**out, "ok": bool(launched.get("ok")), "runner": launched}

def compact_guards(row: dict, self_id: str, procs: Any, min_tokens: int, max_tokens: int,
                   force: bool, reason: str, expect: str = "", local_id: str = "") -> list[dict]:
    """The guard chain shared by the MCP tool and every runner started from a
    shell: the turn guards, the header, the session's local_id, and the band
    (skipped only by force, whose reason goes into the guard's detail)."""
    guards = turn_guards(row, self_id, procs)
    if expect and row.get("front_end") == "desktop":
        # an explicit header hash stands in for a learned one
        guards = [g if g["name"] != "header_learned" else
                  _guard("header_learned", bool(fc.HASH_RE.match(expect)), "given by the caller")
                  for g in guards]
    if local_id and row.get("local_id") != local_id:
        guards.append(_guard("local_id_matches", False,
                             f"{local_id} is not this transcript's session"))
    band, _ = band_guard(int(row.get("ctx_tokens") or 0), min_tokens, max_tokens, force,
                         COMPACT_POLICY)
    if force:
        band = _guard("band", bool(reason.strip()), band["detail"] + (
            f": {reason.strip()}" if reason.strip() else " without a reason: refused"))
    guards.append(band)
    return guards


def runner_check(transcript: str, front_end: str, local_id: str = "", self_id: str = "",
                 min_tokens: int = COMPACT_POLICY[0], max_tokens: int = COMPACT_POLICY[1],
                 force: bool = False, reason: str = "", expect: str = "", home: Any = None,
                 proc_root: Path = Path("/proc"), procs: Any = None) -> tuple[bool, str]:
    """The guard chain for a runner started from a shell or by spawn
    (`python -m workman.front_compact`, `orchestra_control rc-compact`), on
    the given transcript. One log line either way; a force names its reason."""
    scan = st.Scan(home, None, proc_root)
    row = scan.row(Path(os.path.expanduser(transcript)))
    if row.get("error"):
        return False, "transcript unreadable"
    row["_scan"] = scan
    target = local_id or row["session_id"]
    if row.get("front_end") != front_end:
        why = f"front end is {row.get('front_end')}, not {front_end}"
    else:
        failed = [g for g in compact_guards(row, self_id, procs, min_tokens, max_tokens, force,
                                            reason, expect, local_id) if not g["ok"]]
        why = "; ".join(f"{g['name']}: {g['detail']}" for g in failed)
    log_line(fc.log_path(), target=target, tool="runner_check", front_end=front_end,
             result=f"refused: {why}" if why else
             "guards passed" + (f", band forced: {reason.strip()}" if force else ""))
    return not why, why


def compact_steps(row: dict, resume: str) -> list[str]:
    if row.get("front_end") == "desktop":
        steps = [f"detached runner: wait for the turn to end (<= 30 min), defer while vlc/mpv run",
                 f"hold {fc.FRONTEND_LOCK}",
                 f"xdg-open {fc.deep_link(row.get('local_id', ''))}",
                 "hash the header strip on :0, abort unless it equals the learned hash",
                 "re-check the turn is idle, click the prompt box, type /compact, click send",
                 "wait <= 6 min for a new compact_boundary in the transcript"]
    else:
        steps = ["detached runner: wait for the turn to end (<= 30 min), defer while vlc/mpv run",
                 f"hold {fc.FRONTEND_LOCK}",
                 "open the session's claude.ai/code page in Chrome on :0 (session_[bridge id])",
                 "abort unless the page shows the session title " +
                 repr(expected_titles(row)[:2]),
                 "re-check idle, click the composer, abort unless an editable field has focus",
                 "type /compact, press Return, wait <= 6 min for a new compact_boundary"]
    if resume:
        steps.append(f"re-verify the page/header, then send the resume line ({len(resume)} chars)")
    steps.append("log every outcome to " + str(fc.log_path()))
    return steps

def session_front_compact(local_id: str, transcript: str, resume: str = "", expect: str = "",
                          self_id: str = "", home: Any = None, now: float | None = None,
                          proc_root: Path = Path("/proc"),
                          procs: Callable[[], list[str]] | None = None) -> dict[str, Any]:
    """The PR #6 tool, now through session_compact's guards: the transcript's
    session must be the desktop session `local_id`, every turn guard passes,
    and the policy band holds. It runs (mode "run"); the runner rechecks."""
    why = fc.validate(local_id, transcript, expect, resume)
    if why:
        return {"ok": False, "error": why}
    return session_compact(Path(os.path.expanduser(transcript)).stem, mode="run", resume=resume,
                           self_id=self_id, home=home, now=now, proc_root=proc_root, procs=procs,
                           expect=expect, local_id=local_id)

class RemoteCompact:
    """One compaction of one Remote Control session through claude.ai/code.
    Mirrors front_compact.FrontCompact step for step; run() logs why."""

    def __init__(self, session_id: str, transcript: str, url: str, titles: list[str],
                 resume: str = "", driver: Any = None,
                 sleep: Callable[[float], None] = time.sleep,
                 procs: Callable[[], list[str]] = fc.process_names, self_id: str = "",
                 ready: Callable[[], tuple[bool, str]] | None = None,
                 resume_ready: Callable[[], tuple[bool, str]] | None = None):
        self.session_id, self.transcript, self.url = session_id, transcript, url
        self.titles, self.resume = titles, fc.clean_resume(resume)
        self.drv = driver or ChromeDriver()
        self.sleep, self.procs = sleep, procs
        # the full clean-turn check on the main transcript, every time it is asked
        self.ready = ready or (lambda: st.turn_ready(self.transcript, self_id=self_id))
        self.resume_ready = resume_ready or (lambda: st.turn_ready(
            self.transcript, self_id=self_id, after_compact=True))

    def _end(self, code: int, result: str) -> int:
        log_line(fc.log_path(), target=self.session_id, tool="session_compact",
                 front_end="remote_control", result=result)
        return code

    def run(self) -> int:
        held: list[int] = []
        try:
            return self._run(held)
        except Exception as exc:
            return self._end(fc.ERROR, f"abort: {type(exc).__name__}: {exc}")
        finally:
            for fd in held:
                os.close(fd)

    def _type_line(self, text: str) -> str:
        """Click the composer, prove an editable field has focus, type, Return."""
        composer = os.environ.get("WORKMAN_RC_COMPOSER", "Reply")
        if not self.drv.click_text(composer).get("ok"):
            return f"abort: composer {composer!r} not found"
        if not is_editable(self.drv.focused()):
            return "abort: focus is not an editable field"
        if not self.drv.type(text).get("ok", True):
            return "abort: typing failed"
        self.drv.key("Return")
        return ""

    def _run(self, held: list[int]) -> int:
        fc.state_dir().mkdir(parents=True, exist_ok=True)
        mine = fc.try_lock(fc.state_dir() / f"rc-{self.session_id}.run.lock", 0, self.sleep)
        if mine is None:
            return self._end(fc.ALREADY_RUNNING, "abort: another run for this target is active")
        held.append(mine)
        for _ in range(fc.IDLE_POLLS):
            if self.ready()[0]:
                break
            self.sleep(fc.IDLE_POLL_S)
        else:
            return self._end(fc.NEVER_IDLE, "abort: never idle in 30 min")
        for attempt in range(fc.FILM_POLLS + 1):
            if not fc.film_playing(self.procs()):
                break
            if attempt == fc.FILM_POLLS:
                return self._end(fc.FILM_TOO_LONG, "abort: film player still running after 4 h")
            self.sleep(fc.FILM_POLL_S)
        front = fc.try_lock(fc.FRONTEND_LOCK, fc.LOCK_WAIT_S, self.sleep)
        if front is None:
            return self._end(fc.LOCK_BUSY, "abort: frontend lock busy")
        held.append(front)
        before = fc.count_boundaries(self.transcript)
        if not self.drv.open_url(self.url).get("ok"):
            return self._end(fc.ERROR, "abort: could not open the session page")
        self.drv.wait_load(20)
        if not page_shows(self.drv.read_page(), self.titles):
            return self._end(fc.HEADER_MISMATCH, "abort: page does not show the session title")
        ok, busy = self.ready()
        if not ok:
            return self._end(fc.BUSY_AGAIN, f"abort: target busy again: {busy}")
        why = self._type_line("/compact")
        if why:
            return self._end(fc.HEADER_MISMATCH, why)
        for _ in range(fc.BOUNDARY_POLLS):
            if fc.count_boundaries(self.transcript) > before:
                break
            self.sleep(fc.BOUNDARY_POLL_S)
        else:
            return self._end(fc.NO_BOUNDARY, "fail: no compact_boundary within 6 min")
        self._end(fc.OK, "compacted")
        if not self.resume:
            return fc.OK
        self.sleep(fc.RESUME_SETTLE_S)
        ok, busy = fc.wait_ready(self.resume_ready, self.sleep)
        if not ok:
            return self._end(fc.OK, f"resume skipped: {busy}")
        if not page_shows(self.drv.read_page(), self.titles):
            return self._end(fc.OK, "resume skipped: page changed")
        why = self._type_line(self.resume)
        return self._end(fc.OK, f"resume skipped: {why}" if why else "resume sent")

def spawn_remote(session_id: str, transcript: str, resume: str = "", self_id: str = "",
                 min_tokens: int = COMPACT_POLICY[0], max_tokens: int = COMPACT_POLICY[1],
                 force: bool = False, reason: str = "") -> dict[str, Any]:
    """Start the RC runner detached; it re-reads the registry for the page and
    runs the guard chain again before anything else."""
    if not rg.self_ids(self_id) or rg.bad_self_id(self_id):
        return {"ok": False, "error": "refused: the caller's session id is unknown"}
    argv = [sys.executable, "-m", "workman.orchestra_control", "rc-compact", session_id,
            f"--transcript={transcript}", f"--self-id={self_id}"]
    argv += fcli.band_args(min_tokens, max_tokens, force, reason)
    if resume:
        argv.append(f"--resume={resume}")
    try:
        fc.state_dir().mkdir(parents=True, exist_ok=True)
        err = fc.state_dir() / f"rc-{session_id[:8]}.err"
        with err.open("w", encoding="utf-8") as stderr:
            proc = subprocess.Popen(argv, cwd=str(Path(__file__).resolve().parent.parent),
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=stderr, start_new_session=True)
        return {"ok": True, "pid": proc.pid, "target": session_id[:8],
                "log": str(fc.log_path()), "stderr": str(err)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def run_rc(session_id: str, transcript: str, resume: str = "", self_id: str = "",
           min_tokens: int = COMPACT_POLICY[0], max_tokens: int = COMPACT_POLICY[1],
           force: bool = False, reason: str = "", home: Any = None,
           proc_root: Path = Path("/proc"), driver: Any = None) -> int:
    """The detached Remote Control runner (`orchestra_control rc-compact`): the
    same guard chain as the MCP tool (band and force included), then the page."""
    row, err = find_session(session_id, home, None, proc_root)
    if not row:
        log_line(fc.log_path(), target=session_id, tool="session_compact",
                 front_end="remote_control", result=f"abort: {err}")
        return fc.ERROR
    if row["path"] != os.path.expanduser(transcript):
        log_line(fc.log_path(), target=session_id, tool="session_compact",
                 front_end="remote_control", result="abort: transcript is not this session's")
        return fc.ERROR
    ok, _why = runner_check(transcript, "remote_control", self_id=self_id, min_tokens=min_tokens,
                            max_tokens=max_tokens, force=force, reason=reason, home=home,
                            proc_root=proc_root)
    if not ok:
        return fc.REFUSED
    reg = row["_scan"].reg.get(row["session_id"]) or {}
    ids = rg.caller_ids(self_id, home, proc_root)
    return RemoteCompact(row["session_id"], transcript, rc_url(reg.get("_bridge") or ""),
                         expected_titles(row), resume, driver=driver,
                         self_id=",".join(ids)).run()
