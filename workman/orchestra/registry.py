"""Who and where: accounts (email only), the CLI's live registry
(<config>/sessions/<pid>.json), the desktop app's session store, /proc, tmux,
headless seat runs, and which session is the caller."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from .common import SELF_ENV, _read_json, _read_span, config_dirs, home_dir, orchestra_dir


def _oauth(path: Path) -> tuple[str, str]:
    """(email, accountUuid) from a .claude.json; nothing else is kept."""
    data = _read_json(path)
    acct = data.get("oauthAccount") if isinstance(data, dict) else None
    if not isinstance(acct, dict):
        return "", ""
    email, uuid = acct.get("emailAddress"), acct.get("accountUuid")
    return (email if isinstance(email, str) else "", uuid if isinstance(uuid, str) else "")

def accounts(home: str | Path | None = None) -> dict[str, Any]:
    """{"by_dir": {".claude-x": {"email", "source"}}, "by_uuid": {uuid: email}}.

    A dir's own .claude.json wins; the default ~/.claude dir falls back to
    ~/.claude.json, which is the file the CLI uses when CLAUDE_CONFIG_DIR is
    unset. Desktop sessions are resolved by their store's account uuid instead.
    """
    h = home_dir(home)
    by_dir: dict[str, dict] = {}
    by_uuid: dict[str, str] = {}
    root_email, root_uuid = _oauth(h / ".claude.json")
    if root_uuid and root_email:
        by_uuid[root_uuid] = root_email
    for d in config_dirs(h):
        email, uuid = _oauth(d / ".claude.json")
        if uuid and email:
            by_uuid.setdefault(uuid, email)
        if d.name == ".claude" and root_email:
            by_dir[d.name] = {"email": root_email, "source": "~/.claude.json"}
        elif email:
            by_dir[d.name] = {"email": email, "source": f"~/{d.name}/.claude.json"}
        else:
            by_dir[d.name] = {"email": "", "source": "none"}
    return {"by_dir": by_dir, "by_uuid": by_uuid}

REG_KEYS = ("pid", "sessionId", "cwd", "kind", "entrypoint", "name", "status",
            "waitingFor", "startedAt", "updatedAt")

def _pid_alive(pid: Any, proc_root: Path) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        return b"claude" in (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return False

def registry(home: str | Path | None = None, proc_root: Path = Path("/proc")) -> dict[str, dict]:
    """{sessionId: entry} from every <config>/sessions/<pid>.json. Only the
    allow-listed keys are kept; the bridge id is kept privately (as `_bridge`)
    for routing and never printed. Alive and newest wins per session."""
    out: dict[str, dict] = {}
    for d in config_dirs(home):
        for f in (d / "sessions").glob("*.json"):
            data = _read_json(f)
            if not isinstance(data, dict) or not isinstance(data.get("sessionId"), str):
                continue
            e = {k: data.get(k) for k in REG_KEYS}
            e["config_dir"] = d.name
            e["alive"] = _pid_alive(data.get("pid"), proc_root)
            bridge = data.get("bridgeSessionId")
            e["bridge"] = bool(bridge)
            e["_bridge"] = bridge if isinstance(bridge, str) else ""
            host = data.get("hostSessionId")
            e["host_local_id"] = host if isinstance(host, str) and host.startswith("local_") else ""
            sid = data["sessionId"]
            old = out.get(sid)
            rank = (e["alive"], e.get("updatedAt") or 0)
            if old is None or rank > (old["alive"], old.get("updatedAt") or 0):
                out[sid] = e
    return out

DESK_KEYS = ("sessionId", "cliSessionId", "cwd", "title", "model", "effort", "isArchived",
             "lastActivityAt", "permissionMode")

def desktop_store_dir(home: str | Path | None = None) -> Path:
    return home_dir(home) / ".config" / "Claude" / "claude-code-sessions"

def desktop_sessions(home: str | Path | None = None) -> dict[str, dict]:
    """{cliSessionId: entry} from the desktop app's session files, allow-listed."""
    out: dict[str, dict] = {}
    root = desktop_store_dir(home)
    for f in root.glob("*/*/local_*.json"):
        data = _read_json(f)
        if not isinstance(data, dict) or not isinstance(data.get("cliSessionId"), str):
            continue
        e = {k: data.get(k) for k in DESK_KEYS}
        e["account_uuid"] = f.parent.parent.name
        e["file"] = str(f)
        old = out.get(data["cliSessionId"])
        if old is None or (e.get("lastActivityAt") or 0) > (old.get("lastActivityAt") or 0):
            out[data["cliSessionId"]] = e
    return out

def _flag(args: list[str], name: str) -> str:
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return ""

