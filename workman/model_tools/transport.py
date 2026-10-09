"""Controller transport: fleet machines, job ids, the jobctl bundle piped to
`python3 -I -` locally or over the fleet ssh aliases, and the local job registry
(~/orchestra/runs/workman-jobs). A job record is written before every launch, so a
launch whose reply never came back still has a record and a unit name.
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from . import jobctl
from .jobctl.common import JOB_RE, MARK, _err


MACHINES = {
    "dgx": {"alias": "dgx", "code": "dgx", "hosts": ("amg-dgx",)},
    "mac-one": {"alias": "mac-one", "code": "m1", "hosts": ("room1-tariqul",)},
    "mac-air": {"alias": "mac-air", "code": "air", "hosts": ("muhammads-macbook-air-13846",)},
}


MACHINE_ALIASES = {"m1": "mac-one", "mac-mini": "mac-one", "macone": "mac-one", "air": "mac-air",
                   "macair": "mac-air", "spark": "dgx", "amg-dgx": "dgx"}


CODE_MACHINE = {v["code"]: k for k, v in MACHINES.items()}
CODE_MACHINE["loc"] = "local"


DEFAULT_MACHINE = "mac-one"      # owner 17:14Z: builds and suites off the DGX


GROK_MACHINE = "dgx"             # ~/grok-web-mcp and its CDP Chrome live on the DGX


SSH_CONNECT_S = 10


TERMINAL = ("finished", "failed", "timed_out", "cancelled", "lost", "unknown", "launch_failed")


# Launch replies after which the job may exist on the target although we were not told so.
UNCONFIRMED = ("timeout", "no_reply", "bad_reply", "systemd_run_failed")


_SRC_CACHE: dict = {}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def local_machine() -> str:
    env = (os.environ.get("WORKMAN_MACHINE") or "").strip().lower()
    if env:
        return MACHINE_ALIASES.get(env, env)
    host = socket.gethostname().split(".")[0].lower()
    for name, m in MACHINES.items():
        if host in m["hosts"]:
            return name
    return "local"


def resolve_machine(machine: str | None, default: str = DEFAULT_MACHINE) -> str:
    m = (machine or "").strip().lower() or default
    m = MACHINE_ALIASES.get(m, m)
    if m == "local":
        return local_machine()
    if m not in MACHINES:
        raise ValueError(f"machine must be one of {sorted(MACHINES)} or 'local', got {machine!r}")
    return m


def is_local(machine: str) -> bool:
    return machine == "local" or machine == local_machine()


def machine_of(job: str) -> str | None:
    m = re.match(r"^wm-\d{8}T\d{6}Z-([a-z0-9]+)-", job or "")
    if not m or not JOB_RE.match(job):
        return None
    name = CODE_MACHINE.get(m.group(1))
    if name is None:
        return None
    return local_machine() if name == "local" else name


def new_job_id(machine: str, what: str) -> str:
    code = MACHINES[machine]["code"] if machine in MACHINES else "loc"
    what = re.sub(r"[^a-z0-9]", "", what.lower())[:8] or "job"
    return f"wm-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{code}-{what}-{secrets.token_hex(3)}"


def _ctl_source() -> str:
    """The target-side program: the jobctl modules bundled into one file."""
    if "src" not in _SRC_CACHE:
        _SRC_CACHE["src"] = jobctl.bundle()
    return _SRC_CACHE["src"]


def _program(spec: dict | None = None, with_self: bool = False) -> str:
    src = _ctl_source()
    tail = []
    if spec is not None:
        blob = base64.b64encode(json.dumps(spec).encode("utf-8")).decode("ascii")
        tail.append(f"_SPEC_B64 = {blob!r}")
    if with_self:
        tail.append(f"_SELF_B64 = {base64.b64encode(src.encode('utf-8')).decode('ascii')!r}")
    tail.append("sys.exit(main(sys.argv[1:]))")
    return src + "\n" + "\n".join(tail) + "\n"


def _local_python() -> str:
    return os.environ.get("WORKMAN_JOBCTL_PYTHON") or sys.executable or "python3"


def _ssh_argv(machine: str, args: list[str]) -> list[str]:
    alias = MACHINES[machine]["alias"]
    return ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_CONNECT_S}", alias,
            "python3 -I - " + " ".join(shlex.quote(a) for a in args)]


def _ssh_detail(stderr: str) -> str:
    lines = [ln for ln in (stderr or "").splitlines() if ln.strip()]
    return " | ".join(lines[-4:])[-500:]


def _call(machine: str, args: list[str], spec: dict | None = None, timeout: float = 60,
          with_self: bool = False) -> dict:
    """Run one jobctl subcommand on `machine`; return its JSON reply or an error
    that names the failing layer."""
    prog = _program(spec, with_self)
    local = is_local(machine)
    argv = [_local_python(), "-I", "-", *args] if local else _ssh_argv(machine, args)
    try:
        r = subprocess.run(argv, input=prog, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return _err("timeout", f"no reply from {machine} within {timeout:.0f}s", machine=machine)
    except FileNotFoundError as exc:
        return _err("spawn_failed", str(exc), machine=machine)
    out = r.stdout or ""
    i = out.rfind(MARK)
    if i < 0:
        detail = _ssh_detail(r.stderr) or out[-300:]
        if not local and r.returncode == 255:
            return _err("unreachable", detail, machine=machine, layer="ssh",
                        hint=f"check `ssh {MACHINES[machine]['alias']} true`; fleet-connect tries "
                             "cable > office LAN > Headscale > Cloudflare")
        return _err("no_reply", detail, machine=machine, exit=r.returncode)
    try:
        data = json.loads(out[i + len(MARK):].splitlines()[0])
    except (ValueError, IndexError) as exc:
        return _err("bad_reply", str(exc), machine=machine)
    if isinstance(data, dict):
        data.setdefault("machine", machine)
    return data


def _registry_dir() -> Path:
    return Path(os.path.expanduser("~/orchestra/runs/workman-jobs"))


def _save(rec: dict) -> None:
    d = _registry_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{rec['job']}.tmp"
    tmp.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    os.replace(tmp, d / f"{rec['job']}.json")


def _delete(job: str) -> None:
    try:
        (_registry_dir() / f"{job}.json").unlink()
    except OSError:
        pass


def _launch(machine: str, spec: dict, rec: dict) -> dict:
    """Write the job record first, then launch. A reply that leaves the outcome open
    (timeout, no reply, failed systemd-run) keeps the record and returns the job id and
    unit name, so the unit can never become an orphan nobody knows about."""
    job = spec["job"]
    rec = dict(rec, launch="pending", started=_now_iso())
    _save(rec)
    res = _call(machine, ["launch"], spec=spec, timeout=90, with_self=True)
    if res.get("ok"):
        rec.update(launch="ok", cwd=res.get("cwd"), out=res.get("out"), unit=res.get("unit"),
                   dir=res.get("dir"), host=res.get("host"), out_dir=res.get("out_dir"))
        _save(rec)
        return res
    if res.get("error") in UNCONFIRMED:
        unit = res.get("unit") or f"run-model-{job}.service"
        rec.update(launch="unconfirmed", launch_error=res.get("error"), unit_if_systemd=unit)
        _save(rec)
        return dict(res, job=job, launch_unconfirmed=True, unit_if_systemd=unit,
                    hint="the job may exist on the target: agent_status(job), "
                         "agent_status(reconcile=True); agent_cancel(job) stops it")
    _delete(job)      # the target answered and started nothing
    return res


def _load(job: str) -> dict:
    try:
        return json.loads((_registry_dir() / f"{job}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _recent(limit: int) -> list[dict]:
    d = _registry_dir()
    if not d.is_dir():
        return []
    files = sorted(d.glob("wm-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for f in files[:limit]:
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def _check_job(job: str) -> str | dict:
    m = machine_of(job)
    if m is None:
        return _err("bad_job", f"not a workman job id: {job!r}")
    return m
