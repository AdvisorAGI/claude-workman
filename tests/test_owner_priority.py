"""The owner's mouse and keyboard outrank the agent's.

Presence is a stamp file the listener touches; every input action reads
its age. While he is active the action waits briefly and then refuses with
`human_active`; a release is never held back; the Escape switch refuses
outright; nothing polls when he is not there.
"""
from __future__ import annotations

import os
import time

import pytest

from workman import desktop, owner_pause


def _age_stamp(name: str, seconds: float) -> None:
    p = owner_pause._stamp_path(name)
    t = time.time() - seconds
    os.utime(p, (t, t))


class TestPresence:
    def test_no_stamp_means_nobody_there(self):
        assert owner_pause.human_input_age() is None
        assert owner_pause.human_active() is False

    def test_fresh_human_stamp_means_active(self):
        owner_pause.mark_human_input()
        assert owner_pause.human_active() is True
        age = owner_pause.human_input_age()
        assert age is not None and age < 0.5

    def test_old_stamp_means_quiet(self):
        owner_pause.mark_human_input()
        _age_stamp(owner_pause.HUMAN_STAMP, owner_pause.HUMAN_QUIET_S + 1)
        assert owner_pause.human_active() is False

    def test_marks_are_throttled(self):
        owner_pause.mark_human_input()
        first = os.stat(owner_pause._stamp_path(owner_pause.HUMAN_STAMP)).st_mtime_ns
        owner_pause.mark_human_input()
        assert os.stat(owner_pause._stamp_path(owner_pause.HUMAN_STAMP)).st_mtime_ns == first

    def test_activity_stamp_never_claims_an_event(self):
        owner_pause.mark_agent_input(escape=True)
        assert owner_pause.agent_claims() is False
        assert owner_pause.agent_claims({"kind": "key_press", "detail": 9, "time": 20}) is False

    def test_exact_claim_is_single_use_and_display_scoped(self):
        event = {"kind": "key_press", "detail": 38, "time": 1001}
        token = owner_pause.claim_agent_event(":0", "key_press", 38, 1000)
        owner_pause.finish_agent_event(":0", token, 1002)
        assert not owner_pause.agent_claims(event, display=":99")
        assert not owner_pause.agent_claims(dict(event, detail=56))
        assert not owner_pause.agent_claims(dict(event, kind="key_release"))
        assert not owner_pause.agent_claims(dict(event, time=2000))
        assert owner_pause.agent_claims(event)
        assert not owner_pause.agent_claims(event)

    def test_server_time_wrap_and_failed_request_removal(self):
        token = owner_pause.claim_agent_event(":0", "button_release", 1, 0xfffffffe)
        owner_pause.finish_agent_event(":0", token, 2)
        assert owner_pause.agent_claims({"kind": "button_release", "detail": 1, "time": 1})
        token = owner_pause.claim_agent_event(":0", "key_press", 38, 1)
        owner_pause.finish_agent_event(":0", token, None)
        assert not owner_pause.agent_claims({"kind": "key_press", "detail": 38, "time": 1})


class TestWaitForQuiet:
    def test_returns_immediately_when_nobody_is_there(self):
        t0 = time.monotonic()
        assert owner_pause.wait_for_human_quiet(max_wait=1.0) is False
        assert time.monotonic() - t0 < 0.05

    def test_gives_up_after_max_wait_while_active(self, monkeypatch):
        owner_pause.mark_human_input()
        t0 = time.monotonic()
        assert owner_pause.wait_for_human_quiet(max_wait=0.08) is True
        assert 0.07 <= time.monotonic() - t0 < 0.5

    def test_returns_false_once_the_stamp_ages_out(self, monkeypatch):
        owner_pause.mark_human_input()
        _age_stamp(owner_pause.HUMAN_STAMP, owner_pause.HUMAN_QUIET_S - 0.03)
        assert owner_pause.wait_for_human_quiet(max_wait=0.5) is False


class TestSafeFacade:
    @pytest.fixture
    def quick(self, monkeypatch):
        monkeypatch.setattr(owner_pause, "HUMAN_YIELD_S", 0.05)
        moved = []
        backend = desktop.active()
        monkeypatch.setattr(backend, "move", lambda x, y: moved.append((x, y)) or {"ok": True})
        monkeypatch.setattr(backend, "mouse_up",
                            lambda button=1, x=None, y=None: moved.append(("up", button)) or {"ok": True})
        monkeypatch.setattr(backend, "foreign_pointer_motion", lambda: False)
        return moved

    def test_input_refused_while_owner_is_active(self, quick):
        owner_pause.mark_human_input()
        out = desktop.move(5, 5)
        assert out["ok"] is False and out["error"] == "human_active"
        assert "instruction" in out and out["human_input_age_s"] is not None
        assert quick == []

    def test_input_runs_when_quiet(self, quick):
        assert desktop.move(5, 5) == {"ok": True}
        assert quick == [(5, 5)]

    def test_foreign_pointer_motion_counts_as_the_owner(self, quick, monkeypatch):
        monkeypatch.setattr(desktop.active(), "foreign_pointer_motion", lambda: True)
        out = desktop.move(5, 5)
        assert out["ok"] is False and out["error"] == "human_active"
        assert owner_pause.human_active() is True
        assert quick == []

    def test_escape_switch_refuses_outright(self, quick):
        owner_pause.pause_from_escape()
        out = desktop.move(5, 5)
        assert out["ok"] is False and out["error"] == "input_disabled"
        assert out["input_switches"] == {"mouse": False, "keyboard": False}
        assert quick == []

    def test_releases_are_never_held_back(self, quick):
        owner_pause.pause_from_escape()
        owner_pause.mark_human_input()
        assert "mouse_up" not in owner_pause.INPUT_ACTIONS
        assert "key_up" not in owner_pause.INPUT_ACTIONS
        assert desktop.mouse_up(button=1) == {"ok": True}
        assert quick == [("up", 1)]

    def test_status_reports_everything(self):
        owner_pause.pause_from_escape()
        s = owner_pause.status()
        assert s["input_switches"] == {"mouse": False, "keyboard": False}
        assert s["paused_by"] == "escape"
        assert s["quiet_s"] == owner_pause.HUMAN_QUIET_S
        assert owner_pause.resume()["mouse"] is True
