"""Job folder, heat gate, one render at a time, PID-only process handling.

A studio job is one folder, ~/studio-jobs/<YYYYMMDD>-<slug>/: the crew's documents (JOB,
STATE, BRIEF, SCRIPT, CLIPS, SOUND, QC; docs/STUDIO-CREW.md) plus clips/ stills/ audio/
ident/ out/ review/. Every local ffmpeg call goes
through `ff()` (nice 19, 4 threads) and every render waits in `wait_turn()`:
the board under the heat line (86 C stop, 80 C resume, the DGX powered off at 97 C
on 2026-09-25) and no other render writing into the same job.

Processes are only ever touched by PID: every process the studio starts for a job is
written to <job>/.studio/pids.json (`track_pid`), and `kill_job_pid` signals only a PID
in that ledger whose /proc argv still matches; never pkill/killall by name, which on
this box would hit the owner's own ffmpeg and Chrome.

    python -m workman.studio.job new <slug> [--root DIR] [--title T]
    python -m workman.studio.job status <job>
    python -m workman.studio.job heat
    python -m workman.studio.job kill <job> <pid>     (only a PID this job's ledger lists)
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

HEAT_MAX, HEAT_OK = 86.0, 80.0      # owner heat rule: stop at 86 C, resume under 80 C
HEAT_WAIT_MAX = 45 * 60             # build_v3: give up (fail closed) after 45 min hot
TURN_WAIT_MAX = 90 * 60             # build_series: longest wait for another render
FFMPEG_THREADS = "4"
THERMAL = Path("/sys/class/thermal")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
TEMPLATE_MARK = "<!-- studio:template -->"
DIRS = ("clips", "stills", "audio", "ident", "out", "review")
DOCS = {
    "JOB.md": ("# {title}: job (studio-video-director)\n\nPlan, step status, gates.\n\n## Review\n"),
    "BRIEF.md": ("# {title}: brief (studio-strategist)\n\nStance, audience, the one true claim.\n"
                 "Every claim carries a fact ID from refs/FACTS.md, e.g. (F12).\n"),
    "SCRIPT.md": ("# {title}: script (studio-copywriter)\n\nHook, turn, reveal. One thought per shot.\n"
                  "| shot | time | VO line | words on screen | fact IDs |\n|---|---|---|---|---|\n"),
    "CLIPS.md": ("# {title}: clips (studio-cinematographer)\n\n"
                 "| name | source | post id | prompt | probe | verdict |\n|---|---|---|---|---|---|\n"),
    "SOUND.md": ("# {title}: sound (studio-sound)\n\nVO takes (Brian, with-timestamps), one continuous bed, "
                 "SFX on detected frames, loudness.\n"),
    "QC.md": "# {title}: QC (studio-qc)\n\nNot run yet.\n",
    "STATE.md": ("# {title}: state (studio-producer)\n\nPhase: brief\nOwner gates: spend, publish.\n"
                 "Next: strategist writes BRIEF.md.\n"),
}


class StudioError(RuntimeError):
    """A studio step could not finish; the message says what and on what."""


class HeatTimeout(StudioError):
    """The board stayed hot past the wait limit; the job did not start."""


class TurnTimeout(StudioError):
    """Another render outlasted the wait: a condition of the machine, not of one ad."""


# ------------------------------------------------------------------ heat

def board_temp(root: Path = THERMAL) -> float:
    """Hottest thermal zone in C (0.0 when none can be read)."""
    temps = []
    for z in root.glob("thermal_zone*/temp"):
        try:
            temps.append(int(z.read_text().strip()) / 1000)
        except (OSError, ValueError):
            continue
    return max(temps, default=0.0)


def cool_down(max_wait: float = HEAT_WAIT_MAX, poll: float = 20.0, temp=None,
              sleep=time.sleep, clock=time.monotonic) -> float:
    """At or above HEAT_MAX, block until under HEAT_OK; returns seconds waited.
    Raises HeatTimeout after max_wait (fail closed: the job does not start hot)."""
    temp = temp or board_temp
    if temp() < HEAT_MAX:
        return 0.0
    t0 = clock()
    while temp() >= HEAT_OK:
        if clock() - t0 > max_wait:
            raise HeatTimeout(f"board still {temp():.1f} C after {max_wait:.0f}s, job not started")
        sleep(poll)
    return clock() - t0


# ------------------------------------------------------------------ subprocess

def sh(cmd: list[str], timeout: float = 1800) -> subprocess.CompletedProcess:
    """Run a command with a hard timeout (the child is killed by its PID on expiry)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise StudioError(f"{Path(cmd[0]).name} timed out after {timeout:.0f}s") from e
    except FileNotFoundError as e:
        raise StudioError(f"{cmd[0]} is not installed") from e
    if r.returncode:
        raise StudioError(f"{Path(cmd[0]).name} exit {r.returncode}: {(r.stderr or '')[-800:]}")
    return r


