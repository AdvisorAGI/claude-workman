"""macOS keys and typing on a fake Quartz (nothing here runs on a Mac): real
US-layout keycodes, a real Shift around capitals, and the Human Mode dwell."""
from __future__ import annotations

import pytest

from workman import desktop
from workman.platform import darwin

SHIFT, CMD, CTRL = 56, 55, 59


class FakeQuartz:
    kCGHIDEventTap = "tap"
    kCGWindowListOptionOnScreenOnly = 1
    kCGWindowListExcludeDesktopElements = 16
    kCGNullWindowID = 0

    def __init__(self):
        self.posted: list[dict] = []
        self.windows: list[dict] = []
        self.sleeps: list[float] = []

    def CGEventCreateKeyboardEvent(self, src, code, down):
        return {"code": code, "down": bool(down), "flags": 0, "text": None}

    def CGEventSetFlags(self, ev, flags):
        ev["flags"] = flags

    def CGEventKeyboardSetUnicodeString(self, ev, n, text):
        ev["text"], ev["n"] = text, n

    def CGEventPost(self, tap, ev):
        self.posted.append(ev)

    def CGWindowListCopyWindowInfo(self, opts, wid):
        return list(self.windows)


def front(app: str, title: str = "") -> dict:
    return {"kCGWindowOwnerName": app, "kCGWindowName": title, "kCGWindowLayer": 0,
            "kCGWindowNumber": 1, "kCGWindowOwnerPID": 1, "kCGWindowBounds": {}}


def keys(q: FakeQuartz) -> list[tuple[int, bool]]:
    return [(e["code"], e["down"]) for e in q.posted]


@pytest.fixture
def mac(monkeypatch):
    q = FakeQuartz()
    monkeypatch.setattr(darwin, "_quartz_mod", q)
    monkeypatch.setattr(darwin, "_quartz_tried", True)
    monkeypatch.setattr(darwin.time, "sleep", q.sleeps.append)
    return q


class TestTypeText:
    def test_capitals_and_shifted_symbols_get_a_real_shift(self, mac):
        out = darwin.type_text("Hi!", delay_ms=0)
        assert out["ok"] is True and out["typed_len"] == 3
        assert keys(mac) == [(SHIFT, True), (4, True), (4, False), (SHIFT, False),
                             (34, True), (34, False),
                             (SHIFT, True), (18, True), (18, False), (SHIFT, False)]
        assert mac.posted[1]["flags"] == darwin._CG_FLAGS["shift"] and mac.posted[1]["text"] == "H"
        assert mac.posted[4]["flags"] == 0 and mac.posted[4]["text"] == "i"
        assert mac.posted[3]["flags"] == 0  # Shift up clears the flag

    def test_every_character_is_its_real_keycode(self, mac):
        """Regression (2026-09-12): every character went out on keycode 0 as a
        unicode payload, so a page saw an empty event.code and no Shift."""
        text = "Hello World, 42!"
        darwin.type_text(text, delay_ms=0)
        downs = [e for e in mac.posted if e["down"] and e["code"] != SHIFT]
        assert [e["text"] for e in downs] == list(text)
        assert [e["code"] for e in downs] == [darwin.US_CHARS[c][0] for c in text]
        assert sum(1 for e in mac.posted if e["code"] == SHIFT and e["down"]) == 3

    def test_dwell_holds_each_key_between_down_and_up(self, mac):
        darwin.type_text("ab", delay_ms=0, dwell_ms=80)
        assert mac.sleeps == [0.08, 0.08]

    def test_dwell_is_bounded(self, mac):
        darwin.type_text("a", delay_ms=0, dwell_ms=10**9)
        assert mac.sleeps == [darwin.MAX_KEY_MS / 1000.0]

    def test_off_layout_character_in_a_browser_is_pasted(self, mac, monkeypatch):
        mac.windows = [front("Google Chrome", "Inbox")]
        clip = []
        monkeypatch.setattr(darwin, "clipboard_get",
                            lambda selection="clipboard": {"ok": True, "text": "keep"})
        monkeypatch.setattr(darwin, "clipboard_set",
                            lambda text="", selection="clipboard": clip.append(text) or {"ok": True})
        out = darwin.type_text("é", delay_ms=0, dwell_ms=50)
        assert out["ok"] is True and clip == ["é", "keep"]
        assert keys(mac) == [(CMD, True), (9, True), (9, False), (CMD, False)]
        assert mac.sleeps[0] == 0.05 and darwin.PASTE_RESTORE_S in mac.sleeps

    def test_off_layout_character_in_a_native_app_is_a_unicode_payload(self, mac, monkeypatch):
        mac.windows = [front("TextEdit", "Untitled")]
        monkeypatch.setattr(darwin, "clipboard_set", lambda **k: pytest.fail("clipboard used"))
        darwin.type_text("é", delay_ms=0)
        assert [(e["code"], e["text"]) for e in mac.posted] == [(0, "é"), (0, "é")]

    def test_a_failed_paste_reports_how_far_it_got(self, mac, monkeypatch):
        mac.windows = [front("Safari")]
        monkeypatch.setattr(darwin, "clipboard_get",
                            lambda selection="clipboard": {"ok": True, "text": "keep"})
        monkeypatch.setattr(darwin, "clipboard_set",
                            lambda text="", selection="clipboard": {"ok": False, "error": "pbcopy failed"})
        out = darwin.type_text("aé", delay_ms=0)
        assert out["ok"] is False and out["typed_len"] == 1 and "pbcopy" in out["error"]

    def test_paste_restores_the_clipboard(self, mac, monkeypatch):
        mac.windows = [front("Google Chrome", "Inbox")]
        clip = {"text": "owner clip"}
        monkeypatch.setattr(darwin, "clipboard_get",
                            lambda selection="clipboard": {"ok": True, "text": clip["text"]})
        monkeypatch.setattr(darwin, "clipboard_set",
                            lambda text="", selection="clipboard":
                            clip.__setitem__("text", text) or {"ok": True})
        out = darwin.type_text("café", delay_ms=0)
        assert out["ok"] is True and clip["text"] == "owner clip"
        assert darwin.PASTE_RESTORE_S in mac.sleeps


