"""Launch Workman Desk: localhost composer that drives CLI seats."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser


def open_window(url: str) -> None:
    """Prefer an app-mode window; fall back to the default browser."""
    if sys.platform == "darwin":
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.exists(chrome):
            subprocess.Popen([chrome, f"--app={url}"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return
        subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    if sys.platform.startswith("linux"):
        for bin_name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
            path = shutil.which(bin_name)
            if path:
                subprocess.Popen([path, f"--app={url}"], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return
        webbrowser.open(url)
        return
    for exe in (
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
    ):
        if os.path.isfile(exe):
            subprocess.Popen([exe, f"--app={url}"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return
    webbrowser.open(url)


def status_ok(host: str, port: int) -> bool:
    url = f"http://{host}:{port}/api/status"
    try:
        with urllib.request.urlopen(url, timeout=1.5) as resp:
            body = json_loads_quiet(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError):
        return False
    return bool(body.get("ok") and body.get("app") == "workman-desk")


def json_loads_quiet(raw: bytes) -> dict:
    import json
    try:
        data = json.loads(raw.decode() or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def spawn_detached(host: str, port: int) -> None:
    args = [sys.executable, "-m", "workman.desk", "--foreground", "--no-open",
            "--host", host, "--port", str(port)]
    log = os.path.expanduser("~/.workman/desk")
    os.makedirs(log, exist_ok=True)
    handle = open(os.path.join(log, "desk.log"), "ab", buffering=0)
    kwargs: dict = dict(
        args=args, stdout=handle, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, close_fds=True,
    )
    if os.name != "nt":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    subprocess.Popen(**kwargs)


def wait_up(host: str, port: int, seconds: float = 6.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if status_ok(host, port):
            return True
        time.sleep(0.15)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="workman-desk",
        description="Composer + Monday-style board that runs Grok/Claude/Codex/Cursor/Workman CLIs.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--foreground", action="store_true",
                        help="Run the HTTP server in this process (do not detach).")
    args = parser.parse_args(argv)
    host, port = args.host, args.port
    url = f"http://{host}:{port}/"

    if not args.foreground:
        if not status_ok(host, port):
            spawn_detached(host, port)
            if not wait_up(host, port):
                print("Workman Desk did not start on", url, file=sys.stderr)
                return 1
        print(f"Workman Desk {url}", flush=True)
        if not args.no_open:
            open_window(url)
        return 0

    if status_ok(host, port):
        print(f"Workman Desk already {url}", flush=True)
        if not args.no_open:
            open_window(url)
        return 0

    from .server import serve
    try:
        httpd = serve(host, port)
    except OSError as exc:
        print(f"could not bind {url}: {exc}", file=sys.stderr)
        return 1
    bound_host, bound_port = httpd.server_address[:2]
    url = f"http://{bound_host}:{bound_port}/"
    print(f"Workman Desk {url}", flush=True)
    if not args.no_open:
        open_window(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