def ffmpeg_cmd(args: list[str]) -> list[str]:
    """The only ffmpeg argv the studio runs: lowest priority, four threads, no stdin."""
    return ["nice", "-n", "19", "ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-y",
            "-threads", FFMPEG_THREADS, *args]


def ff(args: list[str], timeout: float = 1800) -> str:
    """ffmpeg via ffmpeg_cmd; returns stderr (where ebur128/volumedetect report)."""
    return sh(ffmpeg_cmd(args), timeout).stderr


def probe(path: str | Path) -> dict:
    """ffprobe streams and format as a dict."""
    out = sh(["ffprobe", "-v", "error", "-show_entries",
              "stream=codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,sample_rate,channels:"
              "format=duration,bit_rate", "-of", "json", str(path)], timeout=60).stdout
    return json.loads(out)


def duration(path: str | Path) -> float:
    out = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
             timeout=60).stdout.strip()
    try:
        return float(out)
    except ValueError as e:
        raise StudioError(f"no duration for {path}: {out!r}") from e


# ------------------------------------------------------------------ one render at a time

def renders_into(procs: list[tuple[str, str, str]], job_dir: str) -> list[str]:
    """PIDs of ffmpeg processes that block a render of this job: writing into its out/,
    or running from its folder (build_series.renders_here, generalised to any job)."""
    out_dir = os.path.join(job_dir, "out") + os.sep
    return [pid for pid, args, cwd in procs if out_dir in args or cwd == job_dir]


def ffmpeg_procs() -> list[tuple[str, str, str]]:
    """(pid, argv, cwd) of every running ffmpeg, read from /proc."""
    r = subprocess.run(["pgrep", "-x", "ffmpeg"], capture_output=True, text=True, timeout=10)
    procs = []
    for pid in r.stdout.split():
        try:
            args = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            cwd = os.path.realpath(os.readlink(f"/proc/{pid}/cwd"))
        except OSError:  # exited between pgrep and the read
            continue
        procs.append((pid, args, cwd))
    return procs


def wait_turn(job_dir: str | Path, max_wait: float = TURN_WAIT_MAX, poll: float = 15.0,
              busy=None, heat=None, sleep=time.sleep, clock=time.monotonic) -> None:
    """Block until no ffmpeg renders into this job and the board is under the heat line;
    raises TurnTimeout after max_wait seconds (build_series.wait_turn)."""
    root = os.path.realpath(str(job_dir))
    busy = busy or (lambda: renders_into(ffmpeg_procs(), root))
    heat = heat or cool_down
    t0 = clock()
    while True:
        pids = busy()
        if not pids:
            heat()
            if not busy():
                return
        if clock() - t0 >= max_wait:
            raise TurnTimeout(f"another ffmpeg ({pids}) still running after {max_wait:.0f}s, render not started")
        sleep(poll)


