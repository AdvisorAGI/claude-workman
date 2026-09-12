"""Application launching — desktop-entry parsing and the exec guard."""
from __future__ import annotations

import pytest

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
        monkeypatch.setattr(apps, "IS_MAC", False)
        monkeypatch.setattr(apps, "IS_WINDOWS", False)
        monkeypatch.setattr(apps, "DESKTOP_DIRS", (str(tmp_path),))
        names = [e["name"] for e in apps.list_launchable()]
        assert names == ["Shown"]

    def test_query_filters_by_name(self, tmp_path, monkeypatch):
        (tmp_path / "a.desktop").write_text("[Desktop Entry]\nName=Calculator\nExec=galculator\n")
        (tmp_path / "b.desktop").write_text("[Desktop Entry]\nName=Browser\nExec=firefox\n")
        monkeypatch.setattr(apps, "IS_MAC", False)
        monkeypatch.setattr(apps, "IS_WINDOWS", False)
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
        monkeypatch.setattr(apps.desktop, "list_windows", lambda: [])
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

    def test_wait_for_window_skips_a_1x1_leader(self, monkeypatch):
        monkeypatch.delenv("WORKMAN_ALLOW_LAUNCH", raising=False)
        monkeypatch.setattr(apps.shutil, "which", lambda binary: "/usr/bin/zenity")
        frames = [
            [{"id": "0x1", "name": "workman-typing-check", "w": 1, "h": 1}],
            [{"id": "0x1", "name": "workman-typing-check", "w": 1, "h": 1},
             {"id": "0x2", "name": "workman-typing-check", "w": 360, "h": 140}],
        ]
        monkeypatch.setattr(apps.desktop, "list_windows",
                            lambda: list(frames[0]))
        monkeypatch.setattr(apps.time, "sleep",
                            lambda s: frames.pop(0) if len(frames) > 1 else None)
        monkeypatch.setattr(apps.subprocess, "Popen",
                            lambda argv, **k: type("P", (), {"pid": 77}))
        result = apps.launch("zenity --entry", wait_for_window=2)
        assert result["ok"] is True
        assert result["window"]["id"] == "0x2"
        assert int(result["window"]["w"]) >= 20

    def test_explicit_args_are_not_word_split(self, monkeypatch):
        monkeypatch.delenv("WORKMAN_ALLOW_LAUNCH", raising=False)
        monkeypatch.setattr(apps.shutil, "which", lambda binary: "/usr/bin/app")
        monkeypatch.setattr(apps.desktop, "list_windows", lambda: [])
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


class TestMacApps:
    """macOS bundle listing and launch. Directories are fakes under tmp_path."""

    @pytest.fixture
    def mac_apps(self, tmp_path, monkeypatch):
        monkeypatch.setattr(apps, "IS_MAC", True)
        monkeypatch.setattr(apps, "IS_WINDOWS", False)
        monkeypatch.setattr(apps, "IS_LINUX", False)
        monkeypatch.setattr(apps, "MAC_APP_DIRS", (str(tmp_path),))
        return tmp_path

    def test_hidden_bundles_are_not_listed(self, mac_apps):
        (mac_apps / "Shown.app").mkdir()
        (mac_apps / ".Karabiner-VirtualHIDDevice-Manager.app").mkdir()
        names = [e["name"] for e in apps.list_launchable()]
        assert names == ["Shown"]

    def test_nested_utilities_are_listed(self, mac_apps):
        (mac_apps / "Utilities" / "Terminal.app").mkdir(parents=True)
        (mac_apps / "Safari.app").mkdir()
        names = [e["name"] for e in apps.list_launchable()]
        assert names == ["Safari", "Terminal"]

    def test_query_is_case_insensitive(self, mac_apps):
        (mac_apps / "TextEdit.app").mkdir()
        assert [e["name"] for e in apps.list_launchable(query="text")] == ["TextEdit"]

    def test_missing_bundle_is_reported_not_opened(self, mac_apps, monkeypatch):
        monkeypatch.setattr(apps.shutil, "which", lambda binary: None)
        started = []
        monkeypatch.setattr(apps.subprocess, "Popen", lambda *a, **k: started.append(a) or None)
        result = apps.launch("definitely-not-installed")
        assert result["ok"] is False
        assert "not found" in result["error"]
        assert started == []

    def test_installed_bundle_uses_open_dash_a(self, mac_apps, monkeypatch):
        (mac_apps / "TextEdit.app").mkdir()
        monkeypatch.setattr(apps.shutil, "which", lambda binary: None)
        monkeypatch.setattr(apps.desktop, "list_windows", lambda: [])
        captured = {}

        class FakeProc:
            pid = 7

        monkeypatch.setattr(apps.subprocess, "Popen",
                            lambda argv, **k: captured.update(argv=argv, kwargs=k) or FakeProc())
        result = apps.launch("textedit")
        assert result["ok"] is True and result["pid"] == 7
        assert captured["argv"][:3] == ["open", "-a", "textedit"]
        assert captured["kwargs"]["start_new_session"] is True

    def test_absolute_app_path_is_accepted(self, mac_apps, monkeypatch):
        bundle = mac_apps / "Safari.app"
        bundle.mkdir()
        monkeypatch.setattr(apps.shutil, "which", lambda binary: None)
        monkeypatch.setattr(apps.desktop, "list_windows", lambda: [])
        captured = {}
        monkeypatch.setattr(apps.subprocess, "Popen",
                            lambda argv, **k: captured.update(argv=argv) or type("P", (), {"pid": 1})())
        assert apps.launch(str(bundle))["ok"] is True
        assert captured["argv"][:3] == ["open", "-a", str(bundle)]
