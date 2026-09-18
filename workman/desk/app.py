"""Launch Workman Desk: localhost composer that drives CLI seats."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
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
    # Windows
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="workman-desk",
        description="Composer + vendor/model chooser that runs Grok/Claude/Codex/Cursor CLIs.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)
    from .server import serve
    httpd = serve(args.host, args.port)
    host, port = httpd.server_address[:2]
    url = f"http://{host}:{port}/"
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
