"""What a page would see: the shape of the events, not just their count.

Motion at a mouse-report rate with no teleport, a hover before every click,
a 60-140 ms hold, per-key dwell in a human band, a real Shift on capitals,
no scratch-keycode remap while a browser has focus, and a11y used only to
FIND elements inside a browser.
"""
from __future__ import annotations

import random

import pytest

from workman import atspi, human, server, x11


@pytest.fixture
def desk(monkeypatch):
    """A fake desktop that records every primitive in order."""
    events: list[tuple] = []
    monkeypatch.setattr(human, "_pointer", lambda: (0, 0))
    monkeypatch.setattr(human.time, "sleep", lambda s: events.append(("sleep", round(s * 1000))))
    monkeypatch.setattr(human.desktop, "move", lambda x, y: events.append(("move", x, y)) or {"ok": True})
    monkeypatch.setattr(human.desktop, "mouse_down",
                        lambda button=1, x=None, y=None: events.append(("down", button)) or {"ok": True})
    monkeypatch.setattr(human.desktop, "mouse_up",
                        lambda button=1, x=None, y=None: events.append(("up", button)) or {"ok": True})
    monkeypatch.setattr(human.desktop, "scroll",
                        lambda direction, amount=3: events.append(("scroll", direction, amount)) or {"ok": True})
    monkeypatch.setattr(human.desktop, "type_text",
                        lambda text, delay_ms=40, dwell_ms=0: events.append(("type", text, dwell_ms)) or {"ok": True})
    monkeypatch.setattr(human.desktop, "press_key", lambda k: events.append(("key", k)) or {"ok": True})
    return events


class TestPointerShape:
    def test_click_is_many_moves_a_pause_then_hold_then_release(self, desk):
        out = human.human_click(500, 300, rng=random.Random(1))
        assert out["ok"] is True
        kinds = [e[0] for e in desk]
        moves = [e for e in desk if e[0] == "move"]
        assert len(moves) >= human.PATH_MIN_STEPS
        i_down = kinds.index("down")
        # The last event before the press is a pause (the hover), not a move.
        assert desk[i_down - 1][0] == "sleep"
        assert human.AIM_PAUSE_MS[0] <= desk[i_down - 1][1] <= human.AIM_PAUSE_MS[1]
        hold = desk[i_down + 1]
        assert hold[0] == "sleep" and 60 <= hold[1] <= 140
        assert desk[i_down + 2] == ("up", 1)

    def test_no_teleport_between_samples(self, desk):
        human.human_click(900, 500, rng=random.Random(2))
        pts = [(e[1], e[2]) for e in desk if e[0] == "move"]
        total = ((pts[-1][0] - pts[0][0]) ** 2 + (pts[-1][1] - pts[0][1]) ** 2) ** 0.5
        for a, b in zip(pts, pts[1:]):
            assert ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 <= max(8.0, total * 0.12)

    def test_server_click_in_human_mode_never_teleports(self, desk, monkeypatch):
        human.set_mode(True, seed=5)
        monkeypatch.setattr(server.desktop, "click", lambda *a, **k: (_ for _ in ()).throw(AssertionError("teleport click")))
        out = server.click(640, 360)
        assert out["ok"] is True
        assert len([e for e in desk if e[0] == "move"]) >= human.PATH_MIN_STEPS
        human.reset()

    def test_scroll_is_discrete_ticks_with_cadence(self, desk):
        out = human.human_scroll("down", amount=6, x=100, y=100, rng=random.Random(3))
        assert out["ok"] is True and out["amount"] == 6
        ticks = [e for e in desk if e[0] == "scroll"]
        assert ticks == [("scroll", "down", 1)] * 6
        # every tick is separated from the next by a pause
        idx = [i for i, e in enumerate(desk) if e[0] == "scroll"]
        for a, b in zip(idx, idx[1:]):
            assert any(e[0] == "sleep" for e in desk[a + 1:b])

    def test_drag_moves_along_a_path_while_held(self, desk):
        out = human.human_drag(10, 10, 400, 200, rng=random.Random(4))
        assert out["ok"] is True and out["released"] is True
        kinds = [e[0] for e in desk]
        i_down, i_up = kinds.index("down"), kinds.index("up")
        assert kinds[i_down + 1:i_up].count("move") >= human.PATH_MIN_STEPS - 1


