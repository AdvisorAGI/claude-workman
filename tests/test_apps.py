"""Application launching — desktop-entry parsing and the exec guard."""
from __future__ import annotations

from workman import apps

ENTRY = """[Desktop Entry]
Version=1.0
Type=Application
Name=Text Editor
Exec=gnome-text-editor %U
Icon=org.gnome.TextEditor
Categories=Utility;

[Desktop Action new-window]
Name=New Window
Exec=gnome-text-editor --new-window
"""


class TestDesktopEntryParsing:
    def test_strips_launcher_field_codes(self, tmp_path):
        path = tmp_path / "editor.desktop"
        path.write_text(ENTRY)
        fields = apps._parse_desktop(str(path))
        # %U would otherwise be passed to the program as a literal argument.
        assert fields["Exec"] == "gnome-text-editor"
        assert fields["Name"] == "Text Editor"

    def test_ignores_keys_from_other_sections(self, tmp_path):
        path = tmp_path / "editor.desktop"
        path.write_text(ENTRY)
        fields = apps._parse_desktop(str(path))
        assert "new-window" not in fields.get("Exec", "")

    def test_missing_file_yields_nothing(self, tmp_path):
        assert apps._parse_desktop(str(tmp_path / "absent.desktop")) == {}

    def test_hidden_entries_are_not_listed(self, tmp_path, monkeypatch):
        (tmp_path / "hidden.desktop").write_text(
            "[Desktop Entry]\nName=Hidden\nExec=nope\nNoDisplay=true\n"
        )
        (tmp_path / "shown.desktop").write_text("[Desktop Entry]\nName=Shown\nExec=yes\n")
        monkeypatch.setattr(apps, "DESKTOP_DIRS", (str(tmp_path),))
        names = [e["name"] for e in apps.list_launchable()]
        assert names == ["Shown"]

    def test_query_filters_by_name(self, tmp_path, monkeypatch):
        (tmp_path / "a.desktop").write_text("[Desktop Entry]\nName=Calculator\nExec=galculator\n")
        (tmp_path / "b.desktop").write_text("[Desktop Entry]\nName=Browser\nExec=firefox\n")
        monkeypatch.setattr(apps, "DESKTOP_DIRS", (str(tmp_path),))
        assert [e["name"] for e in apps.list_launchable(query="calc")] == ["Calculator"]


class TestLaunchGuards:
    def test_launch_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("WORKMAN_ALLOW_LAUNCH", "0")
        result = apps.launch("xterm")
        assert result["ok"] is False
        assert "disabled" in result["error"]

    def test_missing_binary_is_reported_not_raised(self, monkeypatch):
        monkeypatch.delenv("WORKMAN_ALLOW_LAUNCH", raising=False)
        monkeypatch.setattr(apps.shutil, "which", lambda binary: None)
        result = apps.launch("definitely-not-installed")
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_empty_command_is_rejected(self, monkeypatch):
        monkeypatch.delenv("WORKMAN_ALLOW_LAUNCH", raising=False)
        assert apps.launch("   ")["ok"] is False

    def test_launch_is_detached_from_the_server(self, monkeypatch):
        """A child that dies with the server would take the user's app with it."""
        monkeypatch.delenv("WORKMAN_ALLOW_LAUNCH", raising=False)
        monkeypatch.setattr(apps.shutil, "which", lambda binary: "/usr/bin/xterm")
        monkeypatch.setattr(apps.x11, "list_windows", lambda: [])
        captured = {}

        class FakeProc:
            pid = 4321

        def fake_popen(argv, **kwargs):
            captured.update(argv=argv, kwargs=kwargs)
            return FakeProc()

        monkeypatch.setattr(apps.subprocess, "Popen", fake_popen)
        result = apps.launch("xterm -e htop")
        assert result["ok"] is True and result["pid"] == 4321
        assert captured["argv"] == ["xterm", "-e", "htop"]
        assert captured["kwargs"]["start_new_session"] is True

    def test_explicit_args_are_not_word_split(self, monkeypatch):
        monkeypatch.delenv("WORKMAN_ALLOW_LAUNCH", raising=False)
        monkeypatch.setattr(apps.shutil, "which", lambda binary: "/usr/bin/app")
        monkeypatch.setattr(apps.x11, "list_windows", lambda: [])
        captured = {}
        monkeypatch.setattr(apps.subprocess, "Popen",
                            lambda argv, **k: captured.update(argv=argv) or type("P", (), {"pid": 1}))
        # A filename with spaces must survive as one argument.
        apps.launch("app", args=["my file.txt"])
        assert captured["argv"] == ["app", "my file.txt"]


class TestTerminate:
    def test_missing_process_is_reported(self, monkeypatch):
        def gone(pid, sig):
            raise ProcessLookupError

        monkeypatch.setattr(apps.os, "kill", gone)
        assert apps.terminate(999999)["ok"] is False

    def test_permission_error_is_reported(self, monkeypatch):
        def denied(pid, sig):
            raise PermissionError

        monkeypatch.setattr(apps.os, "kill", denied)
        result = apps.terminate(1)
        assert result["ok"] is False and "permitted" in result["error"]

    def test_force_uses_sigkill_and_does_not_wait(self, monkeypatch):
        sent = {}
        monkeypatch.setattr(apps.os, "kill", lambda pid, sig: sent.update(pid=pid, sig=sig))
        result = apps.terminate(1234, force=True)
        assert result["signal"] == "SIGKILL"
        assert sent["sig"] == apps.signal.SIGKILL
