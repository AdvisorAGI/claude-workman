"""Localhost WebSocket bridge to the workman-chrome extension.

The extension is SIGHT (DOM query / locate / read). workman is the OS-level
HANDS (xdotool, Human Mode). This module is the listener the extension's
service worker retries against: ws://127.0.0.1:8765/workman

Wire protocol (fixed by the shipped extension, do not redesign):

    extension -> workman:  {"op":"hello","ext_version","tabs"}
    workman   -> extension: {"id","op","args"}
    extension -> workman:  {"id","ok","result","error"}

Stdlib only. RFC 6455 handshake and frames are implemented here because we
cannot add a dependency. Bind 127.0.0.1 only: an empty host env var must not
become a wide bind (empty string is "every interface" to socket.bind).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from . import desktop, human

LOOPBACK = "127.0.0.1"
HOST_ENV = "WORKMAN_BRIDGE_HOST"
DEFAULT_PORT = 8765
PATH = "/workman"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

MAX_HTTP_HEAD = 8192
MAX_BUFFER = 32 * 1024 * 1024
ACCEPT_TIMEOUT_S = 0.3
HANDSHAKE_TIMEOUT_S = 5.0
RECV_SIZE = 65536
CLOSE_NORMAL = 1000

# CSS-looking queries go to dom.locate as selector; everything else as text.
_SELECTOR_START = frozenset("#.[")
_SELECTOR_MARKERS = (">", "+", "~", ":", "[", "]", "#", ".")
_TAG_SELECTOR_RE = re.compile(
    r"^[A-Za-z][\w-]*([#.][\w-]+|\[[^\]]+\])+$"
)


# ---- bind host --------------------------------------------------------------

def resolve_bind_host(explicit: str | None = None) -> str:
    """Host to bind. Always loopback.

    os.environ.get substitutes its default only when the variable is ABSENT,
    so HOST="" would otherwise become "" and socket.bind would listen on
    0.0.0.0. Empty, unset, whitespace, 0.0.0.0, and :: all coerce to 127.0.0.1.
    """
    if explicit is None:
        raw = os.environ.get(HOST_ENV)
    else:
        raw = explicit
    if raw is None:
        return LOOPBACK
    host = str(raw).strip()
    if not host:
        return LOOPBACK
    if host in {"0.0.0.0", "::", "*", "[::]", "localhost", "::1", LOOPBACK}:
        return LOOPBACK
    return LOOPBACK


# ---- RFC 6455 handshake -----------------------------------------------------

def accept_key(key: str) -> str:
    """Sec-WebSocket-Accept = base64(SHA1(key + GUID)). RFC 6455 1.3 / 4.2.2."""
    digest = hashlib.sha1((key.strip() + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def handshake_response(key: str) -> bytes:
    accept = accept_key(key)
    return (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    ).encode("ascii")


def parse_http_head(raw: bytes) -> tuple[str, str, dict[str, str]]:
    """Parse a request line plus headers. No body."""
    text = raw.decode("iso-8859-1")
    if "\r\n" in text:
        lines = text.split("\r\n")
    else:
        lines = text.split("\n")
    if not lines or not lines[0].strip():
        raise ValueError("empty request line")
    parts = lines[0].split()
    if len(parts) < 2:
        raise ValueError("malformed request line")
    method, path = parts[0], parts[1]
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    return method, path, headers


def _header_has(headers: dict[str, str], name: str, token: str) -> bool:
    raw = headers.get(name, "")
    parts = {p.strip().lower() for p in raw.split(",")}
    return token.lower() in parts


def _path_of(path: str) -> str:
    return path.split("?", 1)[0]


def upgrade_ok(method: str, path: str, headers: dict[str, str]) -> tuple[bool, str]:
    """Validate a WebSocket upgrade for /workman. Returns (ok, error)."""
    if method.upper() != "GET":
        return False, "method must be GET"
    if _path_of(path) != PATH:
        return False, f"path must be {PATH}"
    if not _header_has(headers, "upgrade", "websocket"):
        return False, "Upgrade: websocket required"
    if not _header_has(headers, "connection", "upgrade"):
        return False, "Connection: Upgrade required"
    version = headers.get("sec-websocket-version", "")
    if version != "13":
        return False, "Sec-WebSocket-Version must be 13"
    key = headers.get("sec-websocket-key", "")
    if not key:
        return False, "Sec-WebSocket-Key required"
    try:
        decoded = base64.b64decode(key, validate=True)
    except Exception:
        return False, "Sec-WebSocket-Key is not valid base64"
    if len(decoded) != 16:
        return False, "Sec-WebSocket-Key must decode to 16 bytes"
    return True, ""


def http_error(status: int, reason: str) -> bytes:
    body = reason.encode("utf-8")
    return (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii") + body


# ---- RFC 6455 frames --------------------------------------------------------

@dataclass(frozen=True)
class Frame:
    opcode: int
    payload: bytes
    fin: bool = True
    masked: bool = False


def encode_frame(payload: bytes, opcode: int = OP_TEXT, *,
                 fin: bool = True, mask: bytes | None = None) -> bytes:
    """Build one WebSocket frame. Server frames must not be masked; client frames must."""
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("payload must be bytes")
    payload = bytes(payload)
    if mask is not None and len(mask) != 4:
        raise ValueError("mask must be 4 bytes")
    n = len(payload)
    header = bytearray()
    header.append((0x80 if fin else 0x00) | (opcode & 0x0F))
    mask_bit = 0x80 if mask is not None else 0x00
    if n < 126:
        header.append(mask_bit | n)
    elif n < 65536:
        header.append(mask_bit | 126)
        header.extend(struct.pack("!H", n))
    else:
        header.append(mask_bit | 127)
        header.extend(struct.pack("!Q", n))
    if mask is None:
        return bytes(header) + payload
    header.extend(mask)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes(header) + masked


def encode_text(text: str, mask: bytes | None = None) -> bytes:
    return encode_frame(text.encode("utf-8"), OP_TEXT, mask=mask)


def encode_close(code: int = CLOSE_NORMAL, reason: str = "") -> bytes:
    payload = struct.pack("!H", code) + reason.encode("utf-8")
    return encode_frame(payload, OP_CLOSE)


def decode_frame(data: bytes | bytearray) -> tuple[Frame | None, int]:
    """Parse one frame from the front of `data`.

    Returns (None, 0) if the buffer is incomplete. Raises ValueError on a
    protocol violation (RSV set, overlong 64-bit length).
    """
    if len(data) < 2:
        return None, 0
    b0 = data[0]
    b1 = data[1]
    fin = bool(b0 & 0x80)
    rsv = b0 & 0x70
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    idx = 2
    if length == 126:
        if len(data) < idx + 2:
            return None, 0
        length = struct.unpack("!H", data[idx:idx + 2])[0]
        idx += 2
    elif length == 127:
        if len(data) < idx + 8:
            return None, 0
        length = struct.unpack("!Q", data[idx:idx + 8])[0]
        idx += 8
        if length >= (1 << 63):
            raise ValueError("invalid 64-bit payload length")
    if masked:
        if len(data) < idx + 4:
            return None, 0
        mask = bytes(data[idx:idx + 4])
        idx += 4
    else:
        mask = None
    if len(data) < idx + length:
        return None, 0
    payload = bytes(data[idx:idx + length])
    if mask is not None:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    if rsv:
        raise ValueError("RSV bits must be 0 (no extensions negotiated)")
    return Frame(opcode=opcode, payload=payload, fin=fin, masked=masked), idx + length


class FrameBuffer:
    """Accumulate recv() bytes and yield complete frames."""

    def __init__(self, max_size: int = MAX_BUFFER):
        self._buf = bytearray()
        self.max_size = max_size

    def feed(self, data: bytes) -> list[Frame]:
        self._buf.extend(data)
        if len(self._buf) > self.max_size:
            raise ValueError("frame buffer exceeded 32 MiB")
        frames: list[Frame] = []
        while True:
            frame, n = decode_frame(self._buf)
            if frame is None:
                break
            del self._buf[:n]
            frames.append(frame)
        return frames


class MessageAssembler:
    """Reassemble fragmented data frames. Control frames pass through."""

    def __init__(self):
        self._parts: list[bytes] = []
        self._opcode: int | None = None

    def push(self, frame: Frame) -> tuple[int, bytes] | None:
        if frame.opcode in (OP_CLOSE, OP_PING, OP_PONG):
            if not frame.fin:
                raise ValueError("control frames must not be fragmented")
            if len(frame.payload) > 125:
                raise ValueError("control frame payload must be <= 125 bytes")
            return frame.opcode, frame.payload
        if self._opcode is None:
            if frame.opcode == OP_CONT:
                raise ValueError("continuation with no start frame")
            if frame.opcode not in (OP_TEXT, OP_BIN):
                raise ValueError(f"unknown opcode {frame.opcode}")
            self._opcode = frame.opcode
            self._parts = [frame.payload]
        else:
            if frame.opcode != OP_CONT:
                raise ValueError("expected continuation opcode")
            self._parts.append(frame.payload)
        if not frame.fin:
            return None
        payload = b"".join(self._parts)
        opcode = self._opcode
        self._parts = []
        self._opcode = None
        return opcode, payload


# ---- request / response map -------------------------------------------------

class PendingMap:
    """id -> waiter. The socket thread fulfills; the MCP thread waits.

    Always pops on wait() so a timeout cannot leak an entry. A late reply
    for an unknown id is ignored.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._waiters: dict = {}

    def register(self, req_id) -> threading.Event:
        ev = threading.Event()
        with self._lock:
            self._waiters[req_id] = {"event": ev, "reply": None}
        return ev

    def fulfill(self, req_id, reply) -> bool:
        with self._lock:
            waiter = self._waiters.get(req_id)
            if waiter is None:
                return False
            waiter["reply"] = reply
            waiter["event"].set()
        return True

    def wait(self, req_id, timeout_s: float):
        with self._lock:
            waiter = self._waiters.get(req_id)
        if waiter is None:
            return None
        waiter["event"].wait(timeout_s)
        with self._lock:
            gone = self._waiters.pop(req_id, None)
        target = gone if gone is not None else waiter
        return target.get("reply")

    def fail_all(self, error: str) -> None:
        payload = {"ok": False, "error": error, "result": None}
        with self._lock:
            for waiter in self._waiters.values():
                if waiter["reply"] is None:
                    waiter["reply"] = dict(payload)
                waiter["event"].set()

    def count(self) -> int:
        with self._lock:
            return len(self._waiters)


