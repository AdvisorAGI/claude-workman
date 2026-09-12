"""Escape pauses, resume continues from where it stopped, never from the start."""
from __future__ import annotations

import random

import pytest

from workman import human, owner_pause, resume, server

REFUSED = {"ok": False, "error": "input_disabled", "action": "type_text"}


class TestMemory:
    def test_only_interruptions_are_remembered(self):
        resume.remember("type_text", {"text": "hi"}, {"ok": True})
        assert resume.last() is None
        out = {"ok": False, "error": "interrupted", "reason": REFUSED, "remaining": "i"}
        resume.remember("type_text", {"text": "hi"}, out)
        assert resume.last()["result"]["remaining"] == "i"

    def test_a_later_success_of_the_same_tool_clears_it(self):
        resume.remember("click", {"x": 1, "y": 2}, {"ok": False, "error": "human_active"})
        assert resume.last()
        resume.remember("click", {"x": 1, "y": 2}, {"ok": True})
        assert resume.last() is None

    def test_bot_challenge_refusal_is_resumable(self):
        resume.remember("click", {"x": 1, "y": 2}, {"ok": False, "error": "bot_challenge"})
        assert resume.plan() == [{"action": "click", "x": 1, "y": 2}]


class TestPlans:
    def test_typing_resumes_with_the_rest_and_fixes_a_stray_char(self):
        entry = {"tool": "type_text", "args": {"text": "hello world", "typos": True, "field": ""},
                 "result": {"ok": False, "error": "interrupted", "remaining": "lo world",
                            "uncorrected_typo": True}}
        assert resume.plan(entry) == [
            {"action": "press_key", "key": "BackSpace"},
            {"action": "type_text", "text": "lo world", "typos": True, "field": ""},
        ]

    def test_scroll_resumes_the_remaining_ticks(self):
        entry = {"tool": "scroll", "args": {"direction": "down", "amount": 7, "x": 10, "y": 20},
                 "result": {"ok": False, "error": "interrupted", "remaining": 4}}
        assert resume.plan(entry) == [{"action": "scroll", "direction": "down", "amount": 4,
                                       "x": 10, "y": 20}]

    def test_drag_resumes_from_where_it_dropped(self):
        entry = {"tool": "drag", "args": {"from_x": 0, "from_y": 0, "to_x": 300, "to_y": 100},
                 "result": {"ok": False, "error": "interrupted", "at": [120, 40],
                            "to": [300, 100], "released": False}}
        assert resume.plan(entry) == [
            {"action": "mouse_button", "button": 1, "press": False},
            {"action": "drag", "from_x": 120, "from_y": 40, "to_x": 300, "to_y": 100},
        ]

    def test_click_resumes_at_its_target(self):
        entry = {"tool": "click", "args": {"x": 50, "y": 60, "button": 1, "count": 1, "modifiers": None},
                 "result": {"ok": False, "error": "interrupted", "at": [20, 20], "target": [50, 60]}}
        assert resume.plan(entry) == [{"action": "click", "x": 50, "y": 60, "button": 1,
                                       "count": 1, "modifiers": None}]

    def test_nothing_to_resume(self):
        assert resume.plan(None) == []


class TestServerTools:
    @pytest.fixture
    def typed(self, monkeypatch):
        seen = []
        monkeypatch.setattr(server.desktop, "type_text",
                            lambda text, delay_ms=40, dwell_ms=None: seen.append(text) or {"ok": True})
        monkeypatch.setattr(server.desktop, "press_key", lambda k: seen.append(f"<{k}>") or {"ok": True})
        return seen

    def test_interrupted_typing_is_remembered_and_resumed(self, monkeypatch, typed):
        human.set_mode(True, seed=3)
        monkeypatch.setattr(human.time, "sleep", lambda s: None)
        calls = {"n": 0}

        def type_text(text, delay_ms=40, dwell_ms=None):
            calls["n"] += 1
            if calls["n"] == 3:
                return dict(REFUSED)
            typed.append(text)
            return {"ok": True}
        monkeypatch.setattr(server.desktop, "type_text", type_text)
        out = server.type_text("hello")
        assert out["ok"] is False and out["remaining"] == "llo"
        assert resume.last()["tool"] == "type_text"
        dry = server.resume_interrupted(dry_run=True)
        assert dry["steps"] == [{"action": "type_text", "text": "llo", "delay_ms": 40,
                                 "typos": False, "field": ""}]
        done = server.resume_interrupted()
        assert done["ok"] is True and done["resumed"] is True
        assert "".join(typed) == "hello"
        assert resume.last() is None
        human.reset()

    def test_resume_refuses_while_the_switch_is_off(self, typed):
        resume.remember("type_text", {"text": "abc"}, {"ok": False, "error": "interrupted",
                                                        "remaining": "bc"})
        owner_pause.pause_from_escape()
        out = server.resume_interrupted()
        assert out["ok"] is False and out["error"] == "input_disabled"
        assert typed == []
        assert resume.last() is not None

    def test_nothing_interrupted_is_a_quiet_no(self):
        out = server.resume_interrupted()
        assert out == {"ok": True, "resumed": False, "note": "nothing was interrupted"}

    def test_input_control_resume_needs_the_owner(self, monkeypatch):
        owner_pause.pause_from_escape()
        out = server.input_control("resume")
        assert out["ok"] is False and out["error"] == "owner_confirmation_required"
        assert owner_pause.blocked("click") is True
        out = server.input_control("resume", owner_confirmed=True)
        assert out["ok"] is True and out["state"]["mouse"] is True
        assert owner_pause.blocked("click") is False

    def test_input_control_pause_and_release(self, monkeypatch):
        monkeypatch.setattr(server.esc_pause, "release_agent_holds",
                            lambda display=None: {"ok": True, "released_keys": []})
        out = server.input_control("pause")
        assert out["ok"] is True and out["state"]["paused_by"] == "escape"
        assert server.input_control("release")["ok"] is True
        st = server.input_control("status")
        assert st["ok"] is True and st["input_switches"] == {"mouse": False, "keyboard": False}
        assert server.input_control("dance")["ok"] is False

    def test_bot_challenge_stops_input_tools_and_is_resumable(self, monkeypatch):
        monkeypatch.setattr(server.chrome, "challenge_active", lambda: True)
        moved = []
        monkeypatch.setattr(server.desktop, "click", lambda *a, **k: moved.append(a) or {"ok": True})
        out = server.click(10, 10)
        assert out["ok"] is False and out["error"] == "bot_challenge"
        assert "owner" in out["instruction"].lower()
        assert moved == []
        assert resume.plan() == [{"action": "click", "x": 10, "y": 10, "button": 1, "count": 1,
                                  "modifiers": None}]
        monkeypatch.setattr(server.chrome, "challenge_active", lambda: False)
        done = server.resume_interrupted()
        assert done["ok"] is True and moved == [(10, 10)]