@contextlib.contextmanager
def render_lock(path: Path | None = None, wait: float = TURN_WAIT_MAX, poll: float = 5.0):
    """One studio render at a time on this machine, across processes (flock)."""
    path = path or lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TurnTimeout(f"studio render lock {path} still held after {wait:.0f}s") from None
                time.sleep(poll)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def pid_cmdline(pid: int) -> str:
    """argv of a live PID joined by spaces, or '' when it is gone."""
    try:
        return Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def kill_pid(pid: int, expect: str, sig: int = signal.SIGTERM) -> dict:
    """Signal one PID, only if /proc says its argv still contains `expect`
    (a recycled PID is never signalled). Never kills by name."""
    args = pid_cmdline(pid)
    if not args:
        return {"ok": False, "error": "not_running", "pid": pid}
    if expect not in args:
        return {"ok": False, "error": "pid_mismatch", "pid": pid, "expect": expect}
    try:
        os.kill(int(pid), sig)
    except OSError as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "pid": pid}
    return {"ok": True, "pid": pid, "signal": int(sig)}


# ------------------------------------------------------------------ PID ledger

MAX_PIDS = 200      # the ledger keeps the newest entries only


def _ledger(job_dir: Path) -> Path:
    return job_dir / ".studio" / "pids.json"


def tracked(job_dir: Path) -> list[dict]:
    """Every process the studio started for this job: [{pid, what, expect, started}]."""
    try:
        rows = json.loads(_ledger(job_dir).read_text())
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("pid"), int)] if isinstance(rows, list) else []