class TestKeyboardShape:
    def test_each_key_has_dwell_and_flight_in_human_bands(self, desk):
        out = human.human_type("hello world", rng=random.Random(7))
        assert out["ok"] is True
        for i, e in enumerate(desk):
            if e[0] == "type":
                lo, hi = human.KEY_DWELL_MS
                assert lo <= e[2] <= hi
                nxt = desk[i + 1]
                assert nxt[0] == "sleep" and nxt[1] >= 20

    def test_return_is_a_key_press_after_a_pause(self, desk):
        human.human_type("a\n", rng=random.Random(8))
        assert ("key", "Return") in desk
        i = desk.index(("key", "Return"))
        assert desk[i - 1][0] == "sleep" and desk[i - 1][1] >= human.ENTER_MIN_MS

    def test_capitals_go_through_shift_on_the_channel(self, monkeypatch):
        from test_xtest import make_channel
        ch = make_channel()
        monkeypatch.setattr(x11.time, "sleep", lambda s: None)
        monkeypatch.setattr(x11, "_xt", lambda: ch)
        monkeypatch.setattr(x11, "active_window_is_browser", lambda: True)
        out = x11.type_text("Hi", dwell_ms=5)
        assert out["ok"] is True and out["via"] == "xtest"
        assert ch.xtst.events[0] == ("key", 50, True)  # Shift_L first
        assert ch.xtst.events[3] == ("key", 50, False)

    def test_browser_gets_paste_instead_of_a_scratch_remap(self, monkeypatch):
        from test_xtest import make_channel
        ch = make_channel()
        monkeypatch.setattr(x11, "_xt", lambda: ch)
        monkeypatch.setattr(x11, "active_window_is_browser", lambda: True)
        pasted = []
        monkeypatch.setattr(x11, "_paste", lambda t: pasted.append(t) or {"ok": True})
        monkeypatch.setattr(x11, "_clipboard_snapshot", lambda: None)
        out = x11.type_text("hi é ab")  # only é is off the fake keyboard
        assert out["ok"] is True and out["via"] == "xtest+paste"
        assert pasted == ["é"]
        assert not hasattr(ch.x11, "remapped")  # no XChangeKeyboardMapping
        typed_codes = [e[1] for e in ch.xtst.events if e[0] == "key" and e[2]]
        assert 250 not in typed_codes

    def test_browser_paste_restores_the_clipboard(self, monkeypatch):
        from test_xtest import make_channel
        ch = make_channel()
        monkeypatch.setattr(x11, "_xt", lambda: ch)
        monkeypatch.setattr(x11, "active_window_is_browser", lambda: True)
        monkeypatch.setattr(x11.time, "sleep", lambda s: None)
        clip = {"text": "owner clip"}
        monkeypatch.setattr(x11, "_clipboard_snapshot",
                            lambda: {"ok": True, "text": clip["text"]})

        def restore(snap):
            if snap and snap.get("ok"):
                clip["text"] = snap.get("text") or ""
        monkeypatch.setattr(x11, "_clipboard_restore", restore)
        monkeypatch.setattr(x11, "_paste",
                            lambda t: clip.__setitem__("text", t) or {"ok": True})
        out = x11.type_text("café")
        assert out["ok"] is True and clip["text"] == "owner clip"

    def test_native_app_still_uses_the_scratch_keycode(self, monkeypatch):
        from test_xtest import make_channel
        ch = make_channel()
        monkeypatch.setattr(x11, "_xt", lambda: ch)
        monkeypatch.setattr(x11, "active_window_is_browser", lambda: False)
        out = x11.type_text("é")
        assert out["via"] == "xtest" and ch.x11.remapped[0] == 250


class FakeIface:
    def __init__(self):
        self.done = []
        self.set_to = None

    def get_n_actions(self):
        return 1

    def get_action_name(self, i):
        return "click"

    def do_action(self, i):
        self.done.append(i)

    def set_text_contents(self, v):
        self.set_to = v


