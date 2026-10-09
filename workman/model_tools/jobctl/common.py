"""Target-side constants and small helpers shared by every jobctl module: paths,
file reads and writes, the worker environment allow-list, run-model header parsing.

Money rule carried here, not only in the caller: a worker gets an allow-listed
environment only (ENV_ALLOW / ENV_ALLOW_PREFIX, never a name that looks like a key,
token, secret or Claude/Anthropic variable, in any letter case), so no API key,
BASH_ENV, ENV, NODE_OPTIONS or PYTHON* setting reaches it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time


MARK = "@@WM@@"


JOB_RE = re.compile(r"^wm-[0-9]{8}T[0-9]{6}Z-[a-z0-9]{2,4}-[a-z0-9]{2,8}-[0-9a-f]{6}$")


RESULT_CAP = 4000


TAIL_BYTES = 256 * 1024


GRACE_S = 90


IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")


MAGIC = ((b"\x89PNG", "png"), (b"\xff\xd8\xff", "jpg"), (b"RIFF", "webp"))


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


# Worker environment allow-list (owner money rule): exact names, then prefixes whose
# names must also miss ENV_DENY_RE. Anything else, in any letter case, is dropped.
ENV_ALLOW = frozenset((
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "TZ", "TMPDIR",
    "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
    "DBUS_SESSION_BUS_ADDRESS", "SSH_AUTH_SOCK", "WORK_TASK_ID", "WORK_ROLE", "CODEX_HOME",
    "MODEL_ROUTER", "WM_RUN_MODEL", "WM_JOB_UNIT"))


ENV_ALLOW_PREFIX = ("LC_", "RUN_MODEL_", "MODEL_ROUTER_", "SESSION_ESCAPE_", "WM_JOBCTL_", "WORKMAN_GROK_")


ENV_DENY_RE = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|COOKIE|CLAUDE|ANTHROPIC", re.I)


# Loader and injection variables: never passed, even when WM_JOBCTL_KEEP_ENV names them.
ENV_NEVER_RE = re.compile(r"^(BASH_ENV|ENV|NODE_OPTIONS|NODE_PATH|PYTHON\w*|LD_\w*|DYLD_\w*|PERL5?LIB|PERL5OPT|"
                          r"RUBYOPT|RUBYLIB|GIT_\w*|SHELLOPTS|BASHOPTS|PS4|PROMPT_COMMAND|IFS|CDPATH|"
                          r"GLOBIGNORE|ZDOTDIR|JAVA_TOOL_OPTIONS|_JAVA_OPTIONS)$", re.I)


# What a codex image job keeps (plus HOME = its empty work dir, CODEX_HOME and a PATH of
# the sandbox's own bin dir and the system dirs: never ~/bin or ~/.local/bin).
IMAGE_ENV_KEEP = ("LANG", "LANGUAGE", "TERM", "TZ", "USER", "LOGNAME", "WORK_TASK_ID")
IMAGE_PATH = ("/tmp/wm-sbx/bin", "/usr/local/bin", "/usr/bin", "/bin")


MODES = ("direct", "role", "grok", "codex-image")


DIRECT_CASE_RE = re.compile(r'case "\$\{1:-\}" in ([a-z|]+)\) direct')


ROLE_LINE_RE = re.compile(r"^#   ([a-z][a-z0-9-]+)\s{2,}(.+)$")


def _emit(obj) -> int:
    sys.stdout.write(MARK + json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0


def _err(error: str, detail: str = "", **extra) -> dict:
    d = {"ok": False, "error": error}
    if detail:
        d["detail"] = detail
    d.update(extra)
    return d


def home() -> str:
    return os.path.expanduser("~")


def jobs_root() -> str:
    return os.path.join(home(), "orchestra", "runs", "workman")


def runs_dir() -> str:
    return os.path.join(home(), "orchestra", "runs")


def run_model_path() -> str:
    return os.environ.get("WM_RUN_MODEL") or os.path.join(home(), "bin", "run-model")


def grok_dir() -> str:
    return os.path.expanduser(os.environ.get("WORKMAN_GROK_WEB_DIR") or "~/grok-web-mcp")


def grok_python() -> str:
    return os.environ.get("WORKMAN_GROK_PYTHON") or "/usr/bin/python3"


def grace_s() -> int:
    try:
        return max(0, int(os.environ.get("WM_JOBCTL_GRACE", str(GRACE_S))))
    except ValueError:
        return GRACE_S


def poll_s() -> float:
    try:
        return max(0.05, float(os.environ.get("WM_JOBCTL_POLL", "1")))
    except ValueError:
        return 1.0


def expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path)) if path else ""


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _read(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return default


def _write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _tail(path: str, nbytes: int = TAIL_BYTES) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - nbytes))
            data = fh.read()
    except OSError:
        return ""
    return ANSI_RE.sub("", data.decode("utf-8", errors="replace"))


def _json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _first_int(text: str):
    m = re.search(r"-?\d+", text or "")
    return int(m.group(0)) if m else None


def job_dir(job: str) -> str:
    if not JOB_RE.match(job or ""):
        raise ValueError("bad job id %r" % (job,))
    return os.path.join(jobs_root(), job)


def direct_clis(rm_text: str) -> list:
    m = DIRECT_CASE_RE.search(rm_text or "")
    return m.group(1).split("|") if m else []


def run_model_roles(rm_text: str) -> dict:
    roles = {}
    for line in (rm_text or "").splitlines()[:40]:
        m = ROLE_LINE_RE.match(line)
        if m:
            roles[m.group(1)] = re.sub(r"\s+", " ", m.group(2)).strip()[:110]
    return roles


def use_systemd() -> bool:
    if os.environ.get("WM_JOBCTL_NO_SYSTEMD") == "1":
        return False
    return sys.platform.startswith("linux") and bool(shutil.which("systemd-run"))


def newest_model(name: str, default: str) -> str:
    for line in _read(os.path.join(runs_dir(), "models.env")).splitlines():
        m = re.match(r'^%s="?([A-Za-z0-9._-]+)"?' % re.escape(name), line.strip())
        if m:
            return m.group(1)
    return default


def clean_env(base=None) -> dict:
    """The worker environment, built from an allow-list: no API key, token or
    Claude/Anthropic variable in any letter case, no BASH_ENV/ENV/NODE_OPTIONS/PYTHON*.
    WM_JOBCTL_KEEP_ENV may name extra variables (tests), still filtered by ENV_DENY_RE,
    and ENV_NEVER_RE names (BASH_ENV, ENV, NODE_OPTIONS, PYTHON*, LD_*...) never pass."""
    src = base if base is not None else os.environ
    keep = {n.strip() for n in src.get("WM_JOBCTL_KEEP_ENV", "").split(",") if n.strip()}
    env = {}
    for k, v in src.items():
        if ENV_NEVER_RE.match(k):
            continue
        if k in ENV_ALLOW:
            env[k] = v
        elif (k.startswith(ENV_ALLOW_PREFIX) or k in keep) and not ENV_DENY_RE.search(k):
            env[k] = v
    h = home()
    extra = [os.path.join(h, ".local", "bin"), os.path.join(h, "bin")]
    tail = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    parts = extra + [p for p in env.get("PATH", "").split(":") if p] + tail
    seen, path = set(), []
    for p in parts:
        if p not in seen:
            seen.add(p)
            path.append(p)
    env["PATH"] = ":".join(path)
    env["HOME"] = h
    return env


def image_env(env: dict, work: str, codex_home: str) -> dict:
    """The environment of a codex image run (inside the bwrap sandbox): HOME is its empty
    work dir; CODEX_HOME is the sandbox's allow-listed codex home; PATH is the sandbox's
    bin dir (node) and the system dirs only."""
    out = {k: v for k, v in env.items() if k in IMAGE_ENV_KEEP or k.startswith("LC_")}
    out["PATH"] = ":".join(IMAGE_PATH)
    out["CODEX_HOME"] = codex_home
    out["HOME"] = work
    return out


def _unit_state(unit: str) -> str:
    if not unit or not shutil.which("systemctl"):
        return ""
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (r.stdout or "").strip()


def park_comeback(rm_pid) -> None:
    f = os.path.join(runs_dir(), "active", "%s.job" % rm_pid)
    if os.path.isfile(f):
        stopped = os.path.join(runs_dir(), "active", "stopped")
        os.makedirs(stopped, exist_ok=True)
        try:
            os.replace(f, os.path.join(stopped, os.path.basename(f)))
        except OSError:
            pass


def image_kind(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
    except OSError:
        return ""
    for magic, kind in MAGIC:
        if head.startswith(magic):
            return kind
    return ""


def _new_images(folder: str, since: float) -> list:
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    out = []
    for n in names:
        p = os.path.join(folder, n)
        if n.lower().endswith(IMAGE_EXT) and os.path.isfile(p) and os.path.getmtime(p) >= since:
            if image_kind(p):
                out.append(p)
    return out
