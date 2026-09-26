"""X11 backend — parsing and the command sequences we build.

The backend is shell-driven, so these fake `_run` and assert on the argv that
would have been executed. That catches the failures that actually happen here:
a misparsed xrandr line, or a modifier left held down.
"""
from __future__ import annotations

import subprocess

import pytest

from workman import x11


class FakeRun:
    """Records argv and replays canned stdout per command."""

    def __init__(self, outputs: dict[str, str] | None = None, fail_on: str | None = None):
        self.calls: list[list[str]] = []
        self.outputs = outputs or {}
        self.fail_on = fail_on

    def __call__(self, cmd, timeout=20):
        self.calls.append(list(cmd))
        if self.fail_on and self.fail_on in cmd:
            raise RuntimeError("boom")
        stdout = ""
        for key, value in self.outputs.items():
            if key in cmd:
                stdout = value
                break
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    def argv(self, index=0) -> list[str]:
        return self.calls[index]

    @property
    def flat(self) -> list[str]:
        return [arg for call in self.calls for arg in call]


@pytest.fixture
def fake(monkeypatch):
    runner = FakeRun()
    monkeypatch.setattr(x11, "_run", runner)
    monkeypatch.setattr(x11, "_require", lambda binary: None)
    monkeypatch.setattr(x11, "emit_cursor", lambda *a, **k: None)
    return runner


class TestMonitors:
    def test_parses_xrandr_listmonitors(self, monkeypatch):
        listing = (
            "Monitors: 2\n"
            " 0: +*USB-C-1 3840/800x2160/450+0+0  USB-C-1\n"
            " 1: +HDMI-0 1920/600x1080/340+3840+0  HDMI-0\n"
        )
        monkeypatch.setattr(x11, "_run", FakeRun({"--listmonitors": listing}))
        monkeypatch.setattr(x11.shutil, "which", lambda binary: "/usr/bin/xrandr")
        found = x11.monitors()
        assert [m["name"] for m in found] == ["USB-C-1", "HDMI-0"]
        assert found[0] == {"name": "USB-C-1", "x": 0, "y": 0, "w": 3840, "h": 2160,
                            "primary": True}
        # The second head is offset, which is what makes coordinates non-obvious.
        assert found[1]["x"] == 3840 and found[1]["primary"] is False

    def test_falls_back_when_xrandr_missing(self, monkeypatch):
        monkeypatch.setattr(x11.shutil, "which", lambda binary: None)
        monkeypatch.setattr(x11, "screen_size", lambda: (1280, 1024))
        assert x11.monitors() == [
            {"name": "screen", "x": 0, "y": 0, "w": 1280, "h": 1024, "primary": True}
        ]

    def test_falls_back_when_output_unparseable(self, monkeypatch):
        monkeypatch.setattr(x11, "_run", FakeRun({"--listmonitors": "Monitors: 0\n"}))
        monkeypatch.setattr(x11.shutil, "which", lambda binary: "/usr/bin/xrandr")
        monkeypatch.setattr(x11, "screen_size", lambda: (800, 600))
        assert x11.monitors()[0]["w"] == 800


class TestPointer:
    def test_parses_shell_output(self, monkeypatch):
        out = "X=1536\nY=864\nSCREEN=0\nWINDOW=23068676\n"
        monkeypatch.setattr(x11, "_run", FakeRun({"getmouselocation": out}))
        monkeypatch.setattr(x11, "_require", lambda binary: None)
        assert x11.pointer_position() == {"ok": True, "x": 1536, "y": 864,
                                          "screen": "0", "window": "23068676"}


def test_drag_releases_mouse_when_motion_fails(monkeypatch):
    calls = []
    def run(cmd):
        calls.append(cmd)
        if cmd == ["xdotool", "mousemove", "30", "40"]:
            raise RuntimeError("motion failed")
    monkeypatch.setattr(x11, "_run", run)
    monkeypatch.setattr(x11.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError): x11.drag(10, 20, 30, 40)
    assert calls[-1] == ["xdotool", "mouseup", "1"]


def test_typing_uses_stdin_and_reports_command_failure(monkeypatch):
    def run(cmd, **kwargs):
        assert "private-sentinel" not in cmd
        assert cmd[-2:] == ["--file", "-"]
        assert kwargs["input_text"] == "private-sentinel"
        return subprocess.CompletedProcess(cmd, 1)
    monkeypatch.setattr(x11, "_require", lambda _: None)
    monkeypatch.setattr(x11, "_run", run)
    assert x11.type_text("private-sentinel")["ok"] is False


class TestModifiedClick:
    def test_holds_and_releases_around_click(self, fake):
        x11.click_with(10, 20, modifiers=["ctrl", "shift"])
        assert fake.calls[0] == ["xdotool", "keydown", "ctrl"]
        assert fake.calls[1] == ["xdotool", "keydown", "shift"]
        assert "mousemove" in fake.calls[2]
        # Released in reverse order, so nesting unwinds correctly.
        assert fake.calls[-2] == ["xdotool", "keyup", "shift"]
        assert fake.calls[-1] == ["xdotool", "keyup", "ctrl"]

    def test_releases_modifiers_even_when_click_raises(self, monkeypatch):
        runner = FakeRun()
        monkeypatch.setattr(x11, "_run", runner)
        monkeypatch.setattr(x11, "_require", lambda binary: None)

        def explode(*args, **kwargs):
            raise RuntimeError("click failed")

        monkeypatch.setattr(x11, "click", explode)
        with pytest.raises(RuntimeError):
            x11.click_with(1, 2, modifiers=["ctrl"])
        # A stuck ctrl would break the human's keyboard afterwards.
        assert ["xdotool", "keyup", "ctrl"] in runner.calls

    def test_rejects_unknown_modifier_without_touching_keyboard(self, fake):
        result = x11.click_with(1, 2, modifiers=["hyperspace"])
        assert result["ok"] is False
        assert "hyperspace" in str(result["error"])
        assert fake.calls == []


class TestScroll:
    def test_rejects_bad_direction(self, fake):
        assert x11.scroll_at(1, 2, "sideways")["ok"] is False
        assert fake.calls == []

    def test_moves_before_scrolling(self, fake):
        x11.scroll_at(400, 500, "down", amount=2)
        argv = fake.argv(0)
        assert argv[:4] == ["xdotool", "mousemove", "400", "500"]
        assert argv[-3:] == ["--repeat", "2", "5"]  # button 5 == wheel down


class TestMouseAndKeyHold:
    def test_mouse_down_can_move_first(self, fake):
        x11.mouse_down(button=1, x=7, y=9)
        assert fake.argv(0) == ["xdotool", "mousemove", "7", "9", "mousedown", "1"]

    def test_mouse_up_without_coordinates(self, fake):
        x11.mouse_up(button=3)
        assert fake.argv(0) == ["xdotool", "mouseup", "3"]

    def test_key_hold_roundtrip(self, fake):
        x11.key_down("ctrl")
        x11.key_up("ctrl")
        assert fake.calls == [["xdotool", "keydown", "ctrl"], ["xdotool", "keyup", "ctrl"]]


class TestKillWindow:
    def test_reports_missing_window(self, monkeypatch):
        monkeypatch.setattr(x11, "_require", lambda binary: None)
        monkeypatch.setattr(x11, "list_windows", lambda: [{"id": "1", "name": "Terminal"}])
        assert x11.kill_window("Firefox")["ok"] is False

    def test_resolves_name_to_id(self, monkeypatch):
        runner = FakeRun()
        monkeypatch.setattr(x11, "_run", runner)
        monkeypatch.setattr(x11, "_require", lambda binary: None)
        monkeypatch.setattr(x11, "list_windows", lambda: [{"id": "4242", "name": "Terminal"}])
        result = x11.kill_window("termin")
        assert result["ok"] is True
        assert runner.argv(0) == ["xdotool", "windowkill", "4242"]
