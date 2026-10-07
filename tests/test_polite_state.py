"""Persistence/import checks; no display connection or input."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from workman import polite_state


def test_display_alias_and_boot_change_discard_stale_state(monkeypatch):
    with polite_state.locked("unix:99.0") as store:
        store.data["target"] = {"ok": True, "window": 200}
        store.data["snapshot"] = {"pointer": [10, 20]}
        store.write()
    with polite_state.locked(":99") as store:
        assert store.data["target"]["window"] == 200
    monkeypatch.setattr(polite_state, "boot_id", lambda: "a different boot")
    with polite_state.locked(":99") as store:
        assert store.data == {}


def test_corrupt_state_fails_closed_instead_of_overwriting():
    path, _ = polite_state.paths(":99")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{bad json")
    with pytest.raises(ValueError):
        with polite_state.locked(":99"):
            pytest.fail("corrupt snapshot was accepted")
    assert path.read_text() == "{bad json"


def test_non_linux_import_without_fcntl_is_usable():
    root = Path(__file__).resolve().parents[1]
    code = '''import builtins
original = builtins.__import__
def no_fcntl(name, *args, **kwargs):
    if name == 'fcntl':
        raise ImportError('Windows has no fcntl')
    return original(name, *args, **kwargs)
builtins.__import__ = no_fcntl
from workman import polite, desktop
from types import SimpleNamespace
assert not polite.enabled(SimpleNamespace(PLATFORM='Windows'))
'''
    out = subprocess.run([sys.executable, "-c", code], cwd=root, env=os.environ.copy(), capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
