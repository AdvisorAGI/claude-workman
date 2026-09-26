"""Drive another fleet machine's desktop fast: one persistent channel per host.

The old way ran `ssh host wm.py <verb>` per action: a fresh ssh session, a
fresh Python, a fresh backend import, for every screenshot or click. This
module keeps ONE remote `wm.py serve` process alive per host behind a local
relay, so a call is a JSON line each way over an already-open pipe:

    caller (CLI or MCP tool)
      -> unix socket ~/.cache/workman/remote/<host>.sock
      -> relay process (python -m workman.remote --relay <host>)
      -> one `ssh host` child running `wm.py serve` on the remote
      -> that machine's own backend (Workman.app daemon on the Mac mini,
         workman.server on Linux), which does the human synthesis locally.

Path selection is the fleet ssh ladder already in ~/.ssh/config: the
`fleet-connect.sh` ProxyCommand races the point-to-point cable, LAN and
Headscale candidates and takes the first that answers, then falls back to
DERP or Cloudflare Access. The transport it picked is reported in status().

The remote never needs a newer wm.py installed: the relay streams this
machine's ~/.claude/tools/wm.py over the channel and the remote interpreter
executes it, so both ends always run the same code. Only a Python and the
backend venv have to exist over there (the launcher tries the known venvs
in order, the daemon-backed one first on a Mac).

Every remote action is the remote's own `wm.py` verb, so Human Mode, the
owner-pause switch and the TCC grants are all the remote machine's.
"""
from __future__ import annotations

import base64
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

#: Logical names -> ssh alias (what ~/.ssh/config knows).
HOSTS = {
    "mac-mini": "mac-mini", "mini": "mac-mini", "tariqul": "mac-mini",
    "md": "md", "air": "air", "dgx": "dgx", "spark": "dgx",
    "wind1": "wind1", "windows": "wind1",
}
#: hostname -s values, to run locally instead of ssh-ing to ourselves.
LOCAL_NAMES = {"dgx": ("spark-0ed8",), "mac-mini": ("room1-tariqul",),
               "md": ("mds-mac-mini",), "air": ("muhammads-macbook-air",)}

#: Remote interpreters, in order. The Mac mini's daemon-backed venv first:
#: over ssh it is the only route with Screen Recording and Accessibility.
#: This deliberately differs from wm.py's VENVS (workman venv first): an ssh
#: session's TCC grants come from sshd, which holds no Screen Recording, so
#: workman.server's own Quartz capture comes back blank there. Removable once
#: the mac-mini's ~/workman venv can capture over ssh (a grant for its python,
#: or capture routed through the Workman.app daemon).
REMOTE_PYTHONS = (
    "$HOME/src/claude-atmos-workman/computer-mcp/.venv/bin/python",
    "$HOME/workman/.venv/bin/python",
    "$HOME/claude-atmos-workman/.venv/bin/python",
    "python3",
)
SENTINEL = "#__WM_END__"
IDLE_EXIT_S = float(os.environ.get("WORKMAN_REMOTE_IDLE_S", "900"))
READY_TIMEOUT_S = 25.0
# No multiplexing: riding another session's ControlMaster skips the
# fleet-connect ProxyCommand, so the transport went unreported (null), and
# the channel would die with a master this relay does not own.
SSH = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
       "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
       "-o", "ControlMaster=no", "-o", "ControlPath=none"]

_LOADER = (
    "import sys\n"
    "src=''\n"
    "for line in iter(sys.stdin.readline, ''):\n"
    f"    if line.rstrip('\\n') == {SENTINEL!r}: break\n"
    "    src += line\n"
    "sys.argv=['wm.py','serve']\n"
    "g={'__name__':'__main__','__file__':'wm.py'}\n"
    "exec(compile(src,'wm.py','exec'), g)\n"
)


def launcher_script() -> str:
    """The shell the remote runs: pick an interpreter, exec the loader."""
    py = " ".join(shlex.quote(p) if not p.startswith("$") else p.replace(" ", "")
                  for p in REMOTE_PYTHONS)
    return (
        "export WM_NO_REEXEC=1; "
        f"for p in {py}; do "
        "if [ -x \"$p\" ] || command -v \"$p\" >/dev/null 2>&1; then "
        f"exec \"$p\" -c {shlex.quote(_LOADER)}; fi; done; "
        "echo '{\"ready\": false, \"error\": \"no python found on remote\"}'; exit 2"
    )


