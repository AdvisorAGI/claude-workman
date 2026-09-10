"""Learned shortcuts store + harvest chord parsing."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from workman import learned_shortcuts as ls
from workman import shortcut_harvest as harvest
from workman import shortcuts
from workman import cu_skills


@pytest.fixture
def learn_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ls, "LEARN_ROOT", tmp_path)
    monkeypatch.setattr(ls, "SHORTCUTS", tmp_path / "shortcuts.jsonl")
    monkeypatch.setattr(ls, "META", tmp_path / "shortcuts-meta.json")
    monkeypatch.setattr(ls, "FLEET", tmp_path / "fleet")
    monkeypatch.setattr(cu_skills, "LEARN_ROOT", tmp_path)
    monkeypatch.setattr(cu_skills, "SKILLS", tmp_path / "skills.jsonl")
    monkeypatch.setattr(cu_skills, "FLEET", tmp_path / "fleet")
    return tmp_path


class TestChordFromAx:
    def test_command_letter(self):
        # NoCommand unset => Command is implied.
        assert harvest.chord_from_ax("T", 0, None) == "super+t"

    def test_shift_command(self):
        assert harvest.chord_from_ax("T", harvest._AX_SHIFT, None) == "super+shift+t"

    def test_no_command_control(self):
        mods = harvest._AX_NO_COMMAND | harvest._AX_CONTROL
        assert harvest.chord_from_ax("g", mods, None) == "ctrl+g"

    def test_virtual_return(self):
        assert harvest.chord_from_ax(None, 0, 36) == "super+Return"

    def test_empty_is_none(self):
        assert harvest.chord_from_ax(None, 0, None) is None


class TestLearnedStore:
    def test_upsert_and_lookup(self, learn_dir):
        out = ls.upsert_many([{
            "app": "Notion",
            "platform": "darwin",
            "action": "new_page",
            "label": "New Page",
            "keys": "super+n",
            "menu_path": ["File", "New Page"],
        }])
        assert out["upserted"] == 1
        hit = ls.lookup("new_page", app="Notion", platform="darwin")
        assert hit is not None
        assert hit["keys"] == "super+n"
        assert ls.needs_harvest("Notion", "darwin") is True
        ls.mark_harvested("Notion", "darwin", 1)
        assert ls.needs_harvest("Notion", "darwin") is False

    def test_resolve_uses_learned(self, learn_dir):
        ls.upsert_many([{
            "app": "WeirdApp",
            "app_key": "weirdapp",
            "platform": shortcuts.detect_platform(),
            "action": "open_palette",
            "label": "Open Palette",
            "keys": "super+k",
        }])
        ans = shortcuts.resolve("open_palette", app="WeirdApp")
        assert ans["ok"] is True
        assert ans["keys"] == "super+k"
        assert ans["source"] == "learned"


class TestCuSkills:
    def test_teach_and_recall(self, learn_dir):
        taught = cu_skills.teach(
            "Chrome new tab then address bar",
            [
                {"action": "key", "value": "super+t"},
                {"action": "key", "value": "super+l"},
                {"action": "paste", "value": "<url>"},
            ],
            app="Chrome",
            source="lab",
        )
        assert taught["ok"] is True
        found = cu_skills.recall("new tab address", app="Chrome")
        assert found["matches"]
        assert "super+t" in found["matches"][0]["steps"]

    def test_teach_keeps_task_id(self, learn_dir):
        taught = cu_skills.teach(
            "Calculator 12 times 7",
            [{"action": "key", "value": "super+space"}],
            app="Calculator",
            task_id="calc-12x7",
        )
        assert taught["ok"] is True
        row = json.loads(Path(taught["path"]).read_text().splitlines()[0])
        assert row["task_id"] == "calc-12x7"

    def test_redacts_secrets(self, learn_dir):
        taught = cu_skills.teach(
            "login",
            [{"action": "type", "value": "password=hunter2"}],
            app="x",
        )
        assert taught["ok"] is True
        rows = json.loads(Path(taught["path"]).read_text().splitlines()[0])
        assert "hunter2" not in rows["steps"]
