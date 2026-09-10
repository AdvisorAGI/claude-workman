"""Durable Workman memory: training board, work history, sessions, graph.

File-backed under ``WORKMAN_LEARN_ROOT`` (default ``~/.grok/workman-learn``)
so every machine can document computer-use work without a database. The
DGX ``workman-learn-db`` is the later ingest target; this store is the source
of truth the tuner and MCP tools write today.

Never raises. Callers get ``{"ok": False, "error": ...}`` on failure.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

LEARN_ROOT = Path(os.path.expanduser(
    os.environ.get("WORKMAN_LEARN_ROOT", "~/.grok/workman-learn")))

NODE_KINDS = (
    "session", "episode", "task", "skill", "recipe", "lesson", "board", "fact",
)
EDGE_RELS = (
    "RAN", "ON_TASK", "TAUGHT", "USED", "FOLLOWED", "ON_BOARD", "REMEMBERED",
    "SUPERSEDES", "FAILED_ON", "INVALIDATES",
)

# Packed replies stay under this so Qwen-class models can parse them.
CHAR_BUDGET = 1600
LINE = 100
MAX_LINES = 8


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _root(root: Path | None = None) -> Path:
    return Path(root) if root is not None else LEARN_ROOT


def _ensure(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    return root


def _path(name: str, root: Path | None = None) -> Path:
    return _root(root) / name


def _append(path: Path, row: dict[str, Any]) -> None:
    _ensure(path.parent)
    payload = dict(row)
    payload.setdefault("ts", _now())
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    with path.open("a", encoding="utf-8") as fh:
        path.chmod(0o600) if path.exists() else None
        fh.write(line + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
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


def _count_lines(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        with path.open(encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0


def _load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _dump_json(path: Path, row: Any) -> None:
    _ensure(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def remember_node(kind: str, title: str, *, node_id: str = "",
                   attrs: dict[str, Any] | None = None,
                   root: Path | None = None) -> dict[str, Any]:
    kind = (kind or "").strip().lower()
    if kind not in NODE_KINDS:
        return {"ok": False, "error": f"unknown kind {kind}"}
    title = (title or "").strip() or kind
    nid = (node_id or "").strip() or str(uuid.uuid4())
    row = {
        "kind": "node",
        "node_kind": kind,
        "id": nid,
        "title": title[:400],
        "attrs": dict(attrs or {}),
    }
    try:
        _append(_path("graph-nodes.jsonl", root), row)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "id": nid, "node_kind": kind, "title": title}


def remember_edge(src: str, rel: str, dst: str, *,
                   attrs: dict[str, Any] | None = None,
                   root: Path | None = None) -> dict[str, Any]:
    rel = (rel or "").strip().upper()
    if rel not in EDGE_RELS:
        return {"ok": False, "error": f"unknown rel {rel}"}
    src = (src or "").strip()
    dst = (dst or "").strip()
    if not src or not dst:
        return {"ok": False, "error": "src and dst required"}
    payload_attrs = dict(attrs or {})
    payload_attrs.setdefault("valid_from", _now())
    if "valid_to" not in payload_attrs:
        payload_attrs["valid_to"] = ""
    row = {"kind": "edge", "src": src, "rel": rel, "dst": dst,
           "attrs": payload_attrs}
    try:
        _append(_path("graph-edges.jsonl", root), row)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **row}


def work(event: str, detail: str = "", *, session_id: str = "",
          episode_uid: str = "", task_id: str = "",
          root: Path | None = None) -> dict[str, Any]:
    row = {
        "kind": "work",
        "event": (event or "note").strip()[:80],
        "detail": (detail or "")[:800],
        "session_id": (session_id or os.environ.get("WORKMAN_SESSION_ID") or ""),
        "episode_uid": (episode_uid or os.environ.get("WORKMAN_EPISODE_UID") or ""),
        "task_id": (task_id or os.environ.get("WORKMAN_TASK_ID") or ""),
    }
    try:
        _append(_path("work-history.jsonl", root), row)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **row}


def session_open(title: str, *, source: str = "session", machine: str = "",
                  model: str = "", rung: str = "",
                  root: Path | None = None) -> dict[str, Any]:
    sid = str(uuid.uuid4())
    title = (title or "").strip() or "untitled session"
    node = remember_node(
        "session", title, node_id=sid,
        attrs={"source": source, "machine": machine, "model": model, "rung": rung,
               "status": "open"},
        root=root)
    row = {
        "kind": "session",
        "session_id": sid,
        "title": title,
        "source": source,
        "machine": machine,
        "model": model,
        "rung": rung,
        "status": "open",
        "ended_at": None,
        "outcome": "",
    }
    try:
        _append(_path("session-history.jsonl", root), row)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    os.environ["WORKMAN_SESSION_ID"] = sid
    work("session_open", title, session_id=sid, root=root)
    node["session"] = row
    return {"ok": True, "session_id": sid, "title": title, "node": node}


def session_close(session_id: str = "", outcome: str = "done",
                  summary: str = "", root: Path | None = None) -> dict[str, Any]:
    sid = (session_id or os.environ.get("WORKMAN_SESSION_ID") or "").strip()
    if not sid:
        return {"ok": False, "error": "no session"}
    row = {
        "kind": "session_end",
        "session_id": sid,
        "status": "closed",
        "outcome": (outcome or "done")[:40],
        "summary": (summary or "")[:800],
        "ended_at": _now(),
    }
    try:
        _append(_path("session-history.jsonl", root), row)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    work("session_close", summary or outcome, session_id=sid, root=root)
    os.environ.pop("WORKMAN_SESSION_ID", None)
    return {"ok": True, **row}


def on_episode_start(row: dict[str, Any], *, root: Path | None = None) -> None:
    try:
        uid = str(row.get("episode_uid") or "")
        task = str(row.get("task") or "")
        task_id = str(row.get("task_id") or "")
        remember_node(
            "episode", task, node_id=uid,
            attrs={"task_id": task_id, "app": row.get("app") or "",
                   "model": row.get("model") or "", "rung": row.get("rung") or "",
                   "source": row.get("source") or ""},
            root=root)
        if task_id:
            remember_node("task", task, node_id=f"task:{task_id}",
                           attrs={"task_id": task_id, "app": row.get("app") or ""},
                           root=root)
            remember_edge(uid, "ON_TASK", f"task:{task_id}", root=root)
        sid = os.environ.get("WORKMAN_SESSION_ID") or ""
        if sid:
            remember_edge(sid, "RAN", uid, root=root)
        work("episode_start", task, episode_uid=uid, task_id=task_id, root=root)
    except Exception:
        return


def on_episode_close(row: dict[str, Any], *, root: Path | None = None) -> None:
    try:
        uid = str(row.get("episode_uid") or "")
        work(
            "episode_end",
            f"{row.get('outcome') or 'unknown'}: {row.get('task') or ''}",
            episode_uid=uid, task_id=str(row.get("task_id") or ""),
            root=root)
        remember_node(
            "episode", str(row.get("task") or ""),
            node_id=uid,
            attrs={"outcome": row.get("outcome"), "task_id": row.get("task_id") or "",
                   "judged_by": row.get("judged_by") or ""},
            root=root)
    except Exception:
        return


def on_skill_taught(skill_id: str, title: str, *, task_id: str = "",
                    episode_uid: str = "", root: Path | None = None) -> None:
    try:
        remember_node("skill", title, node_id=skill_id,
                       attrs={"task_id": task_id}, root=root)
        ep = episode_uid or os.environ.get("WORKMAN_EPISODE_UID") or ""
        if ep:
            remember_edge(ep, "TAUGHT", skill_id, root=root)
        if task_id:
            remember_edge(f"task:{task_id}", "REMEMBERED", skill_id, root=root)
        work("skill_taught", title, episode_uid=ep, task_id=task_id, root=root)
    except Exception:
        return


def history(limit: int = 20, *, root: Path | None = None) -> dict[str, Any]:
    rows = _read_jsonl(_path("work-history.jsonl", root))
    rows = rows[-max(1, min(limit, 200)):]
    rows.reverse()
    return {"ok": True, "count": len(rows), "events": rows}


def sessions(limit: int = 20, *, root: Path | None = None) -> dict[str, Any]:
    rows = _read_jsonl(_path("session-history.jsonl", root))
    rows = rows[-max(1, min(limit, 200)):]
    rows.reverse()
    return {"ok": True, "count": len(rows), "sessions": rows}


def search(query: str, limit: int = 12, *, root: Path | None = None) -> dict[str, Any]:
    q = (query or "").strip().lower()
    if not q:
        return {"ok": True, "matches": []}
    tokens = q.split()
    hits: list[dict[str, Any]] = []
    for row in _read_jsonl(_path("graph-nodes.jsonl", root)):
        blob = " ".join([
            str(row.get("node_kind") or ""),
            str(row.get("title") or ""),
            json.dumps(row.get("attrs") or {}, default=str),
        ]).lower()
        score = sum(2 for t in tokens if t in blob)
        if score:
            hits.append({"score": score, **row})
    hits.sort(key=lambda r: -int(r.get("score") or 0))
    return {"ok": True, "query": query, "matches": hits[: max(1, min(limit, 50))]}


def neighbors(node_id: str, *, root: Path | None = None) -> dict[str, Any]:
    nid = (node_id or "").strip()
    if not nid:
        return {"ok": False, "error": "node_id required"}
    edges = [e for e in _read_jsonl(_path("graph-edges.jsonl", root))
             if e.get("src") == nid or e.get("dst") == nid]
    return {"ok": True, "id": nid, "edges": edges[-40:]}


def _clip(text: str, n: int = LINE) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


def packed(op: str, lines: list[str], *, ok: bool = True,
           extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """One small JSON object every model can parse. No nested dumps."""
    clean = [_clip(x) for x in lines if str(x).strip()][:MAX_LINES]
    row: dict[str, Any] = {"ok": ok, "op": op, "n": len(clean), "lines": clean}
    if extra:
        for key, value in extra.items():
            if value is None or value == "" or key in row:
                continue
            if isinstance(value, (str, int, float, bool)):
                row[key] = value if not isinstance(value, str) else _clip(str(value), 80)
    return _fit(row)


def _fit(row: dict[str, Any]) -> dict[str, Any]:
    raw = json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(raw) <= CHAR_BUDGET:
        return row
    lines = list(row.get("lines") or [])
    while lines and len(json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str)) > CHAR_BUDGET:
        lines.pop()
        row["lines"] = lines
        row["n"] = len(lines)
    row["trunc"] = True
    return row


def _fact_id(subject: str, predicate: str, obj: str) -> str:
    key = f"{subject.strip().lower()}|{predicate.strip().lower()}|{obj.strip().lower()}"
    return "fact:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _facts_latest(root: Path | None) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(_path("facts.jsonl", root)):
        fid = str(row.get("id") or "")
        if fid:
            latest[fid] = row
    return latest


def _fact_valid(row: dict[str, Any], as_of: str = "") -> bool:
    end = str(row.get("valid_to") or "").strip()
    if not end:
        return True
    now = as_of or _now()
    return end > now


def assert_fact(subject: str, predicate: str, obj: str, *,
                 note: str = "", source: str = "session",
                 root: Path | None = None) -> dict[str, Any]:
    subject = _clip(subject, 80)
    predicate = _clip(predicate, 40)
    obj = _clip(obj, 80)
    if not subject or not predicate or not obj:
        return packed("fact", ["need: subject | predicate | object"], ok=False)
    now = _now()
    superseded: list[str] = []
    try:
        latest = _facts_latest(root)
        for old in latest.values():
            if not _fact_valid(old):
                continue
            if (str(old.get("subject") or "").lower() == subject.lower()
                    and str(old.get("predicate") or "").lower() == predicate.lower()
                    and str(old.get("object") or "").lower() != obj.lower()):
                old["valid_to"] = now
                old["ts"] = now
                _append(_path("facts.jsonl", root), old)
                superseded.append(old["id"])
        fid = _fact_id(subject, predicate, obj)
        for old_id in superseded:
            remember_edge(fid, "SUPERSEDES", old_id, root=root)
        row = {
            "id": fid,
            "subject": subject,
            "predicate": predicate,
            "object": obj,
            "note": _clip(note, 80),
            "source": (source or "session")[:40],
            "valid_from": now,
            "valid_to": "",
        }
        _append(_path("facts.jsonl", root), row)
        remember_node(
            "fact", f"{subject} {predicate} {obj}", node_id=fid,
            attrs={"predicate": predicate, "object": obj, "valid_from": now},
            root=root)
        lines = [f"f:{subject} {predicate}={obj}"]
        if superseded:
            lines.append("superseded " + ",".join(superseded[:3]))
        return packed("fact", lines, extra={"id": fid})
    except Exception as exc:
        return packed("fact", [str(exc)], ok=False)


def invalidate_fact(query: str, *, root: Path | None = None) -> dict[str, Any]:
    q = (query or "").strip().lower()
    if not q:
        return packed("forget", ["need query or fact id"], ok=False)
    now = _now()
    hit = 0
    try:
        for fid, row in _facts_latest(root).items():
            blob = " ".join([
                fid, str(row.get("subject") or ""), str(row.get("predicate") or ""),
                str(row.get("object") or ""),
            ]).lower()
            if q == fid.lower() or q in blob:
                if not _fact_valid(row):
                    continue
                row["valid_to"] = now
                row["ts"] = now
                _append(_path("facts.jsonl", root), row)
                hit += 1
                if hit >= 5:
                    break
        return packed("forget", [f"invalidated {hit}"])
    except Exception as exc:
        return packed("forget", [str(exc)], ok=False)


def recall_facts(query: str, *, limit: int = 4, root: Path | None = None) -> list[str]:
    q = (query or "").strip().lower()
    tokens = q.split()
    scored: list[tuple[int, dict[str, Any]]] = []
    for row in _facts_latest(root).values():
        if not _fact_valid(row):
            continue
        blob = " ".join([
            str(row.get("subject") or ""), str(row.get("predicate") or ""),
            str(row.get("object") or ""), str(row.get("note") or ""),
        ]).lower()
        score = sum(2 for t in tokens if t in blob) if tokens else 1
        if score:
            scored.append((score, row))
    scored.sort(key=lambda x: -x[0])
    lines = []
    for _, row in scored[: max(1, min(limit, MAX_LINES))]:
        lines.append(_clip(
            f"f:{row.get('subject')} {row.get('predicate')}={row.get('object')}"))
    return lines


def training_board(title: str = "", items: list[str] | None = None,
                    working: str = "", done: str = "",
                    summary: str = "", root: Path | None = None) -> dict[str, Any]:
    """Durable computer-use training board (not the per-session pinned board)."""
    path = _path("cu-board.json", root)
    board = _load_json(path, {"title": "Workman training", "items": [], "summary": ""})
    if not isinstance(board, dict):
        board = {"title": "Workman training", "items": [], "summary": ""}
    changed = False
    if title:
        board["title"] = title[:200]
        changed = True
    if items is not None:
        board["items"] = [
            {"text": str(it).strip()[:200], "state": "pending"}
            for it in items if str(it).strip()
        ]
        changed = True
    if working:
        _mark_item(board, working, "working")
        changed = True
    if done:
        _mark_item(board, done, "done")
        changed = True
    if summary:
        board["summary"] = summary[:400]
        changed = True
    if changed:
        board["updated_at"] = _now()
        _dump_json(path, board)
        remember_node("board", board.get("title") or "Workman training",
                       node_id="board:training", attrs={"summary": board.get("summary") or ""},
                       root=root)
    return {"ok": True, **board}


def _mark_item(board: dict[str, Any], needle: str, state: str) -> None:
    needle_l = needle.strip().lower()
    items = board.get("items") or []
    for item in items:
        text = str(item.get("text") or "")
        if needle_l in text.lower() or needle_l == str(item.get("state") or ""):
            item["state"] = state
            return
    if needle.strip():
        items.append({"text": needle.strip()[:200], "state": state})
        board["items"] = items


def _file_counts(root: Path) -> dict[str, int]:
    names = (
        "journal.jsonl", "recipes.jsonl", "lessons.jsonl", "skills.jsonl",
        "shortcuts.jsonl", "episodes.jsonl", "graph-nodes.jsonl",
        "graph-edges.jsonl", "work-history.jsonl", "session-history.jsonl",
        "facts.jsonl",
    )
    return {name: _count_lines(root / name) for name in names}


def status(root: Path | None = None) -> dict[str, Any]:
    """What Workman memory exists on this machine (counts only, no row text)."""
    base = _root(root)
    counts = _file_counts(base)
    board = _load_json(base / "cu-board.json", {})
    fleet = []
    fleet_dir = base / "fleet"
    if fleet_dir.is_dir():
        fleet = sorted(p.name for p in fleet_dir.iterdir() if p.is_dir())
    return {
        "ok": True,
        "root": str(base),
        "counts": counts,
        "training_board": {
            "title": (board or {}).get("title") or "",
            "items": len((board or {}).get("items") or []),
            "summary": (board or {}).get("summary") or "",
        },
        "facts_valid": sum(1 for r in _facts_latest(base).values() if _fact_valid(r)),
        "working": (_root(root) / "working.json").is_file(),
        "fleet_devices": fleet,
        "stores": [
            "jsonl under WORKMAN_LEARN_ROOT (journal, recipes, lessons, skills, shortcuts, episodes)",
            "graph-nodes.jsonl + graph-edges.jsonl (session/episode/task/skill)",
            "work-history.jsonl (what ran)",
            "session-history.jsonl (who ran, when it closed)",
            "cu-board.json (durable training board)",
            "workman-learn-db Postgres on the DGX (wl schema, ingest later)",
            "pinned session board (board_* MCP, 24h, not this store)",
            "advisor pgvector (fleet advice, separate from Workman CU)",
        ],
    }