# ---- locate helpers ---------------------------------------------------------

def classify_locate_query(selector_or_text: str) -> dict:
    """Split a mixed locator into the extension's selector-or-text args."""
    s = (selector_or_text or "").strip()
    if not s:
        raise ValueError("selector_or_text is required")
    if s[0] in _SELECTOR_START:
        return {"selector": s}
    if _TAG_SELECTOR_RE.match(s):
        return {"selector": s}
    if any(mark in s for mark in (">", "+", "~", "[", "]")) or ":" in s:
        return {"selector": s}
    return {"text": s}


def pick_click_point(located: dict,
                     screen: tuple[int, int] | None = None) -> tuple[int, int, str]:
    """Map a dom.locate result onto X11 pointer space.

    The extension reports both device-pixel `center` and CSS `centerCss`.
    xdotool lives in the X11 screenshot space: if that space is about
    screen.width * dpr, use center; if it is about screen.width, use centerCss.
    """
    located = located or {}
    rect = located.get("boundingRect") or {}
    try:
        dpr = float(rect.get("dpr") or 1.0)
    except (TypeError, ValueError):
        dpr = 1.0
    if dpr <= 0:
        dpr = 1.0
    center = located.get("center") or {}
    center_css = located.get("centerCss") or {}
    try:
        dx = center.get("x")
        dy = center.get("y")
        device = (int(round(float(dx))), int(round(float(dy)))) if dx is not None and dy is not None else None
    except (TypeError, ValueError):
        device = None
    try:
        cx = center_css.get("x")
        cy = center_css.get("y")
        css = (int(round(float(cx))), int(round(float(cy)))) if cx is not None and cy is not None else None
    except (TypeError, ValueError):
        css = None
    if device is None and css is None:
        raise ValueError("dom.locate result has no centre")
    if screen and dpr > 1.05 and device is not None and css is not None:
        sw = int(screen[0])
        # Device point off the X screen means X11 is in CSS pixels.
        if device[0] > sw * 1.2 or device[1] > int(screen[1]) * 1.2:
            return css[0], css[1], "css"
    if device is not None:
        return device[0], device[1], "device"
    return css[0], css[1], "css"


