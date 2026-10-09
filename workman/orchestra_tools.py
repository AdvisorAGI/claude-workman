"""Orchestrator session tools: see every Claude Code session on this machine
from one place, read-only, with zero model calls. The MCP tools (registered in
workman/server.py) and the CLI live here; the logic lives in workman/orchestra/:

    sessions_list    every session active recently: account, model, effort,
                     context, state, front end, owning pid    (state, registry)
    session_needs    only the sessions waiting on someone      (state)
    session_usage    tokens, images and turns per session or account, with the
                     repeated first prompts that show work being redone  (usage)
    session_events   a cursor feed of new events, backed by `collect`, which a
                     timer may run every 30-60 s                (events)

Sources, all plain files the CLI already writes: the transcripts
(<config>/projects/<slug>/<session>.jsonl, config = ~/.claude*), the live
registry (<config>/sessions/<pid>.json), oauthAccount.emailAddress/accountUuid
from .claude.json (nothing else), the desktop app's session store, /proc, and
the Stop/Notification hook feed. It consolidates the orchestra watchers,
question.py and ~/bin/agent-watch.

Never prints a token, a bridge session id, a socket path, a tool input or a
command line. Prompts, titles and questions pass through `redact`.

Env:
  WORKMAN_ORCHESTRA_DIR     where session-events.jsonl and its state live (default ~/orchestra)
  WORKMAN_FLEET_FEED        the hook feed (default ~/.claude/fleet-session-events.jsonl)
  WORKMAN_REMOTE_ORCHESTRA  the command run over ssh for machine="<alias>"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from .orchestra.common import SILENT_MIN, iso, redact
from .orchestra.events import collect, session_events  # noqa: F401
from .orchestra.registry import bad_self_id, caller_ids, is_self, self_ids  # noqa: F401
from .orchestra.state import Scan, brief, need_of, turn_ready  # noqa: F401
from .orchestra.usage import session_usage as _local_usage

NEED_ORDER = {"question": 0, "permission": 1, "blocked": 2, "limit_hit": 3, "silent": 4}


def _remote(machine: str, argv: list[str],
            run: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """Run this CLI on a fleet machine over ssh, read-only, and return its JSON."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", machine or ""):
        return {"ok": False, "error": f"bad machine name {machine!r}"}
    cmd = os.environ.get("WORKMAN_REMOTE_ORCHESTRA",
                         "cd ~/workman 2>/dev/null; python3 -m workman.orchestra_tools")
    remote = cmd + " " + " ".join(shlex.quote(a) for a in argv) + " --json"
    try:
        out = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", machine, remote],
                  capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "machine": machine, "error": f"{type(exc).__name__}: {exc}"}
    try:
        data = json.loads(out.stdout)
    except ValueError:
        return {"ok": False, "machine": machine, "rc": out.returncode,
                "error": redact(out.stderr or out.stdout or "no output", 300)}
    if isinstance(data, dict):
        data["machine"] = machine
    return data


def _local(machine: Any) -> bool:
    return machine in ("local", "", None)


def sessions_list(machine: str = "local", active_within_min: float = 180,
                  include_subagents: bool = False, silent_min: float = SILENT_MIN,
                  home: str | Path | None = None, now: float | None = None,
                  proc_root: Path = Path("/proc")) -> dict[str, Any]:
    """Every Claude Code session on this machine active within the window."""
    if not _local(machine):
        return _remote(machine, ["list", "--within", str(active_within_min)])
    scan = Scan(home, now, proc_root)
    rows = scan.rows(active_within_min * 60, include_subagents, silent_min)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    return {"ok": True, "machine": "local", "now": iso(scan.now), "count": len(rows),
            "states": counts, "sessions": [brief(r) for r in rows],
            "accounts": {k: v["email"] for k, v in scan.accts["by_dir"].items()}}


