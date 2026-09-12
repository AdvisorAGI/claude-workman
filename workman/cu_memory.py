"""Packed computer-use memory for every model class.

One dispatcher, tiny JSON: ``{ok, op, n, lines}``. No journal dumps, no
nested recipes, no screenshot bytes. Qwen-class models get the same shape as
frontier models; they just get fewer, shorter lines.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import cu_skills, memory_graph as mg, working

OPS = (
    "status", "working", "tick", "recall", "fact", "forget", "history", "board",
)
ALIASES = {
    "get": "status", "stat": "status", "counts": "status",
    "set": "working", "task": "working", "checklist": "working",
    "done": "tick", "ok": "tick", "check": "tick",
    "search": "recall", "find": "recall", "skill": "recall", "skills": "recall",
    "remember": "fact", "assert": "fact", "know": "fact",
    "drop": "forget", "invalidate": "forget", "unlearn": "forget",
    "log": "history", "sessions": "history", "work": "history",
}
MAX_RECALL = 4


def _parse_op(op: str, q: str) -> tuple[str, str]:
    raw = (op or "status").strip()
    if not raw:
        return "status", q
    bits = raw.split(None, 1)
    token = bits[0].strip().lower().strip(":,")
    rest = bits[1].strip() if len(bits) > 1 else ""
    token = ALIASES.get(token, token)
    if token not in OPS:
        token = "status"
    if rest and not (q or "").strip():
        q = rest
    return token, q


def _parse_fact(q: str) -> tuple[str, str, str, str]:
    blob = (q or "").replace("/", "|")
    parts = [p.strip() for p in blob.split("|") if p.strip()]
    if len(parts) < 3:
        return "", "", "", ""
    note = parts[3] if len(parts) > 3 else ""
    return parts[0], parts[1], parts[2], note


def _skill_lines(query: str, root: Path | None) -> list[str]:
    # Skills live at LEARN_ROOT; tests patch cu_skills.SKILLS, so don't
    # re-bind root here. Recall is already token-capped.
    found = cu_skills.recall(query, limit=MAX_RECALL, compact=True, root=root)
    return list(found.get("lines") or [])


def _status_line(root: Path | None) -> str:
    row = mg.status(root)
    c = row.get("counts") or {}
    left = ""
    wm = working.get(root=root)
    if wm.get("n"):
        left = f" left={wm['n']}"
    facts = row.get("facts_valid")
    return (
        f"ep={c.get('episodes.jsonl', 0)} skill={c.get('skills.jsonl', 0)} "
        f"fact={facts if facts is not None else c.get('facts.jsonl', 0)} "
        f"journal={c.get('journal.jsonl', 0)} work={c.get('work-history.jsonl', 0)}"
        f"{left}"
    )


def handle(op: str, q: str = "", items: list[str] | None = None,
           *, root: Path | None = None) -> dict[str, Any]:
    """The only memory API models should call."""
    try:
        op, q = _parse_op(op, q)
        q = (q or "").strip()
        if op == "status":
            return mg.packed("status", [_status_line(root)])
        if op == "working":
            if items or q:
                cur = working.get(root=root)
                task = q or ""
                if not task:
                    # keep existing title if we are only replacing items
                    for line in cur.get("lines") or []:
                        if line.startswith("task="):
                            task = line[5:]
                            break
                return working.open_task(
                    task or "task", items=items, root=root)
            return working.get(root=root)
        if op == "tick":
            if not q:
                return working.get(root=root)
            return working.tick(q, "done", root=root)
        if op == "recall":
            if not q:
                return working.get(root=root)
            lines = _skill_lines(q, root)
            lines.extend(mg.recall_facts(q, limit=MAX_RECALL, root=root))
            if len(lines) < MAX_RECALL:
                for hit in (mg.search(q, limit=MAX_RECALL, root=root).get("matches") or []):
                    title = hit.get("title") or ""
                    kind = hit.get("node_kind") or ""
                    if title:
                        lines.append(mg._clip(f"{kind[0] if kind else '?'}:{title}"))
                    if len(lines) >= MAX_RECALL:
                        break
            if not lines:
                return mg.packed("recall", ["none"], extra={"q": q})
            # de-dupe, keep order
            seen: set[str] = set()
            uniq: list[str] = []
            for line in lines:
                if line in seen:
                    continue
                seen.add(line)
                uniq.append(line)
            return mg.packed("recall", uniq[:MAX_RECALL], extra={"q": q})
        if op == "fact":
            subject, predicate, obj, note = _parse_fact(q)
            if not subject:
                return mg.packed("fact", ["need: subject | predicate | object"], ok=False)
            return mg.assert_fact(subject, predicate, obj, note=note, root=root)
        if op == "forget":
            return mg.invalidate_fact(q, root=root)
        if op == "history":
            events = mg.history(limit=MAX_RECALL, root=root).get("events") or []
            lines = [
                mg._clip(f"{e.get('event')}: {e.get('detail') or ''}")
                for e in events
            ]
            if not lines:
                sess = mg.sessions(limit=MAX_RECALL, root=root).get("sessions") or []
                lines = [
                    mg._clip(f"{s.get('status') or s.get('kind')}: {s.get('title') or s.get('session_id')}")
                    for s in sess
                ]
            return mg.packed("history", lines or ["none"])
        if op == "board":
            if items is not None:
                board = mg.training_board(items=items, root=root)
            elif q:
                board = mg.training_board(working=q, root=root)
            else:
                board = mg.training_board(root=root)
            lines = []
            for item in (board.get("items") or [])[:8]:
                if isinstance(item, dict):
                    lines.append(mg._clip(f"{item.get('state', '?')} {item.get('text')}"))
            if board.get("summary"):
                lines.append(mg._clip(f"sum {board['summary']}"))
            return mg.packed("board", lines or ["empty"])
        return mg.packed("status", [_status_line(root)])
    except Exception as exc:
        return mg.packed(op or "status", [str(exc)], ok=False)


def from_skills(found: dict[str, Any]) -> dict[str, Any]:
    """Compact a cu_skills.recall payload for MCP."""
    if found.get("lines"):
        return mg.packed("recall", list(found["lines"]), extra={"q": found.get("query") or ""})
    lines = []
    for row in found.get("matches") or []:
        lines.append(mg._clip(
            f"s:{row.get('title') or ''} | {row.get('app') or ''} | {row.get('steps') or ''}"))
    if not lines:
        return mg.packed("recall", ["none"], extra={"q": found.get("query") or ""})
    return mg.packed("recall", lines, extra={"q": found.get("query") or ""})
