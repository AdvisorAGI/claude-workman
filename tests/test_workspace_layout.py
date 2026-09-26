"""F3: focus_and_verify waits for a mapped window with real geometry."""
from __future__ import annotations

from workman import workspace_layout as layout


LEADER = {"id": "0x1", "name": "workman-typing-check", "app": "zenity",
          "pid": "99", "x": 0, "y": 0, "w": 1, "h": 1}
DIALOG = {"id": "0x2", "name": "workman-typing-check", "app": "zenity",
          "pid": "99", "x": 400, "y": 200, "w": 360, "h": 140}


def _listing(windows):
    return lambda: list(windows)


class TestFindWindow:
    def test_substring_prefers_the_real_dialog_over_a_1x1_leader(self, monkeypatch):
        monkeypatch.setattr(layout, "_windows", _listing([LEADER, DIALOG]))
        hit = layout._find_window("workman-typing-check")
        assert hit["id"] == "0x2" and hit["w"] == 360

    def test_exact_id_of_the_leader_is_still_returned(self, monkeypatch):
        monkeypatch.setattr(layout, "_windows", _listing([LEADER, DIALOG]))
        assert layout._find_window("0x1")["id"] == "0x1"


class TestFrontmostVerdict:
    def test_pid_match_on_a_1x1_is_not_verified(self, monkeypatch):
        monkeypatch.setattr(layout.desktop, "active_window",
                            lambda: {"ok": True, "id": "0x1", "pid": "99", "app": "zenity"})
        out = layout._frontmost_verdict(LEADER)
        assert out["verified"] is False and out.get("reason") == "not_mapped"

    def test_id_match_on_the_dialog_is_verified(self, monkeypatch):
        monkeypatch.setattr(layout.desktop, "active_window",
                            lambda: {"ok": True, "id": "0x2", "pid": "99", "app": "zenity"})
        out = layout._frontmost_verdict(DIALOG)
        assert out["verified"] is True and out["matched_on"] == "id"


class TestFocusAndVerify:
    def test_waits_until_the_dialog_maps(self, monkeypatch):
        frames = [[LEADER], [LEADER], [LEADER, DIALOG]]
        monkeypatch.setattr(layout, "_windows", lambda: list(frames[0]))
        monkeypatch.setattr(layout.time, "sleep", lambda s: frames.pop(0) if len(frames) > 1 else None)
        monkeypatch.setattr(layout.desktop, "focus_window",
                            lambda q, minimize_blockers=True: {"ok": True})
        monkeypatch.setattr(layout.desktop, "active_window",
                            lambda: {"ok": True, "id": frames[0][-1]["id"],
                                     "pid": "99", "app": "zenity"})
        monkeypatch.setattr(layout, "backend_name", lambda: "linux_x11")
        out = layout.focus_and_verify("workman-typing-check", timeout_s=2)
        assert out["ok"] is True and out["verified"] is True
        assert out["window"]["id"] == "0x2"
        assert out["window"]["w"] >= layout.MIN_MAPPED_SIDE

    def test_1x1_never_maps_is_not_verified(self, monkeypatch):
        monkeypatch.setattr(layout, "_windows", _listing([LEADER]))
        monkeypatch.setattr(layout.time, "sleep", lambda s: None)
        monkeypatch.setattr(layout.desktop, "focus_window",
                            lambda q, minimize_blockers=True: {"ok": True})
        monkeypatch.setattr(layout.desktop, "active_window",
                            lambda: {"ok": True, "id": "0x1", "pid": "99", "app": "zenity"})
        monkeypatch.setattr(layout, "backend_name", lambda: "linux_x11")
        out = layout.focus_and_verify("workman-typing-check", timeout_s=0)
        assert out["ok"] is False and out["verified"] is False
        assert "viewable" in out["error"]

    def test_focus_path_does_not_press_keys(self, monkeypatch):
        pressed = []
        monkeypatch.setattr(layout, "_windows", _listing([DIALOG]))
        monkeypatch.setattr(layout.desktop, "focus_window",
                            lambda q, minimize_blockers=True: {"ok": True})
        monkeypatch.setattr(layout.desktop, "active_window",
                            lambda: {"ok": True, "id": "0x2", "pid": "99"})
        monkeypatch.setattr(layout.desktop, "press_key",
                            lambda *a, **k: pressed.append(("press_key", a, k)))
        monkeypatch.setattr(layout.desktop, "key_down",
                            lambda *a, **k: pressed.append(("key_down", a, k)))
        monkeypatch.setattr(layout, "backend_name", lambda: "linux_x11")
        assert layout.focus_and_verify("workman-typing-check", timeout_s=0)["verified"] is True
        assert pressed == []

class TestAgentStrip:
    def test_left_puts_agent_in_the_first_quarter(self, monkeypatch):
        seen = {}

        def fake_split(left, right, ratio=0.5, display=None, prefer_menu=True):
            seen.update(left=left, right=right, ratio=ratio,
                        prefer_menu=prefer_menu, display=display)
            return {"ok": True, "layout": "split"}

        monkeypatch.setattr(layout, "split", fake_split)
        monkeypatch.setattr(layout, "_find_window", lambda q, listing=None: {"id": q})
        monkeypatch.setattr(layout, "_raise_window", lambda w: {"ok": True})
        out = layout.agent_strip("Terminal", work="Chrome", side="left")
        assert out["ok"] is True
        assert out["layout"] == "agent"
        assert out["agent_side"] == "left"
        assert out["agent_ratio"] == 0.25
        assert seen == {"left": "Terminal", "right": "Chrome", "ratio": 0.25,
                        "prefer_menu": False, "display": None}

    def test_right_puts_agent_in_the_last_quarter(self, monkeypatch):
        seen = {}

        def fake_split(left, right, ratio=0.5, display=None, prefer_menu=True):
            seen.update(left=left, right=right, ratio=ratio)
            return {"ok": True, "layout": "split"}

        monkeypatch.setattr(layout, "split", fake_split)
        monkeypatch.setattr(layout, "_find_window", lambda q, listing=None: {"id": q})
        monkeypatch.setattr(layout, "_raise_window", lambda w: {"ok": True})
        out = layout.agent_strip("Terminal", work="Chrome", side="right")
        assert out["layout"] == "agent"
        assert out["agent_side"] == "right"
        assert seen["left"] == "Chrome"
        assert seen["right"] == "Terminal"
        assert seen["ratio"] == 0.75

    def test_bad_side_is_data_not_an_exception(self):
        out = layout.agent_strip("Terminal", side="top")
        assert out["ok"] is False
        assert "left or right" in out["error"]

    def test_missing_agent_is_rejected(self):
        out = layout.agent_strip("  ")
        assert out["ok"] is False

    def test_arrange_dispatches_agent(self, monkeypatch):
        seen = {}

        def fake(agent, work=None, side="left", display=None):
            seen.update(agent=agent, work=work, side=side)
            return {"ok": True, "layout": "agent"}

        monkeypatch.setattr(layout, "agent_strip", fake)
        out = layout.arrange({"layout": "workman", "agent": "Terminal",
                              "work": "Chrome", "side": "right"})
        assert out["ok"] is True
        assert seen == {"agent": "Terminal", "work": "Chrome", "side": "right"}

    def test_arrange_lists_agent_among_layouts(self):
        out = layout.arrange({"layout": "nope"})
        assert out["ok"] is False
        assert "agent" in out["layouts"]

