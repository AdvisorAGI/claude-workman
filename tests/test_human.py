"""Human Mode: cadence bounds, path shape, seeded determinism, flag routing."""
from __future__ import annotations

import random

import pytest

from workman import human, server


@pytest.fixture(autouse=True)
def reset_mode():
    human.reset()
    yield
    human.reset()


def _paths(n=16, **kwargs):
    for seed in range(n):
        yield human.eased_path(10, 20, 400, 280, rng=random.Random(seed), **kwargs)


class TestCadenceBounds:
    def test_letter_delays_stay_in_50_300(self):
        delays = human.typing_cadence("helloworld" * 8, rng=random.Random(0))
        assert delays
        assert all(50 <= d <= 300 for d in delays)

    def test_space_gaps_are_longer_than_intra_word(self):
        text = "ab cd ef gh ij kl mn op"
        delays = human.typing_cadence(text, rng=random.Random(1))
        letters = [d for ch, d in zip(text, delays) if ch.isalpha()]
        spaces = [d for ch, d in zip(text, delays) if ch == " "]
        assert spaces and letters
        assert all(150 <= d <= 300 for d in spaces)
        assert all(50 <= d <= 300 for d in letters)
        assert sum(spaces) / len(spaces) > sum(letters) / len(letters)

    def test_enter_is_never_faster_than_150ms(self):
        delays = human.typing_cadence("hi\n", rng=random.Random(2))
        assert delays[-1] >= human.ENTER_MIN_MS

    def test_click_press_stays_in_60_140(self):
        rng = random.Random(0)
        samples = [human.click_press_ms(rng) for _ in range(40)]
        assert all(60 <= s <= 140 for s in samples)

    def test_enter_delay_floor(self):
        rng = random.Random(3)
        samples = [human.enter_delay_ms(rng) for _ in range(20)]
        assert all(s >= 150 for s in samples)


class TestEasedPath:
    def test_step_count_is_8_to_20(self):
        for path in _paths():
            assert human.PATH_STEPS[0] <= len(path) <= human.PATH_STEPS[1]

    def test_waypoint_count_is_4_to_6(self):
        rng = random.Random(0)
        for _ in range(20):
            wps = human._waypoints((0.0, 0.0), (400.0, 80.0), rng)
            assert human.PATH_WAYPOINTS[0] <= len(wps) <= human.PATH_WAYPOINTS[1]
            assert wps[0] == (0.0, 0.0)
            assert wps[-1] == (400.0, 80.0)

    def test_timing_is_monotonic_and_in_duration_band(self):
        for path in _paths():
            times = [p[2] for p in path]
            assert times[0] == 0
            assert times == sorted(times)
            lo, hi = human.PATH_DURATION_MS
            assert lo <= times[-1] <= hi
            assert all(times[i] <= times[i + 1] for i in range(len(times) - 1))

    def test_endpoint_jitter_is_1_to_3px(self):
        lo, hi = human.ENDPOINT_JITTER_PX
        seen_nonzero = False
        for seed in range(30):
            path = human.eased_path(0, 0, 100, 50, rng=random.Random(seed))
            x, y, _ = path[-1]
            assert abs(x - 100) <= hi
            assert abs(y - 50) <= hi
            if abs(x - 100) >= lo or abs(y - 50) >= lo:
                seen_nonzero = True
        assert seen_nonzero

    def test_path_is_not_a_straight_line(self):
        bent = False
        for seed in range(20):
            path = human.eased_path(0, 0, 400, 0, rng=random.Random(seed))
            if any(abs(p[1]) > 2 for p in path[1:-1]):
                bent = True
                break
        assert bent

    def test_short_move_is_near_minimum_duration(self):
        path = human.eased_path(0, 0, 1, 0, rng=random.Random(3))
        assert 300 <= path[-1][2] <= 400

    def test_long_move_is_near_maximum_duration(self):
        path = human.eased_path(0, 0, 5000, 5000, rng=random.Random(3))
        assert 800 <= path[-1][2] <= 900

    def test_overshoot_sometimes_corrects(self):
        found = False
        for seed in range(80):
            path = human.eased_path(0, 0, 200, 0, rng=random.Random(seed))
            xs = [p[0] for p in path]
            if max(xs) > 200 + human.ENDPOINT_JITTER_PX[1] and xs[-1] <= 200 + human.ENDPOINT_JITTER_PX[1]:
                found = True
                break
        assert found


class TestSeededDeterminism:
    def test_same_rng_seed_same_path(self):
        a = human.eased_path(0, 0, 220, 90, rng=random.Random(99))
        b = human.eased_path(0, 0, 220, 90, rng=random.Random(99))
        assert a == b

    def test_different_seed_changes_path(self):
        a = human.eased_path(0, 0, 220, 90, rng=random.Random(99))
        b = human.eased_path(0, 0, 220, 90, rng=random.Random(100))
        assert a != b

    def test_same_rng_seed_same_cadence(self):
        a = human.typing_cadence("seeded phrase", rng=random.Random(7))
        b = human.typing_cadence("seeded phrase", rng=random.Random(7))
        assert a == b

    def test_session_seed_replays(self):
        human.set_mode(True, seed=42)
        p1 = human.eased_path(0, 0, 100, 100)
        human.set_mode(True, seed=42)
        p2 = human.eased_path(0, 0, 100, 100)
        assert p1 == p2


class TestScrollPlan:
    def test_bursts_sum_to_amount(self):
        for seed in range(15):
            plan = human.scroll_plan(7, rng=random.Random(seed))
            assert sum(n for n, _pause in plan) == 7
            assert all(human.SCROLL_PAUSE_MS[0] <= p <= human.SCROLL_PAUSE_MS[1]
                       for _n, p in plan)
            assert all(1 <= n <= 3 for n, _p in plan)

    def test_zero_amount_is_empty(self):
        assert human.scroll_plan(0, rng=random.Random(0)) == []


class TestTyposAndSensitive:
    def test_password_url_money_are_sensitive(self):
        assert human.sensitive_field("password", "hunter2")
        assert human.sensitive_field("", "https://example.com/a")
        assert human.sensitive_field("", "$12.50")
        assert human.sensitive_field("money", "12")
        assert not human.sensitive_field("", "hello there")

    def test_typos_skipped_on_url_even_when_requested(self, monkeypatch):
        typed = []
        monkeypatch.setattr(human.desktop, "type_text",
                            lambda text, delay_ms=40: typed.append(text) or {"ok": True})
        monkeypatch.setattr(human.desktop, "press_key",
                            lambda k: typed.append(f"<{k}>") or {"ok": True})
        monkeypatch.setattr(human.time, "sleep", lambda s: None)
        result = human.human_type("https://x.test", rng=random.Random(0),
                                  typos=True, field="url")
        assert result["typos"] is False
        assert result["corrections"] == 0
        assert "<BackSpace>" not in typed


class TestModeFlag:
    def test_default_is_off(self):
        state = human.get_mode()
        assert state["human_mode"] is False
        assert state["seed"] is None
        assert human.enabled() is False

    def test_set_on_without_seed_draws_one(self):
        state = human.set_mode(True)
        assert state["human_mode"] is True
        assert isinstance(state["seed"], int)
        assert human.get_mode() == state

    def test_set_on_with_seed_stores_it(self):
        state = server.workman_set_human_mode(True, seed=123)
        assert state == {"human_mode": True, "seed": 123}
        assert server.workman_get_human_mode() == state

    def test_set_off_clears_flag_keeps_seed(self):
        human.set_mode(True, seed=5)
        state = human.set_mode(False)
        assert state["human_mode"] is False
        assert state["seed"] == 5

    def test_mode_off_move_is_direct(self, monkeypatch):
        seen = []
        monkeypatch.setattr(server.desktop, "move",
                            lambda x, y: seen.append(("direct", x, y)) or {"ok": True, "at": [x, y]})
        monkeypatch.setattr(human, "human_move",
                            lambda *a, **k: seen.append("human") or {"ok": True})
        human.set_mode(False)
        result = server.move(10, 20)
        assert result["ok"] is True
        assert seen == [("direct", 10, 20)]

    def test_mode_on_move_is_human(self, monkeypatch):
        seen = []
        monkeypatch.setattr(server.desktop, "move",
                            lambda x, y: seen.append(("direct", x, y)) or {"ok": True})
        monkeypatch.setattr(human, "human_move",
                            lambda x, y, rng=None: seen.append(("human", x, y)) or
                            {"ok": True, "at": [x, y], "human": True, "points": 10})
        human.set_mode(True, seed=1)
        result = server.move(10, 20)
        assert result["human"] is True
        assert seen == [("human", 10, 20)]

    def test_mode_on_click_is_human(self, monkeypatch):
        seen = []
        monkeypatch.setattr(server.desktop, "click",
                            lambda *a, **k: seen.append("direct") or {"ok": True})
        monkeypatch.setattr(human, "human_click",
                            lambda x, y, rng=None, button=1, count=1: seen.append("human") or
                            {"ok": True, "human": True, "press_ms": 80, "at": [x, y]})
        human.set_mode(True, seed=1)
        result = server.click(4, 5)
        assert result["human"] is True
        assert seen == ["human"]

    def test_mode_off_click_is_direct(self, monkeypatch):
        seen = []
        monkeypatch.setattr(server.desktop, "click",
                            lambda x, y, button=1, count=1: seen.append("direct") or
                            {"ok": True, "clicked": [x, y]})
        monkeypatch.setattr(human, "human_click",
                            lambda *a, **k: seen.append("human") or {"ok": True})
        result = server.click(4, 5)
        assert seen == ["direct"]

    def test_mode_on_type_uses_cadence(self, monkeypatch):
        seen = []
        monkeypatch.setattr(server.desktop, "type_text",
                            lambda text, delay_ms=40: seen.append(("direct", text, delay_ms)) or
                            {"ok": True, "typed_len": len(text)})
        monkeypatch.setattr(human, "human_type",
                            lambda text, rng=None, typos=False, field="": seen.append(("human", text, typos)) or
                            {"ok": True, "human": True, "typed_len": len(text)})
        human.set_mode(True, seed=1)
        result = server.type_text("hi", typos=False)
        assert result["human"] is True
        assert seen == [("human", "hi", False)]

    def test_mode_off_type_is_one_shot(self, monkeypatch):
        seen = []
        monkeypatch.setattr(server.desktop, "type_text",
                            lambda text, delay_ms=40: seen.append(("direct", delay_ms)) or
                            {"ok": True, "typed_len": len(text)})
        server.type_text("hi", delay_ms=40)
        assert seen == [("direct", 40)]

    def test_mode_on_scroll_uses_bursts(self, monkeypatch):
        seen = []
        monkeypatch.setattr(server.desktop, "scroll",
                            lambda direction, amount=3: seen.append(("direct", amount)) or
                            {"ok": True})
        monkeypatch.setattr(human, "human_scroll",
                            lambda direction, amount=3, x=None, y=None, rng=None:
                            seen.append(("human", amount)) or
                            {"ok": True, "human": True, "scrolled": direction, "amount": amount})
        human.set_mode(True, seed=1)
        result = server.scroll("down", amount=5)
        assert result["human"] is True
        assert seen == [("human", 5)]