def wm_source_path() -> Path:
    return Path(os.environ.get("WM_PY_SOURCE") or "~/.claude/tools/wm.py").expanduser()


def resolve(host: str) -> str:
    key = (host or "").strip().lower()
    if key not in HOSTS:
        raise ValueError(f"unknown host {host!r}; known: {sorted(set(HOSTS.values()))}")
    return HOSTS[key]


def is_local(host: str) -> bool:
    alias = resolve(host)
    me = socket.gethostname().split(".")[0].lower()
    return me in LOCAL_NAMES.get(alias, ())


def sock_dir() -> Path:
    d = Path(os.environ.get("WORKMAN_REMOTE_DIR") or "~/.cache/workman/remote").expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def sock_path(alias: str) -> Path:
    return sock_dir() / f"{alias}.sock"


def info_path(alias: str) -> Path:
    return sock_dir() / f"{alias}.json"


class RemoteError(RuntimeError):
    pass


# ---- the ssh session ---------------------------------------------------------
class Session:
    """One ssh child running `wm.py serve` on the remote."""

    def __init__(self, alias: str, source: str | None = None):
        self.alias = alias
        self.transport = None
        self.stderr_lines: list[str] = []
        self.ready: dict = {}
        self.lock = threading.Lock()
        src = source if source is not None else wm_source_path().read_text()
        self.proc = subprocess.Popen(
            SSH + [alias, launcher_script()],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self.proc.stdin.write(src)
        if not src.endswith("\n"):
            self.proc.stdin.write("\n")
        self.proc.stdin.write(SENTINEL + "\n")
        self.proc.stdin.flush()
        self.ready = self._read_json(READY_TIMEOUT_S)
        if not self.ready.get("ready"):
            err = self.ready.get("error") or " | ".join(self.stderr_lines[-3:]) or "no ready line"
            self.close()
            raise RemoteError(f"{alias}: remote did not start: {err}")

    def _drain_stderr(self) -> None:
        for line in self.proc.stderr:
            line = line.rstrip("\n")
            self.stderr_lines.append(line)
            del self.stderr_lines[:-50]
            if "fleet-connect[" in line and " via " in line:
                self.transport = line.split(" via ", 1)[1].strip()

    def _read_json(self, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if line == "":
                raise RemoteError(f"{self.alias}: remote closed the channel "
                                  f"({' | '.join(self.stderr_lines[-3:])})")
            line = line.strip()
            if not line.startswith("{"):
                continue  # stray chatter from the remote shell
            try:
                return json.loads(line)
            except ValueError:
                continue
        raise RemoteError(f"{self.alias}: timed out waiting for the remote")

    def alive(self) -> bool:
        return self.proc.poll() is None

    def call(self, verb: str, args: list | None = None, timeout: float = 60.0) -> dict:
        with self.lock:
            if not self.alive():
                raise RemoteError(f"{self.alias}: remote session died")
            req = {"verb": verb, "args": list(args or [])}
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
            reply = self._read_json(timeout)
        return reply.get("result", reply)

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


# ---- the relay ------------------------------------------------------------------
def _exit_on_sigterm():
    """Make SIGTERM unwind through the relay's cleanup. Returns the previous
    handler; None off the main thread, where Python cannot set one."""
    if threading.current_thread() is not threading.main_thread():
        return None

    def handler(signum, frame):
        raise SystemExit(0)
    return signal.signal(signal.SIGTERM, handler)


def relay_main(host: str) -> int:
    """Serve host's channel on the unix socket until idle, a `_shutdown`
    request or SIGTERM; every exit removes the socket and the info file."""
    # Fixed 2026-09-12: with no handler SIGTERM (`wm.py stop`) killed the relay
    # before its `finally`, leaving <host>.sock and <host>.json behind, and the
    # stale json aimed the next stop at a pid that could by then be anyone's.
    previous = _exit_on_sigterm()
    try:
        return _relay(resolve(host))
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def _relay(alias: str) -> int:
    path = sock_path(alias)
    path.unlink(missing_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    session = None
    try:
        listener.bind(str(path))
        os.chmod(path, 0o600)
        listener.listen(8)
        listener.settimeout(IDLE_EXIT_S)
        try:
            session = Session(alias)
        except RemoteError as exc:
            info_path(alias).write_text(json.dumps({"alias": alias, "error": str(exc),
                                                    "at": time.time()}))
            sys.stderr.write(str(exc) + "\n")
            return 2
        info_path(alias).write_text(json.dumps({
            "alias": alias, "pid": os.getpid(), "started": time.time(),
            "transport": session.transport, "remote": session.ready,
        }))
        last_used = time.monotonic()
        while True:
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                if time.monotonic() - last_used >= IDLE_EXIT_S:
                    return 0
                continue
            last_used = time.monotonic()
            with conn, conn.makefile("r") as rf, conn.makefile("w") as wf:
                for line in rf:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        req = json.loads(line)
                        if req.get("verb") == "_shutdown":
                            wf.write(json.dumps({"result": {"ok": True,
                                                            "relay_pid": os.getpid()}}) + "\n")
                            wf.flush()
                            return 0
                        if req.get("verb") == "_transport":
                            result = {"ok": True, "transport": session.transport,
                                      "remote": session.ready, "relay_pid": os.getpid()}
                        else:
                            result = session.call(req.get("verb", ""), req.get("args") or [],
                                                  timeout=float(req.get("timeout") or 60))
                    except RemoteError as exc:
                        result = {"ok": False, "error": str(exc)}
                        wf.write(json.dumps({"result": result}) + "\n")
                        wf.flush()
                        return 3
                    except Exception as exc:
                        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                    wf.write(json.dumps({"result": result}, default=str) + "\n")
                    wf.flush()
                    last_used = time.monotonic()
                    if not session.alive():
                        return 3
    finally:
        if session is not None:
            session.close()
            # A failed start keeps its error info: _connect reports it.
            info_path(alias).unlink(missing_ok=True)
        listener.close()
        path.unlink(missing_ok=True)


def relay_command(alias: str) -> tuple[list[str], dict[str, str]]:
    """argv and env for the relay child, such that `workman.remote` imports
    whichever interpreter the caller itself was started with."""
    # Fixed 2026-09-12: this was `sys.executable -m workman.remote`, and wm.py
    # run by /usr/bin/python3 finds this package only through a sys.path insert
    # the child never inherits ("No module named workman.remote", then a 25 s
    # timeout). The package root on PYTHONPATH makes the import independent of
    # the interpreter; the repo venv is preferred so the relay matches the server.
    root = Path(__file__).resolve().parents[1]
    venv = root / ".venv" / "bin" / "python"
    py = str(venv) if os.access(venv, os.X_OK) else sys.executable
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(root), env.get("PYTHONPATH")) if p)
    return [py, "-m", "workman.remote", "--relay", alias], env


def _spawn_relay(alias: str) -> None:
    argv, env = relay_command(alias)
    with open(sock_dir() / f"{alias}.relay.log", "ab") as log:
        subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=log, start_new_session=True)


