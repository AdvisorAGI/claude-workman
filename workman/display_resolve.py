"""Find the live X display for the owner's desktop, by itself, after reboots.

Why this exists: the MCP server env pinned ``WORKMAN_DISPLAY=:1``; after the
2026-10-10 reboot GNOME's Xorg came up on ``:0`` instead, so every capture
returned ffmpeg error text instead of a PNG and ``list_windows`` was empty.

Rules, in order:

1. An explicitly configured display (``WORKMAN_DISPLAY``, then ``DISPLAY``) is
   kept whenever it can actually be opened.
2. Otherwise the live desktop is auto-detected from ``/tmp/.X11-unix/X<n>``:
   ``:99`` and any display served by Xvfb are never chosen (that is a headless
   scratch screen, not the owner's desk). The display used by the running
   ``gnome-shell`` wins, then one served by an Xorg started by gdm
   (``-auth /run/user/<uid>/gdm/Xauthority``), then any Xorg, then whatever
   socket is left, lowest number first. Every candidate must pass the probe.
3. When the chosen display came from gdm, ``XAUTHORITY`` is set to that gdm
   cookie file (in ``os.environ`` too, so the in-process XTest channel can
   authenticate).

The probe is a 2-second ``xdpyinfo`` run, falling back to an in-process
``XOpenDisplay`` when ``xdpyinfo`` is not installed. Off Linux this module
does nothing: :func:`resolve` returns the configured value unchanged.

Everything that touches the system goes through small module-level hooks
(``_probe``, ``_socket_displays``, ``_x_servers``, ``_shell_env``) so the tests
can drive the whole decision without an X server.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

SOCKET_DIR = "/tmp/.X11-unix"
#: Displays that are never the owner's desktop, whatever serves them.
SKIP_DISPLAYS = frozenset({99})
PROBE_TIMEOUT_S = 2.0

_X_SERVER_NAMES = {"Xorg", "X", "Xvfb", "Xwayland", "Xvnc", "Xtigervnc", "Xephyr", "Xdummy"}
_DISPLAY_RE = re.compile(r"^:(\d+)(?:\.\d+)?$")


@dataclass
class XServer:
    pid: int
    name: str                      # basename of argv[0], e.g. "Xorg", "Xvfb"
    display: int | None            # None when started with -displayfd and unmapped
    auth: str | None = None        # the -auth argument, if any
    sockets: set[int] = field(default_factory=set)  # socket inodes it holds


@dataclass
class Resolution:
    display: str
    xauthority: str | None
    source: str                    # "configured" | "auto" | "fallback" | "passthrough"
    reason: str


# ---- helpers ---------------------------------------------------------------

def display_number(name: str | None) -> int | None:
    """``":0"`` / ``":0.0"`` -> 0. Remote or odd names (``host:0``) -> None."""
    m = _DISPLAY_RE.match((name or "").strip())
    return int(m.group(1)) if m else None


_ENV_AUTODETECT = "WORKMAN_DISPLAY_AUTODETECT"


def autodetect_enabled() -> bool:
    """``WORKMAN_DISPLAY_AUTODETECT=0`` restores the old import-time behaviour."""
    return os.environ.get(_ENV_AUTODETECT, "1").strip().lower() not in ("0", "false", "no", "off")


def configured_display() -> str | None:
    return os.environ.get("WORKMAN_DISPLAY") or os.environ.get("DISPLAY") or None


def gdm_xauthority(uid: int | None = None) -> str:
    return f"/run/user/{os.getuid() if uid is None else uid}/gdm/Xauthority"


def _readable(path: str | None) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.R_OK)


def _is_gdm_auth(path: str | None) -> bool:
    """gdm writes the session cookie to /run/user/<uid>/gdm/Xauthority."""
    return bool(path) and path.endswith("/gdm/Xauthority")


# ---- system hooks (monkeypatched in tests) ---------------------------------

def _socket_displays() -> list[int]:
    """Display numbers that have a socket in /tmp/.X11-unix, ascending."""
    out = []
    try:
        names = os.listdir(SOCKET_DIR)
    except OSError:
        return []
    for n in names:
        m = re.match(r"^X(\d+)$", n)
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


def _unix_socket_inodes() -> dict[int, int]:
    """inode -> display number for the X sockets listed in /proc/net/unix."""
    out: dict[int, int] = {}
    try:
        with open("/proc/net/unix") as fh:
            next(fh, None)
            for line in fh:
                parts = line.split()
                if len(parts) < 8:
                    continue
                m = re.match(r"^@?/tmp/\.X11-unix/X(\d+)$", parts[7])
                if m:
                    try:
                        out[int(parts[6])] = int(m.group(1))
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def _fd_socket_inodes(pid: int) -> set[int]:
    """Socket inodes a process holds open (only readable for our own uid)."""
    out: set[int] = set()
    try:
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            m = re.match(r"^socket:\[(\d+)\]$", target)
            if m:
                out.add(int(m.group(1)))
    except OSError:
        pass
    return out


def _x_servers() -> list[XServer]:
    """Every running X server we can see, with its display when knowable."""
    servers: list[XServer] = []
    inode_map: dict[int, int] | None = None
    try:
        pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return []
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
        except OSError:
            continue
        if not argv:
            continue
        name = os.path.basename(argv[0])
        if name not in _X_SERVER_NAMES:
            continue
        display = None
        auth = None
        for i, a in enumerate(argv[1:], start=1):
            n = display_number(a)
            if n is not None and display is None:
                display = n
            if a == "-auth" and i + 1 < len(argv):
                auth = argv[i + 1]
        srv = XServer(pid=pid, name=name, display=display, auth=auth)
        if display is None:  # -displayfd: map through the socket it listens on
            if inode_map is None:
                inode_map = _unix_socket_inodes()
            srv.sockets = _fd_socket_inodes(pid)
            hits = sorted({inode_map[i] for i in srv.sockets if i in inode_map})
            if hits:
                srv.display = hits[0]
        servers.append(srv)
    return servers


def _shell_env() -> dict[str, str] | None:
    """DISPLAY/XAUTHORITY of our own running gnome-shell, if any."""
    uid = os.getuid()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return None
    for p in pids:
        try:
            if os.stat(f"/proc/{p}").st_uid != uid:
                continue
            with open(f"/proc/{p}/comm") as fh:
                if fh.read().strip() != "gnome-shell":
                    continue
            with open(f"/proc/{p}/environ", "rb") as fh:
                env = {}
                for kv in fh.read().split(b"\0"):
                    k, _, v = kv.decode(errors="replace").partition("=")
                    if k in ("DISPLAY", "XAUTHORITY"):
                        env[k] = v
            if env.get("DISPLAY"):
                return env
        except OSError:
            continue
    return None


_libx11 = None


def _xopen_probe(display: str, xauthority: str | None) -> bool:
    """In-process XOpenDisplay; used only when xdpyinfo is missing."""
    global _libx11
    if _libx11 is None:
        path = ctypes.util.find_library("X11") or "libX11.so.6"
        try:
            _libx11 = ctypes.CDLL(path)
        except OSError:
            _libx11 = False
            return False
        _libx11.XOpenDisplay.restype = ctypes.c_void_p
        _libx11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        _libx11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    if not _libx11:
        return False
    old = os.environ.get("XAUTHORITY")
    try:
        if xauthority:
            os.environ["XAUTHORITY"] = xauthority
        dpy = _libx11.XOpenDisplay(display.encode())
        if not dpy:
            return False
        _libx11.XCloseDisplay(dpy)
        return True
    finally:
        if xauthority:
            if old is None:
                os.environ.pop("XAUTHORITY", None)
            else:
                os.environ["XAUTHORITY"] = old


def _probe(display: str, xauthority: str | None = None) -> bool:
    """True when the display can actually be opened right now."""
    exe = shutil.which("xdpyinfo")
    if exe:
        env = dict(os.environ)
        env["DISPLAY"] = display
        if xauthority:
            env["XAUTHORITY"] = xauthority
        try:
            r = subprocess.run([exe, "-display", display], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=PROBE_TIMEOUT_S)
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    try:
        return _xopen_probe(display, xauthority)
    except Exception:
        return False


# ---- the decision ----------------------------------------------------------

def _candidates() -> list[tuple[int, str | None, str]]:
    """(display number, xauthority, why) in preference order, skips removed."""
    sockets = _socket_displays()
    servers = _x_servers()
    shell = _shell_env() or {}
    by_display: dict[int, list[XServer]] = {}
    for s in servers:
        if s.display is not None:
            by_display.setdefault(s.display, []).append(s)
    shell_n = display_number(shell.get("DISPLAY"))
    default_gdm = gdm_xauthority()

    ranked = []
    for n in sockets:
        if n in SKIP_DISPLAYS:
            continue
        owners = by_display.get(n, [])
        if any(o.name == "Xvfb" for o in owners):
            continue
        xorg = [o for o in owners if o.name in ("Xorg", "X")]
        gdm = [o for o in xorg if _is_gdm_auth(o.auth)]
        if shell_n == n:
            rank, why = 0, "gnome-shell"
        elif gdm:
            rank, why = 1, "Xorg started by gdm"
        elif xorg:
            rank, why = 2, "Xorg"
        elif not owners:
            rank, why = 3, "socket with unknown server"
        else:
            rank, why = 4, f"{owners[0].name}"
        auth = None
        if shell_n == n and _readable(shell.get("XAUTHORITY")):
            auth = shell.get("XAUTHORITY")
        elif gdm and _readable(gdm[0].auth):
            auth = gdm[0].auth
        elif rank in (0, 1, 2, 3) and _readable(default_gdm):
            auth = default_gdm
        ranked.append((rank, n, auth, why))
    ranked.sort(key=lambda t: (t[0], t[1]))
    return [(n, auth, why) for _, n, auth, why in ranked]


def resolve(configured: str | None = None, *, probe_configured: bool = True) -> Resolution:
    """Pick the display to drive. Never raises."""
    if configured is None:
        configured = configured_display()
    if not sys.platform.startswith("linux"):
        return Resolution(configured or ":0", None, "passthrough", "not linux")
    if not autodetect_enabled():
        return Resolution(configured or ":0", None, "passthrough", f"{_ENV_AUTODETECT}=0")
    if display_number(configured) in SKIP_DISPLAYS:
        # A session pinned to the headless screen (e.g. the :99 test harness)
        # must never be switched onto the owner's desk, even if :99 is down.
        return Resolution(configured, None, "configured", "headless display pinned; never auto-switched")
    xauth_env = os.environ.get("XAUTHORITY")
    if configured and probe_configured and _probe(configured, xauth_env):
        return Resolution(configured, None, "configured", "configured display opens")
    for n, auth, why in _candidates():
        name = f":{n}"
        if configured and name == configured and auth in (None, xauth_env):
            continue  # already tried exactly this
        if _probe(name, auth):
            return Resolution(name, auth, "auto", why)
    return Resolution(configured or ":0", None, "fallback",
                      "no live display found" + (" (configured one does not open)" if configured else ""))
