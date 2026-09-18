"""Workspace layout: workman-mode 1/4 agent strip."""
from __future__ import annotations

from workman import workspace_layout as layout


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