def _connect(alias: str, start: bool = True, wait_s: float = READY_TIMEOUT_S) -> socket.socket:
    path = sock_path(alias)
    deadline = time.monotonic() + wait_s
    spawned = False
    while True:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(str(path))
            return s
        except OSError:
            s.close()
        if not start:
            raise RemoteError(f"no relay for {alias}")
        if not spawned:
            _spawn_relay(alias)
            spawned = True
        if time.monotonic() >= deadline:
            info = {}
            try:
                info = json.loads(info_path(alias).read_text())
            except Exception:
                pass
            raise RemoteError(f"relay for {alias} did not come up: {info.get('error', 'timeout')}")
        time.sleep(0.1)


def call(host: str, verb: str, args: list | None = None, timeout: float = 60.0) -> dict:
    """Run one wm.py verb on `host`, through the persistent relay."""
    alias = resolve(host)
    if is_local(host):
        return _local(verb, args or [])
    s = _connect(alias)
    with s, s.makefile("r") as rf, s.makefile("w") as wf:
        wf.write(json.dumps({"verb": verb, "args": list(args or []), "timeout": timeout}) + "\n")
        wf.flush()
        line = rf.readline()
    if not line:
        raise RemoteError(f"{alias}: relay closed without answering")
    return json.loads(line).get("result", {})


