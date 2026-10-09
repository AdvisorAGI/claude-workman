"""Start a job: check the target, write ~/orchestra/runs/workman/<job>/, start the
supervisor as a transient systemd --user unit on Linux (never a nohup fallback
after a systemd attempt) or as a detached session on a Mac.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from .common import MODES, _err, _read, _unit_state, _write, clean_env, direct_clis  # jobctl:local
from .common import expand, grace_s, grok_dir, home, job_dir, now_iso, run_model_path  # jobctl:local
from .common import use_systemd  # jobctl:local
from .sandbox import sandbox_problem  # jobctl:local


_SELF_B64 = ""   # this file's own source, so `launch` can leave a runnable copy


def _preflight(spec: dict, host: str):
    """The reason this machine cannot run the job, or None. Nothing is written yet."""
    kind, mode = spec.get("kind"), spec.get("mode")
    if kind not in ("agent", "image", "video") or mode not in MODES:
        return _err("bad_spec", "kind=%r mode=%r" % (kind, mode))
    rm = run_model_path()
    if mode in ("direct", "role"):
        if not os.access(rm, os.X_OK):
            return _err("run_model_missing", "%s is not on %s" % (rm, host))
        if mode == "direct":
            clis = direct_clis(_read(rm))
            if spec.get("cli") not in clis:
                return _err("cli_unsupported_here",
                            "run-model on %s supports direct mode for %s only" % (host, clis or "none"),
                            supported=clis)
            return None
        router = os.environ.get("MODEL_ROUTER") or os.path.join(os.path.dirname(rm), "model-router")
        if not os.access(router, os.X_OK):
            return _err("role_mode_unavailable",
                        "%s has no %s; use direct mode (cli + effort) on this machine" % (host, router))
        return None
    if mode == "codex-image":
        if not shutil.which("codex", path=clean_env()["PATH"]):
            return _err("codex_missing", "codex is not on %s" % host)
        why = sandbox_problem()
        return _err("sandbox_unavailable", "%s (%s)" % (why, host)) if why else None
    gd = grok_dir()
    if not os.path.isfile(os.path.join(gd, "server.py")):
        return _err("grok_web_missing", "%s/server.py is not on %s" % (gd, host))
    for ref in spec.get("refs") or []:
        if not os.path.isfile(expand(ref)):
            return _err("ref_missing", expand(ref))
    return None


def _prepare(spec: dict, d: str, host: str):
    """Create the job dir and its files; return the error and remove the dir on failure."""
    os.makedirs(d)
    if spec["kind"] in ("image", "video"):
        out_dir = expand(spec.get("out_dir") or os.path.join(d, "media"))
        os.makedirs(out_dir, exist_ok=True)
        spec["out_dir"] = out_dir
    if spec["mode"] == "codex-image":
        # A throwaway, empty working dir: the read-only codex run sees nothing of ours.
        work = os.path.join(d, "work")
        os.makedirs(work)
        spec["cwd"] = work
    cwd = expand(spec.get("cwd") or d)   # default: the job dir itself, a scratch dir outside any git tree
    src = base64.b64decode(_SELF_B64).decode("utf-8") if _SELF_B64 else ""
    problem = None
    if not os.path.isdir(cwd):
        problem = _err("cwd_missing", "%s is not a directory on %s" % (cwd, host))
    elif not src:
        problem = _err("no_self_source", "launch needs the controller to pass the jobctl source")
    if problem:
        shutil.rmtree(d, ignore_errors=True)
        return problem
    spec.update(cwd=cwd, out=expand(spec.get("out") or os.path.join(d, "out.log")), dir=d, host=host,
                launched=now_iso())
    with open(os.path.join(d, "prompt.txt"), "w", encoding="utf-8") as fh:
        fh.write(spec.pop("prompt", ""))
    with open(os.path.join(d, "jobctl.py"), "w", encoding="utf-8") as fh:
        fh.write(src + "\nsys.exit(main(sys.argv[1:]))\n")
    return None


def _systemd_argv(spec: dict, d: str, name: str) -> list:
    sup_log = os.path.join(d, "supervisor.log")
    tmo = int(spec.get("timeout_s") or 2700)
    env = clean_env()
    sr = ["systemd-run", "--user", "--collect", "--quiet", "--service-type=exec",
          "--property=ExitType=cgroup", "--unit=" + name,
          "--description=workman %s %s" % (spec["kind"], spec["job"]),
          "--property=StandardOutput=append:" + sup_log,
          "--property=StandardError=append:" + sup_log,
          "--property=RuntimeMaxSec=%d" % (tmo + grace_s() + 600),
          "--working-directory=" + d,
          "--setenv=HOME=" + home(), "--setenv=PATH=" + env["PATH"],
          "--setenv=WM_JOB_UNIT=%s.service" % name]
    for k, v in sorted(env.items()):
        if k.startswith(("WM_JOBCTL_", "WORKMAN_GROK_")) or k in ("WM_RUN_MODEL", "CODEX_HOME"):
            sr.append("--setenv=%s=%s" % (k, v))
    return sr


def _start_systemd(spec: dict, d: str, argv: list):
    """Start the supervisor as run-model-<job>.service. Returns (unit, error)."""
    name = "run-model-" + spec["job"]
    # The record names the unit before systemd-run runs, so a unit that starts even
    # when this call times out is never an orphan without a job record.
    spec["unit"] = name + ".service"
    _write(os.path.join(d, "job.json"), json.dumps(spec, indent=1))
    try:
        r = subprocess.run(_systemd_argv(spec, d, name) + ["--"] + argv, capture_output=True,
                           text=True, timeout=30)
        rc, err = r.returncode, (r.stderr or "")[-300:]
    except (OSError, subprocess.SubprocessError) as exc:
        rc, err = -1, str(exc)
    if rc == 0:
        return name + ".service", None
    # Never fall back to nohup after a systemd attempt: the unit may exist anyway.
    detail = "systemd-run exit %s: %s" % (rc, err.strip())
    _write(os.path.join(d, "launch_failed"), now_iso() + " " + detail + "\n")
    return "", _err("systemd_run_failed", detail, job=spec["job"], unit=name + ".service", dir=d,
                    unit_state=_unit_state(name + ".service"), host=spec["host"])


def _start_nohup(spec: dict, d: str, argv: list) -> None:
    _write(os.path.join(d, "job.json"), json.dumps(spec, indent=1))
    with open(os.path.join(d, "supervisor.log"), "ab") as lf:
        subprocess.Popen(argv, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         cwd=d, env=clean_env(), start_new_session=True, close_fds=True)


def cmd_launch(spec: dict) -> dict:
    job = spec.get("job", "")
    try:
        d = job_dir(job)
    except ValueError as exc:
        return _err("bad_job", str(exc))
    if os.path.exists(d):
        return _err("job_exists", d)
    host = socket.gethostname().split(".")[0]
    problem = _preflight(spec, host) or _prepare(spec, d, host)
    if problem:
        return problem
    argv = [sys.executable or "python3", "-I", os.path.join(d, "jobctl.py"), "run", d]
    if use_systemd():
        unit, problem = _start_systemd(spec, d, argv)
        if problem:
            return problem
        how = "systemd"
    else:
        _start_nohup(spec, d, argv)
        unit, how = "", "nohup"
    pid = ""
    for _ in range(50):
        pid = _read(os.path.join(d, "pid")).strip()
        if pid:
            break
        time.sleep(0.1)
    res = {"ok": True, "job": job, "host": host, "dir": d, "how": how, "unit": unit,
           "pid": int(pid) if pid.isdigit() else None, "out": spec["out"], "cwd": spec["cwd"],
           "launch_log": os.path.join(d, "launch.log")}
    if spec["kind"] in ("image", "video"):
        res["out_dir"] = spec["out_dir"]
    return res
