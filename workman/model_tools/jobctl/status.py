"""Job state and the bounded result of a job on this machine.
"""
from __future__ import annotations

import json
import os
import re
import socket
import time
from .common import ANSI_RE, RESULT_CAP, TAIL_BYTES, _first_int, _json, _read, _tail  # jobctl:local
from .common import _unit_state, job_dir  # jobctl:local
from .procs import owned  # jobctl:local


def _times(d: str):
    started = _read(os.path.join(d, "started")).split()
    ended = _read(os.path.join(d, "ended")).split()
    t0 = int(started[1]) if len(started) > 1 and started[1].isdigit() else None
    t1 = int(ended[1]) if len(ended) > 1 and ended[1].isdigit() else None
    return (started[0] if started else ""), t0, t1


def _state(job: str, d: str, rc, unit_state: str, t0) -> str:
    exists = lambda n: os.path.exists(os.path.join(d, n))  # noqa: E731
    if rc is not None:
        if exists("cancelled"):
            return "cancelled"
        if exists("timed_out"):
            return "timed_out"
        return "finished" if rc == 0 else "failed"
    # A pid only counts as this job's supervisor when it is provably ours (no pid reuse).
    pid = _first_int(_read(os.path.join(d, "pid")))
    if pid and owned(pid, job, _read(os.path.join(d, "pid_start")).strip()):
        return "running"
    if unit_state in ("active", "activating", "reloading"):
        return "running"
    if exists("launch_failed"):
        return "launch_failed"
    if t0 is None and time.time() - os.path.getmtime(d) < 30:
        return "starting"
    return "cancelled" if exists("cancelled") else "lost"


def _model_effort(s: dict, launch: str, role: dict):
    """The model and effort that really ran: run-model's own line, or for role mode
    the router's lane and the codex header the router copied."""
    m = re.search(r"run-model: \S+ (\S+) effort=", launch)
    model = m.group(1) if m else ""
    effort = "router" if s.get("mode") == "role" else (s.get("effort") or "")
    if role.get("out"):
        lane = _read(role["out"] + ".lane").strip()
        seat = re.search(r"done seat=(\S+)", _tail(role.get("err", ""), 65536))
        model = lane or (seat.group(1) if seat else model)
        m3 = re.search(r"^reasoning effort: (\S+)", _read(role["out"])[:4096], re.M)
        if m3:
            effort = m3.group(1)
    return model, effort


def _notes(launch: str) -> list:
    pats = (("looks NOT LOGGED IN", "cli not logged in on this machine"),
            ("hit a usage limit", "cli hit a usage limit"),
            ("only .. GB RAM free", "refused: low RAM"))
    return [note for pat, note in pats if re.search(pat, launch)]


def job_status(job: str) -> dict:
    try:
        d = job_dir(job)
    except ValueError as exc:
        return {"job": job, "state": "unknown", "error": str(exc)}
    if not os.path.isdir(d):
        return {"job": job, "state": "unknown", "error": "no such job on %s" % socket.gethostname()}
    s = _json(os.path.join(d, "job.json")) or {}
    rc = _first_int(_read(os.path.join(d, "rc")))
    unit = s.get("unit") or ""
    ustate = _unit_state(unit) if unit and rc is None else ""
    started, t0, t1 = _times(d)
    state = _state(job, d, rc, ustate, t0)
    launch = _tail(os.path.join(d, "launch.log"), 16384)
    role = _json(os.path.join(d, "role.json")) or {}
    model, effort = _model_effort(s, launch, role)
    now = time.time()
    st = {"job": job, "state": state, "kind": s.get("kind"), "mode": s.get("mode"),
          "cli": s.get("cli") or "", "role": s.get("role") or "", "provider": s.get("provider") or "",
          "model": model, "effort": effort, "machine": s.get("machine") or "",
          "host": s.get("host") or socket.gethostname().split(".")[0], "cwd": s.get("cwd") or "",
          "started": started or s.get("launched", ""),
          "age_s": int(now - t0) if t0 else None, "runtime_s": int((t1 or now) - t0) if t0 else None,
          "exit": rc, "pid": _first_int(_read(os.path.join(d, "pid"))), "unit": unit,
          "unit_state": ustate, "out": role.get("out") or s.get("out") or "",
          "launch_log": os.path.join(d, "launch.log"), "dir": d}
    if state == "launch_failed":
        st["launch_error"] = _read(os.path.join(d, "launch_failed")).strip()[:300]
    man = _json(s.get("out", "") + ".manifest.json") if s.get("out") else None
    if isinstance(man, dict):
        st["manifest"] = {k: man.get(k) for k in ("exit", "seconds", "report", "dirty_action", "commit")}
    notes = _notes(launch)
    if notes:
        st["notes"] = notes
    return st


def codex_final(text: str) -> str:
    """Codex exec prints `tokens used`, the count, then the final message."""
    lines = text.splitlines()
    idx = None
    for i, line in enumerate(lines):
        if line.strip() == "tokens used":
            idx = i
    if idx is None:
        return ""
    rest = lines[idx + 1:]
    if rest and re.fullmatch(r"[\d,._ ]+", rest[0].strip() or "x"):
        rest = rest[1:]
    return "\n".join(rest).strip()


def clamp_text(text: str, max_lines: int, cap: int = RESULT_CAP):
    text = ANSI_RE.sub("", text or "").rstrip()
    lines = text.splitlines()
    truncated = False
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        truncated = True
    out = "\n".join(lines)
    if len(out) > cap:
        out = out[-cap:]
        truncated = True
    return out, truncated


def job_result(job: str, max_lines: int = 40) -> dict:
    st = job_status(job)
    if st.get("state") == "unknown":
        return st
    max_lines = max(1, min(int(max_lines), 200))
    d = st["dir"]
    text, source = "", ""
    report = ((st.get("manifest") or {}).get("report") or "")
    if report and os.path.isfile(report):
        text, source = _tail(report, 64 * 1024), "report"
    role = _json(os.path.join(d, "role.json")) or {}
    if not text and role.get("out") and os.path.isfile(role["out"]):
        text, source = _tail(role["out"], 64 * 1024), "role_out"
        fin = codex_final(text)   # the Mac router copies the whole codex transcript into .out
        if fin:
            text, source = fin, "role_final_message"
    if not text and st.get("mode") == "grok":
        r = _json(os.path.join(d, "result.json"))
        if r is not None:
            text, source = json.dumps(r, indent=1), "grok_result"
    final = _read(os.path.join(d, "final.txt")).strip()
    if not text and final:
        text, source = final, "final_message"
    out_tail = _tail(st.get("out", ""), TAIL_BYTES) if st.get("out") else ""
    if not text and out_tail:
        fin = codex_final(out_tail)
        if fin:
            text, source = fin, "final_message"
        else:
            text, source = out_tail, "log_tail"
    if not text:
        text, source = _tail(os.path.join(d, "launch.log"), 32768), "launch_log"
    body, truncated = clamp_text(text, max_lines)
    res = {"job": job, "state": st["state"], "exit": st.get("exit"), "source": source,
           "text": body, "truncated": truncated, "out": st.get("out"),
           "launch_log": st.get("launch_log"), "dir": d}
    if report:
        res["report"] = report
    files = _json(os.path.join(d, "files.json"))
    if files:
        res["files"] = files.get("files", [])[:20]
    gr = _json(os.path.join(d, "result.json"))
    if gr:
        res["files"] = (gr.get("files") or [])[:20]
        for k in ("post_ids", "post_id_prefixes", "error"):
            if gr.get(k):
                res[k] = gr[k]
    if st.get("notes"):
        res["notes"] = st["notes"]
    return res
