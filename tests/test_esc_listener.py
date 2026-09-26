"""The Escape listener: raw XInput2 events in, one decision out.

No X server here. `RawInputWatcher.feed` is fed the dicts the channel
produces, with `claims` injected so the tests never touch the stamps.
"""
from __future__ import annotations

import pathlib

import pytest

from workman import esc_pause, owner_pause

ESC = esc_pause.X11_ESCAPE
CTRL = next(iter(esc_pause.X11_MODIFIERS))
PHYSICAL, XTEST = 9, 5


def watcher(claims_escape=False, claims_input=False):
    def claims(escape=False):
        return claims_escape if escape else claims_input
    return esc_pause.RawInputWatcher(injected={XTEST}, claims=claims)


class TestRawInputWatcher:
    def test_physical_escape_pauses(self):
        w = watcher()
        assert w.feed({"kind": "key_press", "source": PHYSICAL, "detail": ESC}) == "escape"

    def test_modified_escape_is_just_presence(self):
        w = watcher()
        assert w.feed({"kind": "key_press", "source": PHYSICAL, "detail": CTRL}) == "human"
        assert w.feed({"kind": "key_press", "source": PHYSICAL, "detail": ESC}) == "human"
        assert w.feed({"kind": "key_release", "source": PHYSICAL, "detail": CTRL}) == "human"
        assert w.feed({"kind": "key_press", "source": PHYSICAL, "detail": ESC}) == "escape"

    def test_agents_own_escape_is_ignored(self):
        w = watcher(claims_escape=True)
        assert w.feed({"kind": "key_press", "source": XTEST, "detail": ESC}) == "agent"

    def test_unclaimed_xtest_escape_is_the_owner_over_the_kvm(self):
        w = watcher(claims_escape=False)
        assert w.feed({"kind": "key_press", "source": XTEST, "detail": ESC}) == "escape"

    def test_agent_motion_is_not_presence_but_kvm_motion_is(self):
        assert watcher(claims_input=True).feed(
            {"kind": "motion", "source": XTEST, "detail": 0}) == "agent"
        assert watcher(claims_input=False).feed(
            {"kind": "motion", "source": XTEST, "detail": 0}) == "human"

    def test_physical_motion_and_buttons_are_presence(self):
        w = watcher(claims_input=True)
        assert w.feed({"kind": "motion", "source": PHYSICAL, "detail": 0}) == "human"
        assert w.feed({"kind": "button_press", "source": PHYSICAL, "detail": 1}) == "human"

    def test_empty_event_is_nothing(self):
        assert watcher().feed(None) is None


class TestFallbackParsing:
    def test_xtest_device_ids_from_xinput_list(self):
        listing = (
            "  Virtual core pointer                    \tid=2\t[master pointer  (3)]\n"
            "    Virtual core XTEST pointer            \tid=4\t[slave  pointer  (2)]\n"
            "    Logitech G402                          \tid=9\t[slave  pointer  (2)]\n"
            "    Virtual core XTEST keyboard           \tid=5\t[slave  keyboard (3)]\n"
        )
        assert esc_pause.xtest_device_ids(listing) == {4, 5}

    def test_raw_key_watcher_folds_xinput_lines(self):
        w = esc_pause.RawKeyWatcher(injected={5})
        lines = ["EVENT type 13 (RawKeyPress)", "    device: 3 (9)", f"    detail: {ESC}"]
        assert [w.feed(line) for line in lines][-1] is True
        lines = ["EVENT type 13 (RawKeyPress)", "    device: 3 (5)", f"    detail: {ESC}"]
        assert [w.feed(line) for line in lines][-1] is False


class TestSwitchHandling:
    def test_physical_escape_pauses_and_releases(self, monkeypatch):
        released = []
        monkeypatch.setattr(esc_pause, "release_agent_holds",
                            lambda display=None: released.append(display) or {"ok": True})
        out = esc_pause._on_physical_escape(":0")
        assert out["state"]["mouse"] is False and out["state"]["paused_by"] == "escape"
        assert released == [":0"]
        assert owner_pause.blocked("click") is True

    def test_fresh_listener_starts_with_input_on_after_escape(self):
        owner_pause.pause_from_escape()
        s = esc_pause.start_with_input_on()
        assert s["mouse"] is True and s["keyboard"] is True

    def test_fresh_listener_leaves_a_manual_pause_alone(self):
        owner_pause._write({"mouse": False, "keyboard": True, "paused_by": "owner"})
        s = esc_pause.start_with_input_on()
        assert s["mouse"] is False and s["keyboard"] is True

    def test_status_without_channel_says_xdotool(self, monkeypatch):
        monkeypatch.setattr(esc_pause, "listener_pid", lambda: None)
        s = esc_pause.status(":99")
        assert s["input_channel"] == "xdotool"
        assert s["listener_pid"] is None
        assert "input_switches" in s

    def test_release_without_channel_is_an_error_not_a_crash(self):
        out = esc_pause.release_agent_holds(":99")
        assert out["ok"] is False


class TestUnitFiles:
    ROOT = pathlib.Path(__file__).resolve().parents[1] / "ops" / "esc-pause"

    def test_systemd_unit_starts_listener_with_input_on_and_no_grab(self):
        path = self.ROOT / "workman-esc-pause.service"
        if not path.is_file():
            pytest.skip("ops/esc-pause is not in this checkout")
        unit = path.read_text()
        assert "workman.esc_pause" in unit
        assert "--pause" not in unit
        assert "Restart=always" in unit
        assert "WantedBy=default.target" in unit
        assert "grab" not in unit.lower().replace("no grab", "")

    def test_launchd_plist_runs_the_darwin_listener(self):
        path = self.ROOT / "ai.atmosphere.workman-esc-pause.plist"
        if not path.is_file():
            pytest.skip("ops/esc-pause is not in this checkout")
        plist = path.read_text()
        assert "workman.esc_pause" in plist
        assert "RunAtLoad" in plist and "KeepAlive" in plist