def track_pid(job_dir: Path, pid: int, what: str, expect: str, log: str = "") -> dict:
    """Record a PID this job started; `expect` is an argv substring that identifies it later."""
    row = {"pid": int(pid), "what": what[:80], "expect": expect, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if log:
        row["log"] = log
    rows = (tracked(job_dir) + [row])[-MAX_PIDS:]
    _ledger(job_dir).parent.mkdir(parents=True, exist_ok=True)
    _ledger(job_dir).write_text(json.dumps(rows, indent=1))
    return row


def kill_job_pid(job_dir: Path, pid: int, sig: int = signal.SIGTERM) -> dict:
    """Signal a PID only when this job's ledger lists it and its argv still matches."""
    row = next((r for r in reversed(tracked(job_dir)) if r["pid"] == int(pid)), None)
    if row is None:
        return {"ok": False, "error": "not_tracked", "pid": pid,
                "detail": "the studio only signals PIDs it started for this job"}
    return kill_pid(pid, row["expect"], sig)


# ------------------------------------------------------------------ job folder

def job_dirname(slug: str, day: str | None = None) -> str:
    """<YYYYMMDD>-<slug>, the folder name under the jobs root."""
    return f"{day or time.strftime('%Y%m%d')}-{slug}"


def jobs_root() -> Path:
    """~/studio-jobs, or WORKMAN_STUDIO_ROOT (read per call so tests and sessions can point elsewhere)."""
    return Path(os.environ.get("WORKMAN_STUDIO_ROOT", "~/studio-jobs")).expanduser()


def lock_path() -> Path:
    return Path(os.environ.get("WORKMAN_STUDIO_LOCK", "~/.cache/workman-studio/render.lock")).expanduser()


def resolve_job(job: str | Path, root: Path | None = None) -> Path:
    """A job given as a path (contains a slash or starts with ~), as a folder name under root
    (20260925-shield), or as a slug (the newest <date>-<slug> folder under root)."""
    s = str(job)
    if os.sep in s or s.startswith("~"):
        p = Path(s).expanduser()
        if not p.is_absolute():
            raise StudioError(f"job path {s!r} must be absolute (or ~/...)")
        return p.resolve()
    base = (root or jobs_root()).expanduser()
    if re.fullmatch(r"\d{8}-[a-z0-9][a-z0-9-]{0,63}", s):
        return base / s
    if not SLUG.match(s):
        raise StudioError(f"job {s!r} is neither a path, a <YYYYMMDD>-<slug> folder nor a slug")
    found = sorted(p for p in base.glob("[0-9]" * 8 + f"-{s}") if p.is_dir())
    return found[-1] if found else base / job_dirname(s)


def new_job(slug: str, root: Path | None = None, title: str = "", day: str | None = None) -> dict:
    """Create <root>/<YYYYMMDD>-<slug>/ with its folders and documents; never overwrites a file."""
    if not SLUG.match(slug):
        raise StudioError(f"slug {slug!r} must be a-z, 0-9 and dashes, at most 64 chars")
    if day is not None and not re.fullmatch(r"\d{8}", day):
        raise StudioError(f"day {day!r} must be YYYYMMDD")
    title = (title or slug).replace("\n", " ")[:120]
    d = (root or jobs_root()).expanduser() / job_dirname(slug, day)
    created, kept = [], []
    for sub in DIRS:
        (d / sub).mkdir(parents=True, exist_ok=True)
    for name, body in DOCS.items():
        p = d / name
        if p.exists():
            kept.append(name)
            continue
        p.write_text(body.format(title=title) + "\n" + TEMPLATE_MARK + "\n")
        created.append(name)
    return {"ok": True, "job": str(d), "created": created, "kept": kept, "dirs": list(DIRS)}


def qc_verdict(text: str) -> str:
    """The first line verdict of a QC.md: 'PASS', 'FAIL', 'PENDING REVIEW', or 'none'."""
    m = re.search(r"^RESULT:\s*(PASS|FAIL|PENDING REVIEW)\b", text, re.M)
    return m.group(1) if m else "none"


def render_state(job_dir: Path) -> dict:
    """The newest background render in this job's PID ledger and whether it still runs."""
    rows = [r for r in tracked(job_dir) if r.get("what", "").startswith("render")]
    if not rows:
        return {"state": "none"}
    rec = rows[-1]
    alive = rec["expect"] in pid_cmdline(rec["pid"])
    log = Path(rec.get("log", "")) if rec.get("log") else None
    tail = log.read_text(errors="replace").splitlines()[-12:] if log and log.is_file() else []
    return {"state": "running" if alive else "finished", "pid": rec["pid"], "what": rec["what"],
            "started": rec.get("started"), "log": str(log) if log else None, "log_tail": tail}


def status(job: str | Path, root: Path | None = None) -> dict:
    """Documents written or still templates, clips, renders, QC verdict, render process, heat."""
    d = resolve_job(job, root)
    if not d.is_dir():
        raise StudioError(f"no job folder at {d}")
    docs = {}
    for name in DOCS:
        p = d / name
        docs[name] = "missing" if not p.exists() else ("template" if TEMPLATE_MARK in p.read_text() else "written")
    qc = d / "QC.md"
    return {"ok": True, "job": str(d), "docs": docs,
            "clips": sorted(p.name for p in (d / "clips").glob("*.mp4")) if (d / "clips").is_dir() else [],
            "renders": sorted(p.name for p in (d / "out").glob("*.mp4")) if (d / "out").is_dir() else [],
            "qc": qc_verdict(qc.read_text()) if qc.exists() else "none",
            "render": render_state(d),
            "processes": [{"pid": r["pid"], "what": r["what"], "alive": r["expect"] in pid_cmdline(r["pid"])}
                          for r in tracked(d)[-10:]],
            "heat_c": round(board_temp(), 1),
            "heat_gate": {"stop_at": HEAT_MAX, "resume_under": HEAT_OK}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m workman.studio.job", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new")
    n.add_argument("slug")
    n.add_argument("--root", type=Path)
    n.add_argument("--title", default="")
    s = sub.add_parser("status")
    s.add_argument("job")
    sub.add_parser("heat")
    k = sub.add_parser("kill")
    k.add_argument("job")
    k.add_argument("pid", type=int)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "new":
            out = new_job(a.slug, a.root, a.title)
        elif a.cmd == "status":
            out = status(a.job)
        elif a.cmd == "kill":
            out = kill_job_pid(resolve_job(a.job), a.pid)
        else:
            out = {"heat_c": board_temp(), "stop_at": HEAT_MAX, "resume_under": HEAT_OK}
    except StudioError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
