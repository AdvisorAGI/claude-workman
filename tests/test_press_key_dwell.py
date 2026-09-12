"""Human Mode press_key holds the key for a per-key dwell."""
from __future__ import annotations

import pytest

from workman import desktop, human, server, x11


class FakeChannel:
    def __init__(self):
        self.combos: list[dict] = []

    def press_combo(self, combo: str, hold_ms: int = 0):
        self.combos.append({"combo": combo, "hold_ms": hold_ms})
        return [combo]


@pytest.fixture
def channel(monkeypatch):
    ch = FakeChannel()
    monkeypatch.setattr(x11, "_xt", lambda: ch)
    monkeypatch.setattr(desktop, "_ACCEPTS", {})
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    human.reset()
    yield ch
    human.reset()
    desktop._ACCEPTS.clear()


class TestPressKeyDwell:
    def test_human_mode_passes_dwell_in_band(self, channel):
        human.set_mode(True, seed=7)
        out = server.press_key("a")
        assert out["ok"] is True
        assert channel.combos == [{"combo": "a", "hold_ms": channel.combos[0]["hold_ms"]}]
        assert 35 <= channel.combos[0]["hold_ms"] <= 180

    def test_human_mode_dwell_stays_in_band_across_seeds(self, channel):
        holds = []
        for seed in (1, 2, 3, 7, 11, 42):
            human.set_mode(True, seed=seed)
            channel.combos.clear()
            server.press_key("Tab")
            holds.append(channel.combos[0]["hold_ms"])
        assert all(35 <= h <= 180 for h in holds)
        assert len(set(holds)) > 1

    def test_human_mode_off_hold_ms_is_zero(self, channel):
        human.set_mode(False)
        out = server.press_key("ctrl+c")
        assert out["ok"] is True
        assert channel.combos == [{"combo": "ctrl+c", "hold_ms": 0}]

    def test_backend_without_hold_ms_does_not_get_the_kwarg(self, monkeypatch):
        seen = {}

        def fake_press(key):
            seen["key"] = key
            return {"ok": True, "key": key}

        monkeypatch.setattr(desktop.active(), "press_key", fake_press)
        monkeypatch.setattr(desktop, "_ACCEPTS", {})
        monkeypatch.setattr(server.time, "sleep", lambda s: None)
        human.set_mode(True, seed=1)
        try:
            out = server.press_key("x")
        finally:
            human.reset()
        assert out == {"ok": True, "key": "x"}
        assert seen == {"key": "x"}
