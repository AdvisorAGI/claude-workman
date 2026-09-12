"""macOS accessibility: in a browser or Electron app the AX tree only FINDS an
element and the real pointer and keyboard act; native apps keep AX actions.
Fakes only: nothing here runs on a Mac."""
from __future__ import annotations

import pytest

from workman import desktop, human
from workman.platform import ax_darwin

INFO = {"app": "Google Chrome", "role": "push button", "ax_role": "AXButton",
        "name": "Sign in", "x": 100, "y": 200, "w": 80, "h": 30, "center": [140, 215]}


class Node:
    def __init__(self, role: str, parent: "Node | None" = None):
        self.attrs = {"AXRole": role, "AXParent": parent}


class FakeAX:
    kAXPressAction = "AXPress"
    kAXValueAttribute = "AXValue"
    kAXRoleAttribute = "AXRole"
    kAXParentAttribute = "AXParent"

    def __init__(self):
        self.performed: list[str] = []
        self.set: list[tuple] = []

    def AXUIElementCopyAttributeValue(self, element, name, _):
        value = element.attrs.get(name)
        return (0 if value is not None else -25212), value

    def AXUIElementPerformAction(self, element, action):
        self.performed.append(action)
        return 0

    def AXUIElementSetAttributeValue(self, element, attr, value):
        self.set.append((attr, value))
        return 0


@pytest.fixture
def ax(monkeypatch):
    api = FakeAX()
    monkeypatch.setattr(ax_darwin, "_api", lambda: api)
    monkeypatch.setattr(ax_darwin.time, "sleep", lambda s: None)
    return api


@pytest.fixture
def clicks(monkeypatch):
    seen: list[tuple[int, int]] = []
    monkeypatch.setattr(desktop, "click",
                        lambda x, y, button=1, count=1: seen.append((x, y)) or {"ok": True})
    human.set_mode(False)
    yield seen
    human.reset()


def found(monkeypatch, node: Node, info: dict | None = None) -> None:
    monkeypatch.setattr(ax_darwin, "find_node", lambda *a, **k: (node, dict(info or INFO)))


def inside(x: int, y: int) -> bool:
    return 100 <= x < 180 and 200 <= y < 230


class TestBrowserIsFindOnly:
    def test_click_is_a_pointer_click_inside_the_frame(self, ax, clicks, monkeypatch):
        found(monkeypatch, Node("AXButton"))
        out = ax_darwin.click_element("Sign in")
        assert out["ok"] is True and out["via"] == "pointer" and ax.performed == []
        (x, y), = clicks
        assert inside(x, y) and out["aimed"] == [x, y]

    def test_click_points_vary_inside_the_frame(self, ax, clicks, monkeypatch):
        found(monkeypatch, Node("AXButton"))
        for _ in range(20):
            ax_darwin.click_element("Sign in")
        assert all(inside(x, y) for x, y in clicks) and len(set(clicks)) > 1

    def test_a_web_view_in_a_native_app_is_found_by_its_web_area(self, ax, clicks, monkeypatch):
        page = Node("AXWebArea")
        found(monkeypatch, Node("AXButton", parent=Node("AXGroup", parent=page)),
              {**INFO, "app": "Mail"})
        assert ax_darwin.click_element("Sign in")["via"] == "pointer"
        assert ax.performed == [] and len(clicks) == 1

    def test_perform_action_uses_the_pointer(self, ax, clicks, monkeypatch):
        found(monkeypatch, Node("AXButton"))
        out = ax_darwin.perform_action("Sign in", "click")
        assert out["ok"] is True and out["via"] == "pointer" and out["performed"] == "click"
        assert ax.performed == [] and len(clicks) == 1

    def test_set_value_is_click_cmd_a_then_type(self, ax, clicks, monkeypatch):
        found(monkeypatch, Node("AXTextField"), {**INFO, "role": "text"})
        pressed, typed = [], []
        monkeypatch.setattr(desktop, "press_key",
                            lambda k, hold_ms=None: pressed.append(k) or {"ok": True})
        monkeypatch.setattr(desktop, "type_text", lambda t, **k: typed.append(t) or {"ok": True})
        out = ax_darwin.set_value("Email", "me@example.com")
        assert out["ok"] is True and out["via"] == "pointer+keys" and out["set_len"] == 14
        assert len(clicks) == 1 and pressed == ["super+a"] and typed == ["me@example.com"]
        assert ax.set == []

    def test_human_mode_clicks_along_a_human_path(self, ax, monkeypatch):
        found(monkeypatch, Node("AXButton"))
        human.set_mode(True, seed=3)
        seen = []
        monkeypatch.setattr(human, "human_click",
                            lambda x, y, rng=None, button=1, count=1, target_px=None:
                            seen.append((x, y, target_px)) or {"ok": True})
        try:
            out = ax_darwin.click_element("Sign in")
        finally:
            human.reset()
        x, y, target = seen[0]
        assert out["ok"] is True and inside(x, y) and target == 30.0

    def test_an_element_without_a_frame_is_not_clicked_at_the_origin(self, ax, clicks, monkeypatch):
        found(monkeypatch, Node("AXButton"), {**INFO, "w": 0, "h": 0, "center": [0, 0]})
        out = ax_darwin.click_element("Sign in")
        assert out["ok"] is False and clicks == [] and ax.performed == []


class TestNativeKeepsAX:
    def test_click_is_ax_press(self, ax, clicks, monkeypatch):
        found(monkeypatch, Node("AXButton"), {**INFO, "app": "System Settings"})
        out = ax_darwin.click_element("Sign in")
        assert out["via"] == "AXPress" and ax.performed == ["AXPress"] and clicks == []

    def test_perform_action_is_ax(self, ax, clicks, monkeypatch):
        found(monkeypatch, Node("AXButton"), {**INFO, "app": "Finder"})
        out = ax_darwin.perform_action("Sign in", "click")
        assert out["action"] == "AXPress" and ax.performed == ["AXPress"] and clicks == []

    def test_set_value_writes_ax_value(self, ax, clicks, monkeypatch):
        found(monkeypatch, Node("AXTextField"), {**INFO, "app": "TextEdit"})
        out = ax_darwin.set_value("Name", "Tariqul")
        assert out["ok"] is True and ax.set == [("AXValue", "Tariqul")] and clicks == []
