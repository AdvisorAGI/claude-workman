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