class FakeNode:
    def __init__(self, browser: bool):
        self.browser = browser
        self.iface = FakeIface()

    def get_action_iface(self):
        return self.iface

    def get_editable_text_iface(self):
        return self.iface


INFO = {"app": "x", "role": "push button", "name": "Sign in", "x": 100, "y": 200, "w": 80, "h": 30,
        "cx": 140, "cy": 215}


@pytest.fixture
def a11y(monkeypatch):
    clicks = []
    monkeypatch.setattr(atspi, "is_browser_element", lambda node: node.browser)
    monkeypatch.setattr(atspi, "_human_click_info",
                        lambda info: clicks.append(info) or {"ok": True, "aimed": [141, 216]})
    monkeypatch.setattr(atspi.time, "sleep", lambda s: None)
    return clicks


class TestBrowserA11yIsFindOnly:
    def test_perform_action_in_browser_uses_the_pointer(self, monkeypatch, a11y):
        node = FakeNode(browser=True)
        monkeypatch.setattr(atspi, "find_node", lambda *a, **k: (node, dict(INFO)))
        out = atspi.perform_action("Sign in", "click")
        assert out["ok"] is True and out["via"] == "pointer"
        assert node.iface.done == [] and a11y == [INFO]

    def test_perform_action_in_native_app_uses_the_toolkit(self, monkeypatch, a11y):
        node = FakeNode(browser=False)
        monkeypatch.setattr(atspi, "find_node", lambda *a, **k: (node, dict(INFO)))
        out = atspi.perform_action("Sign in", "click")
        assert out["ok"] is True and out["via"] == "atspi"
        assert node.iface.done == [0] and a11y == []

    def test_set_value_in_browser_is_click_select_all_type(self, monkeypatch, a11y):
        node = FakeNode(browser=True)
        monkeypatch.setattr(atspi, "find_node", lambda *a, **k: (node, dict(INFO)))
        keys, typed = [], []
        from workman import desktop
        monkeypatch.setattr(desktop, "press_key", lambda k: keys.append(k) or {"ok": True})
        monkeypatch.setattr(desktop, "type_text", lambda t, **k: typed.append(t) or {"ok": True})
        out = atspi.set_value("Email", "me@example.com")
        assert out["ok"] is True and out["via"] == "pointer+keys"
        assert a11y == [INFO] and keys == ["ctrl+a"] and typed == ["me@example.com"]
        assert node.iface.set_to is None

    def test_set_value_in_native_app_writes_through_the_toolkit(self, monkeypatch, a11y):
        node = FakeNode(browser=False)
        monkeypatch.setattr(atspi, "find_node", lambda *a, **k: (node, dict(INFO)))
        out = atspi.set_value("Name", "Tariqul")
        assert out["via"] == "editable_text" and node.iface.set_to == "Tariqul"
        assert a11y == []

    def test_click_element_aims_inside_the_box_not_the_centre(self, monkeypatch):
        monkeypatch.setattr(atspi, "find", lambda *a, **k: dict(INFO))
        human.set_mode(True, seed=11)
        seen = []
        monkeypatch.setattr(human, "human_click",
                            lambda x, y, rng=None, button=1, count=1, target_px=None:
                            seen.append((x, y, target_px)) or {"ok": True})
        out = atspi.click_element("Sign in")
        assert out["ok"] is True
        x, y, target = seen[0]
        assert INFO["x"] <= x < INFO["x"] + INFO["w"] and INFO["y"] <= y < INFO["y"] + INFO["h"]
        assert target == 30.0
        points = {human.jitter_point(100, 200, 80, 30, random.Random(s)) for s in range(30)}
        assert len(points) > 10  # not the same point every time
        human.reset()

    def test_is_browser_element_reads_toolkit_or_app_name(self):
        class App:
            def __init__(self, toolkit, name):
                self._t, self._n = toolkit, name

            def get_toolkit_name(self):
                return self._t

            def get_name(self):
                return self._n

        class Node:
            def __init__(self, app):
                self._app = app

            def get_application(self):
                return self._app

        assert atspi.is_browser_element(Node(App("Chromium", "Google Chrome"))) is True
        assert atspi.is_browser_element(Node(App("gtk", "claude"))) is True
        assert atspi.is_browser_element(Node(App("gtk", "gedit"))) is False
