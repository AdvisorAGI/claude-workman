"""Applications — starting them, finding them, ending them.

Desktop control that cannot open an application is only half a desktop, so this
module spawns programs and waits for their window to actually map. Two details
matter more than they look:

**Detachment.** A child of the MCP server dies when the server restarts, which
would take the user's editor with it. Launches therefore get their own session
(`start_new_session=True`) and inherit no pipes.

**Launch is arbitrary code execution.** That is not a new capability — a server
that can type into a terminal could already run anything — but it is a blunter
one, so it is opt-out via WORKMAN_ALLOW_LAUNCH=0 for deployments that want the
input surface without the exec surface.
"""
from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import time

from . import desktop

IS_LINUX = sys.platform.startswith("linux")
IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")

DESKTOP_DIRS = (
    "/usr/share/applications",
    "/usr/local/share/applications",
    os.path.expanduser("~/.local/share/applications"),
)

MAC_APP_DIRS = ("/Applications", "/System/Applications",
                os.path.expanduser("~/Applications"))

WINDOWS_MENU_DIRS = (
    os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"),
    os.path.expandvars(r"%AppData%\Microsoft\Windows\Start Menu\Programs"),
)


def _launch_allowed() -> bool:
    return (os.environ.get("WORKMAN_ALLOW_LAUNCH", "1") or "1").strip().lower() not in (
        "0", "false", "no",
    )


#: A browser started with any of these is not the owner's browser, and a page
#: can tell: an open debug port answers probes, --enable-automation sets
#: navigator.webdriver, a scratch profile has no history and no logins. The
#: launcher refuses them; a person opens the normal launcher and the
#: logged-in Default profile.
AUTOMATION_FLAGS = (
    "--remote-debugging-port", "--remote-debugging-pipe", "--remote-allow-origins",
    "--enable-automation", "--headless", "--user-data-dir", "--load-extension",
    "--disable-blink-features", "--test-type", "--enable-logging=stderr",
)


def automation_flags(argv: list[str]) -> list[str]:
    """The arguments in argv that would mark a browser as automated."""
    return [a for a in argv[1:] if any(a == f or a.startswith(f + "=") for f in AUTOMATION_FLAGS)]


