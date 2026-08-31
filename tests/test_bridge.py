"""workman-chrome WebSocket bridge: handshake, frames, pending map, bind host.

No browser. Socket tests stay on 127.0.0.1 with an ephemeral port.
"""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from workman import bridge, human, server
from workman.bridge import (
    OP_TEXT,
    PATH,
    PendingMap,
    accept_key,
    classify_locate_query,
    decode_frame,
    encode_frame,
    handshake_response,
    parse_http_head,
    pick_click_point,
    resolve_bind_host,
    upgrade_ok,
)


@pytest.fixture(autouse=True)
def _reset():
    human.reset()
    yield
    try:
        bridge.stop()
    finally:
        human.reset()


# RFC 6455 1.3 / 4.2.2 worked example.
RFC_KEY = "dGhlIHNhbXBsZSBub25jZQ=="
RFC_ACCEPT = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
RFC_MASK = bytes([0x37, 0xFA, 0x21, 0x3D])
RFC_UNMASKED_HELLO = bytes([0x81, 0x05, 0x48, 0x65, 0x6C, 0x6C, 0x6F])
RFC_MASKED_HELLO = bytes([
    0x81, 0x85, 0x37, 0xFA, 0x21, 0x3D, 0x7F, 0x9F, 0x4D, 0x51, 0x58,
])


class TestHandshake:
    def test_rfc6455_accept_key(self):
        assert accept_key(RFC_KEY) == RFC_ACCEPT

    def test_response_carries_the_accept_value(self):
        raw = handshake_response(RFC_KEY).decode("ascii")
        assert "HTTP/1.1 101 Switching Protocols" in raw
        assert f"Sec-WebSocket-Accept: {RFC_ACCEPT}" in raw
        assert "Upgrade: websocket" in raw
        # Do not advertise permessage-deflate; we do not implement it.
        assert "Sec-WebSocket-Extensions" not in raw

    def test_upgrade_ok_for_workman_path(self):
        head = (
            b"GET /workman HTTP/1.1\r\n"
            b"Host: 127.0.0.1:8765\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            b"Sec-WebSocket-Version: 13\r\n"
        )
        method, path, headers = parse_http_head(head)
        ok, err = upgrade_ok(method, path, headers)
        assert ok is True
        assert err == ""
        assert path == PATH

    def test_wrong_path_is_rejected(self):
        ok, err = upgrade_ok("GET", "/other", {
            "upgrade": "websocket",
            "connection": "Upgrade",
            "sec-websocket-key": RFC_KEY,
            "sec-websocket-version": "13",
        })
        assert ok is False
        assert "path" in err


class TestFrames:
    def test_rfc6455_unmasked_hello_bytes(self):
        assert encode_frame(b"Hello", OP_TEXT) == RFC_UNMASKED_HELLO

    def test_rfc6455_masked_hello_bytes(self):
        assert encode_frame(b"Hello", OP_TEXT, mask=RFC_MASK) == RFC_MASKED_HELLO

    def test_masked_client_frame_round_trip(self):
        payload = b"Hello"
        encoded = encode_frame(payload, OP_TEXT, mask=RFC_MASK)
        frame, n = decode_frame(encoded)
        assert n == len(encoded)
        assert frame is not None
        assert frame.masked is True
        assert frame.opcode == OP_TEXT
        assert frame.payload == payload

    def test_multibyte_utf8_round_trip(self):
        text = "café 日本語 🎯"
        payload = text.encode("utf-8")
        assert len(payload) > len(text)
        encoded = encode_frame(payload, OP_TEXT, mask=RFC_MASK)
        frame, n = decode_frame(encoded)
        assert n == len(encoded)
        assert frame.payload.decode("utf-8") == text

    def test_extended_length_round_trip(self):
        payload = b"x" * 200
        encoded = encode_frame(payload, OP_TEXT)
        assert (encoded[1] & 0x7F) == 126
        frame, n = decode_frame(encoded)
        assert n == len(encoded)
        assert frame.payload == payload

    def test_incomplete_buffer_returns_none(self):
        frame, n = decode_frame(RFC_UNMASKED_HELLO[:3])
        assert frame is None
        assert n == 0


class TestPendingMap:
    def test_correlates_reply_by_id(self):
        pending = PendingMap()
        pending.register(7)
        assert pending.count() == 1
        assert pending.fulfill(7, {"id": 7, "ok": True, "result": {"n": 1}})
        reply = pending.wait(7, 1.0)
        assert reply == {"id": 7, "ok": True, "result": {"n": 1}}
        assert pending.count() == 0

    def test_wrong_id_does_not_fulfill(self):
        pending = PendingMap()
        pending.register(1)
        assert pending.fulfill(2, {"ok": True}) is False
        assert pending.count() == 1
        pending.fail_all("cleanup")
        reply = pending.wait(1, 0.2)
        assert reply["ok"] is False

    def test_timeout_returns_none_and_does_not_leak(self):
        pending = PendingMap()
        pending.register("req-a")
        t0 = time.monotonic()
        reply = pending.wait("req-a", 0.05)
        elapsed = time.monotonic() - t0
        assert reply is None
        assert elapsed < 1.0
        assert pending.count() == 0

    def test_fulfill_from_another_thread(self):
        pending = PendingMap()
        pending.register(3)

        def later():
            time.sleep(0.02)
            pending.fulfill(3, {"id": 3, "ok": True, "result": "yes"})

        threading.Thread(target=later, daemon=True).start()
        reply = pending.wait(3, 1.0)
        assert reply["result"] == "yes"


