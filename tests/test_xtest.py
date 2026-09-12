"""The persistent XTest channel, without an X server.

`Channel` is built around ctypes calls into libX11/libXtst. These tests
stand a fake pair of libraries under a Channel created with __new__, so the
sequencing logic (Shift around capitals, dwell before release, modifier
order in a combo, releasing what XTEST holds) is checked exactly, and the
pure helpers (keysym tables, combo parsing) too.
"""
from __future__ import annotations

import threading

import pytest

from workman import xtest


XK = {"a": 0x61, "A": 0x41, "b": 0x62, "h": 0x68, "H": 0x48, "i": 0x69, "Shift_L": 0xFFE1,
      "Control_L": 0xFFE3, "Return": 0xFF0D, "Escape": 0xFF1B, "t": 0x74, "space": 0x20,
      "eacute": 0xE9}
KEYCODE = {0x61: 38, 0x62: 56, 0x68: 43, 0x69: 31, 0xFFE1: 50, 0xFFE3: 37, 0xFF0D: 36,
           0xFF1B: 9, 0x74: 28, 0x20: 65}


class FakeX11:
    def __init__(self):
        self.flushes = 0

    def XStringToKeysym(self, name: bytes) -> int:
        return XK.get(name.decode(), 0)

    def XKeysymToString(self, sym: int):
        for k, v in XK.items():
            if v == sym:
                return k.encode()
        return None

    def XKeysymToKeycode(self, dpy, sym: int) -> int:
        return 0  # nothing outside the table is on the keyboard

    def XFlush(self, dpy):
        self.flushes += 1

    def XSync(self, dpy, discard):
        pass

    def XChangeKeyboardMapping(self, dpy, code, n, arr, count):
        self.remapped = (code, list(arr))


class FakeXtst:
    def __init__(self):
        self.events: list[tuple] = []

    def XTestFakeKeyEvent(self, dpy, code, down, t):
        self.events.append(("key", int(code), bool(down)))

    def XTestFakeButtonEvent(self, dpy, button, down, t):
        self.events.append(("button", int(button), bool(down)))

    def XTestFakeMotionEvent(self, dpy, screen, x, y, t):
        self.events.append(("move", int(x), int(y)))


def make_channel() -> xtest.Channel:
    ch = xtest.Channel.__new__(xtest.Channel)
    ch.display_name = ":fake"
    ch.lock = threading.RLock()
    ch.x11 = FakeX11()
    ch.xtst = FakeXtst()
    ch.xi = None
    ch.xext = None
    ch.dead = False
    ch.last_error = None
    ch.dpy = 1
    ch.screen = 0
    ch.root = 2
    # keysym -> (keycode, level): capitals live on the same key at level 1.
    ch._keymap = {sym: (code, 0) for sym, code in KEYCODE.items()}
    ch._keymap[0x41] = (38, 1)
    ch._keymap[0x48] = (43, 1)
    ch._scratch = {}
    ch._scratch_free = [250, 251]
    ch._xi_opcode = None
    ch._raw_selected = False
    return ch


@pytest.fixture
def ch(monkeypatch):
    monkeypatch.setattr(xtest.time, "sleep", lambda s: None)
    return make_channel()


class TestPureHelpers:
    def test_keysym_for_char_latin_and_controls(self):
        assert xtest.keysym_for_char("a") == 0x61
        assert xtest.keysym_for_char("A") == 0x41
        assert xtest.keysym_for_char(" ") == 0x20
        assert xtest.keysym_for_char("\n") == 0xFF0D
        assert xtest.keysym_for_char("\t") == 0xFF09
        assert xtest.keysym_for_char("é") == 0xE9
        assert xtest.keysym_for_char("€") == 0x01000000 | 0x20AC

    def test_parse_combo_uses_xdotool_aliases(self):
        assert xtest.parse_combo("ctrl+shift+t") == ["Control_L", "Shift_L", "t"]
        assert xtest.parse_combo("Return") == ["Return"]
        assert xtest.parse_combo("super+v") == ["Super_L", "v"]
        assert xtest.parse_combo("Escape") == ["Escape"]

    def test_enabled_env_switch(self, monkeypatch):
        monkeypatch.setenv("WORKMAN_XTEST", "0")
        assert xtest.enabled() is False
        monkeypatch.setenv("WORKMAN_XTEST", "1")
        assert xtest.enabled() is True

    def test_channel_off_returns_none_without_touching_x(self, monkeypatch):
        monkeypatch.setenv("WORKMAN_XTEST", "0")
        assert xtest.channel(":99") is None

    def test_no_grab_anywhere(self):
        # The owner's rule: nothing in the input path grabs a device.
        import inspect
        from workman import esc_pause, x11
        for mod in (xtest, esc_pause, x11):
            assert "XGrab" not in inspect.getsource(mod)