def launch(command: str, args: list[str] | None = None, wait_for_window: float = 0.0) -> dict:
    """Start a program detached from the server.

    `command` may be a bare program name or a full command line; `args` is
    appended when given. With wait_for_window > 0 this blocks until a new window
    appears, so the caller can act immediately instead of guessing at a sleep.
    """
    if not _launch_allowed():
        return {"ok": False, "error": "launching is disabled (WORKMAN_ALLOW_LAUNCH=0)"}
    if not command.strip():
        return {"ok": False, "error": "no command given"}
    argv = shlex.split(command) if args is None else [command, *args]
    if not argv:
        return {"ok": False, "error": "no command given"}
    flagged = automation_flags(argv)
    if flagged and os.environ.get("WORKMAN_ALLOW_AUTOMATION_FLAGS") != "1":
        return {"ok": False, "error": "automation_flags_refused", "flags": flagged,
                "hint": "launch the browser the way the owner does: no debug port, no "
                        "automation switch, no headless, the normal Default profile"}
    if shutil.which(argv[0]) is None:
        # macOS and Windows install most software as a bundle or a Start Menu
        # entry rather than something on PATH, so "not on PATH" is not the same
        # as "not installed" there.
        bundled = _bundle_argv(argv)
        if bundled is None:
            return {"ok": False, "error": f"{argv[0]!r} not found on PATH",
                    "hint": ("try the display name of the app, e.g. 'Safari' or "
                             "'Notepad'") if not IS_LINUX else ""}
        argv = bundled

    before = {w["id"] for w in desktop.list_windows()}
    env = dict(os.environ)
    if IS_LINUX:
        env["DISPLAY"] = desktop.display_name()
    try:
        proc = subprocess.Popen(
            argv, env=env, start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        return {"ok": False, "error": f"could not start {argv[0]!r}: {exc}"}

    result = {"ok": True, "pid": proc.pid, "argv": argv}
    if wait_for_window > 0:
        deadline = time.monotonic() + wait_for_window
        last_new: list[dict] = []
        while time.monotonic() < deadline:
            new = [w for w in desktop.list_windows() if w["id"] not in before]
            last_new = new
            ready = [w for w in new if _window_ready(w)]
            if ready:
                result["window"] = max(
                    ready, key=lambda w: _window_area(w))
                return result
            time.sleep(0.3)
        result["window"] = last_new[0] if last_new else None
        result["note"] = f"no new window within {wait_for_window}s — it may still be starting"
    return result


#: GTK maps a 1x1 client-leader at (0,0) before the real dialog is viewable.
_MIN_MAPPED_SIDE = 20


def _window_area(win: dict) -> int:
    try:
        return max(0, int(win.get("w") or 0)) * max(0, int(win.get("h") or 0))
    except (TypeError, ValueError):
        return 0


def _window_ready(win: dict) -> bool:
    """True when a newly mapped window is large enough to type into."""
    try:
        w, h = int(win.get("w") or 0), int(win.get("h") or 0)
    except (TypeError, ValueError):
        return False
    return w >= _MIN_MAPPED_SIDE and h >= _MIN_MAPPED_SIDE


def list_apps() -> list[dict]:
    """Applications that currently own a window, one entry per process."""
    by_pid: dict[str, dict] = {}
    for win in desktop.list_windows():
        pid = win.get("pid") or ""
        entry = by_pid.setdefault(pid, {"pid": pid,
                                        "command": _command_for(pid) or win.get("app", ""),
                                        "windows": []})
        entry["windows"].append({"id": win["id"], "name": win["name"]})
    return sorted(by_pid.values(), key=lambda e: e["command"] or "")


def _command_for(pid: str) -> str:
    """The command line behind a pid. /proc on Linux, ps on macOS, and on
    Windows the caller falls back to the window's owning executable."""
    if not pid or not pid.isdigit():
        return ""
    if IS_LINUX:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                return " ".join(handle.read().decode(errors="replace").split("\0")).strip()
        except OSError:
            return ""
    if IS_MAC:
        try:
            out = subprocess.run(["ps", "-p", pid, "-o", "command="],
                                 capture_output=True, text=True, timeout=10)
            return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    return ""


def _bundle_argv(argv: list[str]) -> list[str] | None:
    """Turn an app *name* into a launchable argv on macOS and Windows."""
    if IS_MAC:
        if not _mac_app_installed(argv[0]):
            return None
        return ["open", "-a", argv[0]] + (["--args", *argv[1:]] if len(argv) > 1 else [])
    if IS_WINDOWS:
        match = next((a for a in list_launchable(argv[0], limit=1)), None)
        if match:
            return ["cmd", "/c", "start", "", match["exec"], *argv[1:]]
    return None


def list_launchable(query: str = "", limit: int = 60) -> list[dict]:
    """Installed desktop entries — what there is to launch, by name.

    Reads .desktop files directly rather than shelling out, and strips the
    field codes (%U, %F ...) that would otherwise be passed as literal argv.
    """
    if IS_MAC:
        return _list_mac_apps(query, limit)
    if IS_WINDOWS:
        return _list_windows_apps(query, limit)
    found: dict[str, dict] = {}
    needle = query.lower().strip()
    for directory in DESKTOP_DIRS:
        if not os.path.isdir(directory):
            continue
        for entry in sorted(os.listdir(directory)):
            if not entry.endswith(".desktop") or entry in found:
                continue
            fields = _parse_desktop(os.path.join(directory, entry))
            if not fields.get("Exec") or fields.get("NoDisplay", "").lower() == "true":
                continue
            name = fields.get("Name", entry[:-8])
            if needle and needle not in name.lower() and needle not in entry.lower():
                continue
            found[entry] = {"name": name, "exec": fields["Exec"], "desktop_file": entry}
            if len(found) >= limit:
                return list(found.values())
    return list(found.values())


def _parse_desktop(path: str) -> dict:
    fields: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            in_entry = False
            for line in handle:
                line = line.strip()
                if line.startswith("["):
                    in_entry = line == "[Desktop Entry]"
                    continue
                if not in_entry or "=" not in line or line.startswith("#"):
                    continue
                key, _, value = line.partition("=")
                fields.setdefault(key.strip(), value.strip())
    except OSError:
        return {}
    if "Exec" in fields:
        # %U/%F/%i/%c/%k are launcher placeholders, not arguments.
        fields["Exec"] = " ".join(
            part for part in fields["Exec"].split() if not part.startswith("%")
        )
    return fields


def _iter_mac_bundles():
    """Yield (bundle name without .app, path) for visible .app bundles.

    Scans each MAC_APP_DIRS entry and one nested level (Utilities, Chrome Apps).
    Dot-prefixed bundles are helper apps, not something to launch.
    """
    for directory in MAC_APP_DIRS:
        if not os.path.isdir(directory):
            continue
        try:
            entries = sorted(os.listdir(directory))
        except OSError:
            continue
        for entry in entries:
            path = os.path.join(directory, entry)
            if entry.endswith(".app"):
                if not entry.startswith(".") and os.path.isdir(path):
                    yield entry[:-4], path
                continue
            if entry.startswith(".") or not os.path.isdir(path):
                continue
            try:
                inner = sorted(os.listdir(path))
            except OSError:
                continue
            for child in inner:
                child_path = os.path.join(path, child)
                if child.endswith(".app") and not child.startswith(".") and os.path.isdir(child_path):
                    yield child[:-4], child_path


def _mac_app_installed(name: str) -> bool:
    """True when `name` is an installed .app (bare name, .app suffix, or path)."""
    raw = (name or "").strip()
    if not raw:
        return False
    stripped = raw.rstrip("/\\")
    base = os.path.basename(stripped)
    if base.endswith(".app"):
        stem = base[:-4]
        if stem.startswith("."):
            return False
        if os.path.isdir(stripped):
            return True
        want = stem
    else:
        if raw.startswith("."):
            return False
        want = raw
    want_l = want.lower()
    return any(n.lower() == want_l for n, _p in _iter_mac_bundles())


def _list_mac_apps(query: str = "", limit: int = 60) -> list[dict]:
    """Installed .app bundles. `exec` is the bundle name because that is what
    `open -a` wants, not the binary inside Contents/MacOS."""
    needle = query.lower().strip()
    found: list[dict] = []
    for name, bundle in _iter_mac_bundles():
        if needle and needle not in name.lower():
            continue
        found.append({"name": name, "exec": name, "bundle": bundle})
        if len(found) >= limit:
            return found
    return found


def _list_windows_apps(query: str = "", limit: int = 60) -> list[dict]:
    """Start Menu shortcuts, which is where Windows actually keeps 'installed'."""
    needle = query.lower().strip()
    found: list[dict] = []
    for directory in WINDOWS_MENU_DIRS:
        if not directory or not os.path.isdir(directory):
            continue
        for root, _dirs, files in os.walk(directory):
            for entry in sorted(files):
                if not entry.lower().endswith((".lnk", ".url")):
                    continue
                name = os.path.splitext(entry)[0]
                if needle and needle not in name.lower():
                    continue
                found.append({"name": name, "exec": os.path.join(root, entry)})
                if len(found) >= limit:
                    return found
    return found


def terminate(pid: int, force: bool = False, timeout: float = 5.0) -> dict:
    """Ask a process to exit (SIGTERM), escalating to SIGKILL only if asked.

    Prefer closing the window through the window manager — that gives the app a
    chance to prompt about unsaved work, which a signal does not.
    """
    if IS_WINDOWS:
        # Windows has no SIGTERM for GUI processes: taskkill without /F posts
        # WM_CLOSE, which is the same "ask nicely first" contract.
        cmd = ["taskkill", "/PID", str(pid)] + (["/F"] if force else [])
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        if out.returncode != 0:
            return {"ok": False, "error": out.stdout.strip() or out.stderr.strip()}
        return {"ok": True, "pid": pid, "signal": "taskkill /F" if force else "WM_CLOSE"}
    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        return {"ok": False, "error": f"no process {pid}"}
    except PermissionError:
        return {"ok": False, "error": f"not permitted to signal {pid}"}
    if force:
        return {"ok": True, "pid": pid, "signal": "SIGKILL"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return {"ok": True, "pid": pid, "signal": "SIGTERM", "exited": True}
        time.sleep(0.2)
    return {"ok": True, "pid": pid, "signal": "SIGTERM", "exited": False,
            "note": "still running — call again with force=True to SIGKILL"}
