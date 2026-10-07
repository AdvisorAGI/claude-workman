"""Owner priority across composite MCP tools and interrupted resumption."""
from types import SimpleNamespace
import ctypes

import pytest

from workman import atspi, owner_pause, resume, server
from test_polite import seat, touch_owner


def test_paused_batch_never_reaches_native_toolkit(seat, monkeypatch):
    owner_pause.pause_from_escape()
    monkeypatch.setattr(atspi, "find_node", lambda *a, **k: pytest.fail("paused lookup"))
    out = server.batch([{"action": "set_element_value", "name": "field", "value": "text"}])
    assert out["error"] == "input_disabled"


def test_native_edit_yields_when_owner_arrives_during_lookup(seat, monkeypatch):
    writes = []
    node = SimpleNamespace(get_editable_text_iface=lambda: SimpleNamespace(
        set_text_contents=lambda value: writes.append(value)))

    def lookup(*args, **kwargs):
        touch_owner()
        return node, {}

    monkeypatch.setattr(atspi, "find_node", lookup)
    monkeypatch.setattr(atspi, "is_browser_element", lambda node: False)
    out = server.set_element_value("field", "must not arrive")
    assert out["error"] == "human_active" and writes == []


@pytest.mark.parametrize("polite_on", [True, False])
def test_extension_mutation_obeys_escape(seat, monkeypatch, polite_on):
    if not polite_on:
        monkeypatch.setenv("WORKMAN_POLITE", "0")
    owner_pause.pause_from_escape()
    monkeypatch.setattr(server.bridge, "call", lambda *a, **k: pytest.fail("paused extension mutation"))
    assert server.bridge_tabs_activate(42)["error"] == "input_disabled"


def test_resume_does_not_repeat_completed_clicks_or_key():
    assert resume.plan({"tool": "press_key", "args": {"key": "Return"},
                        "result": {"pressed": True}}) == []
    assert resume.plan({"tool": "click", "args": {"x": 1, "y": 2, "count": 3},
                        "result": {"remaining_clicks": 1}}) == [
        {"action": "click", "x": 1, "y": 2, "count": 1}]


def test_focus_identity_follows_real_input_child_to_client(seat, monkeypatch):
    seat.ch.input_focus = lambda: {"ok": True, "window": 101, "revert_to": 1}
    seat.ch._identity = lambda wid: {"ok": True, "id": wid,
                                    "class": "Remmina" if wid == 100 else ""}
    def tree(dpy, wid, root, parent, children, count):
        ctypes.cast(parent, ctypes.POINTER(ctypes.c_ulong))[0] = 100
        return 1
    monkeypatch.setattr(seat.ch.x11, "XQueryTree", tree, raising=False)
    assert seat.ch.focused_window_identity()["class"] == "Remmina"
