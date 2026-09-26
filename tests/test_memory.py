"""Token-light computer-use memory (working, facts, packed MCP shape)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from workman import cu_memory, cu_skills, episode, memory_graph as mg, working


def test_working_remaining_only(tmp_path):
    out = working.open_task(
        "compute 12*7", items=["open calc", "type 12*7", "screenshot"],
        root=tmp_path)
    assert out["ok"] is True
    assert out["n"] == 3
    assert all(line.startswith("TODO") or line.startswith("task=") for line in out["lines"])
    ticked = working.tick("open calc", root=tmp_path)
    assert ticked["n"] == 2
    assert not any("open calc" in line for line in ticked["lines"] if line.startswith("TODO"))


def test_working_caps_items(tmp_path):
    items = [f"step {i}" for i in range(20)]
    out = working.open_task("big", items=items, root=tmp_path)
    disk = json.loads((tmp_path / "working.json").read_text())
    assert len(disk["items"]) == working.MAX_ITEMS
    assert out["n"] == working.MAX_ITEMS


def test_fact_supersedes_old_value(tmp_path):
    a = mg.assert_fact("HDMI-0", "hz", "50", root=tmp_path)
    assert a["ok"]
    b = mg.assert_fact("HDMI-0", "hz", "60", root=tmp_path)
    assert b["ok"]
    lines = mg.recall_facts("HDMI", root=tmp_path)
    blob = " ".join(lines)
    assert "60" in blob
    assert "50" not in blob


def test_forget_invalidates(tmp_path):
    mg.assert_fact("panel", "mode", "cinema", root=tmp_path)
    gone = mg.invalidate_fact("cinema", root=tmp_path)
    assert gone["ok"]
    assert mg.recall_facts("cinema", root=tmp_path) == []


def test_handle_qwen_aliases(tmp_path):
    cu_memory.handle("working", q="paste a string", items=["focus", "paste"], root=tmp_path)
    done = cu_memory.handle("done", q="focus", root=tmp_path)
    assert done["op"] == "working"
    assert done["n"] == 1
    fact = cu_memory.handle("remember", q="HDMI-0 | hz | 60", root=tmp_path)
    assert fact["ok"] is True
    found = cu_memory.handle("Recall HDMI hz", root=tmp_path)
    assert found["op"] == "recall"
    assert found["n"] >= 1
    assert any("60" in line for line in found["lines"])


def test_packed_reply_stays_small(tmp_path):
    working.open_task("x" * 400, items=["a" * 200] * 8, root=tmp_path)
    row = cu_memory.handle("working", root=tmp_path)
    raw = json.dumps(row, separators=(",", ":"))
    assert len(raw) <= mg.CHAR_BUDGET
    assert set(row) >= {"ok", "op", "n", "lines"}
    assert isinstance(row["lines"], list)


def test_status_is_one_line(tmp_path):
    row = cu_memory.handle("status", root=tmp_path)
    assert row["n"] == 1
    assert "ep=" in row["lines"][0]
    assert "matches" not in row


def test_episode_opens_and_closes_working(tmp_path, monkeypatch):
    monkeypatch.setattr(episode, "LEARN_ROOT", tmp_path)
    episode._EPISODE = None
    for key in ("WORKMAN_EPISODE_UID", "WORKMAN_TASK", "WORKMAN_TASK_ID"):
        monkeypatch.delenv(key, raising=False)
    started = episode.start("hostname", task_id="term-hostname",
                             checklist=["focus term", "run hostname"])
    assert (tmp_path / "working.json").is_file()
    closed = episode.close(outcome="success", judged_by="test")
    assert closed["outcome"] == "success"
    assert not (tmp_path / "working.json").exists()
    assert (tmp_path / "episodes.jsonl").is_file()
    assert started["episode_uid"] == closed["episode_uid"]


def test_skill_teach_graphs(tmp_path, monkeypatch):
    monkeypatch.setattr(cu_skills, "LEARN_ROOT", tmp_path)
    monkeypatch.setattr(cu_skills, "SKILLS", tmp_path / "skills.jsonl")
    monkeypatch.setattr(cu_skills, "FLEET", tmp_path / "fleet")
    taught = cu_skills.teach(
        "paste then return", [{"action": "key", "value": "super+v"}],
        app="terminal", task_id="shortcut-paste")
    assert taught["ok"]
    nodes = (tmp_path / "graph-nodes.jsonl").read_text()
    assert "paste then return" in nodes


def test_mcp_skill_recall_is_lines_only(tmp_path, monkeypatch):
    monkeypatch.setattr(cu_skills, "LEARN_ROOT", tmp_path)
    monkeypatch.setattr(cu_skills, "SKILLS", tmp_path / "skills.jsonl")
    monkeypatch.setattr(cu_skills, "FLEET", tmp_path / "fleet")
    cu_skills.teach("Chrome new tab", [{"action": "key", "value": "super+t"}], app="Chrome")
    packed = cu_memory.from_skills(
        cu_skills.recall("new tab", app="Chrome", compact=True))
    assert packed["lines"]
    assert "matches" not in packed
    assert "steps_v2" not in json.dumps(packed)


def test_brief_stays_under_budget(tmp_path):
    import sys
    here = Path(__file__).resolve()
    candidates = [
        here.parents[1] / "ops" / "computer-use-lab",
        Path.home() / "ops" / "computer-use-lab",
    ]
    lab = next((p for p in candidates if (p / "brief.py").is_file()), None)
    if lab is None:
        pytest.skip("computer-use-lab brief.py not in this checkout")
    sys.path.insert(0, str(lab))
    import brief  # noqa: E402
    journal = tmp_path / "journal.jsonl"
    journal.write_text("\n".join(
        json.dumps({"kind": "action", "tool": f"click{i}"}) for i in range(40)
    ) + "\n")
    known = tmp_path / "known.json"
    known.write_text(json.dumps([{"title": "paste", "steps": "super+v"}]))
    task = {"task_id": "shortcut-paste", "task": "paste a string " * 40,
            "post": "visible", "app": "gedit"}
    text = brief.build(task, known_path=known, journal=journal)
    assert len(text) <= brief.CHAR_BUDGET
    v = brief.verify_text(task, {"finished": True, "title": "paste", "proof": "ok",
                                  "steps_v2": [{"action": "key", "value": "super+v"}]})
    assert len(v) <= 800
    assert "task_id=shortcut-paste" in v
