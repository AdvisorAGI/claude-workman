"""Remote-viewer guard: no agent key or pointer event while a remote-desktop
viewer has focus, because the viewer forwards it to another machine."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from workman import desktop, x11

REMMINA = {"ok": True, "id": 7, "class": "org.remmina.Remmina", "instance": "remmina",
           "name": "Mac mini screen (via ssh tunnel)", "pid": 99}
EDITOR = {"ok": True, "id": 8, "class": "Gedit", "instance": "gedit", "name": "notes.txt", "pid": 5}


class FakeChannel:
    def __init__(self, win, under=None):
        self.win = win
        self.under = under  # None | dict | (x, y) -> dict|None
        self.reads = 0
        self.point_reads = 0

    def active_window_info(self):
        self.reads += 1
        return dict(self.win)

    def window_at_point(self, x, y):
        self.point_reads += 1
        if callable(self.under):
            return self.under(x, y)
        return self.under


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple] = []
    fake = SimpleNamespace(
        PLATFORM="fake",
        click=lambda x, y, button=1, count=1: seen.append(("click", x, y)) or {"ok": True},
        click_with=lambda x, y, button=1, count=1, modifiers=None:
            seen.append(("click_with", x, y)) or {"ok": True},
        move=lambda x, y: seen.append(("move", x, y)) or {"ok": True},
        drag=lambda fx, fy, tx, ty: seen.append(("drag", fx, fy, tx, ty)) or {"ok": True},
        hover=lambda x, y, settle_ms=350: seen.append(("hover", x, y)) or {"ok": True},
        scroll_at=lambda x, y, direction, amount=3:
            seen.append(("scroll_at", x, y)) or {"ok": True},
        mouse_down=lambda button=1, x=None, y=None:
            seen.append(("mouse_down", button, x, y)) or {"ok": True},
        type_text=lambda text, delay_ms=40: seen.append(("type_text", text)) or {"ok": True},
        press_key=lambda key: seen.append(("press_key", key)) or {"ok": True},
        mouse_up=lambda button=1, x=None, y=None: seen.append(("mouse_up", button)) or {"ok": True},
        key_up=lambda key: seen.append(("key_up", key)) or {"ok": True},
        focus_window=lambda q, minimize_blockers=True: seen.append(("focus_window", q)) or {"ok": True},
        pointer_position=lambda: {"ok": True, "x": 150, "y": 150},
    )
    monkeypatch.setattr(desktop, "active", lambda: fake)
    monkeypatch.setattr(desktop, "_VIEWER_SEEN", {"at": -1.0, "hit": False})
    return seen


def focus(monkeypatch, win, under=None) -> FakeChannel:
    ch = FakeChannel(win, under=under)
    monkeypatch.setattr(x11, "_xt", lambda: ch)
    return ch


def remmina_rect(x, y):
    if 100 <= x < 900 and 100 <= y < 700:
        return REMMINA
    return None


class TestIsRemoteViewer:
    @pytest.mark.parametrize("win", [
        REMMINA,
        {"class": "Vncviewer", "name": "TigerVNC: md"},
        {"class": "Virt-viewer", "name": "vm1"},
        {"class": "xfreerdp", "name": "FreeRDP: wind1"},
        {"class": "krdc", "name": "KRDC"},
        {"class": "krfb", "name": "Desktop Sharing"},
        {"app": "Screen Sharing", "name": "mac-mini"},
    ])
    def test_viewers_are_recognised(self, win):
        assert desktop.is_remote_viewer(win) is True

    @pytest.mark.parametrize("win", [
        EDITOR,
        {"class": "Google-chrome", "name": "Inbox"},
        {"class": "Google-chrome", "name": "TigerVNC setup guide"},
        {"class": "Gnome-terminal", "name": "ssh vnc-box"},
        {},
        None,
    ])
    def test_local_windows_are_not(self, win):
        assert desktop.is_remote_viewer(win) is False

    def test_title_is_consulted_only_when_class_and_app_are_empty(self):
        assert desktop.is_remote_viewer({"name": "vnc session"}) is True
        assert desktop.is_remote_viewer({"class": "Gedit", "name": "vnc session"}) is False


class TestGuard:
    def test_keys_and_pointer_are_refused_while_a_viewer_has_focus(self, calls, monkeypatch):
        focus(monkeypatch, REMMINA)
        for out in (desktop.type_text("hello"), desktop.press_key("Return"),
                    desktop.click(10, 10), desktop.move(5, 5)):
            assert out == {"ok": False, "error": "remote_viewer_focused",
                           "action": out["action"], "instruction": out["instruction"]}
        assert calls == []

    def test_releases_are_never_refused(self, calls, monkeypatch):
        focus(monkeypatch, REMMINA)
        assert desktop.mouse_up(button=1)["ok"] is True
        assert desktop.key_up("ctrl")["ok"] is True
        assert calls == [("mouse_up", 1), ("key_up", "ctrl")]

    def test_the_agent_may_still_focus_a_local_window(self, calls, monkeypatch):
        focus(monkeypatch, REMMINA)
        assert desktop.focus_window("notes")["ok"] is True

    def test_a_local_window_gets_input_for_one_channel_read_per_action(self, calls, monkeypatch):
        ch = focus(monkeypatch, EDITOR)
        assert desktop.type_text("hi")["ok"] is True
        assert desktop.click(1, 2)["ok"] is True
        assert ch.reads == 2
        assert calls == [("type_text", "hi"), ("click", 1, 2)]

    def test_moves_reuse_a_fresh_verdict_and_a_window_change_clears_it(self, calls, monkeypatch):
        ch = focus(monkeypatch, EDITOR)
        for i in range(20):
            assert desktop.move(i, i)["ok"] is True
        assert ch.reads == 1  # one read covers a burst of path samples
        ch.win = REMMINA  # the agent brings the viewer forward...
        desktop.focus_window("Mac mini")
        assert desktop.move(1, 1)["error"] == "remote_viewer_focused"  # ...the next move sees it
        assert ch.reads == 2

    def test_a_cached_verdict_expires(self, calls, monkeypatch):
        ch = focus(monkeypatch, EDITOR)
        clock = {"t": 100.0}
        monkeypatch.setattr(desktop, "time", SimpleNamespace(monotonic=lambda: clock["t"]))
        assert desktop.move(0, 0)["ok"] is True
        ch.win = REMMINA
        clock["t"] += desktop.VIEWER_TTL_S + 0.01
        assert desktop.move(1, 1)["error"] == "remote_viewer_focused"

    def test_without_a_channel_one_read_per_non_move_and_zero_per_cached_move(
            self, calls, monkeypatch):
        # Replaces the earlier "spawn nothing" behaviour: with no channel the
        # guard used to fail open. It now does one bounded identity read per
        # non-move action; moves reuse the 200 ms TTL. conftest stubs the
        # helper so pytest never talks to :0; this test patches it back.
        n = {"n": 0}

        def ident():
            n["n"] += 1
            return EDITOR
        monkeypatch.setattr(desktop, "_xdotool_focused", ident)
        assert desktop.type_text("x")["ok"] is True
        assert desktop.click(1, 2)["ok"] is True
        assert n["n"] == 2
        for i in range(10):
            assert desktop.move(i, i)["ok"] is True
        assert n["n"] == 3  # one more for the first move; the rest are cached
        assert calls[0] == ("type_text", "x") and calls[1] == ("click", 1, 2)

    def test_unreadable_focus_refuses_keys_and_clicks_not_moves(self, calls, monkeypatch):
        monkeypatch.setattr(desktop, "_xdotool_focused",
                            lambda: desktop.FOCUS_UNREADABLE)
        out = desktop.type_text("x")
        assert out["ok"] is False and out["error"] == "remote_viewer_unknown"
        assert desktop.click(1, 2)["error"] == "remote_viewer_unknown"
        assert desktop.move(1, 1)["ok"] is True
        assert desktop.mouse_up(button=1)["ok"] is True
        assert ("type_text", "x") not in calls and ("click", 1, 2) not in calls
        assert ("move", 1, 1) in calls


class TestPointGuard:
    def test_click_inside_an_unfocused_viewer_is_refused(self, calls, monkeypatch):
        ch = focus(monkeypatch, EDITOR, under=remmina_rect)
        out = desktop.click(150, 150)
        assert out["ok"] is False and out["error"] == "remote_viewer_under_target"
        assert out["at"] == [150, 150]
        assert calls == []
        assert ch.point_reads == 1

    def test_click_just_outside_the_viewer_is_allowed(self, calls, monkeypatch):
        ch = focus(monkeypatch, EDITOR, under=remmina_rect)
        assert desktop.click(99, 150)["ok"] is True
        assert calls == [("click", 99, 150)]
        assert ch.point_reads == 1

    def test_mouse_up_is_never_refused_over_a_viewer(self, calls, monkeypatch):
        focus(monkeypatch, EDITOR, under=remmina_rect)
        assert desktop.mouse_up(button=1)["ok"] is True
        assert calls == [("mouse_up", 1)]

    def test_move_does_not_read_the_window_under_the_pointer(self, calls, monkeypatch):
        ch = focus(monkeypatch, EDITOR, under=remmina_rect)
        for i in range(20):
            assert desktop.move(150 + i, 150)["ok"] is True
        assert ch.point_reads == 0
        assert ch.reads == 1

    def test_drag_and_scroll_at_a_viewer_are_refused(self, calls, monkeypatch):
        focus(monkeypatch, EDITOR, under=remmina_rect)
        assert desktop.drag(10, 10, 150, 150)["error"] == "remote_viewer_under_target"
        assert desktop.scroll_at(150, 150, "down")["error"] == "remote_viewer_under_target"
        assert calls == []

    def test_mouse_down_without_coords_uses_the_pointer(self, calls, monkeypatch):
        focus(monkeypatch, EDITOR, under=remmina_rect)
        # The fake pointer sits at (150, 150), inside Remmina.
        out = desktop.mouse_down(button=1)
        assert out["error"] == "remote_viewer_under_target"
        assert calls == []

    def test_keys_still_go_to_the_focused_local_window(self, calls, monkeypatch):
        focus(monkeypatch, EDITOR, under=remmina_rect)
        assert desktop.type_text("hi")["ok"] is True
        assert calls == [("type_text", "hi")]