class TestBindHost:
    def test_unset_is_loopback(self, monkeypatch):
        monkeypatch.delenv("WORKMAN_BRIDGE_HOST", raising=False)
        assert resolve_bind_host() == "127.0.0.1"

    @pytest.mark.parametrize("raw", ["", " ", "\t", "\n", "   "])
    def test_empty_is_loopback_not_a_wide_bind(self, monkeypatch, raw):
        monkeypatch.setenv("WORKMAN_BRIDGE_HOST", raw)
        assert resolve_bind_host() == "127.0.0.1"

    def test_explicitly_set_loopback(self, monkeypatch):
        monkeypatch.setenv("WORKMAN_BRIDGE_HOST", "127.0.0.1")
        assert resolve_bind_host() == "127.0.0.1"

    def test_wide_bind_values_are_coerced(self, monkeypatch):
        for raw in ("0.0.0.0", "::", "*", "localhost"):
            monkeypatch.setenv("WORKMAN_BRIDGE_HOST", raw)
            assert resolve_bind_host() == "127.0.0.1"


class TestLocateHelpers:
    def test_hash_and_dot_are_selectors(self):
        assert classify_locate_query("#submit") == {"selector": "#submit"}
        assert classify_locate_query(".primary") == {"selector": ".primary"}
        assert classify_locate_query("button.primary") == {"selector": "button.primary"}

    def test_visible_words_are_text(self):
        assert classify_locate_query("Sign in") == {"text": "Sign in"}
        assert classify_locate_query("OK") == {"text": "OK"}

    def test_device_pixels_when_x11_is_device_sized(self):
        located = {
            "center": {"x": 200, "y": 100},
            "centerCss": {"x": 100, "y": 50},
            "boundingRect": {"dpr": 2},
        }
        x, y, space = pick_click_point(located, screen=(3840, 2160))
        assert (x, y, space) == (200, 100, "device")

    def test_css_when_device_point_is_off_the_x_screen(self):
        located = {
            "center": {"x": 2400, "y": 100},
            "centerCss": {"x": 1200, "y": 50},
            "boundingRect": {"dpr": 2},
        }
        x, y, space = pick_click_point(located, screen=(1920, 1080))
        assert (x, y, space) == (1200, 50, "css")


class TestLocateAndClick:
    def test_os_click_on_the_located_centre(self, monkeypatch):
        inst = bridge.Bridge()
        monkeypatch.setattr(inst, "call", lambda op, args, timeout_s=15: {
            "ok": True,
            "tag": "button",
            "text": "OK",
            "center": {"x": 100, "y": 200},
            "centerCss": {"x": 100, "y": 200},
            "boundingRect": {"x": 80, "y": 180, "w": 40, "h": 40, "dpr": 1},
        })
        clicks = []
        monkeypatch.setattr(bridge.x11, "click",
                            lambda x, y, **k: clicks.append((x, y)) or {
                                "ok": True, "clicked": [x, y]})
        monkeypatch.setattr(bridge.x11, "screen_size", lambda: (1920, 1080))
        monkeypatch.setattr(bridge.human, "enabled", lambda: False)
        out = inst.locate_and_click("OK", tabId=7)
        assert clicks == [(100, 200)]
        assert out["ok"] is True
        assert out["located"]["tag"] == "button"
        assert out["clicked"]["ok"] is True
        assert out["human"] is False
        assert out["point"] == [100, 200]

    def test_human_mode_uses_human_click(self, monkeypatch):
        inst = bridge.Bridge()
        monkeypatch.setattr(inst, "call", lambda op, args, timeout_s=15: {
            "ok": True,
            "center": {"x": 10, "y": 20},
            "centerCss": {"x": 10, "y": 20},
            "boundingRect": {"dpr": 1},
        })
        seen = {}
        monkeypatch.setattr(bridge.human, "enabled", lambda: True)
        monkeypatch.setattr(bridge.human, "human_click",
                            lambda x, y, **k: seen.update(x=x, y=y) or {
                                "ok": True, "human": True, "at": [x, y]})
        monkeypatch.setattr(bridge.x11, "click",
                            lambda *a, **k: pytest.fail("must not teleport"))
        monkeypatch.setattr(bridge.x11, "screen_size", lambda: (1920, 1080))
        out = inst.locate_and_click("#go", tabId=1)
        assert seen == {"x": 10, "y": 20}
        assert out["human"] is True

    def test_not_connected_is_an_error_dict(self):
        inst = bridge.Bridge()
        out = inst.call("tabs.list", {}, timeout_s=0.2)
        assert out["ok"] is False
        assert "not running" in out["error"] or "not connected" in out["error"]


