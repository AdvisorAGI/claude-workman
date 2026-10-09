"""Cancel a job and list run-model-* units. Only the job's own unit
(exactly run-model-<job>.service) and processes owned() proves are the job's are
touched; a lost job is never signalled.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from .common import JOB_RE, _err, _first_int, _read, _write, jobs_root, now_iso  # jobctl:local
from .common import park_comeback  # jobctl:local
from .procs import _pid_alive, kill_tree, owned  # jobctl:local
from .status import job_status  # jobctl:local


def job_cancel(job: str) -> dict:
    """Stop a running job. Only this job's unit (exactly run-model-<job>.service) and
    processes owned() proves are this job's are touched; a lost job is never signalled."""
    st = job_status(job)
    if st.get("state") == "unknown":
        return dict(st, ok=False)
    d = st["dir"]
    rd = lambda n: _read(os.path.join(d, n)).strip()  # noqa: E731
    child = _first_int(rd("child_pid"))
    if st["state"] == "lost":
        if child:
            park_comeback(child)
        return {"ok": True, "job": job, "state": "lost", "how": [],
                "note": "the supervisor is gone, so nothing was signalled (a lost job never is); "
                        "its run-comeback file, if any, is parked. Check the host by hand."}
    if st["state"] not in ("running", "starting"):
        return {"ok": True, "job": job, "state": st["state"], "note": "already ended"}
    unit, pid, pid_start = st.get("unit") or "", st.get("pid"), rd("pid_start")
    unit_ok = unit == "run-model-%s.service" % job and bool(shutil.which("systemctl"))
    sup_ok = bool(pid) and owned(pid, job, pid_start)
    refused = []
    if unit and unit != "run-model-%s.service" % job:
        refused.append("unit %r is not run-model-%s.service; not stopped" % (unit, job))
    if pid and not sup_ok and _pid_alive(pid):
        refused.append("pid %s is not this job's supervisor; not signalled" % pid)
    if not unit_ok and not sup_ok:
        return _err("not_owned", "; ".join(refused) or "no unit or supervisor of this job runs here yet",
                    job=job, state=st["state"])
    _write(os.path.join(d, "cancelled"), now_iso() + " cancel requested\n")
    how = []
    if unit_ok:
        try:
            r = subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True,
                               text=True, timeout=60)
            how.append("systemctl stop rc=%d" % r.returncode)
        except (OSError, subprocess.SubprocessError) as exc:
            how.append("systemctl stop failed: %s" % exc)
    if sup_ok and owned(pid, job, pid_start):
        try:
            os.kill(int(pid), signal.SIGTERM)
            how.append("TERM supervisor %s" % pid)
        except OSError:
            pass
    end = time.time() + 30
    while time.time() < end and _first_int(rd("rc")) is None and owned(pid, job, pid_start):
        time.sleep(0.2)
    if child and kill_tree(child, signal.SIGKILL, job, rd("child_start")):
        how.append("KILL child %s" % child)
    if child:
        park_comeback(child)
    if _first_int(rd("rc")) is None:
        _write(os.path.join(d, "ended"), "%s %d\n" % (now_iso(), int(time.time())))
        _write(os.path.join(d, "rc"), "143\n")
    after = job_status(job)
    res = {"ok": True, "job": job, "state": after["state"], "exit": after.get("exit"), "how": how}
    if refused:
        res["refused"] = refused
    return res


def cmd_units() -> dict:
    """Every run-model-* user unit here, with the workman job each one belongs to and
    whether that job still has its record (for agent_status(reconcile=True))."""
    if not shutil.which("systemctl"):
        return {"ok": True, "units": [], "note": "no systemctl on this machine"}
    try:
        r = subprocess.run(["systemctl", "--user", "list-units", "run-model-*", "--all", "--no-legend",
                            "--plain"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return _err("systemctl_failed", str(exc))
    units = []
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if not parts or not parts[0].startswith("run-model-"):
            continue
        m = re.match(r"^run-model-(wm-.+)\.service$", parts[0])
        job = m.group(1) if m and JOB_RE.match(m.group(1)) else ""
        units.append({"unit": parts[0], "active": parts[2] if len(parts) > 2 else "",
                      "sub": parts[3] if len(parts) > 3 else "", "job": job,
                      "job_record": bool(job) and os.path.isfile(os.path.join(jobs_root(), job, "job.json"))})
    return {"ok": True, "units": units[:200]}
