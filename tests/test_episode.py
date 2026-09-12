"""Computer-use episode store (file-backed across processes)."""
from __future__ import annotations

import json

import pytest

from workman import episode as ep


@pytest.fixture
def ep_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ep, "LEARN_ROOT", tmp_path)
    ep._EPISODE = None
    for key in ("WORKMAN_EPISODE_UID", "WORKMAN_TASK", "WORKMAN_TASK_ID"):
        monkeypatch.delenv(key, raising=False)
    yield tmp_path
    ep._EPISODE = None


def test_start_close_same_process(ep_dir):
    started = ep.start("compute 12*7", app="Calculator", task_id="calc-12x7")
    assert started["episode_uid"]
    assert (ep_dir / "active-episode.json").is_file()
    closed = ep.close(outcome="success", judged_by="test")
    assert closed["outcome"] == "success"
    assert closed["task_id"] == "calc-12x7"
    assert not (ep_dir / "active-episode.json").exists()
    rows = [json.loads(line) for line in (ep_dir / "episodes.jsonl").read_text().splitlines()]
    assert rows[-1]["episode_uid"] == started["episode_uid"]


def test_close_from_other_process_uses_file(ep_dir):
    started = ep.start("read frontmost title", task_id="frontmost-title")
    uid = started["episode_uid"]
    ep._EPISODE = None
    closed = ep.close(outcome="success", judged_by="other-proc")
    assert closed["episode_uid"] == uid
    assert closed["outcome"] == "success"
    assert ep.current() is None


def test_stamp_uses_active_file(ep_dir):
    ep.start("paste a string", task_id="shortcut-paste")
    ep._EPISODE = None
    stamped = ep.stamp({"kind": "action", "tool": "screenshot"})
    assert stamped["episode_uid"]
    assert stamped["task_id"] == "shortcut-paste"


def test_stamp_falls_back_to_env(ep_dir, monkeypatch):
    monkeypatch.setenv("WORKMAN_EPISODE_UID", "env-uid")
    monkeypatch.setenv("WORKMAN_TASK", "from-env")
    monkeypatch.setenv("WORKMAN_TASK_ID", "term-hostname")
    stamped = ep.stamp({"kind": "action", "tool": "click"})
    assert stamped["episode_uid"] == "env-uid"
    assert stamped["task_id"] == "term-hostname"


def test_close_without_episode(ep_dir):
    assert ep.close()["error"] == "no episode"
