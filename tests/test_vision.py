"""Coordinate and budget maths — the part that silently breaks clicks."""
from __future__ import annotations

import io

import pytest
from PIL import Image

from workman import vision


def png(width: int, height: int, colour=(30, 90, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buf, "PNG")
    return buf.getvalue()


def size_of(data: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(data)).size


class TestBudget:
    def test_env_override_beats_default(self, monkeypatch):
        monkeypatch.setenv("WORKMAN_IMAGE_MAX_EDGE", "2576")
        assert vision.max_edge() == 2576

    def test_tier_name_resolves(self, monkeypatch):
        monkeypatch.setenv("WORKMAN_IMAGE_MAX_EDGE", "high")
        assert vision.max_edge() == 2576

    def test_garbage_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("WORKMAN_IMAGE_MAX_EDGE", "not-a-number")
        assert vision.max_edge() == vision.DEFAULT_MAX_EDGE

    def test_explicit_argument_wins(self, monkeypatch):
        monkeypatch.setenv("WORKMAN_IMAGE_MAX_EDGE", "2576")
        assert vision.max_edge(800) == 800


class TestFit:
    def test_preserves_aspect_ratio(self):
        assert vision.fit(3840, 2160, 1568) == (1568, 882)

    def test_never_upscales_by_default(self):
        assert vision.fit(400, 300, 1568) == (400, 300)

    def test_upscales_when_asked(self):
        w, h = vision.fit(400, 300, 1568, allow_upscale=True)
        assert max(w, h) == 1568
        assert abs((w / h) - (400 / 300)) < 0.01

    def test_portrait_uses_long_edge(self):
        assert vision.fit(1080, 2400, 1200) == (540, 1200)


class TestRegion:
    def test_clamps_to_bounds(self):
        assert vision.normalize_region(-50, -50, 5000, 5000, (3840, 2160)) == (0, 0, 3840, 2160)

    def test_tolerates_swapped_corners(self):
        assert vision.normalize_region(300, 200, 100, 50, (800, 600)) == (100, 50, 200, 150)

    def test_rejects_empty_region(self):
        with pytest.raises(ValueError):
            vision.normalize_region(100, 100, 100, 400, (800, 600))

    def test_rejects_region_entirely_outside(self):
        with pytest.raises(ValueError):
            vision.normalize_region(5000, 5000, 6000, 6000, (800, 600))

    def test_scale_region_maps_view_to_screen(self):
        # A view at scale 0.5 means screen coordinates are twice the view's.
        assert vision.scale_region((100, 50, 200, 100), 0.5) == (200, 100, 400, 200)

    def test_scale_region_is_identity_at_one(self):
        assert vision.scale_region((1, 2, 3, 4), 1.0) == (1, 2, 3, 4)


class TestToView:
    def test_reports_scale_that_maps_back_to_screen(self):
        data, meta = vision.to_view(png(3840, 2160))
        assert meta["view"] == [1568, 882]
        assert meta["resized"] is True
        assert size_of(data) == (1568, 882)
        # The contract callers rely on: screen = view / scale.
        assert round(1568 / meta["scale"]) == 3840

    def test_small_screen_is_untouched(self):
        original = png(1024, 768)
        data, meta = vision.to_view(original)
        assert meta["resized"] is False
        assert meta["scale"] == 1.0
        assert data is original


class TestMagnify:
    def test_small_crop_is_enlarged_to_budget(self):
        data, meta = vision.magnify(png(200, 60))
        assert meta["magnification"] > 1
        assert max(size_of(data)) == vision.DEFAULT_MAX_EDGE

    def test_output_is_jpeg(self):
        data, _ = vision.magnify(png(200, 60))
        assert Image.open(io.BytesIO(data)).format == "JPEG"

    def test_large_crop_is_reduced_not_enlarged(self):
        _, meta = vision.magnify(png(4000, 2000))
        assert meta["magnification"] < 1


class TestSave:
    def test_writes_file_and_returns_path(self, tmp_path):
        path = vision.save(png(10, 10), "png", directory=str(tmp_path))
        assert path.endswith(".png")
        with open(path, "rb") as handle:
            assert handle.read()[:4] == b"\x89PNG"
