"""F2: type/press/paste refuse when GNOME Shell has focus; degrade otherwise."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from workman import a11y, desktop, shell_guard
from workman.shell_guard import _probe_atspi_shell as _real_probe_atspi_shell
from workman.shell_guard import _probe_overview as _real_probe_overview
from workman.shell_guard import _probe_x_active as _real_probe_x_active
from workman.shell_guard import shell_has_focus as _real_shell_has_focus

# Live :0 hit, 2026-09-12: Remmina focused, overview closed, AT-SPI still
# naming a stale gnome-shell window (GTK apps often never report focus).
_STALE_SHELL = {
    "ok": True,
    "element": {
        "app": "gnome-shell", "role": "window", "name": "",
        "x": 0, "y": 55, "w": 99, "h": 56,
    },
}
_REMMINA = {
    "ok": True,
    "class": "org.remmina.Remmina",
    "instance": "org.remmina.Remmina",
    "name": "Mac mini screen (via ssh tunnel)",
}
_NO_ACTIVE = {"ok": False, "error": "no active window"}
_X_SHELL = {"ok": True, "class": "gnome-shell", "instance": "gnome-shell", "name": ""}
# type_text also runs the remote-viewer guard, which refuses Remmina. A
# normal app proves the shell-guard path types when AT-SPI is stale.
_ZENITY = {
    "ok": True, "class": "zenity", "instance": "zenity",
    "name": "workman-typing-check",
}


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple] = []
    fake = SimpleNamespace(
        PLATFORM="fake",
        type_text=lambda text, delay_ms=40, dwell_ms=0:
            seen.append(("type_text", text)) or {"ok": True, "typed_len": len(text)},
        press_key=lambda key, hold_ms=0: seen.append(("press_key", key)) or {"ok": True},
        key_down=lambda key: seen.append(("key_down", key)) or {"ok": True},
        key_up=lambda key: seen.append(("key_up", key)) or {"ok": True},
        mouse_up=lambda button=1, x=None, y=None: seen.append(("mouse_up", button)) or {"ok": True},
    )
    monkeypatch.setattr(desktop, "active", lambda: fake)
    shell_guard.reset()
    return seen


def test_overview_active_refuses_type_press_and_paste(calls, monkeypatch):
    monkeypatch.setattr(shell_guard, "shell_has_focus", lambda: True)
    for out in (desktop.type_text("workman ok"), desktop.press_key("Return"),
                desktop.press_key("ctrl+v"), desktop.key_down("a")):
        assert out["ok"] is False
        assert out["error"] == "shell_has_focus"
        assert out["action"] in {"type_text", "press_key", "key_down"}
    assert calls == []


def test_gnome_shell_atspi_focus_refuses(calls, monkeypatch):
    monkeypatch.setattr(shell_guard, "_probe_overview", lambda: False)
    monkeypatch.setattr(shell_guard, "_probe_atspi_shell", lambda: True)
    shell_guard.reset()
    out = desktop.type_text("hi")
    assert out["error"] == "shell_has_focus" and calls == []


def test_missing_bus_degrades_and_types(calls, monkeypatch):
    monkeypatch.setattr(shell_guard, "_probe_overview", lambda: None)
    monkeypatch.setattr(shell_guard, "_probe_atspi_shell", lambda: None)
    shell_guard.reset()
    out = desktop.type_text("hi")
    assert out["ok"] is True and calls == [("type_text", "hi")]


def test_non_gnome_desktop_does_not_call_gdbus(monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setattr(shell_guard, "sys", SimpleNamespace(platform="linux"))
    ran = {"n": 0}

    def boom(*a, **k):
        ran["n"] += 1
        raise AssertionError("gdbus must not run on KDE")

    monkeypatch.setattr(shell_guard.subprocess, "run", boom)
    shell_guard.reset()
    assert _real_probe_overview() is None
    assert ran["n"] == 0


def test_parse_overview_true_false_and_empty():
    assert shell_guard._parse_overview("(<true>,)") is True
    assert shell_guard._parse_overview("(<false>,)") is False
    assert shell_guard._parse_overview("") is None
    assert shell_guard._parse_overview("nope") is None


def test_releases_are_never_refused(calls, monkeypatch):
    monkeypatch.setattr(shell_guard, "shell_has_focus", lambda: True)
    assert desktop.key_up("a")["ok"] is True
    assert desktop.mouse_up()["ok"] is True
    assert calls == [("key_up", "a"), ("mouse_up", 1)]


def test_probe_cache_avoids_a_second_gdbus(monkeypatch):
    n = {"n": 0}

    def overview():
        n["n"] += 1
        return True

    monkeypatch.setattr(shell_guard, "_probe_overview", overview)
    monkeypatch.setattr(shell_guard, "_probe_atspi_shell", lambda: None)
    monkeypatch.setattr(shell_guard, "shell_has_focus", _real_shell_has_focus)
    shell_guard.reset()
    assert _real_shell_has_focus() is True
    assert _real_shell_has_focus() is True
    assert n["n"] == 1


@pytest.fixture
def real_atspi_x(monkeypatch):
    """Undo conftest stubs so the real AT-SPI/X mapping runs.

    Force the Linux platform so the same proof runs on the Air: production
    `_probe_atspi_shell` is a no-op off Linux (no per-keystroke AX query).
    """
    monkeypatch.setattr(shell_guard, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(shell_guard, "_probe_atspi_shell", _real_probe_atspi_shell)
    monkeypatch.setattr(shell_guard, "_probe_x_active", _real_probe_x_active)
    monkeypatch.setattr(shell_guard, "shell_has_focus", _real_shell_has_focus)
    shell_guard.reset()


def _stub_focus(monkeypatch, element, xwin):
    monkeypatch.setattr(a11y, "focused_element", lambda: element)
    monkeypatch.setattr(desktop, "_focused_window", lambda: xwin)


def test_stale_shell_window_does_not_refuse_when_x_is_an_app(
        calls, monkeypatch, real_atspi_x):
    monkeypatch.setattr(shell_guard, "_probe_overview", lambda: False)
    _stub_focus(monkeypatch, _STALE_SHELL, _REMMINA)
    shell_guard.reset()
    assert _real_probe_atspi_shell() is False
    assert _real_shell_has_focus() is False
    shell_guard.reset()
    _stub_focus(monkeypatch, _STALE_SHELL, _ZENITY)
    out = desktop.type_text("hi")
    assert out["ok"] is True and calls == [("type_text", "hi")]


def test_stale_shell_window_refuses_when_x_has_no_active_window(
        calls, monkeypatch, real_atspi_x):
    monkeypatch.setattr(shell_guard, "_probe_overview", lambda: False)
    _stub_focus(monkeypatch, _STALE_SHELL, _NO_ACTIVE)
    shell_guard.reset()
    assert _real_probe_atspi_shell() is True
    out = desktop.type_text("hi")
    assert out["error"] == "shell_has_focus" and calls == []


def test_stale_shell_window_degrades_when_x_is_unreadable(
        monkeypatch, real_atspi_x):
    monkeypatch.setattr(shell_guard, "_probe_overview", lambda: False)
    _stub_focus(monkeypatch, _STALE_SHELL, desktop.FOCUS_UNREADABLE)
    shell_guard.reset()
    assert _real_probe_atspi_shell() is None
    assert _real_shell_has_focus() is False
    shell_guard.reset()
    _stub_focus(monkeypatch, _STALE_SHELL, None)
    assert _real_probe_atspi_shell() is None
    assert _real_shell_has_focus() is False


def test_editable_shell_entry_refuses_without_an_x_read(
        calls, monkeypatch, real_atspi_x):
    n = {"n": 0}

    def boom():
        n["n"] += 1
        raise AssertionError("X must not be read for an editable shell entry")

    monkeypatch.setattr(shell_guard, "_probe_overview", lambda: False)
    monkeypatch.setattr(a11y, "focused_element", lambda: {
        "ok": True,
        "element": {
            "app": "gnome-shell", "role": "text", "name": "Type to search",
            "x": 10, "y": 10, "w": 200, "h": 24,
        },
    })
    monkeypatch.setattr(desktop, "_focused_window", boom)
    shell_guard.reset()
    assert _real_probe_atspi_shell() is True
    assert n["n"] == 0
    monkeypatch.setattr(desktop, "_focused_window", lambda: _ZENITY)
    shell_guard.reset()
    out = desktop.type_text("hi")
    assert out["error"] == "shell_has_focus" and calls == []


def test_stale_shell_window_refuses_when_x_class_is_gnome_shell(
        monkeypatch, real_atspi_x):
    monkeypatch.setattr(shell_guard, "_probe_overview", lambda: False)
    _stub_focus(monkeypatch, _STALE_SHELL, _X_SHELL)
    shell_guard.reset()
    assert _real_probe_atspi_shell() is True
    assert _real_shell_has_focus() is True


def test_stale_shell_window_does_not_refuse_when_overview_unknown_and_x_is_app(
        monkeypatch, real_atspi_x):
    monkeypatch.setattr(shell_guard, "_probe_overview", lambda: None)
    _stub_focus(monkeypatch, _STALE_SHELL, _REMMINA)
    shell_guard.reset()
    assert _real_probe_atspi_shell() is False
    assert _real_shell_has_focus() is False


def test_atspi_probe_is_linux_only(monkeypatch):
    n = {"n": 0}

    def boom():
        n["n"] += 1
        raise AssertionError("a11y must not run off linux")

    monkeypatch.setattr(shell_guard, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(shell_guard, "_probe_atspi_shell", _real_probe_atspi_shell)
    monkeypatch.setattr(a11y, "focused_element", boom)
    assert _real_probe_atspi_shell() is None
    assert n["n"] == 0


def test_cache_one_atspi_and_at_most_one_x_read(monkeypatch, real_atspi_x):
    a11y_n = {"n": 0}
    x_n = {"n": 0}

    def focused():
        a11y_n["n"] += 1
        return _STALE_SHELL

    def xwin():
        x_n["n"] += 1
        return _REMMINA

    monkeypatch.setattr(shell_guard, "_probe_overview", lambda: False)
    monkeypatch.setattr(a11y, "focused_element", focused)
    monkeypatch.setattr(desktop, "_focused_window", xwin)
    shell_guard.reset()
    assert _real_shell_has_focus() is False
    assert _real_shell_has_focus() is False
    assert a11y_n["n"] == 1
    assert x_n["n"] == 1


def test_probe_x_active_maps_app_none_shell_and_unreadable(
        monkeypatch, real_atspi_x):
    monkeypatch.setattr(desktop, "_focused_window", lambda: _REMMINA)
    assert _real_probe_x_active() == "app"
    monkeypatch.setattr(desktop, "_focused_window", lambda: _NO_ACTIVE)
    assert _real_probe_x_active() == "none"
    monkeypatch.setattr(desktop, "_focused_window", lambda: _X_SHELL)
    assert _real_probe_x_active() == "shell"
    monkeypatch.setattr(desktop, "_focused_window", lambda: desktop.FOCUS_UNREADABLE)
    assert _real_probe_x_active() is None
    monkeypatch.setattr(desktop, "_focused_window", lambda: None)
    assert _real_probe_x_active() is None
    monkeypatch.setattr(desktop, "_focused_window",
                        lambda: (_ for _ in ()).throw(RuntimeError("x down")))
    assert _real_probe_x_active() is None
