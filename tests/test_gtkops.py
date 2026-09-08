"""GTK helper — the subprocess boundary and its failure modes.

The GTK ops themselves need a live display; what is worth testing headless is
that a broken helper produces a clean error instead of an exception or, worse,
a false success.
"""
from __future__ import annotations

import json
import subprocess

from workman import gtkops


class FakeCompleted:
    def __init__(self, stdout="", stderr=""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = 0


class TestCall:
    def test_explicit_system_gtk_interpreter_keeps_venv_unchanged(self, monkeypatch):
        captured = {}
        monkeypatch.setenv("WORKMAN_GTK_PYTHON", "/usr/bin/python3")
        monkeypatch.setattr(gtkops.subprocess, "run", lambda cmd, **k:
                            captured.update(cmd=cmd) or FakeCompleted('{"ok":true}'))
        assert gtkops.call("windows")["ok"]
        assert captured["cmd"][0] == "/usr/bin/python3"

    def test_parses_helper_json(self, monkeypatch):
        monkeypatch.setattr(gtkops.subprocess, "run",
                            lambda *a, **k: FakeCompleted(json.dumps({"ok": True, "n": 2})))
        assert gtkops.call("windows") == {"ok": True, "n": 2}

    def test_takes_the_last_line_so_gtk_warnings_do_not_break_parsing(self, monkeypatch):
        noisy = "Gtk-WARNING **: cannot open display\n" + json.dumps({"ok": True})
        monkeypatch.setattr(gtkops.subprocess, "run", lambda *a, **k: FakeCompleted(noisy))
        assert gtkops.call("windows")["ok"] is True

    def test_timeout_is_reported_as_failure(self, monkeypatch):
        def timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd="x", timeout=25)

        monkeypatch.setattr(gtkops.subprocess, "run", timeout)
        result = gtkops.call("windows")
        assert result["ok"] is False and "timed out" in result["error"]

    def test_empty_output_surfaces_stderr(self, monkeypatch):
        monkeypatch.setattr(gtkops.subprocess, "run",
                            lambda *a, **k: FakeCompleted("", "ImportError: no Wnck"))
        result = gtkops.call("windows")
        assert result["ok"] is False and "Wnck" in result["error"]

    def test_unparseable_output_is_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(gtkops.subprocess, "run",
                            lambda *a, **k: FakeCompleted("not json at all"))
        result = gtkops.call("windows")
        assert result["ok"] is False and "unparseable" in result["error"]

    def test_package_stays_importable_in_the_child(self, monkeypatch):
        """The helper is spawned as `python -m workman.gtkops`; without this the
        server only works when its cwd happens to be the repo."""
        captured = {}
        monkeypatch.setattr(gtkops.subprocess, "run",
                            lambda cmd, **k: captured.update(cmd=cmd, env=k.get("env")) or
                            FakeCompleted(json.dumps({"ok": True})))
        gtkops.call("windows")
        assert captured["cmd"][1:3] == ["-m", "workman.gtkops"]
        assert "workman" in captured["env"]["PYTHONPATH"]


class TestCliDispatch:
    def test_unknown_op_lists_the_real_ones(self, capsys):
        code = gtkops.main(["not-an-op"])
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert code == 2
        assert payload["ok"] is False
        assert "clipboard_get" in payload["ops"]

    def test_bad_json_arguments_are_rejected(self, capsys):
        code = gtkops.main(["windows", "{not json"])
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert code == 2 and "bad json" in payload["error"]

    def test_op_exception_becomes_json_not_traceback(self, capsys, monkeypatch):
        def explode(**kwargs):
            raise RuntimeError("no display")

        monkeypatch.setitem(gtkops._OPS, "windows", explode)
        code = gtkops.main(["windows", "{}"])
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert code == 1
        assert payload["ok"] is False and "no display" in payload["error"]

    def test_exit_code_reflects_op_result(self, capsys, monkeypatch):
        monkeypatch.setitem(gtkops._OPS, "windows", lambda **k: {"ok": True})
        assert gtkops.main(["windows", "{}"]) == 0
        capsys.readouterr()


class TestClipboardSemantics:
    def test_primary_write_is_refused_rather_than_silently_lost(self):
        """No clipboard manager persists PRIMARY, so a write could only ever
        look like it worked."""
        result = gtkops.op_clipboard_set(text="x", selection="primary")
        assert result["ok"] is False
        assert "PRIMARY" in result["error"]


class TestWindowActionValidation:
    def test_unknown_action_is_rejected_with_the_valid_set(self):
        result = gtkops.op_window_action(query="anything", action="explode")
        assert result["ok"] is False
        assert "maximize" in result["actions"]
