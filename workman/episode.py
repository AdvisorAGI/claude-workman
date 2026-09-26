"""Episode boundaries for computer-use traces.

A journal row without a task and an outcome is telemetry, not a training
example. ``start()`` mints ``episode_uid``, stamps every later ``journal``
call, and ``close()`` writes the judged outcome. Failures never raise.

The open episode is also written to ``active-episode.json`` so a later
process (the tuner script starts and closes in separate Python invocations)
can still close it.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

LEARN_ROOT = Path(os.path.expanduser(
    os.environ.get("WORKMAN_LEARN_ROOT", "~/.grok/workman-learn")))

_EPISODE: dict[str, Any] | None = None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _journal() -> Path:
    return LEARN_ROOT / "journal.jsonl"


def _episodes() -> Path:
    return LEARN_ROOT / "episodes.jsonl"


def _active_path() -> Path:
    return LEARN_ROOT / "active-episode.json"


def _save_active(row: dict[str, Any] | None) -> None:
    path = _active_path()
    try:
        LEARN_ROOT.mkdir(parents=True, exist_ok=True)
        if row is None:
            try:
                path.unlink()
            except OSError:
                pass
            return
        path.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
    except Exception:
        return


def _load_active() -> dict[str, Any] | None:
    global _EPISODE
    if _EPISODE is not None:
        return _EPISODE
    try:
        path = _active_path()
        if path.is_file():
            row = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(row, dict) and row.get("episode_uid"):
                _EPISODE = row
                return row
    except Exception:
        pass
    uid = os.environ.get("WORKMAN_EPISODE_UID") or ""
    if uid:
        row = {
            "episode_uid": uid,
            "task": os.environ.get("WORKMAN_TASK") or "",
            "task_id": os.environ.get("WORKMAN_TASK_ID") or "",
            "app": "",
            "model": "",
            "rung": "",
            "source": os.environ.get("WORKMAN_EPISODE_SOURCE") or "cu-tune",
            "started_at": _now(),
            "ended_at": None,
            "outcome": "unknown",
            "n_steps": 0,
        }
        _EPISODE = row
        return row
    return None


def current() -> dict[str, Any] | None:
    row = _load_active()
    return None if row is None else dict(row)


def uid() -> str | None:
    row = _load_active()
    return None if row is None else str(row.get("episode_uid"))


def start(task: str, *, app: str = "", model: str = "",
           rung: str = "", source: str = "cu-tune",
           task_id: str = "", checklist: list[str] | None = None) -> dict[str, Any]:
    """Open a new episode. An already-open one is closed as unknown first."""
    global _EPISODE
    if _load_active() is not None:
        close(outcome="unknown", judged_by="superseded")
    row = {
        "episode_uid": str(uuid.uuid4()),
        "task": (task or "").strip() or "untitled",
        "task_id": (task_id or "").strip(),
        "app": app,
        "model": model,
        "rung": rung,
        "source": source,
        "started_at": _now(),
        "ended_at": None,
        "outcome": "unknown",
        "n_steps": 0,
    }
    _EPISODE = row
    os.environ["WORKMAN_EPISODE_UID"] = row["episode_uid"]
    os.environ["WORKMAN_TASK"] = row["task"]
    if row["task_id"]:
        os.environ["WORKMAN_TASK_ID"] = row["task_id"]
    _save_active(row)
    _append(_journal(), {"kind": "episode_start", **row})
    try:
        from . import memory_graph as mg
        from . import working
        mg.on_episode_start(row, root=LEARN_ROOT)
        working.open_task(
            row["task"], app=str(row.get("app") or ""),
            task_id=str(row.get("task_id") or ""),
            episode_uid=str(row.get("episode_uid") or ""),
            items=checklist, root=LEARN_ROOT)
    except Exception:
        pass
    return dict(row)


def note_step() -> None:
    row = _load_active()
    if row is None:
        return
    row["n_steps"] = int(row.get("n_steps") or 0) + 1
    _save_active(row)


def close(outcome: str = "unknown", *, judged_by: str = "",
           judge_note: str = "") -> dict[str, Any]:
    global _EPISODE
    row = _load_active()
    if row is None:
        return {"ok": False, "error": "no episode"}
    allowed = ("success", "partial", "failed", "aborted", "unknown")
    if outcome not in allowed:
        outcome = "unknown"
    row["ended_at"] = _now()
    row["outcome"] = outcome
    row["judged_by"] = judged_by
    row["judge_note"] = (judge_note or "")[:400]
    out = dict(row)
    _append(_episodes(), out)
    _append(_journal(), {"kind": "episode_end", **out})
    _EPISODE = None
    _save_active(None)
    os.environ.pop("WORKMAN_EPISODE_UID", None)
    os.environ.pop("WORKMAN_TASK", None)
    os.environ.pop("WORKMAN_TASK_ID", None)
    try:
        from . import memory_graph as mg
        from . import working
        mg.on_episode_close(out, root=LEARN_ROOT)
        working.close(outcome=outcome, root=LEARN_ROOT)
    except Exception:
        pass
    return out


def stamp(row: dict[str, Any]) -> dict[str, Any]:
    """Add episode_uid/task to a journal row if a session is open."""
    out = dict(row)
    ep = _load_active()
    env_uid = os.environ.get("WORKMAN_EPISODE_UID") or ""
    if ep:
        out.setdefault("episode_uid", ep["episode_uid"])
        out.setdefault("task", ep.get("task"))
        out.setdefault("task_id", ep.get("task_id") or None)
        note_step()
    elif env_uid:
        out.setdefault("episode_uid", env_uid)
        out.setdefault("task", os.environ.get("WORKMAN_TASK") or None)
        out.setdefault("task_id", os.environ.get("WORKMAN_TASK_ID") or None)
    return out


def _append(path: Path, row: dict[str, Any]) -> None:
    try:
        LEARN_ROOT.mkdir(parents=True, exist_ok=True)
        payload = dict(row)
        payload.setdefault("ts", _now())
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        return
