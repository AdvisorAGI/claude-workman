"""Localhost HTTP UI for Workman Desk. Binds 127.0.0.1 only."""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .run import Job
from .vendors import catalog

INDEX = Path(__file__).with_name("index.html")
_job = Job()
_cwd_default = os.getcwd()


def _tasks() -> list[dict]:
    root = Path(os.environ.get("WORKMAN_BOARD_SESSIONS")
                or (Path.home() / ".claude" / "board" / "sessions"))
    if not root.is_dir():
        return []
    rows = []
    for path in sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        items = data.get("items") or []
        rows.append({
            "id": data.get("id") or path.stem,
            "task": data.get("task") or "",
            "project": data.get("project") or "",
            "summary": data.get("summary") or "",
            "blocked": any(i.get("state") == "blocked" for i in items),
            "working": any(i.get("state") == "working" for i in items),
            "items": [
                {"text": i.get("text") or "", "state": i.get("state") or "pending",
                 "ask": i.get("ask") or ""}
                for i in items[:12]
            ],
        })
        if len(rows) >= 8:
            break
    return rows


def _status() -> dict:
    seats = [s.as_dict() for s in catalog()]
    return {
        "ok": True,
        "cwd": _cwd_default,
        "home": str(Path.home()),
        "vendors": seats,
        "tasks": _tasks(),
        "platform": os.name,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "WorkmanDesk/1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/status":
            self._send(200, json.dumps(_status()).encode(), "application/json")
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._send(400, b'{"ok":false}', "application/json")
            return
        if path == "/api/stop":
            _job.stop()
            self._send(200, b'{"ok":true}', "application/json")
            return
        if path != "/api/run":
            self._send(404, b"not found", "text/plain")
            return
        seats = {s.vendor.id: s for s in catalog()}
        vendor_id = str(payload.get("vendor") or "")
        seat = seats.get(vendor_id)
        if seat is None:
            self._send(400, json.dumps({"ok": False, "error": "unknown vendor"}).encode(),
                       "application/json")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def write(chunk: str) -> None:
            try:
                self.wfile.write(chunk.encode("utf-8", "replace"))
                self.wfile.flush()
            except OSError:
                _job.stop()

        code = _job.run(
            vendor_id, seat.binary, str(payload.get("model") or ""),
            str(payload.get("prompt") or ""),
            str(payload.get("cwd") or "") or None,
            bool(payload.get("auto_approve")),
            write,
        )
        try:
            self.wfile.write(f"\n\n[exit {code}]\n".encode())
        except OSError:
            pass


def serve(host: str = "127.0.0.1", port: int = 8767) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("desk binds loopback only")
    httpd = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd
