"""Stability: an interrupted sequence stops where it stopped, lets go of what
it holds, and one broken module does not take the whole server down."""
from __future__ import annotations

import random

import pytest

from workman import esc_pause, human, owner_pause, server

REFUSED = {"ok": False, "error": "input_disabled"}


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    human.reset()
    monkeypatch.setattr(human.time, "sleep", lambda s: None)
    monkeypatch.setattr(human, "_pointer", lambda: (0, 0))
    yield
    human.reset()


@pytest.fixture
def mouse(monkeypatch):
    """A fake mouse that records presses and releases."""
    state = {"down": False, "events": []}

    def mouse_down(button=1, **_):
        state["down"] = True
        state["events"].append("down")
        return {"ok": True}

    def mouse_up(button=1, **_):
        state["down"] = False
        state["events"].append("up")
        return {"ok": True}

    monkeypatch.setattr(human.desktop, "mouse_down", mouse_down)
    monkeypatch.setattr(human.desktop, "mouse_up", mouse_up)
    monkeypatch.setattr(human.desktop, "move", lambda x, y: {"ok": True})
    return state


class TestLetsGo:
    def test_drag_releases_the_button_when_a_move_is_refused(self, monkeypatch, mouse):
        monkeypatch.setattr(human.desktop, "move",
                            lambda x, y: REFUSED if mouse["down"] else {"ok": True})
        out = human.human_drag(0, 0, 300, 200, rng=random.Random(1))
        assert out["ok"] is False and out["error"] == "interrupted"
        assert out["reason"] == REFUSED and out["released"] is True
        assert mouse["events"] == ["down", "up"] and mouse["down"] is False

    def test_drag_releases_the_button_when_the_backend_dies(self, monkeypatch, mouse):
        def move(x, y):
            if mouse["down"]:
                raise RuntimeError("xdotool timed out")
            return {"ok": True}
        monkeypatch.setattr(human.desktop, "move", move)
        out = human.human_drag(0, 0, 300, 200, rng=random.Random(2))
        assert out["reason"]["error"] == "backend_error"
        assert "xdotool timed out" in out["reason"]["detail"]
        assert mouse["down"] is False

    def test_refused_press_sends_no_stray_release(self, monkeypatch, mouse):
        monkeypatch.setattr(human.desktop, "mouse_down", lambda button=1, **_: REFUSED)
        out = human.human_click(40, 40, rng=random.Random(3))
        assert out["ok"] is False and out["clicked"] is False
        assert mouse["events"] == []

    def test_click_with_modifiers_releases_only_what_went_down(self, monkeypatch, mouse):
        keys = []
        monkeypatch.setattr(server.human, "human_move",
                            lambda x, y, rng=None: {"ok": True, "at": [x, y]})
        monkeypatch.setattr(server.desktop, "key_down",
                            lambda k: keys.append(("down", k)) or {"ok": True})
        monkeypatch.setattr(server.desktop, "key_up",
                            lambda k: keys.append(("up", k)) or {"ok": True})
        monkeypatch.setattr(human.desktop, "mouse_down", lambda button=1, **_: REFUSED)
        out = server._click_with_human(10, 10, 1, 1, ["ctrl", "shift"])
        assert out["ok"] is False and out["clicked"] is False
        assert keys == [("down", "ctrl"), ("down", "shift"),
                        ("up", "shift"), ("up", "ctrl")]

    def test_pause_never_blocks_letting_go(self, monkeypatch, tmp_path):
        monkeypatch.setenv("WORKMAN_LEARN_ROOT", str(tmp_path))
        owner_pause.pause_from_escape()
        assert owner_pause.blocked("mouse_down") and owner_pause.blocked("key_down")
        assert not owner_pause.blocked("mouse_up")
        assert not owner_pause.blocked("key_up")


class TestStopsWhereItStopped:
    def test_typing_reports_what_is_left(self, monkeypatch):
        typed = []

        def type_text(text, delay_ms=0, dwell_ms=0):
            if len(typed) == 3:
                return REFUSED
            typed.append(text)
            return {"ok": True}
        monkeypatch.setattr(human.desktop, "type_text", type_text)
        out = human.human_type("hello", rng=random.Random(0))
        assert out["ok"] is False
        assert out["typed_len"] == 3 and out["remaining"] == "lo"
        assert "".join(typed) == "hel"

    def test_scroll_reports_how_far_it_got(self, monkeypatch):
        done = []
        monkeypatch.setattr(human.desktop, "scroll",
                            lambda d, amount=1: REFUSED if done else done.append(d))
        out = human.human_scroll("down", amount=5, rng=random.Random(4))
        assert out["ok"] is False and out["amount"] == 1
        assert out["remaining"] >= 1

    def test_refused_move_does_not_claim_to_arrive(self, monkeypatch):
        monkeypatch.setattr(human.desktop, "move", lambda x, y: REFUSED)
        out = human.human_move(500, 500, rng=random.Random(5))
        assert out["ok"] is False and out["at"] == [0, 0]


LISTING = """\
⎡ Virtual core pointer                    \tid=2\t[master pointer  (3)]
⎜   ↳ Virtual core XTEST pointer              \tid=4\t[slave  pointer  (2)]
⎣ Virtual core keyboard                   \tid=3\t[master keyboard (2)]
    ↳ Virtual core XTEST keyboard             \tid=5\t[slave  keyboard (3)]
    ↳ Logitech USB Keyboard                   \tid=9\t[slave  keyboard (3)]
"""

OWNER, AGENT = 9, 5
ESC, CTRL = 9, 37


def _raw(kind: str, source: int, code: int) -> list[str]:
    etype, name = (13, "RawKeyPress") if kind == "press" else (14, "RawKeyRelease")
    return [f"EVENT type {etype} ({name})", f"    device: 3 ({source})",
            "    time:   110588489", f"    detail: {code}", "    valuators:"]


def _fires(watcher: esc_pause.RawKeyWatcher, *events) -> bool:
    return any(watcher.feed(line) for ev in events for line in _raw(*ev))


class TestEscapeTellsOwnerFromAgent:
    def test_xtest_devices_are_found(self):
        assert esc_pause.xtest_device_ids(LISTING) == {4, 5}

    def test_owner_escape_pauses(self):
        w = esc_pause.RawKeyWatcher({4, 5})
        assert _fires(w, ("press", OWNER, ESC))

    def test_agent_escape_is_ignored(self):
        w = esc_pause.RawKeyWatcher({4, 5})
        assert not _fires(w, ("press", AGENT, ESC))

    def test_escape_release_does_not_fire(self):
        w = esc_pause.RawKeyWatcher({4, 5})
        assert not _fires(w, ("release", OWNER, ESC))

    def test_owner_ctrl_escape_is_a_shortcut_not_a_pause(self):
        w = esc_pause.RawKeyWatcher({4, 5})
        assert not _fires(w, ("press", OWNER, CTRL), ("press", OWNER, ESC))
        assert _fires(w, ("release", OWNER, CTRL), ("press", OWNER, ESC))

    def test_agent_held_ctrl_does_not_mask_the_owner(self):
        w = esc_pause.RawKeyWatcher({4, 5})
        assert _fires(w, ("press", AGENT, CTRL), ("press", OWNER, ESC))


class TestOneBrokenModule:
    def test_missing_module_answers_instead_of_crashing(self):
        mod = server._optional("no_such_module_here")
        out = mod.anything(1, two=2)
        assert out == {"ok": False, "error": "module_unavailable",
                       "module": "no_such_module_here",
                       "detail": out["detail"]}
        assert "ModuleNotFoundError" in out["detail"]

    def test_present_module_is_the_real_one(self):
        assert server._optional("shotlog") is server.shotlog
