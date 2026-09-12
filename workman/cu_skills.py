"""Computer-use skills — taught recipes that go beyond a single chord.

A skill is how a person works a desk: look up the chord, paste the URL, confirm
the window. Stored next to recipes at ``~/.grok/workman-learn/skills.jsonl`` so
the computer-use lab (red / yellow / green) and any session can teach Workman
new habits without editing code.

Skills are recalled with ``cu_skills.recall`` and written with
``cu_skills.teach``. The nightly / lab lane proposes; a red-zone check rejects
anything that needs network or secrets; green applies into this file.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

LEARN_ROOT = Path(os.path.expanduser(
    os.environ.get("WORKMAN_LEARN_ROOT", "~/.grok/workman-learn")))
SKILLS = LEARN_ROOT / "skills.jsonl"
FLEET = LEARN_ROOT / "fleet"

_SECRETISH = re.compile(
    r"(?i)(password|secret|token|api[_-]?key|authorization:\s*bearer)")


def learn_root() -> Path:
    LEARN_ROOT.mkdir(parents=True, exist_ok=True)
    return LEARN_ROOT


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _atomic_write_lines(path: Path, rows: list[dict]) -> None:
    learn_root()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".skills.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
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


def read_all(include_fleet: bool = True) -> list[dict]:
    rows = [r for r in _read_jsonl(SKILLS) if r.get("kind", "skill") == "skill"]
    if not include_fleet or not FLEET.is_dir():
        return rows
    for device_dir in sorted(FLEET.iterdir()):
        if not device_dir.is_dir():
            continue
        for row in _read_jsonl(device_dir / "skills.jsonl"):
            if row.get("kind", "skill") != "skill":
                continue
            row = dict(row)
            row["_fleet"] = device_dir.name
            rows.append(row)
    return rows


def _scrub(text: str) -> str:
    if _SECRETISH.search(text or ""):
        return "[redacted]"
    return text or ""


def teach(
    title: str,
    steps: list[dict] | str,
    *,
    app: str = "",
    platform: str = "",
    notes: str = "",
    source: str = "session",
    skill_id: str | None = None,
    outcome: str = "success",
    task_id: str = "",
) -> dict:
    """Upsert one computer-use skill. steps is structured steps_v2 or prose."""
    title = _scrub((title or "").strip())
    if not title:
        return {"ok": False, "error": "title is required"}
    if isinstance(steps, str):
        steps_v2 = [{"action": "note", "value": _scrub(steps)}]
        prose = _scrub(steps)
    else:
        steps_v2 = []
        for step in steps or []:
            if not isinstance(step, dict):
                continue
            cleaned = {k: v for k, v in step.items() if k != "value" or not _SECRETISH.search(str(v))}
            if "value" in step and "value" not in cleaned:
                cleaned["value"] = "[redacted]"
            if "value" in cleaned and isinstance(cleaned["value"], str):
                cleaned["value"] = _scrub(cleaned["value"])
            steps_v2.append(cleaned)
        prose = "; ".join(
            f"{s.get('action', '?')}:{s.get('value') or (s.get('target') or {}).get('label', '')}"
            for s in steps_v2
        )
    if not steps_v2:
        return {"ok": False, "error": "steps are required"}
    now = _now()
    sid = skill_id or str(uuid.uuid4())
    incoming = {
        "kind": "skill",
        "skill_id": sid,
        "title": title,
        "app": (app or "").strip(),
        "platform": (platform or "").strip(),
        "steps": prose,
        "steps_v2": steps_v2,
        "notes": _scrub(notes),
        "source": source,
        "outcome": outcome,
        "task_id": (task_id or "").strip(),
        "ts": now,
        "uses": 1,
        "first_ts": now,
    }
    rows = _read_jsonl(SKILLS)
    match = next(
        (i for i, r in enumerate(rows)
         if r.get("kind", "skill") == "skill"
         and (r.get("skill_id") == sid
              or (r.get("title", "").lower() == title.lower()
                  and (r.get("app") or "") == incoming["app"]))),
        None,
    )
    if match is None:
        rows.append(incoming)
        action = "created"
    else:
        old = rows[match]
        rows[match] = {
            **old,
            **incoming,
            "skill_id": old.get("skill_id") or sid,
            "task_id": incoming["task_id"] or old.get("task_id") or "",
            "uses": int(old.get("uses") or 1) + 1,
            "first_ts": old.get("first_ts") or old.get("ts") or now,
        }
        action = "replaced"
        sid = rows[match]["skill_id"]
    _atomic_write_lines(SKILLS, rows)
    try:
        from . import memory_graph as mg
        mg.on_skill_taught(
            sid, title, task_id=incoming.get("task_id") or "",
            root=LEARN_ROOT)
    except Exception:
        pass
    return {
        "ok": True,
        "taught": True,
        "action": action,
        "skill_id": sid,
        "title": title,
        "path": str(SKILLS),
    }


def recall(query: str, app: str = "", limit: int = 3, compact: bool = False,
           root: Path | None = None) -> dict:
    """Rank skills by token overlap. compact=True is the MCP/Qwen shape."""
    q = (query or "").lower().split()
    if not q:
        return {"ok": True, "matches": [], "lines": [], "query": query, "n": 0}
    app_l = (app or "").lower()
    scored: list[tuple[int, dict]] = []
    if root is not None:
        rows = [r for r in _read_jsonl(Path(root) / "skills.jsonl")
                if r.get("kind", "skill") == "skill"]
    else:
        rows = read_all(True)
    for row in rows:
        blob = " ".join([
            str(row.get("title") or ""),
            str(row.get("app") or ""),
            str(row.get("steps") or ""),
            str(row.get("notes") or ""),
        ]).lower()
        score = sum(3 for t in q if t in blob)
        if app_l and app_l in str(row.get("app") or "").lower():
            score += 5
        if score:
            scored.append((score, row))
    scored.sort(key=lambda x: (-x[0], -int(x[1].get("uses") or 0)))
    cap = max(1, min(limit, 5))
    matches = []
    lines: list[str] = []
    for score, row in scored[:cap]:
        steps = str(row.get("steps") or "")
        if len(steps) > 80:
            steps = steps[:79] + "…"
        matches.append({
            "score": score,
            "skill_id": row.get("skill_id"),
            "title": row.get("title"),
            "app": row.get("app"),
            "platform": row.get("platform"),
            "steps": steps,
            "uses": row.get("uses"),
            "source": row.get("source"),
            "fleet": row.get("_fleet"),
        })
        if not compact:
            matches[-1]["steps_v2"] = row.get("steps_v2")
        title = str(row.get("title") or "")
        app_s = str(row.get("app") or "")
        lines.append(f"s:{title} | {app_s} | {steps}".strip(" |"))
    out = {
        "ok": True,
        "matches": matches if not compact else [],
        "lines": lines,
        "n": len(lines),
        "query": query,
        "app": app,
        "no_match": not lines,
        "hint": "none stored" if not lines else "",
    }
    if compact:
        return {"ok": True, "n": len(lines), "lines": lines, "query": query, "app": app,
                "no_match": not lines, "hint": out["hint"]}
    out["matches"] = matches
    return out
