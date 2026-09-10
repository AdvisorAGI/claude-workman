"""Daily tuner: skip when nothing is left to learn."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

LAB = Path(__file__).resolve().parents[1] / "ops" / "computer-use-lab"
import sys
sys.path.insert(0, str(LAB))

import pick_task  # noqa: E402


TASKS = [
    {"task_id": "frontmost-title", "task": "read title"},
    {"task_id": "calc-12x7", "task": "12*7"},
    {"task_id": "term-hostname", "task": "hostname"},
]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_rotation_is_date_stable():
    a = pick_task.pick(TASKS, "2026-09-10")
    b = pick_task.pick(TASKS, "2026-09-10")
    assert a == b


def test_skip_when_everything_is_known(tmp_path):
    _write_jsonl(tmp_path / "skills.jsonl", [
        {"kind": "skill", "task_id": t["task_id"], "title": t["task_id"]}
        for t in TASKS
    ])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_jsonl(tmp_path / "episodes.jsonl", [
        {"source": "cu-tune", "task_id": t["task_id"], "outcome": "success",
         "ended_at": now, "started_at": now}
        for t in TASKS
    ])
    out = pick_task.decide(TASKS, "2026-09-10", learn_root=tmp_path, out_dir=tmp_path)
    assert out["action"] == "skip"
    assert out["reason"] == "nothing-to-learn"


def test_prefers_unlearned_over_rotated_known(tmp_path):
    day = "2026-09-10"
    rotated = pick_task.pick(TASKS, day)
    other = next(t for t in TASKS if t["task_id"] != rotated["task_id"])
    _write_jsonl(tmp_path / "skills.jsonl", [
        {"kind": "skill", "task_id": rotated["task_id"], "title": rotated["task_id"]},
    ])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_jsonl(tmp_path / "episodes.jsonl", [
        {"source": "cu-tune", "task_id": rotated["task_id"], "outcome": "success",
         "ended_at": now, "started_at": now},
    ])
    out = pick_task.decide(TASKS, day, learn_root=tmp_path, out_dir=tmp_path)
    assert out["action"] == "do"
    assert out["mode"] == "learn"
    assert out["task"]["task_id"] == other["task_id"] or out["task"]["task_id"] != rotated["task_id"]
    assert out["task"]["task_id"] != rotated["task_id"]


def test_recheck_when_skill_exists_but_no_recent_win(tmp_path):
    _write_jsonl(tmp_path / "skills.jsonl", [
        {"kind": "skill", "task_id": t["task_id"], "title": t["task_id"]}
        for t in TASKS
    ])
    out = pick_task.decide(TASKS, "2026-09-10", learn_root=tmp_path, out_dir=tmp_path)
    assert out["action"] == "do"
    assert out["mode"] == "recheck"


def test_already_ran_today_skips(tmp_path):
    (tmp_path / "TUNE.md").write_text("# already\n")
    out = pick_task.decide(TASKS, "2026-09-10", learn_root=tmp_path, out_dir=tmp_path)
    assert out["reason"] == "already-ran-today"
    forced = pick_task.decide(TASKS, "2026-09-10", learn_root=tmp_path, out_dir=tmp_path, force=True)
    assert forced["action"] == "do"
    assert forced["mode"] == "learn"


def test_failed_episode_is_learn_again(tmp_path):
    tasks = [{"task_id": "calc-12x7", "task": "12*7"}]
    _write_jsonl(tmp_path / "skills.jsonl", [
        {"kind": "skill", "task_id": "calc-12x7", "title": "calc"},
    ])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_jsonl(tmp_path / "episodes.jsonl", [
        {"source": "cu-tune", "task_id": "calc-12x7", "outcome": "failed",
         "ended_at": now, "started_at": now},
    ])
    out = pick_task.decide(tasks, "2026-09-10", learn_root=tmp_path, out_dir=tmp_path)
    assert out["action"] == "do"
    assert out["mode"] == "learn"
    assert out["task"]["task_id"] == "calc-12x7"
