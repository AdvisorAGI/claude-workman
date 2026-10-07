"""Polite input with fake X libraries only; never opens a real display."""
from __future__ import annotations

import os
import random
import sys
from types import SimpleNamespace

import pytest

import workman
from workman import desktop, handback, human, owner_pause, polite, x11, xtest
from workman.platform import linux_x11
from test_xtest import make_channel


@pytest.fixture
def seat(monkeypatch):
    monkeypatch.setenv("WORKMAN_POLITE", "1")
    monkeypatch.setattr(x11, "DISPLAY", ":99")
    monkeypatch.setattr(desktop, "active", lambda: linux_x11)
    monkeypatch.setattr(desktop, "_focused_window", lambda: None)
    monkeypatch.setattr(desktop, "_window_at_point", lambda x, y: None)
    monkeypatch.setattr(polite.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(polite, "_PENDING", {})
    monkeypatch.setattr(x11, "_INPUT_HELD", {"keys": set(), "buttons": set()})
    ch = make_channel()
    ch.display_name = ":99"
    state = SimpleNamespace(ch=ch, pointer=[10, 20], focus=100, restores=[], ghosts=[],
                            physical_keys=set(), physical_buttons=set(), on_event=lambda e: None)
    original = ch.xtst.XTestFakeMotionEvent

    def motion(dpy, screen, x, y, timestamp):
        original(dpy, screen, x, y, timestamp)
        state.pointer[:] = [x, y]
        state.on_event(("move", x, y))

    original_key = ch.xtst.XTestFakeKeyEvent

    def key(dpy, code, down, timestamp):
        original_key(dpy, code, down, timestamp)
        state.on_event(("key", code, bool(down)))

    original_button = ch.xtst.XTestFakeButtonEvent

    def button(dpy, code, down, timestamp):
        original_button(dpy, code, down, timestamp)
        state.on_event(("button", code, bool(down)))

    ch.xtst.XTestFakeMotionEvent = motion
    ch.xtst.XTestFakeKeyEvent = key
    ch.xtst.XTestFakeButtonEvent = button
    ch.pointer_position = lambda: {"x": state.pointer[0], "y": state.pointer[1], "window": 100,
                                  "buttons": list(state.physical_buttons | getattr(ch, "_agent_buttons", set()))}
    ch.core_keys_down = lambda: list(state.physical_keys)
    ch.active_window_info = lambda: {"ok": True, "class": "local"}
    ch.input_focus = lambda: {"ok": True, "window": state.focus, "revert_to": 1}

    def restore(focus):
        state.restores.append(focus["window"])
        state.focus = focus["window"]
        return {"ok": True, "readback": ch.input_focus()}

    ch.restore_input_focus = restore
    monkeypatch.setattr(x11, "_xt", lambda: ch)
    ghost = SimpleNamespace(notify=lambda display, x, y: state.ghosts.append((display, x, y)),
                            hide=lambda display: state.ghosts.append((display, "hide")))
    monkeypatch.setitem(sys.modules, "workman.ghost_cursor", ghost)
    monkeypatch.setattr(workman, "ghost_cursor", ghost, raising=False)
    return state


def touch_owner():
    p = owner_pause._stamp_path(owner_pause.HUMAN_STAMP)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


def test_default_private_display_ignores_owner_files(seat, monkeypatch):
    monkeypatch.delenv("WORKMAN_POLITE", raising=False)
    owner_pause.pause_from_escape()
    touch_owner()
    monkeypatch.setattr(owner_pause, "blocked", lambda name: pytest.fail("private display read shared switches"))
    monkeypatch.setattr(owner_pause, "human_active", lambda *a: pytest.fail("private display read owner stamp"))
    assert desktop.move(40, 50)["ok"]
    assert seat.pointer == [40, 50]
    assert seat.ghosts == []


def test_mode_defaults_and_non_x11(monkeypatch):
    monkeypatch.delenv("WORKMAN_POLITE", raising=False)
    linux = SimpleNamespace(PLATFORM="Linux/X11", display_name=lambda: ":0.0")
    assert polite.enabled(linux)
    monkeypatch.setenv("WORKMAN_POLITE", "0")
    assert not polite.enabled(linux) and polite.gate_enabled(linux)
    monkeypatch.setenv("WORKMAN_POLITE", "1")
    assert not polite.enabled(SimpleNamespace(PLATFORM="macOS"))


def test_single_start_wait_and_restore_for_nested_human_click(seat, monkeypatch):
    waits = []
    monkeypatch.setattr(owner_pause, "wait_for_human_quiet", lambda **kwargs: waits.append(kwargs) or False)
    monkeypatch.setattr(human, "eased_path", lambda *a, **k: [(10, 20, 0), (30, 40, 10), (50, 60, 20)])
    result = human.human_click(50, 60, random.Random(2))
    assert result["ok"]
    assert waits == [{"max_wait": 20.0, "quiet_s": 2.0}]
    assert seat.pointer == [10, 20]
    assert seat.ch.xtst.events == [("move", 10, 20), ("move", 30, 40), ("move", 50, 60),
                                   ("button", 1, True), ("button", 1, False), ("move", 10, 20)]
    assert seat.restores == []
    assert result["polite"]["pointer_verified"]
    assert all(g[0] == ":99" for g in seat.ghosts)


def test_quiet_settings_and_no_injection_on_start_refusal(seat, monkeypatch):
    monkeypatch.setenv("WORKMAN_POLITE_QUIET_S", "3.5")
    monkeypatch.setenv("WORKMAN_POLITE_MAX_WAIT_S", "0.2")
    waits = []
    monkeypatch.setattr(owner_pause, "wait_for_human_quiet", lambda **kw: waits.append(kw) or True)
    out = desktop.click(100, 200)
    assert out["error"] == "human_active"
    assert waits == [{"max_wait": .2, "quiet_s": 3.5}]
    assert seat.ch.xtst.events == []


def test_typing_interrupt_during_dwell_reports_exact_progress(seat, monkeypatch):
    def event(e):
        if e == ("key", 56, True):
            touch_owner()
    seat.on_event = event
    out = desktop.type_text("abhi", delay_ms=0, dwell_ms=100)
    assert out["error"] == "human_active"
    assert out["typed_len"] == 2 and out["total_len"] == 4 and out["remaining"] == "hi"
    assert seat.ch.xtst.events == [("key", 38, True), ("key", 38, False),
                                  ("key", 56, True), ("key", 56, False)]
    assert not seat.ch.owned_holds()
    assert seat.ghosts[-1] == (":99", "hide")


def test_human_type_interrupt_after_key_reports_sent_character(seat):
    seat.on_event = lambda e: touch_owner() if e == ("key", 38, True) else None
    out = human.human_type("abhi", random.Random(4))
    assert out["error"] == "human_active"
    assert out["typed_len"] == 1 and out["remaining"] == "bhi"
    assert not seat.ch.owned_holds()


def test_foreign_pointer_interrupt_without_stamp_stops_drag_and_releases_only_agent(seat, monkeypatch):
    def event(e):
        if e == ("button", 1, True):
            seat.pointer[:] = [700, 800]
            seat.physical_keys.add(37)
            seat.physical_buttons.add(3)
    seat.on_event = event
    monkeypatch.setattr(seat.ch, "release_xtest_held", lambda: pytest.fail("broad XTEST release"))
    out = desktop.drag(30, 40, 100, 200)
    assert out["error"] == "human_active"
    assert out["at"] == [30, 40] and out["dragged"] is False
    assert seat.pointer == [700, 800]
    assert seat.ch.xtst.events == [("move", 30, 40), ("button", 1, True), ("button", 1, False)]
    assert seat.physical_keys == {37} and seat.physical_buttons == {3}


def test_scroll_interrupt_counts_wheel_events(seat):
    seat.on_event = lambda e: touch_owner() if e == ("button", 5, True) else None
    out = desktop.scroll("down", amount=9)
    assert out["error"] == "human_active" and out["amount"] == 1 and out["remaining"] == 8
    assert seat.ch.xtst.events == [("button", 5, True), ("button", 5, False)]


def test_interruptible_key_hold_releases_combo_without_next_input(seat):
    seat.on_event = lambda e: touch_owner() if e == ("key", 28, True) else None
    out = desktop.press_key("ctrl+t", hold_ms=5000)
    assert out["error"] == "human_active" and out["pressed"] is True
    assert seat.ch.xtst.events == [("key", 37, True), ("key", 28, True), ("key", 28, False), ("key", 37, False)]
    assert not seat.ch.owned_holds()


def test_never_claim_or_release_foreign_holds(seat):
    seat.physical_keys.add(37)
    seat.physical_buttons.add(1)
    assert desktop.key_down("ctrl")["error"] == "human_active"
    assert desktop.mouse_down(1)["error"] == "human_active"
    assert desktop.key_up("ctrl")["ok"]
    assert desktop.mouse_up(1, x=700, y=800)["ok"]
    assert seat.ch.xtst.events == []
    assert seat.pointer == [10, 20]


def test_standalone_hold_defers_pointer_restore_until_release(seat):
    out = desktop.mouse_down(1, x=30, y=40)
    assert out["polite"]["restore"] == "deferred_until_release"
    assert seat.pointer == [30, 40]
    out = desktop.mouse_up(1)
    assert out["ok"] and seat.pointer == [10, 20]
    assert out["polite"]["pointer_verified"]


def test_standalone_release_preserves_owner_pointer_and_never_waits(seat, monkeypatch):
    assert desktop.mouse_down(1, x=30, y=40)["ok"]
    seat.pointer[:] = [400, 500]
    touch_owner()
    monkeypatch.setattr(owner_pause, "wait_for_human_quiet", lambda **kw: pytest.fail("release waited"))
    assert desktop.mouse_up(1, x=800, y=900)["ok"]
    assert seat.pointer == [400, 500]
    assert not seat.ch.owned_holds()


def test_focus_restore_only_after_action_changes_focus(seat):
    seat.on_event = lambda e: setattr(seat, "focus", 200) if e == ("button", 1, True) else None
    out = desktop.click(30, 40)
    assert out["ok"] and seat.restores == [100]
    assert out["polite"]["focus"]["readback"]["window"] == 100


def test_focus_failure_reports_readback(seat):
    seat.on_event = lambda e: setattr(seat, "focus", 200) if e == ("button", 1, True) else None
    seat.ch.restore_input_focus = lambda focus: {"ok": False, "readback": {"window": 200}}
    out = desktop.click(30, 40)
    assert out["polite"]["focus"]["ok"] is False
    assert out["polite"]["focus"]["readback"]["window"] == 200


def test_polite_focus_never_minimizes_or_raises(seat, monkeypatch):
    monkeypatch.setattr(x11, "_require", lambda name: None)
    monkeypatch.setattr(x11, "list_windows", lambda: [{"id": "200", "name": "target"}])
    monkeypatch.setattr(x11, "_run", lambda *a, **kw: pytest.fail("window subprocess mutation"))
    out = desktop.focus_window("target")
    assert out["ok"] and seat.restores == [200, 100]


def test_backend_error_cleans_only_owned_holds_and_restores(seat, monkeypatch):
    def failure(*a):
        seat.ch.button(1, True)
        raise RuntimeError("mock failure")
    monkeypatch.setattr(linux_x11, "move", failure)
    out = desktop.move(30, 40)
    assert out["error"] == "backend_error"
    assert seat.ch.xtst.events == [("button", 1, True), ("button", 1, False)]
    assert not seat.ch.owned_holds()


def test_new_stamp_interrupts_even_if_stamp_is_already_old(seat):
    def event(e):
        if e == ("key", 38, True):
            touch_owner()
            p = owner_pause._stamp_path(owner_pause.HUMAN_STAMP)
            os.utime(p, (1, 1))
    seat.on_event = event
    out = desktop.type_text("ab", delay_ms=1)
    assert out["error"] == "human_active" and out["typed_len"] == 1


def test_no_ghost_module_is_best_effort(seat, monkeypatch):
    monkeypatch.setitem(sys.modules, "workman.ghost_cursor", None)
    monkeypatch.delattr(workman, "ghost_cursor")
    assert desktop.move(30, 40)["ok"]
    assert seat.pointer == [10, 20]


def test_owner_arrives_between_typing_calls_does_not_overcount(seat, monkeypatch):
    original = linux_x11.type_text
    calls = []

    def typing(*args, **kwargs):
        out = original(*args, **kwargs)
        calls.append(args[0])
        return out

    monkeypatch.setattr(linux_x11, "type_text", typing)
    # Stamp appears on the second call's desktop gate, after one successful character.
    original_guard = polite.guard
    gates = []

    def guard(action=None):
        if action == "type_text" and polite.current() and polite.current().progress.get("typed_len") == 1:
            gates.append(action)
            if len(gates) == 1:
                touch_owner()
        return original_guard(action)

    monkeypatch.setattr(polite, "guard", guard)
    out = human.human_type("abhi", random.Random(3))
    assert out["error"] == "human_active" and out["typed_len"] == 1 and out["remaining"] == "bhi"
    assert calls == ["a"]


def test_owner_arrives_before_second_character_press_does_not_overcount(seat, monkeypatch):
    original = seat.ch._fake_key
    def press(code, down):
        if code == 56 and down:
            touch_owner()
        return original(code, down)
    seat.ch._fake_key = press
    out = desktop.type_text("abhi", delay_ms=1, dwell_ms=1)
    assert out["error"] == "human_active"
    assert out["typed_len"] == 1 and out["remaining"] == "bhi"
    assert seat.ch.xtst.events == [("key", 38, True), ("key", 38, False)]


def test_human_typo_interruption_reports_stray_character(seat, monkeypatch):
    monkeypatch.setattr(human, "TYPO_CHANCE", 1)
    monkeypatch.setattr(human, "_typo_char", lambda ch, rng: "b")
    seat.on_event = lambda e: touch_owner() if e == ("key", 56, True) else None
    out = human.human_type("ahi", random.Random(3), typos=True)
    assert out["error"] == "human_active" and out["typed_len"] == 0
    assert out["remaining"] == "ahi" and out["uncorrected_typo"] is True


def test_human_click_interrupt_in_hold_reports_sent_click(seat, monkeypatch):
    monkeypatch.setattr(human, "eased_path", lambda *a, **kw: [(30, 40, 0)])
    seat.on_event = lambda e: touch_owner() if e == ("button", 1, True) else None
    out = human.human_click(30, 40, random.Random(2), count=3)
    assert out["error"] == "human_active" and out["clicked"] is True
    assert out["clicks_done"] == 1 and out["remaining_clicks"] == 2
    assert seat.ch.xtst.events == [("move", 30, 40), ("button", 1, True), ("button", 1, False)]


def test_modifier_click_interrupt_releases_acquired_modifiers(seat):
    seat.on_event = lambda e: touch_owner() if e == ("key", 50, True) else None
    out = desktop.click_with(30, 40, modifiers=["ctrl", "shift"])
    assert out["error"] == "human_active"
    assert seat.ch.xtst.events == [("key", 37, True), ("key", 50, True), ("key", 50, False), ("key", 37, False)]
    assert seat.pointer == [10, 20]


def test_release_failure_stays_owned_for_retry(seat, monkeypatch):
    original = seat.ch.xtst.XTestFakeButtonEvent
    assert desktop.mouse_down(1, x=30, y=40)["ok"]
    seat.ch.xtst.XTestFakeButtonEvent = lambda *args: 0
    out = desktop.mouse_up(1)
    assert out["error"] == "backend_error"
    assert seat.ch.owned_holds() and seat.pointer == [30, 40]
    seat.ch.xtst.XTestFakeButtonEvent = original
    assert desktop.mouse_up(1)["ok"]
    assert not seat.ch.owned_holds() and seat.pointer == [10, 20]


def test_handback_polite_never_uses_broad_release(seat, monkeypatch):
    assert desktop.key_down("ctrl")["ok"]
    monkeypatch.setattr(seat.ch, "release_xtest_held", lambda: pytest.fail("broad shared device release"))
    out = handback.release_all()
    assert out["ok"] and not seat.ch.owned_holds()
    assert seat.ch.xtst.events == [("key", 37, True), ("key", 37, False)]


def test_unknown_outer_action_checks_switch_during_hold(seat):
    seat.on_event = lambda e: owner_pause.pause_from_escape() if e == ("key", 28, True) else None
    out = polite.run("batch", lambda: desktop.press_key("ctrl+t", hold_ms=3000))
    assert out["error"] == "input_disabled"
    assert not seat.ch.owned_holds()


def test_focus_native_api_sets_without_raise_and_reports_readback(monkeypatch):
    ch = make_channel()
    calls = []
    def get_focus(dpy, window, revert):
        window.value = 123
        revert.value = 1
    ch.x11.XGetInputFocus = get_focus
    ch.x11.XSetInputFocus = lambda *args: calls.append(args)
    out = ch.restore_input_focus({"window": 456, "revert_to": 1})
    assert calls == [(1, 456, 1, xtest.CurrentTime)]
    assert out["ok"] is False and out["readback"]["window"] == 123


def test_human_scroll_interrupt_counts_first_wheel_event(seat, monkeypatch):
    monkeypatch.setattr(human, "scroll_plan", lambda amount, rng: [(3, 100), (6, 100)])
    seat.on_event = lambda e: touch_owner() if e == ("button", 5, True) else None
    out = human.human_scroll("down", amount=9, rng=random.Random(2))
    assert out["error"] == "human_active" and out["amount"] == 1 and out["remaining"] == 8
    assert seat.ch.xtst.events == [("button", 5, True), ("button", 5, False)]


def test_human_release_does_not_move_or_wait_for_aim(seat, monkeypatch):
    assert human.human_mouse_button(1, press=True, x=30, y=40, rng=random.Random(3))["ok"]
    seat.pointer[:] = [500, 600]
    touch_owner()
    monkeypatch.setattr(human, "human_move", lambda *a, **kw: pytest.fail("release moved pointer"))
    assert human.human_mouse_button(1, press=False, x=800, y=900)["ok"]
    assert seat.pointer == [500, 600] and not seat.ch.owned_holds()


def test_human_hover_interrupts_settle_without_restore(seat, monkeypatch):
    monkeypatch.setattr(human, "eased_path", lambda *a, **kw: [(30, 40, 0)])
    seat.on_event = lambda e: touch_owner() if e == ("move", 30, 40) else None
    out = human.human_hover(30, 40, settle_ms=5000)
    assert out["error"] == "human_active" and out["at"] == [30, 40]
    assert seat.ch.xtst.events == [("move", 30, 40)]


def test_private_display_does_not_write_shared_agent_stamp(seat, monkeypatch):
    monkeypatch.delenv("WORKMAN_POLITE", raising=False)
    monkeypatch.setattr(owner_pause, "mark_agent_input", lambda *a, **kw: pytest.fail("private display wrote shared stamp"))
    assert desktop.move(30, 40)["ok"]


def test_foreign_modifier_prevents_accidental_shortcut_or_motion(seat):
    seat.physical_keys.add(37)
    assert desktop.type_text("a")["error"] == "human_active"
    assert desktop.move(30, 40)["error"] == "human_active"
    assert seat.ch.xtst.events == []


def test_untracked_release_survives_broken_pointer_and_focus_reads(seat, monkeypatch):
    monkeypatch.setattr(seat.ch, "pointer_position", lambda: (_ for _ in ()).throw(RuntimeError("read failed")))
    monkeypatch.setattr(seat.ch, "input_focus", lambda: (_ for _ in ()).throw(RuntimeError("read failed")))
    assert desktop.mouse_up(1)["ok"]
    assert desktop.key_up("ctrl")["ok"]
    assert seat.ch.xtst.events == []


def test_typing_backend_failure_keeps_completed_prefix(seat):
    original = seat.ch.type_text
    def typing(text, **kwargs):
        if text == "b":
            raise RuntimeError("mocked failure before press")
        return original(text, **kwargs)
    seat.ch.type_text = typing
    out = desktop.type_text("abhi", delay_ms=0)
    assert out["error"] == "backend_error" and out["typed_len"] == 1
    assert out["total_len"] == 4 and out["remaining"] == "bhi"
    assert seat.ch.xtst.events == [("key", 38, True), ("key", 38, False)]


@pytest.mark.parametrize("action", ["click", "drag", "scroll_at", "hover"])
def test_failed_move_prevents_following_button_side_effect(seat, monkeypatch, action):
    monkeypatch.setattr(x11, "move", lambda *args: {"ok": False, "error": "move_failed"})
    args = {"click": (30, 40), "drag": (30, 40, 50, 60), "scroll_at": (30, 40, "down"), "hover": (30, 40)}
    out = getattr(desktop, action)(*args[action])
    assert out["error"] == "move_failed"
    assert seat.ch.xtst.events == []


def test_missing_channel_refuses_polite_input_without_raw_injection(seat, monkeypatch):
    monkeypatch.setattr(x11, "_xt", lambda: None)
    monkeypatch.setattr(x11, "_run", lambda *a, **kw: pytest.fail("polite input tried raw fallback"))
    assert desktop.type_text("hello")["error"] == "owned_state_unavailable"
    assert desktop.click(30, 40)["error"] == "owned_state_unavailable"


def test_pointer_restore_keeps_remote_viewer_guard(seat, monkeypatch):
    def event(e):
        if e == ("move", 30, 40):
            monkeypatch.setattr(desktop, "_focused_window", lambda: {"class": "Remmina"})
            desktop._VIEWER_SEEN["at"] = -1
    seat.on_event = event
    out = desktop.move(30, 40)
    assert out["ok"] and seat.pointer == [30, 40]
    assert out["polite"]["pointer"]["error"] == "remote_viewer_focused"
    assert out["polite"]["pointer_verified"] is False
    assert seat.ch.xtst.events == [("move", 30, 40)]


def test_queued_wheel_release_is_acknowledged_before_next_press(seat, monkeypatch):
    # Emulate XTEST's request returning before its wheel-release event is seen.
    def event(e):
        if e == ("button", 5, False):
            seat.physical_buttons.add(5)
    seat.on_event = event
    monkeypatch.setattr(polite.time, "sleep", lambda seconds: seat.physical_buttons.discard(5))
    out = desktop.scroll("down", amount=3)
    assert out["ok"] and out["amount"] == 3
    assert seat.ch.xtst.events == [("button", 5, v) for _ in range(3) for v in (True, False)]


def test_delayed_click_focus_is_restored_without_raising(seat, monkeypatch):
    delayed = []
    seat.on_event = lambda e: delayed.append(200) if e == ("button", 1, True) else None
    def settle(seconds):
        if delayed:
            seat.focus = delayed.pop()
    monkeypatch.setattr(polite.time, "sleep", settle)
    out = desktop.click(30, 40)
    assert out["ok"] and seat.focus == 100 and seat.restores == [100]


def test_long_typing_refreshes_ghost_without_motion(seat, monkeypatch):
    now = [1.0]
    monkeypatch.setattr(polite.time, "monotonic", lambda: now[0])
    def wait(seconds):
        now[0] += seconds
    monkeypatch.setattr(polite.time, "sleep", wait)
    out = desktop.type_text("abhi", delay_ms=600)
    assert out["ok"]
    assert len([item for item in seat.ghosts if len(item) == 3]) >= 15


def test_status_reports_effective_polite_quiet_settings(seat):
    out = owner_pause.status()
    assert out["polite"] and out["quiet_s"] == 2.0 and out["yield_s"] == 20.0


def test_human_scroll_refusal_before_next_tick_does_not_duplicate_progress(seat, monkeypatch):
    monkeypatch.setattr(human, "scroll_plan", lambda *a: [(2, 0)])
    original = desktop.scroll
    count = [0]
    def scroll(*args, **kwargs):
        count[0] += 1
        if count[0] == 2:
            touch_owner()
        return original(*args, **kwargs)
    monkeypatch.setattr(desktop, "scroll", scroll)
    out = human.human_scroll("down", amount=2)
    assert out["error"] == "human_active" and out["amount"] == 1 and out["remaining"] == 1
