"""In-task working memory (ATMem): a tiny checklist, not a screenshot dump.

One file ``working.json``. Built for low-context models: remaining items only,
short lines, never the journal. Failures never raise.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import memory_graph as mg

MAX_ITEMS = 8
LINE = 100


def _root(root: Path | None) -> Path:
    return Path(root) if root is not None else mg._root()


def _path(root: Path | None) -> Path:
    return _root(root) / "working.json"


def _clip(text: str, n: int = LINE) -> str:
    text = " ".join((text or "").split())
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


def _load(root: Path | None) -> dict[str, Any]:
    row = mg._load_json(_path(root), {})
    if not isinstance(row, dict):
        return {}
    return row


def _save(row: dict[str, Any], root: Path | None) -> None:
    mg._dump_json(_path(root), row)


def _items(row: dict[str, Any]) -> list[dict[str, str]]:
    items = row.get("items") or []
    out: list[dict[str, str]] = []
    for item in items:
        if isinstance(item, dict):
            text = _clip(str(item.get("text") or ""))
            state = str(item.get("state") or "todo")
        else:
            text = _clip(str(item))
            state = "todo"
        if text:
            out.append({"text": text, "state": state if state in {"todo", "done", "blocked"} else "todo"})
        if len(out) >= MAX_ITEMS:
            break
    return out


def open_task(task: str, *, items: list[str] | None = None, app: str = "",
              task_id: str = "", episode_uid: str = "",
              display: str = "", root: Path | None = None) -> dict[str, Any]:
    """Start (or replace) the in-task checklist."""
    try:
        task = _clip(task or "task", 160)
        raw_items = items if items is not None else [task]
        row = {
            "task": task,
            "task_id": (task_id or "")[:80],
            "episode_uid": (episode_uid or "")[:80],
            "app": (app or "")[:80],
            "display": _clip(display, 40),
            "items": [
                {"text": _clip(str(it)), "state": "todo"}
                for it in raw_items if str(it).strip()
            ][:MAX_ITEMS],
            "shot": "",
            "updated_at": mg._now(),
        }
        if not row["items"]:
            row["items"] = [{"text": task, "state": "todo"}]
        _save(row, root)
        return packed(row)
    except Exception as exc:
        return {"ok": False, "op": "working", "n": 0, "lines": [str(exc)[:LINE]]}


def observe(shot_sha: str, *, root: Path | None = None) -> None:
    """Record a short shot hash if a task is open. No-op otherwise."""
    try:
        row = _load(root)
        if not row:
            return
        row["shot"] = (shot_sha or "")[:16]
        row["updated_at"] = mg._now()
        _save(row, root)
    except Exception:
        return


def tick(needle: str, state: str = "done", *, root: Path | None = None) -> dict[str, Any]:
    try:
        row = _load(root)
        if not row:
            return {"ok": False, "op": "tick", "n": 0, "lines": ["no working task"]}
        want = (state or "done").strip().lower()
        if want not in {"todo", "done", "blocked"}:
            want = "done"
        needle_l = (needle or "").strip().lower()
        items = _items(row)
        hit = False
        for item in items:
            if needle_l and needle_l in item["text"].lower():
                item["state"] = want
                hit = True
                break
        if not hit and needle.strip() and len(items) < MAX_ITEMS:
            items.append({"text": _clip(needle), "state": want})
        row["items"] = items
        row["updated_at"] = mg._now()
        _save(row, root)
        return packed(row)
    except Exception as exc:
        return {"ok": False, "op": "tick", "n": 0, "lines": [str(exc)[:LINE]]}


def get(*, root: Path | None = None) -> dict[str, Any]:
    try:
        return packed(_load(root))
    except Exception as exc:
        return {"ok": False, "op": "working", "n": 0, "lines": [str(exc)[:LINE]]}


def close(*, outcome: str = "", root: Path | None = None) -> dict[str, Any]:
    try:
        row = _load(root)
        path = _path(root)
        try:
            path.unlink()
        except OSError:
            pass
        left = [i["text"] for i in _items(row) if i.get("state") != "done"]
        line = _clip(f"closed {outcome or 'done'} left={len(left)}")
        return {"ok": True, "op": "working", "n": 1, "lines": [line]}
    except Exception as exc:
        return {"ok": False, "op": "working", "n": 0, "lines": [str(exc)[:LINE]]}


def packed(row: dict[str, Any] | None) -> dict[str, Any]:
    """Remaining-first view. Nested objects stay on disk, not in the model reply."""
    if not row:
        return {"ok": True, "op": "working", "n": 0, "lines": ["empty"]}
    items = _items(row)
    lines: list[str] = []
    left = 0
    for item in items:
        mark = {"todo": "TODO", "done": "OK", "blocked": "BLOCK"}[item["state"]]
        if item["state"] != "done":
            left += 1
            lines.append(_clip(f"{mark} {item['text']}"))
    if not lines:
        lines = ["OK all done"]
    task = _clip(str(row.get("task") or ""), 80)
    extra = []
    if task:
        extra.append(f"task={task}")
    if row.get("app"):
        extra.append(f"app={row['app']}")
    if row.get("shot"):
        extra.append(f"shot={row['shot']}")
    if extra:
        lines.append(_clip(" ".join(extra)))
    return {
        "ok": True,
        "op": "working",
        "n": left,
        "lines": lines[: MAX_ITEMS + 1],
    }
