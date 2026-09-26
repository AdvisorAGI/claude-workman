#!/usr/bin/env python3
"""Fleet reporter. Runs on every machine (DGX, Macs, teammate PCs) and
tells the Keyward Desk hub on the DGX what this machine is doing:
which agents are running right now and which Claude projects were
touched today. Read-only, stdlib only, one small POST every run.

Install on a new machine:
  1. copy this file to ~/fleet-report.py
  2. create ~/ENV/fleet.local.env containing the shared line
     FLEET_TOKEN=<token from Tariqul>
  3. crontab entry: */10 * * * * python3 $HOME/fleet-report.py
Hub override: set FLEET_HUB env var (default http://100.64.0.2:7071).
"""

import json
import os
import platform
import subprocess
import time
import urllib.request

HOME = os.path.expanduser("~")
HUB = os.environ.get("FLEET_HUB", "http://100.64.0.2:7071")


def token():
    try:
        for line in open(HOME + "/ENV/fleet.local.env"):
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if line.startswith("FLEET_TOKEN="):
                return line.split("=", 1)[1]
    except OSError:
        pass
    return ""


def agents():
    """Agent processes running right now, short labels only."""
    try:
        out = subprocess.run(["ps", "-eo", "args"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return []
    found = []
    for line in out.splitlines():
        low = line.lower()
        if "fleet-report" in low or "grep" in low:
            continue
        if low.startswith("claude") or "/bin/claude" in low:
            found.append("claude" + (" -p (headless)" if " -p " in line else ""))
        elif ("grok" in low and ("--cwd" in line or "--reasoning-effort" in line
                                 or " agent " in line)):
            found.append("grok builder")
        elif low.startswith("codex") or "/bin/codex" in low:
            found.append("codex")
    dedup = []
    for a in found:
        if a not in dedup:
            dedup.append(a)
    return dedup[:8]


def projects_today():
    """Claude project dirs touched in the last 24h on this machine."""
    base = HOME + "/.claude/projects"
    cutoff = time.time() - 86400
    rows = []
    try:
        for d in os.listdir(base):
            p = os.path.join(base, d)
            if os.path.isdir(p) and os.path.getmtime(p) >= cutoff:
                rows.append((os.path.getmtime(p), d.lstrip("-")))
    except OSError:
        return []
    rows.sort(reverse=True)
    return [name for _, name in rows[:10]]


def main():
    tok = token()
    if not tok:
        return
    body = json.dumps({
        "host": platform.node().split(".")[0] or "unknown",
        "platform": platform.system() + " " + platform.machine(),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "agents": agents(),
        "projects_today": projects_today(),
    }).encode()
    req = urllib.request.Request(HUB + "/api/fleet/report", data=body,
                                 headers={"Content-Type": "application/json",
                                          "X-Fleet-Token": tok})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass


def workman_json():
    """Explicit Workman-only export over authenticated SSH; no account token needed.

    Reuses the local Workman journal. Does not scan processes/projects or send HTTP.
    """
    import sys
    sys.path.insert(0, os.path.join(HOME, ".claude/tools/workman-fleet-v1.1"))
    from device import run
    request = json.load(sys.stdin)
    if not isinstance(request, dict) or request.get("action") != "report":
        raise ValueError("Workman report request required")
    print(json.dumps(run(request)))


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["--workman-json"]:
        workman_json()
    else:
        main()