class TestPressKey:
    def test_hold_ms_is_the_dwell(self, mac):
        assert darwin.press_key("a", hold_ms=90)["ok"] is True
        assert keys(mac) == [(0, True), (0, False)] and mac.sleeps == [0.09]

    def test_modifiers_are_real_key_events_around_the_key(self, mac):
        darwin.press_key("ctrl+c")
        assert keys(mac) == [(CTRL, True), (8, True), (8, False), (CTRL, False)]
        assert mac.posted[1]["flags"] == darwin._CG_FLAGS["ctrl"] and mac.posted[3]["flags"] == 0

    def test_a_shifted_name_gets_a_real_shift(self, mac):
        darwin.press_key("plus")
        assert keys(mac) == [(SHIFT, True), (24, True), (24, False), (SHIFT, False)]

    def test_desktop_passes_the_human_dwell_to_darwin(self, mac, monkeypatch):
        """Regression (2026-09-12): darwin.press_key had no hold_ms, so
        desktop._accepts dropped the Human Mode dwell (H3) on macOS."""
        monkeypatch.setattr(desktop, "active", lambda: darwin)
        monkeypatch.setattr(desktop, "_ACCEPTS", {})
        monkeypatch.setattr(desktop, "_VIEWER_SEEN", {"at": -1.0, "hit": False})
        assert desktop._accepts("press_key", "hold_ms") and desktop._accepts("type_text", "dwell_ms")
        assert desktop.press_key("Tab", hold_ms=77)["ok"] is True
        assert desktop.type_text("x", delay_ms=0, dwell_ms=66)["ok"] is True
        assert mac.sleeps == [0.077, 0.066]


class TestFrontmost:
    @pytest.mark.parametrize("name", ["Google Chrome", "Safari", "Arc", "Microsoft Edge",
                                      "Brave Browser", "Firefox", "Chromium", "Claude",
                                      "Cursor", "Slack", "Visual Studio Code", "Electron",
                                      "WebKit Nightly"])
    def test_browsers_and_electron_apps(self, name):
        assert darwin.is_browser_app(name) is True

    @pytest.mark.parametrize("name", ["TextEdit", "Finder", "System Settings", "Terminal", ""])
    def test_native_apps(self, name):
        assert darwin.is_browser_app(name) is False

    def test_screen_sharing_in_front_stops_agent_input(self, mac, monkeypatch):
        mac.windows = [front("Screen Sharing", "mac-mini")]
        monkeypatch.setattr(desktop, "active", lambda: darwin)
        monkeypatch.setattr(desktop, "_VIEWER_SEEN", {"at": -1.0, "hit": False})
        assert desktop.type_text("x")["error"] == "remote_viewer_focused"
        assert mac.posted == []

    def test_window_at_point_hits_an_unfocused_viewer(self, mac):
        mac.windows = [
            {**front("TextEdit", "notes"),
             "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 100, "Height": 100}},
            {**front("Screen Sharing", "mac-mini"),
             "kCGWindowBounds": {"X": 200, "Y": 200, "Width": 400, "Height": 300}},
        ]
        viewer = darwin.window_at_point(250, 250)
        local = darwin.window_at_point(10, 10)
        assert desktop.is_remote_viewer(viewer) is True
        assert desktop.is_remote_viewer(local) is False
        assert darwin.window_at_point(900, 900) is None
