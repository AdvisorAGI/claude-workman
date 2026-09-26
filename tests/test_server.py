"""Server-level behaviour: coordinate spaces and batch dispatch."""
from __future__ import annotations

import pytest

from workman import server


@pytest.fixture(autouse=True)
def reset_view():
    server._LAST_VIEW.update({"scale": 1.0, "screen": None, "view": None})
    yield
    server._LAST_VIEW.update({"scale": 1.0, "screen": None, "view": None})


class TestCoordinateSpace:
    def test_screen_space_is_passed_through(self):
        assert server._to_screen(100, 200, "screen") == (100, 200)

    def test_view_space_scales_back_to_screen(self):
        # 1568-wide view of a 3840-wide screen.
        server._LAST_VIEW["scale"] = 1568 / 3840
        assert server._to_screen(784, 441, "view") == (1920, 1080)

    def test_view_space_is_identity_when_not_downscaled(self):
        server._LAST_VIEW["scale"] = 1.0
        assert server._to_screen(640, 480, "view") == (640, 480)

    def test_unknown_space_is_rejected(self):
        with pytest.raises(ValueError):
            server._to_screen(1, 1, "cartesian")

    def test_click_reports_bad_space_instead_of_clicking(self, monkeypatch):
        called = []
        monkeypatch.setattr(server.desktop, "click", lambda *a, **k: called.append(a))
        result = server.click(1, 2, space="nonsense")
        assert result["ok"] is False
        assert called == []

    def test_click_converts_view_coordinates(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(server.desktop, "click",
                            lambda x, y, button=1, count=1: seen.update(x=x, y=y) or {"ok": True})
        server._LAST_VIEW["scale"] = 0.5
        server.click(100, 100, space="view")
        assert seen == {"x": 200, "y": 200}

    def test_click_with_modifiers_routes_to_modified_path(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(server.desktop, "click_with",
                            lambda *a, **k: seen.update(args=a, kwargs=k) or {"ok": True})
        server.click(5, 6, modifiers=["shift"])
        assert seen["kwargs"]["modifiers"] == ["shift"]


class TestBatch:
    def test_runs_every_step_in_order(self, monkeypatch):
        order = []
        monkeypatch.setitem(server._BATCH_OPS, "wait",
                            lambda seconds=0: order.append(seconds) or {"ok": True})
        result = server.batch([
            {"action": "wait", "seconds": 1},
            {"action": "wait", "seconds": 2},
        ])
        assert result["ok"] is True
        assert result["completed"] == 2
        assert order == [1, 2]

    def test_stops_at_first_failure_by_default(self, monkeypatch):
        ran = []
        monkeypatch.setitem(server._BATCH_OPS, "bad", lambda: {"ok": False, "error": "no"})
        monkeypatch.setitem(server._BATCH_OPS, "after", lambda: ran.append(1) or {"ok": True})
        result = server.batch([{"action": "bad"}, {"action": "after"}])
        assert result["ok"] is False
        assert ran == []
        assert len(result["results"]) == 1

    def test_continues_past_failure_when_asked(self, monkeypatch):
        ran = []
        monkeypatch.setitem(server._BATCH_OPS, "bad", lambda: {"ok": False})
        monkeypatch.setitem(server._BATCH_OPS, "after", lambda: ran.append(1) or {"ok": True})
        result = server.batch([{"action": "bad"}, {"action": "after"}], stop_on_error=False)
        assert ran == [1]
        assert result["completed"] == 1

    def test_unknown_action_lists_what_is_available(self):
        result = server.batch([{"action": "teleport"}])
        assert result["ok"] is False
        assert "click" in result["results"][0]["actions"]

    def test_bad_arguments_do_not_crash_the_batch(self, monkeypatch):
        monkeypatch.setitem(server._BATCH_OPS, "wait", lambda seconds=0: {"ok": True})
        result = server.batch([{"action": "wait", "nonexistent": 1}])
        assert result["ok"] is False
        assert "bad arguments" in result["results"][0]["error"]

    def test_raising_handler_is_reported_not_propagated(self, monkeypatch):
        def explode():
            raise RuntimeError("kaboom")

        monkeypatch.setitem(server._BATCH_OPS, "boom", explode)
        result = server.batch([{"action": "boom"}])
        assert result["ok"] is False
        assert "kaboom" in result["results"][0]["error"]

    def test_non_object_step_is_rejected(self):
        result = server.batch(["click"])
        assert result["ok"] is False


class TestScreenshotMetadata:
    def test_reports_scale_and_records_it_for_view_coordinates(self, monkeypatch):
        monkeypatch.setattr(server.desktop, "screenshot", lambda **k: b"raw-png")
        monkeypatch.setattr(server.desktop, "screen_size", lambda: (3840, 2160))
        monkeypatch.setattr(server.vision, "to_view",
                            lambda data: (b"small-png",
                                          {"screen": [3840, 2160], "view": [1568, 882],
                                           "scale": 0.408333, "resized": True}))
        meta, image = server.screenshot()
        assert "space='view'" in meta["coordinates"]
        assert image.data == b"small-png"
        # Recorded, so a later click(space="view") maps correctly.
        assert server._LAST_VIEW["scale"] == 0.408333

    def test_full_resolution_skips_the_resize(self, monkeypatch):
        monkeypatch.setattr(server.desktop, "screenshot", lambda **k: b"raw-png")
        monkeypatch.setattr(server.desktop, "screen_size", lambda: (3840, 2160))
        monkeypatch.setattr(server.vision, "to_view",
                            lambda data: pytest.fail("should not resize"))
        meta, image = server.screenshot(full_resolution=True)
        assert image.data == b"raw-png"
        assert meta["scale"] == 1.0

    def test_missing_pillow_degrades_to_raw_image(self, monkeypatch):
        monkeypatch.setattr(server.desktop, "screenshot", lambda **k: b"raw-png")
        monkeypatch.setattr(server.desktop, "screen_size", lambda: (3840, 2160))

        def unavailable(data):
            raise server.vision.VisionUnavailable("Pillow is required")

        monkeypatch.setattr(server.vision, "to_view", unavailable)
        meta, image = server.screenshot()
        assert image.data == b"raw-png"
        assert "Pillow" in meta["note"]


class TestRemoteTools:
    """server.remote / remote_screenshot / remote_status, with the module
    the tool bodies look up (`server._remote`) replaced by a fake."""

    @pytest.fixture
    def remote_tools(self, monkeypatch):
        from workman.remote import RemoteError

        fake = _FakeRemoteModule(RemoteError)
        monkeypatch.setattr(server, "_remote", fake)
        return fake, server.remote, server.remote_screenshot, server.remote_status

    def test_the_remote_tool_does_not_shadow_the_module(self):
        import workman.remote

        assert server._remote is workman.remote
        assert callable(server.remote) and server.remote is not workman.remote

    def test_remote_returns_error_dict_on_remote_error(self, remote_tools):
        fake, remote_fn, _shot, _status = remote_tools
        fake.call_impl = lambda *a, **k: (_ for _ in ()).throw(
            fake.RemoteError("no channel"))
        out = remote_fn("hostA", "ping")
        assert out["ok"] is False
        assert out["host"] == "hostA"
        assert "RemoteError" in out["error"]
        assert "no channel" in out["error"]

    def test_remote_passes_verb_and_args(self, remote_tools):
        fake, remote_fn, _shot, _status = remote_tools
        out = remote_fn("hostA", "key", args=["shift"], timeout_s=12.5)
        assert out == {"ok": True, "host": "hostA", "verb": "key"}
        assert fake.calls == [("call", "hostA", "key", ["shift"], 12.5)]

    def test_remote_screenshot_wraps_png_bytes_as_image(self, remote_tools):
        from mcp.server.fastmcp import Image

        fake, _remote, screenshot_fn, _status = remote_tools
        png = b"\x89PNG\r\n\x1a\nfake"
        meta = {"ok": True, "blank_check": "ok", "scale": 1.0}

        def image(host, verb, args=None):
            return dict(meta), png

        fake.image_impl = image
        out = screenshot_fn("hostA")
        assert fake.calls == [("image", "hostA", "shot", ["-"])]
        assert out[0] == meta
        assert isinstance(out[1], Image)
        assert out[1].data == png
        assert out[1]._format == "png"

    def test_remote_screenshot_full_asks_for_full(self, remote_tools):
        fake, _remote, screenshot_fn, _status = remote_tools
        screenshot_fn("hostA", full=True)
        assert fake.calls == [("image", "hostA", "shot", ["-", "full"])]

    def test_remote_screenshot_jpeg_when_bytes_are_not_png(self, remote_tools):
        from mcp.server.fastmcp import Image

        fake, _remote, screenshot_fn, _status = remote_tools
        jpeg = b"\xff\xd8jpeg-bytes"
        fake.image_impl = lambda host, verb, args=None: ({"ok": True}, jpeg)
        out = screenshot_fn("hostA")
        assert isinstance(out[1], Image)
        assert out[1].data == jpeg
        assert out[1]._format == "jpeg"

    def test_remote_screenshot_meta_only_when_bytes_are_none(self, remote_tools):
        fake, _remote, screenshot_fn, _status = remote_tools
        meta = {"ok": True, "note": "no image"}
        fake.image_impl = lambda host, verb, args=None: (meta, None)
        out = screenshot_fn("hostA")
        assert out == [meta]

    def test_remote_screenshot_returns_error_list_on_remote_error(self, remote_tools):
        fake, _remote, screenshot_fn, _status = remote_tools
        fake.image_impl = lambda *a, **k: (_ for _ in ()).throw(
            fake.RemoteError("relay down"))
        out = screenshot_fn("hostA")
        assert out == [{"ok": False, "error": "RemoteError: relay down", "host": "hostA"}]

    def test_remote_status_ok_passthrough(self, remote_tools):
        fake, _remote, _shot, status_fn = remote_tools
        fake.status_impl = lambda host: {
            "ok": True, "host": host, "transport": "direct cable"}
        out = status_fn("hostA")
        assert out == {"ok": True, "host": "hostA", "transport": "direct cable"}
        assert fake.calls == [("status", "hostA")]

    def test_remote_status_returns_error_dict_on_remote_error(self, remote_tools):
        fake, _remote, _shot, status_fn = remote_tools
        fake.status_impl = lambda host: (_ for _ in ()).throw(
            fake.RemoteError("timed out"))
        out = status_fn("hostA")
        assert out["ok"] is False
        assert out["host"] == "hostA"
        assert "RemoteError" in out["error"]
        assert "timed out" in out["error"]


class _FakeRemoteModule:
    def __init__(self, remote_error):
        self.RemoteError = remote_error
        self.calls: list[tuple] = []
        self.call_impl = None
        self.image_impl = None
        self.status_impl = None

    def call(self, host, verb, args, timeout=60.0):
        self.calls.append(("call", host, verb, args, timeout))
        if self.call_impl is not None:
            return self.call_impl(host, verb, args, timeout)
        return {"ok": True, "host": host, "verb": verb}

    def image(self, host, verb, args=None):
        self.calls.append(("image", host, verb, args))
        if self.image_impl is not None:
            return self.image_impl(host, verb, args)
        return {"ok": True, "host": host}, None

    def status(self, host):
        self.calls.append(("status", host))
        if self.status_impl is not None:
            return self.status_impl(host)
        return {"ok": True, "host": host}
