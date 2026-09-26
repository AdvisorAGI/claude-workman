"""F4: AT-SPI tools return an error dict when gi is missing, never raise."""
from __future__ import annotations

import sys

from workman import a11y, atspi, server


def _gi_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "gi", None)
    monkeypatch.setattr(atspi, "_load_gi", lambda: (_ for _ in ()).throw(
        atspi.AtspiUnavailable("gi not importable")))
    # a11y routes to ax_darwin on macOS; these tests prove the Linux AT-SPI
    # error-dict path, so force that backend on every OS (same pattern as
    # test_apps monkeypatching IS_LINUX / test_shell_guard forcing platform).
    monkeypatch.setattr(a11y, "_impl", lambda: atspi)


def test_focused_element_is_an_error_dict(monkeypatch):
    _gi_missing(monkeypatch)
    out = atspi.focused_element()
    assert isinstance(out, dict) and out["ok"] is False
    assert "gi not importable" in out["error"]
    assert sys.executable in out["error"]


def test_a11y_focused_element_does_not_raise(monkeypatch):
    _gi_missing(monkeypatch)
    out = a11y.focused_element()
    assert out["ok"] is False and "gi not importable" in out["error"]


def test_tree_and_accessibility_tree_do_not_raise(monkeypatch):
    _gi_missing(monkeypatch)
    tree = atspi.tree()
    assert isinstance(tree, list) and tree[0]["ok"] is False
    assert "gi not importable" in tree[0]["error"]
    wrapped = a11y.tree()
    assert wrapped[0]["ok"] is False
    listed = server.accessibility_tree()
    assert listed[0]["ok"] is False


def test_system_dist_packages_are_appended_not_prepended(monkeypatch, tmp_path):
    (tmp_path / "gi").mkdir()
    monkeypatch.setattr(atspi, "_SYSTEM_DIST", str(tmp_path))
    saved = sys.modules.pop("gi", None)
    import builtins
    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "gi" or (isinstance(name, str) and name.startswith("gi.")):
            raise ImportError("no gi")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    try:
        try:
            atspi._load_gi()
        except atspi.AtspiUnavailable:
            pass
        else:
            raise AssertionError("expected AtspiUnavailable")
        assert str(tmp_path) in sys.path
        assert sys.path[0] != str(tmp_path)
        assert sys.path[-1] == str(tmp_path)
    finally:
        sys.modules.pop("gi", None)
        if saved is not None:
            sys.modules["gi"] = saved
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))