class TestTyping:
    def test_capital_is_typed_with_a_real_shift(self, ch):
        n = ch.type_text("Hi")
        assert n == 2
        assert ch.xtst.events == [
            ("key", 50, True),   # Shift_L down
            ("key", 43, True),   # h down
            ("key", 43, False),  # h up
            ("key", 50, False),  # Shift_L up
            ("key", 31, True),   # i down
            ("key", 31, False),  # i up
        ]

    def test_dwell_holds_the_key_before_release(self, ch, monkeypatch):
        slept = []
        monkeypatch.setattr(xtest.time, "sleep", lambda s: slept.append(round(s * 1000)))
        ch.type_text("a", dwell_ms=75, delay_ms=30)
        assert slept == [75, 30]
        assert ch.xtst.events == [("key", 38, True), ("key", 38, False)]

    def test_zero_dwell_sleeps_nothing(self, ch, monkeypatch):
        slept = []
        monkeypatch.setattr(xtest.time, "sleep", lambda s: slept.append(s))
        ch.type_text("ab")
        assert slept == []

    def test_needs_remap_only_for_symbols_off_the_keyboard(self, ch):
        assert ch.needs_remap(0x61) is False
        assert ch.needs_remap(0xE9) is True
        assert ch.needs_remap(0) is False

    def test_unmapped_symbol_uses_a_scratch_keycode(self, ch):
        ch.type_text("é")
        assert ch.x11.remapped[0] == 250
        assert ch.xtst.events == [("key", 250, True), ("key", 250, False)]


class TestCombos:
    def test_modifiers_down_in_order_then_up_in_reverse(self, ch):
        names = ch.press_combo("ctrl+shift+t")
        assert names == ["Control_L", "Shift_L", "t"]
        assert ch.xtst.events == [
            ("key", 37, True), ("key", 50, True), ("key", 28, True),
            ("key", 28, False), ("key", 50, False), ("key", 37, False),
        ]

    def test_shifted_base_key_adds_shift_once(self, ch):
        ch.press_combo("ctrl+A")
        assert ch.xtst.events == [
            ("key", 37, True), ("key", 50, True), ("key", 38, True),
            ("key", 38, False), ("key", 50, False), ("key", 37, False),
        ]

    def test_unknown_key_raises_before_anything_is_pressed(self, ch):
        with pytest.raises(xtest.ChannelError):
            ch.press_combo("ctrl+nosuchkey")
        assert ch.xtst.events == []

    def test_key_alias_resolves_and_zero_sym_is_refused(self, ch):
        ch.key("ctrl", True)
        assert ch.xtst.events == [("key", 37, True)]
        with pytest.raises(xtest.ChannelError):
            ch.key("bogus_key_name", True)


class TestHeldState:
    def test_release_xtest_held_lets_go_of_everything_and_reports(self, ch, monkeypatch):
        answers = [{"keys": [37, 50], "buttons": [1]}, {"keys": [], "buttons": []}]
        monkeypatch.setattr(ch, "xtest_held", lambda: answers.pop(0))
        out = ch.release_xtest_held()
        assert ch.xtst.events == [("key", 37, False), ("key", 50, False), ("button", 1, False)]
        assert out == {"released_keys": [37, 50], "released_buttons": [1],
                       "still_held": {"keys": [], "buttons": []}}

    def test_buttons_from_pointer_mask(self):
        assert xtest.Channel._buttons_from_mask(1 << 8) == [1]
        assert xtest.Channel._buttons_from_mask((1 << 8) | (1 << 10)) == [1, 3]
        assert xtest.Channel._buttons_from_mask(0) == []

    def test_keycode_names_maps_plain_level_only(self, ch):
        assert ch.keycode_names([37, 38, 999]) == ["Control_L", "a", "keycode999"]


class TestPointer:
    def test_click_is_move_then_down_up_per_count(self, ch):
        ch.click(10, 20, button=1, count=2)
        assert ch.xtst.events == [("move", 10, 20), ("button", 1, True), ("button", 1, False),
                                  ("button", 1, True), ("button", 1, False)]

    def test_dead_channel_refuses(self, ch):
        ch.dead = True
        with pytest.raises(xtest.ChannelError):
            ch.move(1, 1)


class TestWindowAtPoint:
    def test_picks_the_topmost_client_whose_frame_contains_the_point(self, ch):
        rects = {1: (0, 0, 100, 100), 2: (50, 50, 100, 100)}  # 2 stacked on 1
        ch._client_ids = lambda name: [1, 2]  # EWMH: bottom to top
        ch._frame_rect = lambda wid: rects.get(wid)
        ch._identity = lambda wid: {"ok": True, "id": wid, "class": f"w{wid}"}
        assert ch.window_at_point(60, 60)["id"] == 2
        assert ch.window_at_point(10, 10)["id"] == 1
        assert ch.window_at_point(900, 900) is None
