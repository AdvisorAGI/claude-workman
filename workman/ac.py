"""The office Gree AC, through the DGX driver ~/bin/ac.

The driver owns everything about the unit: the LAN protocol (UDP 7000 to
192.168.0.184), the 16-30 C bounds, reading status before every change, and
the temperature lock (one background `ac _loop` that resets the setpoint every
60 s while the AC is on and never powers it on). This module only checks the
command against the driver's public verbs and runs it with no shell, a
timeout and bounded output, so a tool call cannot reach the internal `_loop`
verb or pass anything the driver would not accept. Only `set` takes several
KEY=VALUE words; every other verb takes at most one plain word.

The driver exists on the DGX only. Elsewhere the answer names the route:
remote(host='dgx', verb='ac', args=[...]). WORKMAN_AC_DRIVER overrides the
path (the test suite points it at a fake) and skips the host check.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess

DRIVER_ENV = "WORKMAN_AC_DRIVER"
DGX_HOST = "amg-dgx"
SETTINGS = frozenset({"power", "mode", "fan", "turbo", "quiet", "louver", "hswing", "xfan", "health", "sleep",
                      "light", "light-sensor", "saving", "heat8", "fresh-air", "anti-direct"})
VERBS = SETTINGS | {"status", "on", "off", "temp", "set", "cool-quiet", "lock", "fixed", "log", "help"}
MAX_CMD = 200
MAX_SET = 12
TIMEOUT_S = 60.0  # the driver's worst case is 5 UDP exchanges x 3 tries x 3 s
MAX_OUTPUT = 4000
_TOKEN = re.compile(r"[a-z0-9-]{1,16}")
_PAIR = re.compile(r"[a-z0-9-]{1,16}=[a-z0-9-]{1,16}")


def parse_cmd(cmd: str) -> list[str]:
    """'temp 26' -> ['temp', '26']; ValueError unless it is one public driver verb plus at most one plain word,
    or `set` plus 1-12 KEY=VALUE words ('set fan=low turbo=off')."""
    if not isinstance(cmd, str) or len(cmd) > MAX_CMD:
        raise ValueError(f"cmd must be a string of at most {MAX_CMD} characters")
    argv = cmd.split() or ["status"]
    if argv[0] not in VERBS:
        raise ValueError(f"unknown ac command {argv[0][:20]!r}; one of {', '.join(sorted(VERBS))}")
    if argv[0] == "set":
        if not 1 <= len(argv) - 1 <= MAX_SET or not all(_PAIR.fullmatch(a) for a in argv[1:]):
            raise ValueError(f"set takes 1-{MAX_SET} KEY=VALUE words, e.g. 'set mode=cool fan=low temp=26'")
        return argv
    if len(argv) > 2 or not all(_TOKEN.fullmatch(a) for a in argv):
        raise ValueError("ac takes a verb and at most one plain argument, e.g. 'temp 26' or 'lock off'")
    return argv


def driver() -> str | None:
    """The driver path on this host, or None off the DGX."""
    override = os.environ.get(DRIVER_ENV)
    if override:
        return override if os.path.exists(override) else None
    path = os.path.expanduser("~/bin/ac")
    on_dgx = socket.gethostname().split(".")[0] == DGX_HOST
    return path if on_dgx and os.path.exists(path) else None


def run(cmd: str = "status") -> dict:
    """Run one driver command; always answers {ok, cmd, output} or {ok: False, error, ...}, never raises."""
    try:
        argv = parse_cmd(cmd)
    except ValueError as exc:
        return {"ok": False, "error": "bad_cmd", "detail": str(exc)}
    path = driver()
    if path is None:
        return {"ok": False, "error": "no_driver", "cmd": " ".join(argv),
                "detail": "the office AC driver ~/bin/ac is on the DGX; "
                          "use remote(host='dgx', verb='ac', args=[...])"}
    try:
        r = subprocess.run([path, *argv], capture_output=True, text=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "cmd": " ".join(argv),
                "detail": f"{path} gave no answer in {TIMEOUT_S:g} s"}
    except OSError as exc:
        return {"ok": False, "error": "driver_failed", "cmd": " ".join(argv), "detail": f"{path}: {exc}"}
    out = (r.stdout + r.stderr).strip()
    return {"ok": r.returncode == 0, "cmd": " ".join(argv), "exit": r.returncode,
            "output": out if len(out) <= MAX_OUTPUT else "..." + out[-MAX_OUTPUT:]}
