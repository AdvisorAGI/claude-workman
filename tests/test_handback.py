"""hand_back: release everything, close only what we opened, park, verify."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from workman import handback, human, owner_pause, server


class FakeChannel:
    def __init__(self, held=None, keys_down=None):
        self.held = held or {"keys": [], "buttons": []}
        self.keys_down = keys_down or []
        self.closed: list[int] = []
        self.woke = 0

    def release_xtest_held(self):
        out = {"released_keys": list(self.held["keys"]),
               "released_buttons": list(self.held["buttons"])}
        self.held = {"keys": [], "buttons": []}
        out["still_held"] = dict(self.held)
        return out

    def xtest_held(self):
        return dict(self.held)

    def core_keys_down(self):
        return list(self.keys_down)

    def keycode_names(self, codes):
        return [f"key{c}" for c in codes]

    def close_window(self, wid):
        self.closed.append(int(wid))

    def wake_screen(self):
        self.woke += 1
        return {"was": {"on": False}, "now": {"on": True}}


class FakeDesktop:
    def __init__(self, windows):
        self.windows = windows
        self.calls: list[tuple] = []
        self.viewer = False

    def remote_viewer_focused(self):
        return self.viewer

    def key_up(self, key):
        self.calls.append(("key_up", key))
        return {"ok": True}

    def mouse_up(self, button=1, x=None, y=None):
        self.calls.append(("mouse_up", button))
        return {"ok": True}

    def list_windows(self):
        return list(self.windows)

    def window_action(self, wid, action):
        self.calls.append(("window_action", wid, action))
        return {"ok": True}

    def pointer_position(self):
        return {"ok": True, "x": 700, "y": 400}

    def screen_size(self):
        return (1920, 1080)

    def move(self, x, y):
        self.calls.append(("move", x, y))
        return {"ok": True, "at": [x, y]}

    def active_window(self):
        return {"ok": True, "id": "0x9", "name": "owner's editor", "class": "gedit"}


@pytest.fixture
def desk(monkeypatch):
    windows = [
        {"id": "0x1", "name": "agent xterm", "pid": "4242"},
        {"id": "0x2", "name": "owner's editor", "pid": "1"},
        {"id": "0x3", "name": "agent child window", "pid": "4300"},
    ]
    d = FakeDesktop(windows)
    monkeypatch.setattr(handback, "desktop", d)
    monkeypatch.setattr(handback, "_descendants", lambda pid: {4242, 4300} if pid == 4242 else {pid})
    return d


class TestBookkeeping:
    def test_launch_and_holds_are_tracked_through_the_tools(self, monkeypatch):
        shown = [[{"id": "0x2", "name": "owner's editor", "pid": "1"}]]

        def launch(c, args=None, wait_for_window=0):
            shown.append(shown[0] + [{"id": "0x5", "name": "xterm", "pid": "77"}])
            return {"ok": True, "pid": 77, "argv": [c], "window": {"id": "0x5"}}
        monkeypatch.setattr(server.apps, "launch", launch)
        monkeypatch.setattr(handback, "_list_windows", lambda: list(shown[-1]))
        monkeypatch.setattr(handback, "_descendants", lambda pid: {pid})
        server.launch_app("xterm")
        entry = handback.launched()[0]
        assert entry["pid"] == 77 and entry["window"] == "0x5" and entry["windows"] == ["0x5"]
        monkeypatch.setattr(server.desktop, "key_down", lambda k: {"ok": True})
        monkeypatch.setattr(server.desktop, "key_up", lambda k: {"ok": True})
        monkeypatch.setattr(server.desktop, "mouse_down", lambda button=1, x=None, y=None: {"ok": True})
        monkeypatch.setattr(server.desktop, "mouse_up", lambda button=1, x=None, y=None: {"ok": True})
        server.key_hold("ctrl", press=True)
        server.mouse_button(button=1, press=True)
        assert handback.held() == {"keys": ["ctrl"], "buttons": [1]}
        server.key_hold("ctrl", press=False)
        server.mouse_button(button=1, press=False)
        assert handback.held() == {"keys": [], "buttons": []}

    def test_failed_launch_is_not_tracked(self):
        handback.note_launch({"ok": False, "error": "nope"})
        assert handback.launched() == []


class TestRelease:
    def test_tracked_holds_and_xtest_state_are_released_and_verified(self, desk, monkeypatch):
        ch = FakeChannel(held={"keys": [37], "buttons": [1]})
        monkeypatch.setattr(handback, "_channel", lambda: ch)
        handback.note_key("shift", True)
        handback.note_button(3, True)
        out = handback.release_all()
        assert ("key_up", "shift") in desk.calls and ("mouse_up", 3) in desk.calls
        assert out["xtest"]["released_keys"] == [37]
        assert out["verified"] is True
        assert out["core_keys_down"] == []
        assert handback.held() == {"keys": [], "buttons": []}

    def test_without_a_channel_it_releases_blind_and_says_so(self, desk, monkeypatch):
        monkeypatch.setattr(handback, "_channel", lambda: None)
        out = handback.release_all()
        assert out["verified"] is None
        assert ("key_up", "ctrl") in desk.calls and ("mouse_up", 1) in desk.calls


class TestCloseLaunched:
    def test_closes_only_our_windows_via_wm_delete(self, desk, monkeypatch):
        ch = FakeChannel()
        monkeypatch.setattr(handback, "_channel", lambda: ch)
        monkeypatch.setattr(handback.time, "sleep", lambda s: desk.windows.clear())
        handback.note_launch({"ok": True, "pid": 4242, "argv": ["xterm"], "window": {"id": "0x1"}},
                             before={"0x2"})
        out = handback.close_launched(wait_s=1.0)
        assert sorted(ch.closed) == [1, 3]
        assert {a["id"] for a in out["asked"]} == {"0x1", "0x3"}
        assert out["ok"] is True and out["still_open"] == []
        assert handback.launched() == []

    def test_a_pre_existing_window_of_the_same_app_survives(self, desk, monkeypatch):
        """Regression (2026-09-12): hand_back closed every window in the launch
        pid tree, so launch_app("google-chrome") then hand_back could close the
        owner's Chrome. A window that existed before the launch is never closed."""
        desk.windows[:] = [{"id": "0x7", "name": "Inbox - Google Chrome", "pid": "4300"}]
        before = handback.window_ids()
        desk.windows.append({"id": "0x8", "name": "New Tab - Google Chrome", "pid": "4242"})
        ch = FakeChannel()
        monkeypatch.setattr(handback, "_channel", lambda: ch)
        handback.note_launch({"ok": True, "pid": 4242, "argv": ["google-chrome"],
                              "window": {"id": "0x8"}}, before=before)
        monkeypatch.setattr(handback.time, "sleep",
                            lambda s: desk.windows.__setitem__(slice(None), desk.windows[:1]))
        out = handback.close_launched(wait_s=1.0)
        assert ch.closed == [8] and out["ok"] is True
        assert [w["id"] for w in desk.windows] == ["0x7"]

    def test_a_window_opened_after_the_launch_is_never_ours(self, desk, monkeypatch):
        # The owner opens a window in the app the agent started: same pid tree, not ours.
        ch = FakeChannel()
        monkeypatch.setattr(handback, "_channel", lambda: ch)
        monkeypatch.setattr(handback.time, "sleep", lambda s: None)
        handback.note_launch({"ok": True, "pid": 4242, "argv": ["xterm"], "window": {"id": "0x1"}},
                             before={"0x2", "0x3"})
        desk.windows.append({"id": "0x9", "name": "owner's second window", "pid": "4242"})
        handback.close_launched(wait_s=0.0)
        assert ch.closed == [1]

    def test_without_a_before_snapshot_only_the_reported_window_is_ours(self, desk, monkeypatch):
        ch = FakeChannel()
        monkeypatch.setattr(handback, "_channel", lambda: ch)
        monkeypatch.setattr(handback.time, "sleep", lambda s: None)
        handback.note_launch({"ok": True, "pid": 4242, "argv": ["xterm"], "window": {"id": "0x1"}})
        handback.close_launched(wait_s=0.0)
        assert ch.closed == [1]

    def test_reports_a_window_that_stays_open(self, desk, monkeypatch):
        ch = FakeChannel()
        monkeypatch.setattr(handback, "_channel", lambda: ch)
        monkeypatch.setattr(handback.time, "sleep", lambda s: None)
        handback.note_launch({"ok": True, "pid": 4242, "argv": ["xterm"], "window": None},
                             before={"0x2"})
        out = handback.close_launched(wait_s=0.0)
        assert out["ok"] is False
        assert {w["id"] for w in out["still_open"]} == {"0x1", "0x3"}
        assert handback.launched()  # still remembered for the next hand_back

    def test_nothing_launched_touches_nothing(self, desk, monkeypatch):
        ch = FakeChannel()
        monkeypatch.setattr(handback, "_channel", lambda: ch)
        out = handback.close_launched()
        assert out["asked"] == [] and ch.closed == []


class TestPark:
    def test_parks_where_the_owner_left_it(self, desk, monkeypatch):
        monkeypatch.setattr(handback, "note_pointer_before", handback.note_pointer_before.__wrapped__
                            if hasattr(handback.note_pointer_before, "__wrapped__") else
                            handback.note_pointer_before)
        handback._POINTER_BEFORE["at"] = (700, 400)
        out = handback.park_pointer()
        assert out["parked"] is True and out["to"] == [700, 400]
        assert ("move", 700, 400) in desk.calls

    def test_does_not_move_while_the_owner_is_active(self, desk):
        owner_pause.mark_human_input()
        handback._POINTER_BEFORE["at"] = (700, 400)
        out = handback.park_pointer()
        assert out["parked"] is False and out["reason"] == "owner_active"
        assert not any(c[0] == "move" for c in desk.calls)

    def test_parks_out_of_the_way_when_nothing_was_noted(self, desk):
        out = handback.park_pointer()
        assert out["parked"] is True and out["reason"] == "out_of_the_way"
        assert out["to"] == [1920 - handback.PARK_MARGIN_PX, 540]

    def test_uses_a_human_path_when_human_mode_is_on(self, desk, monkeypatch):
        human.set_mode(True, seed=1)
        moved = []
        monkeypatch.setattr(handback.human, "human_move", lambda x, y: moved.append((x, y)) or {"ok": True})
        handback._POINTER_BEFORE["at"] = (10, 10)
        assert handback.park_pointer()["parked"] is True
        assert moved == [(10, 10)]
        human.reset()

    def test_does_not_park_while_a_remote_viewer_has_focus(self, desk, monkeypatch):
        # A focused viewer forwards pointer motion to the machine it shows.
        desk.viewer = True
        ch = FakeChannel()
        monkeypatch.setattr(handback, "_channel", lambda: ch)
        handback._POINTER_BEFORE["at"] = (700, 400)
        out = handback.hand_back()
        assert out["pointer"] == {"ok": True, "parked": False, "reason": "remote_viewer_focused"}
        assert not any(c[0] == "move" for c in desk.calls)
        assert out["checks"]["pointer_parked"] is True and out["clean"] is True


class TestPersistedLaunches:
    """F5: a later process's hand_back closes only what the first one launched."""

    def test_a_second_process_closes_windows_from_the_state_file(self, desk, monkeypatch):
        ch = FakeChannel()
        monkeypatch.setattr(handback, "_channel", lambda: ch)
        monkeypatch.setattr(handback.time, "sleep", lambda s: desk.windows.clear())
        handback.note_launch({"ok": True, "pid": 4242, "argv": ["zenity"],
                              "window": {"id": "0x1"}}, before={"0x2"})
        # New process: empty memory, same state file (WORKMAN_STATE_DIR).
        handback._LAUNCHED.clear()
        assert handback.launched()[0]["windows"] == ["0x1", "0x3"]
        out = handback.close_launched(wait_s=1.0)
        assert sorted(ch.closed) == [1, 3]
        assert out["ok"] is True and handback.launched() == []

    def test_stale_pid_with_a_new_starttime_is_dropped(self, desk, monkeypatch):
        ch = FakeChannel()
        monkeypatch.setattr(handback, "_channel", lambda: ch)
        monkeypatch.setattr(handback, "_pid_starttime", lambda pid: 99)
        handback.note_launch({"ok": True, "pid": 4242, "argv": ["xterm"],
                              "window": {"id": "0x1"}}, before={"0x2"})
        assert handback.launched()[0]["starttime"] == 99
        monkeypatch.setattr(handback, "_pid_starttime", lambda pid: 100)
        assert handback.launched() == []
        out = handback.close_launched(wait_s=0.0)
        assert ch.closed == [] and out["asked"] == []

    def test_unreadable_state_is_unknown_and_not_clean(self, desk, monkeypatch, tmp_path):
        path = tmp_path / "wm-state" / handback._LAUNCHED_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        ch = FakeChannel()
        monkeypatch.setattr(handback, "_channel", lambda: ch)
        out = handback.hand_back()
        assert out["launched"] == "unknown"
        assert out["clean"] is False
        assert ch.closed == []
        assert desk.windows  # owner's windows untouched


class TestHandBack:
    def test_clean_when_everything_checks_out(self, desk, monkeypatch):
        ch = FakeChannel(held={"keys": [50], "buttons": []})
        monkeypatch.setattr(handback, "_channel", lambda: ch)
        monkeypatch.setattr(handback.time, "sleep", lambda s: desk.windows.clear())
        handback.note_launch({"ok": True, "pid": 4242, "argv": ["xterm"], "window": {"id": "0x1"}})
        handback._POINTER_BEFORE["at"] = (700, 400)
        out = server.hand_back()
        assert out["clean"] is True and out["ok"] is True
        assert out["checks"] == {"nothing_held_by_agent": True, "our_windows_closed": True,
                                 "pointer_parked": True, "screen_on": True, "no_grab": True}
        assert out["released"]["xtest"]["released_keys"] == [50]
        assert out["screen"]["on"] is True and ch.woke == 1
        assert out["verify"]["active_window"]["name"] == "owner's editor"

    def test_a_failing_platform_hook_is_reported_not_raised(self, monkeypatch):
        mod = SimpleNamespace(release_all=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(handback, "_backend_attr", lambda name: getattr(mod, name, None))
        monkeypatch.setattr(handback, "_channel", lambda: None)
        out = handback.release_all()
        assert out["verified"] is False and out["ok"] is False
        assert out["errors"] == [{"platform_release": "boom"}]

    def test_not_clean_when_a_window_stays(self, desk, monkeypatch):
        ch = FakeChannel()
        monkeypatch.setattr(handback, "_channel", lambda: ch)
        monkeypatch.setattr(handback.time, "sleep", lambda s: None)
        handback.note_launch({"ok": True, "pid": 4242, "argv": ["xterm"], "window": None},
                             before={"0x2"})
        out = handback.hand_back()
        assert out["clean"] is False
        assert out["checks"]["our_windows_closed"] is False
        assert out["verify"]["our_windows_open"]


class FakeQuartz:
    """Records Quartz posts; event-type constants resolve to their own names."""
    kCGWindowListOptionOnScreenOnly = 1
    kCGWindowListExcludeDesktopElements = 16
    kCGNullWindowID = 0

    def __init__(self):
        self.posted: list[dict] = []

    def __getattr__(self, name):
        if name.startswith("kCG"):
            return name
        raise AttributeError(name)

    def CGPointMake(self, x, y):
        return SimpleNamespace(x=x, y=y)

    def CGEventCreateMouseEvent(self, src, kind, point, button):
        return {"kind": kind, "button": button}

    def CGEventCreateKeyboardEvent(self, src, code, down):
        return {"kind": "key", "code": code, "down": bool(down)}

    def CGEventSetFlags(self, ev, flags):
        ev["flags"] = flags

    def CGEventSetIntegerValueField(self, ev, field, value):
        ev[field] = value

    def CGEventPost(self, tap, ev):
        self.posted.append(ev)

    def CGEventCreate(self, src):
        return None

    def CGEventGetLocation(self, ev):
        return SimpleNamespace(x=700.0, y=400.0)

    def CGMainDisplayID(self):
        return 1

    def CGDisplayBounds(self, did):
        return SimpleNamespace(origin=SimpleNamespace(x=0, y=0),
                               size=SimpleNamespace(width=1440, height=900))

    def CGWindowListCopyWindowInfo(self, opts, wid):
        return []


class TestMacHandBack:
    """H2: hand_back on the darwin backend, through fakes (nothing runs on a Mac)."""

    @pytest.fixture
    def mac(self, monkeypatch):
        from workman.platform import darwin
        q = FakeQuartz()
        monkeypatch.setattr(darwin, "_quartz_mod", q)
        monkeypatch.setattr(darwin, "_quartz_tried", True)
        monkeypatch.setattr(darwin, "_run", lambda cmd, timeout=20, **kw:
                            subprocess.CompletedProcess(cmd, 127, "", "not macOS"))
        monkeypatch.setattr(handback.desktop, "active", lambda: darwin)
        monkeypatch.setattr(handback, "_channel", lambda: None)
        human.reset()
        return q

    def test_releases_only_tracked_holds_and_reports_unverified(self, mac):
        handback.note_key("shift", True)
        handback.note_button(1, True)
        out = handback.hand_back()
        rel = out["released"]
        assert rel["ok"] is True and rel["verified"] is None
        assert rel["released"] == {"keys": ["shift"], "buttons": [1]}
        assert rel["platform_release"]["released_blind"] is False
        # Shift only: a blind ctrl/alt/cmd up could cancel a key the owner holds.
        assert [e["code"] for e in mac.posted if e["kind"] == "key" and not e["down"]] == [56]
        kinds = [e["kind"] for e in mac.posted]
        assert kinds.count("kCGEventLeftMouseUp") == 1
        assert "kCGEventRightMouseUp" not in kinds and "kCGEventOtherMouseUp" not in kinds
        assert out["screen"]["checked"] is False
        assert out["clean"] is True and handback.held() == {"keys": [], "buttons": []}

    def test_darwin_hook_contract(self):
        from workman.platform import darwin
        res = darwin.release_all()
        assert res["ok"] is True and res["verified"] is None and res["released_blind"] is False
