"""Suite-wide guards: no test drives the live display or the owner's switch.

The X11 tests fake `x11._run` and assert on xdotool argv, so the persistent
XTest channel is turned off for every test (it would otherwise move the real
pointer on whatever DISPLAY the shell has). The owner-pause files live in a
temp dir so a test can never pause the owner's real desk. The bot-challenge
check and the pointer note are stubbed for the same reason: both would read
the live display; the tests that cover them patch them back in explicitly.

On every OS the real screen, window list, app inventory and synthetic input
are unreachable from the suite: Quartz/AX loaders are short-circuited, helper
subprocesses (`x11._run`, `darwin._run`) return a blocked result, and
desktop-entry directories are empty. A test that needs a backend patches it
in. Do not stub `desktop._focused_window` here — the Linux viewer-guard tests
drive that hook through a fake XTest channel.
"""
from __future__ import annotations

import subprocess

import pytest

from workman import apps, chrome, desktop, handback, owner_pause, resume, x11, xtest
from workman.platform import ax_darwin, darwin


def _blocked_run(cmd, timeout=20, input_text=None, **_kw):
    argv = list(cmd) if not isinstance(cmd, str) else [cmd]
    return subprocess.CompletedProcess(argv, 127, "", "blocked by conftest")


@pytest.fixture(autouse=True)
def _no_live_desktop(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKMAN_XTEST", "0")
    monkeypatch.setattr(x11, "_xt", lambda: None)
    monkeypatch.setattr(x11, "_run", _blocked_run)
    x11._LAST_PLACED["at"] = None
    # Never talk to :0: the xdotool fallback of the viewer guard would otherwise
    # spawn getactivewindow against the live display.
    monkeypatch.setattr(desktop, "_xdotool_focused", lambda: None)
    monkeypatch.setenv("WORKMAN_LEARN_ROOT", str(tmp_path / "learn"))
    monkeypatch.setattr(owner_pause, "_last_mark", {"agent": 0.0, "human": 0.0})
    monkeypatch.setattr(chrome, "challenge_active", lambda: False)
    monkeypatch.setattr(handback, "note_pointer_before", lambda: None)

    monkeypatch.setattr(darwin, "_quartz_mod", None)
    monkeypatch.setattr(darwin, "_quartz_tried", True)
    monkeypatch.setattr(darwin, "_run", _blocked_run)
    darwin._LAST_PLACED["at"] = None
    monkeypatch.setattr(ax_darwin, "_ax", None)
    monkeypatch.setattr(ax_darwin, "_ax_tried", True)

    monkeypatch.setattr(apps, "MAC_APP_DIRS", ())
    monkeypatch.setattr(apps, "DESKTOP_DIRS", ())
    monkeypatch.setattr(apps, "WINDOWS_MENU_DIRS", ())

    handback.reset()
    resume.clear()
    xtest.reset()
    yield
    xtest.reset()
    handback.reset()
    resume.clear()
    x11._LAST_PLACED["at"] = None
    darwin._LAST_PLACED["at"] = None