def _recv_http(sock: socket.socket) -> bytes:
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > 8192:
            break
    return bytes(buf)


def _client_send_json(sock: socket.socket, obj: dict, mask: bytes = RFC_MASK) -> None:
    payload = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    sock.sendall(encode_frame(payload, OP_TEXT, mask=mask))


def _client_recv_json(sock: socket.socket, timeout: float = 2.0) -> dict:
    sock.settimeout(timeout)
    buf = bytearray()
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("server closed")
        buf.extend(chunk)
        frame, n = decode_frame(buf)
        if frame is None:
            continue
        return json.loads(frame.payload.decode("utf-8"))


def _connect_extension(port: int) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), timeout=2)
    sock.sendall(
        (
            "GET /workman HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {RFC_KEY}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
    )
    resp = _recv_http(sock)
    assert b"101" in resp.split(b"\r\n", 1)[0]
    assert RFC_ACCEPT.encode("ascii") in resp
    return sock


class TestListener:
    def test_start_binds_loopback(self, monkeypatch):
        monkeypatch.setenv("WORKMAN_BRIDGE_HOST", "")
        inst = bridge.Bridge()
        try:
            out = inst.start(port=0)
            assert out["ok"] is True
            assert out["running"] is True
            assert out["host"] == "127.0.0.1"
            assert out["port"] > 0
            with socket.create_connection(("127.0.0.1", out["port"]), timeout=1):
                pass
        finally:
            inst.stop()
            assert inst.status()["running"] is False

    def test_hello_then_call_round_trip(self):
        inst = bridge.Bridge()
        try:
            started = inst.start(port=0)
            assert started["ok"] is True
            sock = _connect_extension(started["port"])
            _client_send_json(sock, {
                "op": "hello",
                "ext_version": "0.1.0",
                "tabs": [{"id": 1, "title": "x", "url": "https://example/"}],
            })
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                st = inst.status()
                if st["connected"] and st["ext_version"] == "0.1.0":
                    break
                time.sleep(0.02)
            else:
                pytest.fail(f"hello not seen: {inst.status()}")
            assert inst.status()["tabs"][0]["id"] == 1

            result_holder = {}

            def agent():
                result_holder["out"] = inst.call("tabs.list", {}, timeout_s=2)

            t = threading.Thread(target=agent, daemon=True)
            t.start()
            req = _client_recv_json(sock)
            assert req["op"] == "tabs.list"
            assert "id" in req
            _client_send_json(sock, {
                "id": req["id"],
                "ok": True,
                "result": {"tabs": [{"id": 1}], "count": 1},
                "error": None,
            })
            t.join(timeout=2)
            out = result_holder["out"]
            assert out["ok"] is True
            assert out["count"] == 1
            assert out["tabs"][0]["id"] == 1
            sock.close()
        finally:
            inst.stop()

    def test_second_connect_drops_the_older_socket(self):
        inst = bridge.Bridge()
        try:
            started = inst.start(port=0)
            first = _connect_extension(started["port"])
            _client_send_json(first, {"op": "hello", "ext_version": "0.1.0",
                                      "tabs": [{"id": 1}]})
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and inst.status()["ext_version"] != "0.1.0":
                time.sleep(0.02)
            second = _connect_extension(started["port"])
            _client_send_json(second, {"op": "hello", "ext_version": "0.1.1",
                                       "tabs": [{"id": 2}]})
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and inst.status()["ext_version"] != "0.1.1":
                time.sleep(0.02)
            assert inst.status()["ext_version"] == "0.1.1"
            assert inst.status()["tabs"][0]["id"] == 2
            first.settimeout(1)
            # Older socket is shut down; recv is empty or raises.
            try:
                data = first.recv(16)
            except OSError:
                data = b""
            assert data == b""
            first.close()
            second.close()
        finally:
            inst.stop()

    def test_call_times_out_when_extension_is_silent(self):
        inst = bridge.Bridge()
        try:
            started = inst.start(port=0)
            sock = _connect_extension(started["port"])
            _client_send_json(sock, {"op": "hello", "ext_version": "0.1.0", "tabs": []})
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not inst.status()["connected"]:
                time.sleep(0.02)
            out = inst.call("tabs.list", {}, timeout_s=0.15)
            assert out["ok"] is False
            assert "timed out" in out["error"]
            sock.close()
        finally:
            inst.stop()


class TestServerTools:
    def test_wrappers_are_registered(self):
        assert callable(server.bridge_start)
        assert callable(server.bridge_stop)
        assert callable(server.bridge_status)
        assert callable(server.bridge_call)
        assert callable(server.bridge_tabs_list)
        assert callable(server.bridge_dom_locate)
        assert callable(server.bridge_locate_and_click)

    def test_status_shape(self):
        st = server.bridge_status()
        for key in ("running", "connected", "ext_version", "tabs", "last_seen", "pending"):
            assert key in st
