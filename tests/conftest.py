"""Suite-wide guards: no test drives the live display or the owner's switch.

The X11 tests fake `x11._run` and assert on xdotool argv, so the persistent
XTest channel is turned off for every test (it would otherwise move the real
pointer on whatever DISPLAY the shell has). The owner-pause files live in a
temp dir so a test can never pause the owner's real desk. The bot-challenge
check and the pointer note are stubbed for the same reason: both would read
the live display; the tests that cover them patch them back in explicitly.
"""
from __future__ import annotations

import pytest

from workman import chrome, desktop, handback, owner_pause, resume, x11, xtest


@pytest.fixture(autouse=True)
def _no_live_desktop(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKMAN_XTEST", "0")
    monkeypatch.setattr(x11, "_xt", lambda: None)
    # Never talk to :0: the xdotool fallback of the viewer guard would otherwise
    # spawn getactivewindow against the live display.
    monkeypatch.setattr(desktop, "_xdotool_focused", lambda: None)
    monkeypatch.setenv("WORKMAN_LEARN_ROOT", str(tmp_path / "learn"))
    monkeypatch.setattr(owner_pause, "_last_mark", {"agent": 0.0, "human": 0.0})
    monkeypatch.setattr(chrome, "challenge_active", lambda: False)
    monkeypatch.setattr(handback, "note_pointer_before", lambda: None)
    handback.reset()
    resume.clear()
    xtest.reset()
    yield
    xtest.reset()
    handback.reset()
    resume.clear()
