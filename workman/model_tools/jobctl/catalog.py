"""What this machine can run: run-model's direct CLIs and role table, codex
version and login, grok models, cursor-agent models.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
from .common import ANSI_RE, _read, clean_env, direct_clis, run_model_path  # jobctl:local
from .common import run_model_roles, runs_dir  # jobctl:local


def _run_out(argv, timeout=30) -> tuple:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, env=clean_env())
        return r.returncode, ANSI_RE.sub("", (r.stdout or "") + (r.stderr or ""))
    except FileNotFoundError:
        return 127, ""
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, str(exc)


def cmd_models() -> dict:
    env = clean_env()
    which = lambda b: shutil.which(b, path=env["PATH"])  # noqa: E731
    rm = run_model_path()
    rm_text = _read(rm)
    info = {"ok": True, "host": socket.gethostname().split(".")[0], "platform": sys.platform,
            "run_model": {"path": rm, "present": os.access(rm, os.X_OK),
                          "direct_clis": direct_clis(rm_text), "roles": run_model_roles(rm_text),
                          "router": os.access(os.path.join(os.path.dirname(rm), "model-router"), os.X_OK)},
            "newest": {}}
    for line in _read(os.path.join(runs_dir(), "models.env")).splitlines():
        m = re.match(r'^(NEWEST_[A-Z_]+)="?([^"\s]+)"?', line.strip())
        if m:
            info["newest"][m.group(1)] = m.group(2)
    codex = {"present": bool(which("codex"))}
    if codex["present"]:
        rc, txt = _run_out(["codex", "--version"])
        codex["version"] = txt.strip().splitlines()[-1][:80] if txt.strip() else ""
        rc, txt = _run_out(["codex", "login", "status"])
        codex["login"] = txt.strip().splitlines()[-1][:80] if txt.strip() else ""
    info["codex"] = codex
    grok = {"present": bool(which("grok"))}
    if grok["present"]:
        rc, txt = _run_out(["grok", "models"])
        grok["logged_in"] = "logged in" in txt.lower()
        m = re.search(r"Default model:\s*(\S+)", txt)
        grok["default"] = m.group(1) if m else ""
        grok["models"] = re.findall(r"^\s*[*-]\s+(\S+)", txt, re.M)[:40]
        if rc != 0:
            grok["error"] = txt.strip()[-200:]
    info["grok"] = grok
    cur = {"present": bool(which("cursor-agent"))}
    if cur["present"]:
        rc, txt = _run_out(["cursor-agent", "--list-models"])
        cur["models"] = re.findall(r"^([a-z0-9][a-z0-9.\-]+) - ", txt, re.M)[:120]
        if rc != 0:
            cur["error"] = txt.strip()[-200:]
    info["cursor"] = cur
    return info
