"""Process identity and signalling. A signal only reaches a process proven to be
the job's: owned() checks that the pid is alive and that its command line names
the job id, or that its start time equals the one recorded at spawn (so a reused
pid is never hit). The supervisor's own child is signalled through its group
while it is still unreaped (supervise.py), which needs no proof.
"""
from __future__ import annotations

import os
import subprocess
import sys
from .common import JOB_RE, _read  # jobctl:local


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); treat it as gone.
    stat = _read("/proc/%d/stat" % pid)
    if stat:
        rest = stat.rsplit(")", 1)[-1].split()
        if rest and rest[0] == "Z":
            return False
    return True


def _children(pid: int) -> list:
    try:
        r = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(x) for x in r.stdout.split() if x.isdigit()]


def _cmdline(pid: int) -> str:
    raw = _read("/proc/%d/cmdline" % pid)
    if raw:
        return raw.replace("\x00", " ")
    if sys.platform.startswith("linux"):
        return ""
    try:
        r = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True,
                           text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip()


def proc_start(pid) -> str:
    """Start time of a pid (Linux /proc stat field 22, macOS ps lstart). A pid and
    its start time name one process even after the pid number is reused."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ""
    stat = _read("/proc/%d/stat" % pid)
    if stat:
        rest = stat.rsplit(")", 1)[-1].split()
        return rest[19] if len(rest) > 19 else ""
    if sys.platform.startswith("linux"):
        return ""
    try:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                           timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return " ".join(r.stdout.split())


def owned(pid, job: str, start: str = "") -> bool:
    """True only when `pid` is alive and its start time equals the one recorded when it
    was spawned or adopted. A pid plus its start time names exactly one process, so a
    reused pid never matches; with no recorded start there is no proof and no signal."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 1 or pid == os.getpid() or not JOB_RE.match(job or "") or not start:
        return False
    return _pid_alive(pid) and proc_start(pid) == start


def adopt(pid, job: str) -> str:
    """Record a pid this job did not spawn (the role router named in run-model's .meta
    file): its start time, but only while it is alive and its command line names the
    job. Returns '' otherwise, and then the pid is never signalled."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ""
    if pid <= 1 or not JOB_RE.match(job or "") or not _pid_alive(pid) or job not in _cmdline(pid):
        return ""
    return proc_start(pid)


def _signal_tree(pid: int, sig) -> None:
    """Signal pid's descendants and pid; its process group only when pid leads it."""
    seen, stack = [], [pid]
    while stack and len(seen) < 512:
        p = stack.pop()
        if p in seen:
            continue
        seen.append(p)
        stack.extend(_children(p))
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, sig)
    except OSError:
        pass
    for p in seen:
        try:
            os.kill(p, sig)
        except OSError:
            pass


def kill_tree(pid, sig, job: str, start: str = "") -> bool:
    """Signal a process, its group (when it leads one) and its descendants, but only
    after owned() proves it is this job's process. Returns whether it signalled."""
    if not owned(pid, job, start):
        return False
    _signal_tree(int(pid), sig)
    return True
