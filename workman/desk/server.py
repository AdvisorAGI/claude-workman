"""Localhost HTTP UI for Workman Desk. Binds 127.0.0.1 only."""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import store
from .run import Jobs
from .vendors import catalog

INDEX = Path(__file__).with_name("index.html")
TOKENS_CSS = Path(__file__).with_name("tokens") / "apple-tokens.css"
_jobs = Jobs()
_cwd_default = os.getcwd()


def _live_sessions() -> list[dict]:
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
        "app": "workman-desk",
        "cwd": _cwd_default,
        "home": str(Path.home()),
        "vendors": seats,
        "chats": store.list_chats(),
        "board": store.load_board(),
        "tasks": _live_sessions(),
        "platform": os.name,
    }


def _json(payload) -> bytes:
    return json.dumps(payload).encode()


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length > 2_000_000:
        return {}
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        data = json.loads(raw.decode() or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _ensure_chat(payload: dict) -> dict:
    chat_id = str(payload.get("chat_id") or payload.get("id") or "")
    chat = store.load_chat(chat_id) if chat_id else None
    if chat is None:
        chat = store.empty_chat(
            chat_id or None,
            vendor=str(payload.get("vendor") or "grok"),
            model=str(payload.get("model") or ""),
            cwd=str(payload.get("cwd") or "") or _cwd_default,
            title=str(payload.get("title") or "New chat"),
            item_id=str(payload.get("item_id") or ""),
        )
        store.save_chat(chat)
    if payload.get("vendor"):
        chat["vendor"] = str(payload["vendor"])
    if payload.get("model"):
        chat["model"] = str(payload["model"])
    if payload.get("cwd"):
        chat["cwd"] = str(payload["cwd"])
    return chat


def _seat_for(vendor_id: str):
    seats = {s.vendor.id: s for s in catalog()}
    return seats.get(vendor_id)


def _run_chat(chat: dict, prompt: str, auto_approve: bool, write) -> int:
    seat = _seat_for(str(chat.get("vendor") or ""))
    if seat is None:
        write("unknown vendor\n")
        return 2
    resume = bool(chat.get("messages"))
    packed = store.transcript(chat, prompt) if resume else prompt
    store.append_message(chat, "user", prompt)
    reply: list[str] = []

    def on_chunk(chunk: str) -> None:
        reply.append(chunk)
        write(chunk)

    native = seat.vendor.id in ("grok", "workman")
    code = _jobs.get(chat["id"]).run(
        seat.vendor.id, seat.binary, str(chat.get("model") or ""),
        packed, str(chat.get("cwd") or "") or None, auto_approve, on_chunk,
        session_id=(str(chat.get("session_id") or "") or None) if native else None,
        resume=resume and native,
    )
    text = "".join(reply).strip()
    store.append_message(chat, "assistant", text or f"(exit {code})",
                         exit=code, vendor=seat.vendor.id)
    if chat.get("item_id") and code == 0:
        store.upsert_item({"id": chat["item_id"], "group": "done"})
    elif chat.get("item_id") and code not in (0, None):
        store.upsert_item({"id": chat["item_id"], "group": "blocked"})
    return code


class Handler(BaseHTTPRequestHandler):
    server_version = "WorkmanDesk/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            return
        if path in ("/tokens/apple-tokens.css", "/apple-tokens.css"):
            if TOKENS_CSS.is_file():
                self._send(200, TOKENS_CSS.read_bytes(), "text/css; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")
            return
        if path == "/api/status":
            self._send(200, _json(_status()), "application/json")
            return
        if path == "/api/chats":
            self._send(200, _json({"ok": True, "chats": store.list_chats()}),
                       "application/json")
            return
        if path == "/api/chat":
            chat_id = (parse_qs(parsed.query).get("id") or [""])[0]
            chat = store.load_chat(chat_id)
            if chat is None:
                self._send(404, _json({"ok": False, "error": "no chat"}),
                           "application/json")
                return
            self._send(200, _json({"ok": True, "chat": chat}), "application/json")
            return
        if path == "/api/board":
            self._send(200, _json({"ok": True, "board": store.load_board()}),
                       "application/json")
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        payload = _read_json(self)
        if path == "/api/stop":
            _jobs.stop(str(payload.get("chat_id") or "") or None)
            self._send(200, b'{"ok":true}', "application/json")
            return
        if path == "/api/chats":
            chat = _ensure_chat(payload)
            store.save_chat(chat)
            self._send(200, _json({"ok": True, "chat": chat}), "application/json")
            return
        if path == "/api/board/item":
            if payload.get("delete"):
                ok = store.delete_item(str(payload.get("id") or ""))
                self._send(200, _json({"ok": ok, "board": store.load_board()}),
                           "application/json")
                return
            item = store.upsert_item(payload)
            self._send(200, _json({"ok": True, "item": item,
                                   "board": store.load_board()}),
                       "application/json")
            return
        if path == "/api/board/run":
            item_id = str(payload.get("id") or "")
            item = store.find_item(store.load_board(), item_id)
            if item is None:
                self._send(404, _json({"ok": False, "error": "no item"}),
                           "application/json")
                return
            assignee = str(item.get("assignee") or payload.get("vendor") or "workman")
            prompt = (item.get("brief") or item.get("title") or "").strip()
            if not prompt:
                self._send(400, _json({"ok": False, "error": "empty brief"}),
                           "application/json")
                return
            chat = None
            if item.get("chat_id"):
                chat = store.load_chat(str(item["chat_id"]))
            if chat is None:
                chat = store.empty_chat(
                    vendor=assignee,
                    model=str(payload.get("model") or ""),
                    cwd=str(item.get("cwd") or payload.get("cwd") or "") or _cwd_default,
                    title=str(item.get("title") or "Job"),
                    item_id=item["id"],
                )
                store.save_chat(chat)
                store.upsert_item({"id": item["id"], "chat_id": chat["id"],
                                   "group": "working", "assignee": assignee})
            else:
                chat["vendor"] = assignee
                if item.get("cwd"):
                    chat["cwd"] = item["cwd"]
                store.save_chat(chat)
                store.upsert_item({"id": item["id"], "group": "working",
                                   "assignee": assignee})
            self._stream_run(chat, prompt, bool(payload.get("auto_approve")))
            return
        if path != "/api/run":
            self._send(404, b"not found", "text/plain")
            return
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            self._send(400, _json({"ok": False, "error": "empty prompt"}),
                       "application/json")
            return
        vendor_id = str(payload.get("vendor") or "")
        if _seat_for(vendor_id) is None:
            self._send(400, _json({"ok": False, "error": "unknown vendor"}),
                       "application/json")
            return
        chat = _ensure_chat(payload)
        store.save_chat(chat)
        self._stream_run(chat, prompt, bool(payload.get("auto_approve")))

    def _stream_run(self, chat: dict, prompt: str, auto_approve: bool) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Chat-Id", chat["id"])
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def write(chunk: str) -> None:
            try:
                self.wfile.write(chunk.encode("utf-8", "replace"))
                self.wfile.flush()
            except OSError:
                _jobs.stop(chat["id"])

        _run_chat(chat, prompt, auto_approve, write)


def serve(host: str = "127.0.0.1", port: int = 8767) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("desk binds loopback only")
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.allow_reuse_address = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd
