"""Coverage for the coordinator's review items 1-8 and the requirement-6 gates."""
from __future__ import annotations

import random

import pytest

from workman import apps, atspi, chrome, human, server


class TestServerStubs:
    def test_unavailable_module_exception_names_are_catchable(self):
        stub = server._Unavailable("vision", ImportError("no PIL"))
        assert stub.VisionUnavailable is server._NeverRaised
        try:
            raise ValueError("x")
        except stub.VisionUnavailable:  # a valid except clause, never matches
            raise AssertionError("must not match")
        except ValueError:
            pass
        out = stub.some_function(1, a=2)
        assert out["ok"] is False and out["module"] == "vision" and "no PIL" in out["detail"]


class TestHumanModeDefault:
    def test_on_by_default_and_env_opt_out(self, monkeypatch):
        monkeypatch.delenv("WORKMAN_HUMAN_MODE", raising=False)
        monkeypatch.delenv("WORKMAN_HUMAN_SEED", raising=False)
        assert human.apply_session_default()["human_mode"] is True
        monkeypatch.setenv("WORKMAN_HUMAN_MODE", "0")
        assert human.apply_session_default()["human_mode"] is False
        monkeypatch.setenv("WORKMAN_HUMAN_MODE", "1")
        monkeypatch.setenv("WORKMAN_HUMAN_SEED", "42")
        assert human.apply_session_default() == {"human_mode": True, "seed": 42}
        human.reset()


class TestLaunchGuards:
    def test_automation_flags_are_detected(self):
        argv = ["google-chrome", "--remote-debugging-port=9222", "--enable-automation",
                "--headless=new", "--user-data-dir=/tmp/x", "https://example.com"]
        assert apps.automation_flags(argv) == ["--remote-debugging-port=9222", "--enable-automation",
                                               "--headless=new", "--user-data-dir=/tmp/x"]
        assert apps.automation_flags(["google-chrome", "https://example.com"]) == []

    def test_launch_refuses_a_debug_port_chrome(self, monkeypatch):
        monkeypatch.delenv("WORKMAN_ALLOW_AUTOMATION_FLAGS", raising=False)
        started = []
        monkeypatch.setattr(apps.subprocess, "Popen", lambda *a, **k: started.append(a) or None)
        out = apps.launch("google-chrome --remote-debugging-port=9222")
        assert out["ok"] is False and out["error"] == "automation_flags_refused"
        assert out["flags"] == ["--remote-debugging-port=9222"]
        assert started == []

    def test_launch_allows_flags_only_when_the_owner_says(self, monkeypatch):
        monkeypatch.setenv("WORKMAN_ALLOW_AUTOMATION_FLAGS", "1")
        monkeypatch.setattr(apps.shutil, "which", lambda name: "/usr/bin/true")
        monkeypatch.setattr(apps.desktop, "list_windows", lambda: [])

        class P:
            pid = 1

        monkeypatch.setattr(apps.subprocess, "Popen", lambda *a, **k: P())
        out = apps.launch("chromium --user-data-dir=/tmp/x")
        assert out["ok"] is True


class TestClickWithHumanReleases:
    def test_only_what_went_down_comes_up_even_if_key_up_raises(self, monkeypatch):
        human.set_mode(True, seed=1)
        monkeypatch.setattr(human, "human_move", lambda x, y, rng=None, target_px=None: {"ok": True, "at": [x, y]})
        monkeypatch.setattr(human, "human_press_click", lambda **k: 90)
        downs, ups = [], []

        def key_down(k):
            downs.append(k)
            return {"ok": True} if k == "ctrl" else {"ok": False, "error": "input_disabled"}

        def key_up(k):
            ups.append(k)
            if k == "ctrl":
                raise RuntimeError("X gone")
            return {"ok": True}
        monkeypatch.setattr(server.desktop, "key_down", key_down)
        monkeypatch.setattr(server.desktop, "key_up", key_up)
        out = server._click_with_human(5, 5, button=1, count=1, modifiers=["ctrl", "shift"])
        assert out["ok"] is False and out["error"] == "interrupted"
        assert downs == ["ctrl", "shift"]
        assert ups == ["ctrl"]  # shift never went down, so it is not released
        human.reset()


class TestMouseButtonAndDragOutcomes:
    def test_press_bails_when_the_move_is_interrupted_but_release_still_runs(self, monkeypatch):
        monkeypatch.setattr(human, "human_move",
                            lambda x, y, rng=None, target_px=None: {"ok": False, "error": "interrupted"})
        downs, ups = [], []
        monkeypatch.setattr(human.desktop, "mouse_down", lambda button=1: downs.append(button) or {"ok": True})
        monkeypatch.setattr(human.desktop, "mouse_up", lambda button=1: ups.append(button) or {"ok": True})
        out = human.human_mouse_button(button=1, press=True, x=5, y=5, rng=random.Random(0))
        assert out["ok"] is False and out["pressed"] is False and downs == []
        out = human.human_mouse_button(button=1, press=False, x=5, y=5, rng=random.Random(0))
        assert out["ok"] is True and ups == [1] and out["move"]["ok"] is False

    def test_drag_reports_a_failed_release(self, monkeypatch):
        monkeypatch.setattr(human, "_pointer", lambda: (0, 0))
        monkeypatch.setattr(human.time, "sleep", lambda s: None)
        monkeypatch.setattr(human.desktop, "move", lambda x, y: {"ok": True})
        monkeypatch.setattr(human.desktop, "mouse_down", lambda button=1: {"ok": True})
        monkeypatch.setattr(human.desktop, "mouse_up", lambda button=1: {"ok": False, "error": "x"})
        out = human.human_drag(0, 0, 50, 50, rng=random.Random(0))
        assert out["ok"] is False and out["error"] == "release_failed" and out["released"] is False

    def test_press_click_always_releases_a_pressed_button(self, monkeypatch):
        monkeypatch.setattr(human.time, "sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt()))
        ups = []
        monkeypatch.setattr(human.desktop, "mouse_down", lambda button=1: {"ok": True})
        monkeypatch.setattr(human.desktop, "mouse_up", lambda button=1: ups.append(button) or {"ok": True})
        with pytest.raises(KeyboardInterrupt):
            human.human_press_click(button=1, rng=random.Random(0), aim=False)
        assert ups == [1]


class TestFailurePropagation:
    def test_chrome_click_text_propagates_an_interrupted_click(self, monkeypatch):
        monkeypatch.setattr(chrome, "focus", lambda: {"ok": True, "id": "1"})
        monkeypatch.setattr(chrome, "_find_clickable", lambda text: {"name": "Pricing", "x": 0, "y": 0,
                                                                    "w": 60, "h": 40, "cx": 30, "cy": 20},
                            raising=False)
        monkeypatch.setattr(chrome, "_ensure_visible", lambda el, w: el, raising=False)
        monkeypatch.setattr(chrome, "_in_content_area", lambda e, w: True, raising=False)
        # click_text aims through human.human_click_element (a point inside the
        # box), which moves and presses with human.human_click.
        monkeypatch.setattr(human, "human_click",
                            lambda x, y, rng=None, button=1, count=1, target_px=None:
                            {"ok": False, "error": "interrupted", "at": [3, 3]})
        out = chrome.click_text("Pricing", rng=random.Random(0))
        assert out["ok"] is False and out["error"] == "interrupted"

    def test_atspi_click_element_propagates_an_interrupted_click(self, monkeypatch):
        monkeypatch.setattr(atspi, "find", lambda *a, **k: {"x": 0, "y": 0, "w": 10, "h": 10, "cx": 5, "cy": 5})
        monkeypatch.setattr(atspi, "_human_click_info",
                            lambda info: {"ok": False, "error": "human_active"})
        out = atspi.click_element("Go")
        assert out["ok"] is False and out["error"] == "human_active" and "element" in out


class TestPlatformReport:
    def test_workman_platform_reports_pause_listener_and_cdp(self, monkeypatch):
        monkeypatch.setattr(server, "_port_open", lambda port, host="127.0.0.1", timeout=0.1: False)
        monkeypatch.setattr(server.esc_pause, "listener_pid", lambda: 4321)
        info = server.workman_platform()
        assert info["esc_listener_pid"] == 4321
        assert info["owner_pause"]["input_switches"] == {"mouse": True, "keyboard": True}
        assert info["chrome_cdp"] == {"enabled": False, "port_listening": False}
        assert info["input_channel"] in ("xtest", "xdotool")