def _local(verb: str, args: list) -> dict:
    """The same verb on this machine, without ssh."""
    proc = subprocess.run([sys.executable, str(wm_source_path()), "serve"],
                          input=json.dumps({"verb": verb, "args": args}) + "\n",
                          capture_output=True, text=True, timeout=120)
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{") and '"result"' in line:
            return json.loads(line).get("result", {})
    return {"ok": False, "error": (proc.stderr or proc.stdout)[-400:] or "no reply"}


def image(host: str, verb: str, args: list | None = None) -> tuple[dict, bytes | None]:
    """A see-verb (shot/region/zoom) as (meta, image bytes)."""
    res = call(host, verb, args, timeout=90)
    if not isinstance(res, dict) or not res.get("image_b64"):
        return (res if isinstance(res, dict) else {"ok": False, "error": str(res)}), None
    data = base64.b64decode(res.pop("image_b64"))
    return res, data


def status(host: str) -> dict:
    """Transport in use, remote backend, and a measured round trip."""
    alias = resolve(host)
    out: dict = {"host": host, "alias": alias, "local": is_local(host)}
    if out["local"]:
        out["ok"] = True
        return out
    try:
        t0 = time.perf_counter()
        ping = call(host, "ping", [], timeout=20)
        out["rtt_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        out["ping"] = ping
        out.update(call(host, "_transport", [], timeout=5))
        out["ok"] = bool(ping.get("ok", True))
    except RemoteError as exc:
        out.update({"ok": False, "error": str(exc)})
    return out


SHUTDOWN_TIMEOUT_S = 5.0


def is_relay_cmdline(argv: list[str], alias: str) -> bool:
    """True when a process command line is this module's relay for `alias`."""
    if "workman.remote" not in argv or "--relay" not in argv:
        return False
    i = argv.index("--relay")
    try:
        return i + 1 < len(argv) and resolve(argv[i + 1]) == alias
    except ValueError:
        return False


def _cmdline(pid: int) -> list[str]:
    """A live process's argv (Linux /proc, else `ps`), or [] when unknown."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
    except OSError:
        pass
    if os.path.isdir("/proc"):
        return []  # Linux: no such process
    try:
        res = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    return res.stdout.split() if res.returncode == 0 else []


def stop(host: str) -> dict:
    """Stop host's relay: a shutdown request over its socket, so it cleans up
    after itself; failing that SIGTERM, and only to a pid whose command line
    is that relay (the info file may be stale and its pid reused)."""
    alias = resolve(host)
    try:
        s = _connect(alias, start=False)
    except RemoteError:
        s = None
    if s is not None:
        try:
            s.settimeout(SHUTDOWN_TIMEOUT_S)
            with s, s.makefile("r") as rf, s.makefile("w") as wf:
                wf.write(json.dumps({"verb": "_shutdown", "args": []}) + "\n")
                wf.flush()
                reply = json.loads(rf.readline() or "{}").get("result") or {}
            if reply.get("ok"):
                return {"ok": True, "stopped_pid": reply.get("relay_pid"), "via": "socket"}
        except (OSError, ValueError, AttributeError):
            pass  # a wedged or older relay: fall back to the checked signal
    try:
        info = json.loads(info_path(alias).read_text())
    except (OSError, ValueError):
        return {"ok": False, "error": f"no relay running for {alias}"}
    pid = info.get("pid") if isinstance(info, dict) else None
    if not isinstance(pid, int) or not is_relay_cmdline(_cmdline(pid), alias):
        info_path(alias).unlink(missing_ok=True)
        return {"ok": False, "error": f"stale relay state for {alias}: pid {pid} is not its "
                                      "relay; nothing signalled, state file removed"}
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return {"ok": False, "error": f"SIGTERM to the {alias} relay (pid {pid}) failed: {exc}"}
    return {"ok": True, "stopped_pid": pid, "via": "sigterm"}


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="workman remote relay")
    p.add_argument("--relay", metavar="HOST", help="run the relay for HOST (foreground)")
    p.add_argument("--status", metavar="HOST")
    p.add_argument("--stop", metavar="HOST")
    p.add_argument("--call", nargs="+", metavar=("HOST", "VERB"))
    a = p.parse_args(argv)
    if a.relay:
        return relay_main(a.relay)
    if a.status:
        print(json.dumps(status(a.status), indent=1, default=str))
        return 0
    if a.stop:
        print(json.dumps(stop(a.stop)))
        return 0
    if a.call:
        host, verb, *args = a.call
        print(json.dumps(call(host, verb, args), indent=1, default=str)[:4000])
        return 0
    p.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
