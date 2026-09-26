"""WORKMAN_HUMAN_SPEED: scale sleep once, keep easing, report wall time."""
from __future__ import annotations

import random

import pytest

from workman import human


@pytest.fixture(autouse=True)
def reset_mode(monkeypatch):
    human.reset()
    monkeypatch.delenv("WORKMAN_HUMAN_SPEED", raising=False)
    yield
    human.reset()


INVALID_SPEEDS = ["0", "-2", "nan", "inf", "-inf", "abc", "", " ", "None"]


class TestDefaultCompatibility:
    def test_missing_env_sleeps_unscaled(self, monkeypatch):
        monkeypatch.delenv("WORKMAN_HUMAN_SPEED", raising=False)
        slept = []
        monkeypatch.setattr(human.time, "sleep", lambda s: slept.append(s))
        human._sleep_ms(250)
        assert human.speed() == 1.0
        assert slept == [0.25]

    def test_explicit_one_matches_old_timing(self, monkeypatch):
        monkeypatch.setenv("WORKMAN_HUMAN_SPEED", "1.0")
        slept = []
        monkeypatch.setattr(human.time, "sleep", lambda s: slept.append(s))
        human._sleep_ms(100)
        human._sleep_ms(0)
        human._sleep_ms(-5)
        assert slept == [0.1]


class TestSpeedTwoPointFive:
    def test_sleep_shortens_by_2_5(self, monkeypatch):
        monkeypatch.setenv("WORKMAN_HUMAN_SPEED", "2.5")
        slept = []
        monkeypatch.setattr(human.time, "sleep", lambda s: slept.append(s))
        human._sleep_ms(250)
        assert slept == [0.1]

    def test_move_duration_ms_matches_scaled_sleep(self, monkeypatch):
        monkeypatch.setenv("WORKMAN_HUMAN_SPEED", "2.5")
        slept = []
        monkeypatch.setattr(human.time, "sleep", lambda s: slept.append(s))
        monkeypatch.setattr(human, "_pointer", lambda: (0, 0))
        monkeypatch.setattr(human.desktop, "move", lambda x, y: {"ok": True})
        out = human.human_move(200, 50, rng=random.Random(0))
        assert out["ok"] is True
        wall_ms = sum(slept) * 1000.0
        assert abs(out["duration_ms"] - wall_ms) <= 1.5
        unscaled = human.eased_path(0, 0, 200, 50, rng=random.Random(0))[-1][2]
        assert out["duration_ms"] < unscaled
        assert abs(out["duration_ms"] - unscaled / 2.5) <= 1.5

    def test_press_ms_is_wall_time(self, monkeypatch):
        monkeypatch.setenv("WORKMAN_HUMAN_SPEED", "2.5")
        slept = []
        monkeypatch.setattr(human.time, "sleep", lambda s: slept.append(s))
        monkeypatch.setattr(human.desktop, "mouse_down", lambda button=1: {"ok": True})
        monkeypatch.setattr(human.desktop, "mouse_up", lambda button=1: {"ok": True})
        press = human.human_press_click(button=1, rng=random.Random(0), aim=False)
        assert press == int(round(sum(slept) * 1000.0))
        assert press < human.CLICK_PRESS_MS[1]


class TestInvalidSpeed:
    @pytest.mark.parametrize("raw", INVALID_SPEEDS)
    def test_invalid_uses_one(self, monkeypatch, raw):
        monkeypatch.setenv("WORKMAN_HUMAN_SPEED", raw)
        assert human.speed() == 1.0
        slept = []
        monkeypatch.setattr(human.time, "sleep", lambda s: slept.append(s))
        human._sleep_ms(100)
        assert slept == [0.1]


class TestEasingUnchanged:
    def test_minimum_jerk_at_2_5(self, monkeypatch):
        monkeypatch.setenv("WORKMAN_HUMAN_SPEED", "2.5")
        assert human._ease_in_out(0.0) == 0.0
        assert human._ease_in_out(1.0) == 1.0
        assert abs(human._ease_in_out(0.5) - 0.5) < 1e-9
        early = human._ease_in_out(0.1) - human._ease_in_out(0.0)
        mid = human._ease_in_out(0.55) - human._ease_in_out(0.45)
        assert mid > early * 3

    def test_path_shape_independent_of_speed(self, monkeypatch):
        monkeypatch.delenv("WORKMAN_HUMAN_SPEED", raising=False)
        a = human.eased_path(0, 0, 220, 90, rng=random.Random(99))
        monkeypatch.setenv("WORKMAN_HUMAN_SPEED", "2.5")
        b = human.eased_path(0, 0, 220, 90, rng=random.Random(99))
        assert a == b
        times = [p[2] for p in a]
        assert times == sorted(times)
        assert times[0] == 0


class TestInterruptedRelease:
    def test_press_releases_button_when_sleep_raises(self, monkeypatch):
        monkeypatch.setenv("WORKMAN_HUMAN_SPEED", "2.5")
        ups = []
        monkeypatch.setattr(human.desktop, "mouse_down",
                            lambda button=1: {"ok": True})
        monkeypatch.setattr(human.desktop, "mouse_up",
                            lambda button=1: ups.append(button) or {"ok": True})
        monkeypatch.setattr(human.time, "sleep",
                            lambda s: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            human.human_press_click(button=1, rng=random.Random(0), aim=False)
        assert ups == [1]

    def test_drag_releases_button_when_move_raises(self, monkeypatch):
        monkeypatch.setenv("WORKMAN_HUMAN_SPEED", "2.5")
        events = []
        monkeypatch.setattr(human, "_pointer", lambda: (0, 0))
        monkeypatch.setattr(human.time, "sleep", lambda s: None)
        monkeypatch.setattr(human.desktop, "mouse_down",
                            lambda button=1: events.append("down") or {"ok": True})
        monkeypatch.setattr(human.desktop, "mouse_up",
                            lambda button=1: events.append("up") or {"ok": True})

        def move(x, y):
            events.append("move")
            if "down" in events:
                raise RuntimeError("owner pause")
            return {"ok": True}

        monkeypatch.setattr(human.desktop, "move", move)
        try:
            out = human.human_drag(0, 0, 80, 40, rng=random.Random(1))
        except Exception:
            out = {"ok": False}
        assert "down" in events
        assert "up" in events
        assert events[-1] == "up"
        assert out.get("ok") is False or out.get("released") is True

    def test_type_interrupt_does_not_claim_full_text(self, monkeypatch):
        if not hasattr(human, "Interrupted"):
            pytest.skip("this checkout does not wrap type failures")
        monkeypatch.setenv("WORKMAN_HUMAN_SPEED", "2.5")
        monkeypatch.setattr(human.time, "sleep", lambda s: None)
        typed = []

        def type_text(text, delay_ms=0, dwell_ms=0):
            if len(typed) == 2:
                return {"ok": False, "error": "input_disabled"}
            typed.append(text)
            return {"ok": True}

        monkeypatch.setattr(human.desktop, "type_text", type_text)
        monkeypatch.setattr(human.desktop, "press_key",
                            lambda k: {"ok": True})
        out = human.human_type("hello", rng=random.Random(0))
        assert out["ok"] is False
        assert out["typed_len"] == 2
        assert out["remaining"] == "llo"
        assert "".join(typed) == "he"
