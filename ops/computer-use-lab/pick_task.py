#!/usr/bin/env python3
"""Pick today's one DGX computer-use task, or skip if nothing is left to learn.

30 minutes is a daily ceiling, not a quota. If every task already has a
remembered skill and a recent success, the session does not run.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

FRESH_DAYS = int(os.environ.get("CU_TUNE_FRESH_DAYS", "14"))


def load_tasks(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _parse_ts(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _skill_task_ids(learn_root: Path, known_ids: set[str]) -> set[str]:
    ids: set[str] = set()
    known_l = {k.lower(): k for k in known_ids if k}
    for row in _read_jsonl(learn_root / "skills.jsonl"):
        tid = str(row.get("task_id") or "").strip()
        if tid:
            ids.add(tid)
            continue
        blob = " ".join([
            str(row.get("title") or ""),
            str(row.get("notes") or ""),
            str(row.get("steps") or ""),
        ]).lower()
        for key, original in known_l.items():
            if key and key in blob:
                ids.add(original)
    return ids


def _recent_outcomes(learn_root: Path, days: int) -> dict[str, str]:
    """Latest cu-tune outcome per task_id inside the freshness window."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    latest: dict[str, tuple[datetime, str]] = {}
    for row in _read_jsonl(learn_root / "episodes.jsonl"):
        if str(row.get("source") or "") not in {"cu-tune", "cu-lab"}:
            # Still count rows that carry a task_id from this tuner.
            if not row.get("task_id"):
                continue
        tid = str(row.get("task_id") or "").strip()
        if not tid:
            continue
        ts = _parse_ts(str(row.get("ended_at") or row.get("started_at") or row.get("ts") or ""))
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < cutoff:
            continue
        prev = latest.get(tid)
        if prev is None or ts >= prev[0]:
            latest[tid] = (ts, str(row.get("outcome") or "unknown"))
    return {k: v[1] for k, v in latest.items()}


def rotation_index(tasks: list[dict], day: str) -> int:
    if not tasks:
        return 0
    return sum(map(ord, day)) % len(tasks)


def pick(tasks: list[dict], day: str) -> dict:
    if not tasks:
        raise SystemExit("no tasks")
    return tasks[rotation_index(tasks, day)]


def already_ran(out_dir: Path) -> bool:
    return (out_dir / "TUNE.md").is_file() or (out_dir / "skip.json").is_file()


def task_status(task: dict, skill_ids: set[str], outcomes: dict[str, str]) -> str:
    """Return learn | recheck | skip for one task."""
    tid = str(task.get("task_id") or "").strip()
    has_skill = tid in skill_ids
    outcome = outcomes.get(tid, "")
    if not has_skill:
        return "learn"
    if outcome in {"failed", "aborted", "partial", "unknown"}:
        return "learn"
    if outcome == "success":
        return "skip"
    # Skill exists but no recent episode: cheap replay, not a Fable lesson.
    return "recheck"


def decide(
    tasks: list[dict],
    day: str,
    *,
    learn_root: Path,
    out_dir: Path | None = None,
    force: bool = False,
    fresh_days: int = FRESH_DAYS,
) -> dict:
    if not tasks:
        return {"action": "skip", "mode": "skip", "reason": "no-tasks", "task": None}

    if out_dir is not None and not force and already_ran(out_dir):
        return {
            "action": "skip",
            "mode": "skip",
            "reason": "already-ran-today",
            "task": None,
        }

    known_ids = {str(t.get("task_id") or "") for t in tasks if t.get("task_id")}
    skill_ids = _skill_task_ids(learn_root, known_ids)
    outcomes = _recent_outcomes(learn_root, fresh_days)
    start = rotation_index(tasks, day)
    ordered = [tasks[(start + i) % len(tasks)] for i in range(len(tasks))]

    learn = [t for t in ordered if task_status(t, skill_ids, outcomes) == "learn"]
    recheck = [t for t in ordered if task_status(t, skill_ids, outcomes) == "recheck"]

    if learn:
        task = learn[0]
        return {
            "action": "do",
            "mode": "learn",
            "reason": f"unlearned:{task['task_id']}",
            "task": task,
        }
    if recheck:
        task = recheck[0]
        return {
            "action": "do",
            "mode": "recheck",
            "reason": f"stale:{task['task_id']}",
            "task": task,
        }
    return {
        "action": "skip",
        "mode": "skip",
        "reason": "nothing-to-learn",
        "task": None,
        "known": sorted(skill_ids),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--date", default="")
    ap.add_argument("--learn-root", default="")
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--simple", action="store_true",
                    help="Print only the rotated task (ignore skip logic)")
    args = ap.parse_args()
    day = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tasks = load_tasks(Path(args.tasks))
    if args.simple:
        print(json.dumps(pick(tasks, day)))
        return 0
    learn_root = Path(os.path.expanduser(
        args.learn_root or os.environ.get("WORKMAN_LEARN_ROOT") or "~/.grok/workman-learn"))
    out_dir = Path(args.out_dir) if args.out_dir else None
    print(json.dumps(decide(
        tasks, day, learn_root=learn_root, out_dir=out_dir, force=args.force)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
