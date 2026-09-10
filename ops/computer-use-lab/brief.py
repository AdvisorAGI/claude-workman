#!/usr/bin/env python3
"""<=1200-char leader brief. Journal dumps burn Qwen; this does not."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

CHAR_BUDGET = 1200
JOURNAL_TAIL = 5


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _tail_lines(path: Path, n: int) -> list[str]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-n:]:
        try:
            row = json.loads(line)
        except ValueError:
            out.append(_clip(line, 80))
            continue
        if not isinstance(row, dict):
            continue
        kind = row.get("kind") or row.get("tool") or "row"
        tool = row.get("tool") or row.get("event") or ""
        out.append(_clip(f"{kind} {tool}", 80))
    return out


def _skill_line(path: Path) -> str:
    if not path.is_file():
        return "none"
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "none"
    if isinstance(rows, list):
        row = rows[0] if rows else {}
    elif isinstance(rows, dict):
        row = rows
    else:
        return "none"
    if not row:
        return "none"
    return _clip(
        f"{row.get('title') or row.get('task_id') or 'skill'} | {row.get('steps') or ''}",
        160,
    )


def build(task: dict, *, known_path: Path, journal: Path, working_lines: list[str] | None = None) -> str:
    tid = task.get("task_id") or ""
    lines = [
        f"task_id={tid}",
        _clip(f"do: {task.get('task') or ''}", 200),
        _clip(f"post: {task.get('post') or 'screenshot confirms it'}", 160),
        f"app={task.get('app') or '-'}",
        f"known: {_skill_line(known_path)}",
    ]
    if working_lines:
        lines.append("working: " + " ; ".join(_clip(x, 60) for x in working_lines[:6]))
    tail = _tail_lines(journal, JOURNAL_TAIL)
    if tail:
        lines.append("journal: " + " | ".join(tail))
    lines.append("memory: cu_memory recall then act; cu_skill_teach only on a new win")
    text = "\n".join(lines).strip()
    return text if len(text) <= CHAR_BUDGET else text[: CHAR_BUDGET - 1] + "…"


def verify_text(task: dict, proposal: dict) -> str:
    """<=800 chars for the verifier. JSON-only reply expected."""
    p = proposal or {}
    lines = [
        f"task_id={task.get('task_id') or ''}",
        _clip(f"post: {task.get('post') or ''}", 120),
        f"finished={bool(p.get('finished'))} unchanged={bool(p.get('unchanged'))}",
        _clip(f"title={p.get('title') or ''}", 80),
        _clip(f"proof: {p.get('proof') or p.get('reason') or p.get('error') or ''}", 160),
        _clip(f"steps: {p.get('steps') or p.get('steps_v2') or ''}", 160),
    ]
    text = "\n".join(lines).strip()
    return text if len(text) <= 800 else text[:799] + "…"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--known", required=True)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    task = json.loads(Path(args.task).read_text(encoding="utf-8"))
    text = build(task, known_path=Path(args.known), journal=Path(args.journal))
    Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(len(text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