def process_info(pid: int, proc_root: Path = Path("/proc")) -> dict[str, Any]:
    """What a front end needs from a process, never its command line:
    model/effort flags, print mode, the parent `claude rc --name`, the unit."""
    base = proc_root / str(pid)
    try:
        args = [a.decode("utf-8", "replace") for a in
                (base / "cmdline").read_bytes().split(b"\0") if a]
    except OSError:
        return {}
    info: dict[str, Any] = {"pid": pid, "model": _flag(args, "--model"),
                            "effort": _flag(args, "--effort"),
                            "print": "-p" in args or "--print" in args,
                            "sdk_url": any(a == "--sdk-url" or a.startswith("--sdk-url=")
                                           for a in args)}
    try:
        stat = (base / "stat").read_text()
        info["ppid"] = int(stat.rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        info["ppid"] = 0
    try:
        cg = (base / "cgroup").read_text().strip().splitlines()[-1]
        info["unit"] = cg.rsplit("/", 1)[-1]
    except (OSError, IndexError):
        info["unit"] = ""
    info["rc_name"] = ""
    if info["ppid"]:
        try:
            pargs = [a.decode("utf-8", "replace") for a in
                     (proc_root / str(info["ppid"]) / "cmdline").read_bytes().split(b"\0") if a]
        except OSError:
            pargs = []
        if len(pargs) > 1 and pargs[1] in ("rc", "remote-control"):
            info["rc_name"] = _flag(pargs, "--name") or "rc"
    return info

def ancestors(pid: int, proc_root: Path = Path("/proc"), depth: int = 8) -> list[int]:
    out = []
    for _ in range(depth):
        try:
            stat = (proc_root / str(pid) / "stat").read_text()
            pid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        if pid <= 1:
            break
        out.append(pid)
    return out

def tmux_pane_pids(run: Callable[..., Any] = subprocess.run) -> set[int]:
    try:
        out = run(["tmux", "list-panes", "-a", "-F", "#{pane_pid}"], capture_output=True,
                  text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return set()
    if getattr(out, "returncode", 1) != 0:
        return set()
    return {int(x) for x in (out.stdout or "").split() if x.isdigit()}

def seat_runs(home: str | Path | None = None) -> dict[str, str]:
    """{session_id: unit} for headless seat runs recorded in RUNS.jsonl
    (run-claude-seat writes one row per finished run)."""
    path = orchestra_dir(home) / "runs" / "RUNS.jsonl"
    out: dict[str, str] = {}
    size = path.stat().st_size if path.exists() else 0
    for line in _read_span(path, max(0, size - (1 << 20)), 1 << 20):
        if '"seat"' not in line or '"session_id"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        sid = d.get("session_id") if isinstance(d, dict) else None
        if isinstance(sid, str) and len(sid) >= 8:
            out[sid] = str(d.get("unit") or "")
    return out

# A session id: uuid-shaped, at least 8 lowercase hex chars before any dash. A
# name like "owner" is not one and would make the own-session check a no-op.
SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}[0-9a-f-]*$")


def bad_self_id(explicit: str = "") -> str:
    """Why an explicit self id is unusable, "" when every part is a session id."""
    for part in (explicit or "").split(","):
        p = part.strip()
        if p and not SESSION_ID_RE.match(p):
            return f"self id {p[:24]!r} is not a session id (>= 8 hex chars)"
    return ""


def self_ids(explicit: str = "") -> list[str]:
    """Every session id this caller answers to: env CLAUDE_SESSION_ID /
    CLAUDE_CODE_SESSION_ID and an explicit one (comma-separated allowed).
    Anything that is not a session id is dropped."""
    ids = [os.environ.get(k, "") for k in SELF_ENV] + (explicit or "").split(",")
    return list(dict.fromkeys(i.strip() for i in ids if SESSION_ID_RE.match(i.strip())))

def caller_ids(explicit: str = "", home: str | Path | None = None,
               proc_root: Path = Path("/proc")) -> list[str]:
    """self_ids plus the session of any Claude process this one runs under
    (the live registry entry whose pid is an ancestor). Empty means the caller
    cannot be told apart from a target, and every control action refuses; a
    malformed explicit id empties it too, so a bad --self-id never runs."""
    if bad_self_id(explicit):
        return []
    ids = self_ids(explicit)
    chain = {os.getpid(), *ancestors(os.getpid(), proc_root, depth=12)}
    for sid, e in registry(home, proc_root).items():
        if e.get("alive") and e.get("pid") in chain and sid not in ids:
            ids.append(sid)
    return ids

def is_self(session_id: str, explicit: str = "", local_id: str = "",
            ids: list[str] | None = None) -> bool:
    """The target is the caller. A prefix of at least 8 either way counts, so a
    short id from a table still matches. False when no caller id is known:
    control actions check `caller_ids` themselves and refuse on empty."""
    for mine in (ids if ids is not None else self_ids(explicit)):
        for theirs in (session_id or "", local_id or ""):
            if not theirs:
                continue
            short = min(len(mine), len(theirs))
            if short >= 8 and mine[:short] == theirs[:short]:
                return True
    return False