def click_at_os(x: int, y: int) -> dict:
    """OS-level click, honouring Human Mode. This is the stealth hand."""
    if human.enabled():
        return human.human_click(int(x), int(y))
    return desktop.click(int(x), int(y))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_args(args) -> dict:
    return args if isinstance(args, dict) else {}


def _unwrap_reply(reply: dict) -> dict:
    if not isinstance(reply, dict):
        return {"ok": False, "error": "malformed extension reply"}
    if not reply.get("ok"):
        return {"ok": False, "error": reply.get("error") or "extension error"}
    result = reply.get("result")
    if isinstance(result, dict):
        out = dict(result)
        out.setdefault("ok", True)
        return out
    return {"ok": True, "result": result}


# ---- server -----------------------------------------------------------------

class Bridge:
    """One listener, one live extension connection, request/response by id.

    A second connect is accepted and the older socket is dropped so a browser
    restart recovers. The accept/read loop lives on a daemon thread; MCP tools
    only wait on PendingMap events.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._listen: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._generation = 0
        self._pending = PendingMap()
        self._next_id = 1
        self._running = False
        self._connected = False
        self._host = LOOPBACK
        self._port = DEFAULT_PORT
        self._ext_version = None
        self._tabs: list = []
        self._last_seen = None

    def status(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "connected": bool(self._connected and self._conn is not None),
                "ext_version": self._ext_version,
                "tabs": list(self._tabs),
                "last_seen": self._last_seen,
                "pending": self._pending.count(),
                "host": self._host,
                "port": self._port,
                "path": PATH,
            }

    def start(self, port: int = DEFAULT_PORT) -> dict:
        port = int(port)
        with self._lock:
            if self._running:
                if port == self._port or port == 0:
                    out = self.status()
                    out["ok"] = True
                    out["note"] = "already running"
                    return out
        self.stop()
        host = resolve_bind_host()
        if not host:
            host = LOOPBACK
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as exc:
            sock.close()
            return {"ok": False, "error": f"bind {host}:{port} failed: {exc}",
                    "host": host, "port": port}
        bound_host, bound_port = sock.getsockname()
        if bound_host != LOOPBACK:
            sock.close()
            return {"ok": False,
                    "error": f"refusing to listen on {bound_host}; loopback only"}
        sock.listen(8)
        sock.settimeout(ACCEPT_TIMEOUT_S)
        self._stop.clear()
        with self._lock:
            self._listen = sock
            self._host = bound_host
            self._port = bound_port
            self._running = True
            self._connected = False
            self._thread = threading.Thread(
                target=self._serve, name="workman-bridge", daemon=True
            )
            self._thread.start()
        out = self.status()
        out["ok"] = True
        return out

    def stop(self) -> dict:
        self._stop.set()
        listen = None
        conn = None
        thread = None
        with self._lock:
            listen = self._listen
            conn = self._conn
            thread = self._thread
            self._listen = None
            self._conn = None
            self._connected = False
            self._running = False
            self._thread = None
        for sock in (conn, listen):
            if sock is None:
                continue
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        self._pending.fail_all("bridge stopped")
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        out = self.status()
        out["ok"] = True
        return out

    def call(self, op: str, args: dict | None = None, timeout_s: float = 15) -> dict:
        """Send {id, op, args} and wait for the matching extension reply."""
        if not op:
            return {"ok": False, "error": "op is required"}
        timeout_s = max(0.05, min(float(timeout_s), 120.0))
        with self._lock:
            if not self._running:
                return {"ok": False,
                        "error": "bridge is not running; call bridge_start"}
            if not self._connected or self._conn is None:
                return {"ok": False, "error": "extension is not connected"}
            req_id = self._next_id
            self._next_id += 1
            conn = self._conn
            self._pending.register(req_id)
        envelope = {"id": req_id, "op": str(op), "args": _as_args(args)}
        try:
            self._send_json(conn, envelope)
        except OSError as exc:
            self._pending.fulfill(req_id, {"ok": False, "error": f"send failed: {exc}"})
            self._pending.wait(req_id, 0)
            return {"ok": False, "error": f"send failed: {exc}"}
        reply = self._pending.wait(req_id, timeout_s)
        if reply is None:
            return {"ok": False,
                    "error": f"timed out after {timeout_s}s waiting for {op}",
                    "id": req_id, "op": op}
        return _unwrap_reply(reply)

    def locate_and_click(self, selector_or_text: str, tabId: int,
                         timeout_s: float = 15) -> dict:
        """Extension locates the element; workman clicks at OS level."""
        try:
            locate_args = classify_locate_query(selector_or_text)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        locate_args["tabId"] = tabId
        located = self.call("dom.locate", locate_args, timeout_s=timeout_s)
        if not located.get("ok"):
            return located
        try:
            screen = desktop.screen_size()
        except Exception:
            screen = None
        try:
            x, y, space = pick_click_point(located, screen=screen)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "located": located}
        clicked = click_at_os(x, y)
        return {
            "ok": bool(clicked.get("ok", True)),
            "located": located,
            "clicked": clicked,
            "point": [x, y],
            "point_space": space,
            "human": human.enabled(),
            "note": "extension located the element; workman clicked at OS level",
        }

    def _serve(self) -> None:
        while not self._stop.is_set():
            listen = self._listen
            if listen is None:
                break
            try:
                conn, _addr = listen.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                continue
            leftover = self._handshake(conn)
            if leftover is None:
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            gen = self._replace_conn(conn)
            reader = threading.Thread(
                target=self._read_loop,
                args=(conn, gen, leftover),
                name=f"workman-bridge-conn-{gen}",
                daemon=True,
            )
            reader.start()

    def _handshake(self, conn: socket.socket) -> bytes | None:
        """Return leftover bytes after the HTTP head, or None on failure."""
        conn.settimeout(HANDSHAKE_TIMEOUT_S)
        buf = bytearray()
        try:
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return None
                buf.extend(chunk)
                if len(buf) > MAX_HTTP_HEAD:
                    conn.sendall(http_error(400, "header too large"))
                    return None
        except OSError:
            return None
        head, rest = bytes(buf).split(b"\r\n\r\n", 1)
        try:
            method, path, headers = parse_http_head(head)
        except ValueError as exc:
            try:
                conn.sendall(http_error(400, str(exc)))
            except OSError:
                pass
            return None
        ok, err = upgrade_ok(method, path, headers)
        if not ok:
            try:
                conn.sendall(http_error(400, err))
            except OSError:
                pass
            return None
        try:
            conn.sendall(handshake_response(headers["sec-websocket-key"]))
        except OSError:
            return None
        return rest

    def _replace_conn(self, conn: socket.socket) -> int:
        old = None
        with self._lock:
            old = self._conn
            self._conn = conn
            self._connected = True
            self._generation += 1
            gen = self._generation
        if old is not None and old is not conn:
            try:
                old.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                old.close()
            except OSError:
                pass
            self._pending.fail_all("replaced by a new extension connection")
        return gen

    def _read_loop(self, conn: socket.socket, generation: int,
                   leftover: bytes) -> None:
        buf = FrameBuffer()
        assembler = MessageAssembler()
        try:
            if leftover:
                for frame in buf.feed(leftover):
                    if not self._dispatch_frame(conn, assembler, frame):
                        return
            while not self._stop.is_set():
                if self._generation != generation or self._conn is not conn:
                    return
                try:
                    conn.settimeout(ACCEPT_TIMEOUT_S)
                    data = conn.recv(RECV_SIZE)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                for frame in buf.feed(data):
                    if not self._dispatch_frame(conn, assembler, frame):
                        return
        except ValueError:
            try:
                conn.sendall(encode_close(1002, "protocol error"))
            except OSError:
                pass
        except OSError:
            pass
        finally:
            self._on_disconnect(conn, generation)

    def _dispatch_frame(self, conn: socket.socket, assembler: MessageAssembler,
                        frame: Frame) -> bool:
        """Handle one frame. False means the peer is done; stop reading."""
        ready = assembler.push(frame)
        if ready is None:
            return True
        opcode, payload = ready
        if opcode == OP_PING:
            self._send_raw(conn, encode_frame(payload, OP_PONG))
            return True
        if opcode == OP_PONG:
            return True
        if opcode == OP_CLOSE:
            try:
                self._send_raw(conn, encode_frame(payload, OP_CLOSE))
            except OSError:
                pass
            return False
        if opcode != OP_TEXT:
            return True
        try:
            text = payload.decode("utf-8")
            obj = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return True
        if not isinstance(obj, dict):
            return True
        self._on_message(obj)
        return True

    def _on_message(self, obj: dict) -> None:
        with self._lock:
            self._last_seen = _now_iso()
            if obj.get("op") == "hello":
                self._ext_version = obj.get("ext_version")
                tabs = obj.get("tabs")
                self._tabs = tabs if isinstance(tabs, list) else []
                self._connected = True
                return
        req_id = obj.get("id")
        if req_id is None:
            return
        self._pending.fulfill(req_id, obj)

    def _on_disconnect(self, conn: socket.socket, generation: int) -> None:
        drop = False
        with self._lock:
            if self._conn is conn and self._generation == generation:
                self._conn = None
                self._connected = False
                drop = True
        try:
            conn.close()
        except OSError:
            pass
        if drop:
            self._pending.fail_all("extension disconnected")

    def _send_json(self, conn: socket.socket, obj: dict) -> None:
        payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_raw(conn, encode_frame(payload, OP_TEXT))

    def _send_raw(self, conn: socket.socket, data: bytes) -> None:
        with self._send_lock:
            if conn is not self._conn:
                raise OSError("connection replaced")
            conn.sendall(data)


_default = Bridge()


def start(port: int = DEFAULT_PORT) -> dict:
    return _default.start(port=port)


def stop() -> dict:
    return _default.stop()


def status() -> dict:
    return _default.status()


def call(op: str, args: dict | None = None, timeout_s: float = 15) -> dict:
    return _default.call(op, args, timeout_s=timeout_s)


def locate_and_click(selector_or_text: str, tabId: int,
                     timeout_s: float = 15) -> dict:
    return _default.locate_and_click(selector_or_text, tabId, timeout_s=timeout_s)