def session_needs(machine: str = "local", silent_min: float = SILENT_MIN,
                  active_within_min: float = 180, home: str | Path | None = None,
                  now: float | None = None, proc_root: Path = Path("/proc")) -> dict[str, Any]:
    """Only the sessions waiting on someone: a question, a permission prompt, a
    denial that ended the turn, a usage limit, or silence inside a turn."""
    if not _local(machine):
        return _remote(machine, ["needs", "--within", str(active_within_min),
                                 "--silent-min", str(silent_min)])
    scan = Scan(home, now, proc_root)
    needs = [n for n in (need_of(r) for r in scan.rows(active_within_min * 60,
                                                         silent_min=silent_min)) if n]
    needs.sort(key=lambda n: (NEED_ORDER.get(n["need"], 9), n["age_s"]))
    return {"ok": True, "machine": "local", "now": iso(scan.now), "count": len(needs),
            "needs": needs}


def session_usage(window_h: float = 24, by: str = "session", top: int = 25,
                  include_subagents: bool = False, machine: str = "local",
                  home: str | Path | None = None, now: float | None = None) -> dict[str, Any]:
    """Usage per session or account over the window (orchestra.usage), here or
    on a fleet machine."""
    if not _local(machine):
        return _remote(machine, ["usage", "--window-h", str(window_h), "--by", by,
                                 "--top", str(top)])
    return _local_usage(window_h, by, top, include_subagents, home, now)


# ---- CLI ------------------------------------------------------------------------------
def _fmt_age(s: int) -> str:
    return f"{s // 3600}h{(s % 3600) // 60:02d}" if s >= 3600 else f"{s // 60}m"


def print_table(rows: list[dict]) -> None:
    print(f"{'id':8} {'state':11} {'age':>5} {'ctx':>6} {'model':18} {'eff':6} {'front':14} "
          f"{'account':28} title")
    for r in rows:
        fe = r.get("front_end", "")
        if fe == "remote_control" and r.get("rc_name"):
            fe = "rc:" + r["rc_name"][:11]
        print(f"{r['id']:8} {r['state']:11} {_fmt_age(r['age_s']):>5} "
              f"{r['ctx_tokens'] // 1000:>5}k {str(r.get('model') or '')[:18]:18} "
              f"{str(r.get('effort') or '')[:6]:6} {fe[:14]:14} {str(r['account'])[:28]:28} "
              f"{str(r['title'])[:60]}")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m workman.orchestra_tools",
                                 description="Read-only views of this machine's Claude sessions.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "needs", "usage", "events", "collect"):
        sp = sub.add_parser(name)
        sp.add_argument("--json", action="store_true")
        sp.add_argument("--machine", default="local")
        sp.add_argument("--within", type=float, default=180, help="active within N minutes")
        sp.add_argument("--silent-min", type=float, default=SILENT_MIN)
        if name in ("list", "usage"):
            sp.add_argument("--subagents", action="store_true")
        if name == "usage":
            sp.add_argument("--window-h", type=float, default=24)
            sp.add_argument("--by", default="session", choices=("session", "account"))
            sp.add_argument("--top", type=int, default=25)
        if name == "events":
            sp.add_argument("--since", default="")
            sp.add_argument("--kinds", default="")
            sp.add_argument("--limit", type=int, default=50)
            sp.add_argument("--no-collect", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    if a.cmd == "list":
        out = sessions_list(a.machine, a.within, a.subagents, a.silent_min)
    elif a.cmd == "needs":
        out = session_needs(a.machine, a.silent_min, a.within)
    elif a.cmd == "usage":
        out = session_usage(a.window_h, a.by, a.top, a.subagents, a.machine)
    elif a.cmd == "events":
        out = session_events(a.since, a.kinds, a.limit, collect_first=not a.no_collect)
    else:
        out = collect(active_within_min=a.within, silent_min=a.silent_min)
    if a.json or a.cmd in ("events", "collect", "usage", "needs"):
        print(json.dumps(out, indent=None if a.cmd == "collect" else 1, default=str))
        return 0 if out.get("ok") else 1
    if not out.get("ok"):
        print(json.dumps(out))
        return 1
    print_table(out["sessions"])
    print(f"{out['count']} sessions {out['states']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
