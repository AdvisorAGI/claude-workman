"""Agent tools: launch a non-Claude CLI agent through run-model, read its state and
bounded result, cancel it, and reconcile run-model units against job records.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from .transport import DEFAULT_MACHINE, _call, _check_job, _launch, _load, _recent, is_local
from .transport import local_machine, machine_of, new_job_id, resolve_machine
from .jobctl.common import RESULT_CAP, _err
from .jobctl.status import clamp_text


CLIS = ("codex", "grok", "cursor", "astra", "luna", "terra")


CLI_FAMILY = {"codex": "codex", "astra": "codex", "luna": "codex", "terra": "codex",
              "grok": "grok", "cursor": "cursor"}


EFFORTS = ("max", "xhigh", "high", "medium", "low")


# run-model role -> the CLI family that runs its first lane (from run-model's header).
ROLE_FAMILY = {
    "build": "grok", "build-fast": "grok", "grok45": "grok",
    "build-sol": "codex", "sol": "codex", "build-codex": "codex",
    "review": "codex", "review-deep": "codex", "strategy": "codex", "council": "codex",
    "fast": "codex", "cheap": "codex", "mid": "codex", "legacy": "codex",
    "review-grok": "cursor", "quick": "cursor",
    "local": "local", "local-vision": "local",
}


MAX_PROMPT = 131072              # run-model's argv limit


def _validate_agent(cli, prompt, effort, cwd, machine, role, out, timeout_s) -> dict:
    """Normalised agent_run arguments, or the refusal; nothing has started yet."""
    cli = (cli or "").strip().lower()
    role = (role or "").strip().lower() or None
    if role:
        fam = ROLE_FAMILY.get(role)
        if fam is None:
            return _err("unknown_role", f"role must be one of {sorted(ROLE_FAMILY)}")
        if cli and cli != "local" and CLI_FAMILY.get(cli) != fam:
            return _err("role_cli_mismatch", f"role {role!r} runs on {fam}, not {cli!r}")
    elif cli not in CLIS:
        return _err("unknown_cli", f"cli must be one of {list(CLIS)} (or pass a run-model role)")
    effort = (effort or "high").strip().lower()
    if effort not in EFFORTS:
        return _err("bad_effort", f"effort must be one of {list(EFFORTS)}")
    if not isinstance(prompt, str) or not prompt.strip():
        return _err("empty_prompt")
    if len(prompt.encode("utf-8")) > MAX_PROMPT:
        return _err("prompt_too_big", "over run-model's 128 KB argv limit: put the brief in a file "
                                      "and point the prompt at it")
    try:
        timeout_s = int(timeout_s)
    except (TypeError, ValueError):
        return _err("bad_timeout", "timeout_s must be whole seconds")
    if not 30 <= timeout_s <= 86400:
        return _err("bad_timeout", "timeout_s must be 30..86400")
    if out and not str(out).startswith(("/", "~")):
        return _err("bad_out", "out must be an absolute path on the target machine")
    if cwd and not str(cwd).startswith(("/", "~")):
        return _err("bad_cwd", "cwd must be an absolute (or ~/) path on the target machine")
    try:
        m = resolve_machine(machine)
    except ValueError as exc:
        return _err("bad_machine", str(exc))
    return {"cli": cli, "role": role, "mode": "role" if role else "direct",
            "fam": ROLE_FAMILY[role] if role else CLI_FAMILY[cli], "effort": effort,
            "timeout_s": timeout_s, "machine": m}


def agent_run(cli: str, prompt: str, effort: str = "high", cwd: str = "",
              machine: str = DEFAULT_MACHINE, role: str | None = None, out: str | None = None,
              timeout_s: int = 2700) -> dict:
    """Launch a non-Claude CLI agent through run-model, detached. Returns the job id
    at once; poll agent_status / agent_result. cwd defaults to the job's own scratch
    dir on the target. role (a run-model router role such as cheap, review, build-sol,
    review-grok) uses run-model's role mode instead of direct mode; then effort is the
    router's choice. run-model's own contract applies: a successful write run in a git
    tree is committed and pushed by ~/bin/work-commit unless the prompt says read-only."""
    v = _validate_agent(cli, prompt, effort, cwd, machine, role, out, timeout_s)
    if "error" in v:
        return v
    cli, role, mode, fam, effort, timeout_s, m = (v[k] for k in (
        "cli", "role", "mode", "fam", "effort", "timeout_s", "machine"))
    what = fam if mode == "role" else cli
    job = new_job_id(m, what)
    spec = {"job": job, "kind": "agent", "mode": mode, "cli": cli or fam, "role": role or "",
            "effort": effort, "cwd": cwd or "", "out": out or "", "timeout_s": timeout_s,
            "machine": m, "prompt": prompt}
    rec = {"job": job, "machine": m, "kind": "agent", "mode": mode, "cli": cli or fam,
           "role": role or "", "effort": effort if mode == "direct" else "router",
           "timeout_s": timeout_s, "prompt_preview": prompt.strip().replace("\n", " ")[:90]}
    res = _launch(m, spec, rec)
    if not res.get("ok"):
        return res
    reply = {"ok": True, "job": job, "machine": m, "host": res.get("host"), "mode": mode,
             "cli": rec["cli"], "role": rec["role"], "effort": rec["effort"], "how": res.get("how"),
             "unit": res.get("unit"), "pid": res.get("pid"), "log": res.get("out"),
             "launch_log": res.get("launch_log"), "cwd": res.get("cwd"),
             "next": "agent_status(job) then agent_result(job)"}
    return reply


def _status_one(job: str) -> dict:
    m = _check_job(job)
    if isinstance(m, dict):
        return m
    r = _call(m, ["status", job], timeout=60)
    if not r.get("ok"):
        return dict(r, job=job)
    st = r["jobs"][0]
    st["machine"] = m
    rec = _load(job)
    if rec.get("prompt_preview"):
        st["prompt"] = rec["prompt_preview"]
    st["ok"] = st.get("state") != "unknown"
    return st


def _run_model_units() -> list[str]:
    if not sys.platform.startswith("linux"):
        return []
    try:
        r = subprocess.run(["systemctl", "--user", "list-units", "run-model-*", "--no-legend",
                            "--plain", "--state=active"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()][:12]


def reconcile_units() -> dict:
    """run-model-* units on the Linux machines (here and the DGX) set against the job
    records: workman units whose job has no record are orphans; launches whose outcome
    was never confirmed are listed too."""
    names = []
    for m in (local_machine(), "dgx"):
        if m not in names and (m == "dgx" or (is_local(m) and sys.platform.startswith("linux"))):
            names.append(m)
    out: dict[str, dict] = {}
    for m in names:
        r = _call(m, ["units"], timeout=60)
        if not r.get("ok"):
            out[m] = {"error": r.get("error"), "detail": r.get("detail", "")[:200]}
            continue
        mine = [u for u in r.get("units", []) if u.get("job")]
        orphans = [dict(u, registry=bool(_load(u["job"]))) for u in mine
                   if not u.get("job_record") or not _load(u["job"])]
        out[m] = {"workman_units": len(mine), "orphans": orphans[:50],
                  "other_run_model_units": [u["unit"] for u in r.get("units", []) if not u.get("job")][:20]}
    pending = [{k: r.get(k) for k in ("job", "machine", "launch", "launch_error", "unit_if_systemd",
                                       "started")}
               for r in _recent(200) if r.get("launch") in ("unconfirmed", "pending")]
    return {"machines": out, "unconfirmed_launches": pending[:50]}


def agent_status(job: str | None = None, limit: int = 20, reconcile: bool = False) -> dict:
    """One job, or the most recent `limit` jobs started from this machine, with
    state, model, effort, machine, age and exit code. reconcile=True adds the
    run-model-* units that have no job record, and launches never confirmed."""
    if job:
        return _status_one(job.strip())
    limit = max(1, min(int(limit or 20), 100))
    recs = _recent(limit)
    by_machine: dict[str, list[str]] = {}
    for rec in recs:
        m = machine_of(rec.get("job", ""))
        if m:
            by_machine.setdefault(m, []).append(rec["job"])
    previews = {r["job"]: r.get("prompt_preview", "") for r in recs}
    results: dict[str, dict] = {}

    def fetch(m: str, ids: list[str]) -> None:
        results[m] = _call(m, ["status", *ids], timeout=60)

    threads = [threading.Thread(target=fetch, args=(m, ids)) for m, ids in by_machine.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    jobs = []
    for m, ids in by_machine.items():
        r = results.get(m) or {}
        if r.get("ok"):
            for st in r.get("jobs", []):
                st["machine"] = m
                st["prompt"] = previews.get(st.get("job"), "")
                jobs.append(st)
        else:
            for j in ids:
                jobs.append({"job": j, "machine": m, "state": "unreachable",
                             "error": r.get("error"), "detail": r.get("detail", "")[:200],
                             "prompt": previews.get(j, "")})
    jobs.sort(key=lambda s: s.get("job", ""), reverse=True)
    counts: dict[str, int] = {}
    for st in jobs:
        counts[st.get("state", "?")] = counts.get(st.get("state", "?"), 0) + 1
    keep = ("job", "state", "machine", "host", "kind", "mode", "cli", "role", "provider", "model",
            "effort", "started", "age_s", "runtime_s", "exit", "unit", "out", "notes", "prompt",
            "error", "detail")
    res = {"ok": True, "jobs": [{k: s[k] for k in keep if s.get(k) not in (None, "", [])}
                                for s in jobs],
           "counts": counts, "run_model_units_here": _run_model_units()}
    if reconcile:
        res["reconcile"] = reconcile_units()
    return res


def agent_result(job: str, max_lines: int = 40) -> dict:
    """The bounded tail of a job's result: its report file if run-model found one,
    else the CLI's final message, else the log tail. Never more than RESULT_CAP chars."""
    m = _check_job((job or "").strip())
    if isinstance(m, dict):
        return m
    try:
        n = max(1, min(int(max_lines), 200))
    except (TypeError, ValueError):
        n = 40
    r = _call(m, ["result", job.strip(), str(n)], timeout=60)
    if "text" in r:
        r["text"], cut = clamp_text(r["text"], n, RESULT_CAP)
        r["truncated"] = bool(r.get("truncated")) or cut
    r.setdefault("ok", r.get("state") not in (None, "unknown"))
    return r


def agent_cancel(job: str) -> dict:
    """Stop a job: systemctl --user stop on its unit (Linux) or TERM to its
    supervisor (Mac); the worker tree is killed and run-comeback will not revive it."""
    m = _check_job((job or "").strip())
    if isinstance(m, dict):
        return m
    return _call(m, ["cancel", job.strip()], timeout=120)
