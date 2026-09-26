"""Chats and the Monday-style board. Files under WORKMAN_DESK_HOME."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

GROUPS = (
    ("backlog", "Backlog", "#c4c4c4"),
    ("working", "Working", "#fdab3d"),
    ("blocked", "Blocked", "#e2445c"),
    ("done", "Done", "#00c875"),
)
AGENTS = ("workman", "grok", "claude", "codex", "cursor")


def home() -> Path:
    raw = os.environ.get("WORKMAN_DESK_HOME")
    if raw:
        p = Path(raw)
    else:
        p = Path.home() / ".workman" / "desk"
    p.mkdir(parents=True, exist_ok=True)
    (p / "chats").mkdir(exist_ok=True)
    return p


def _atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


def _read(path: Path, fallback: dict) -> dict:
    if not path.is_file():
        return fallback
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    return data if isinstance(data, dict) else fallback


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def now() -> float:
    return time.time()


# ---- chats -----------------------------------------------------------------
def _chat_path(chat_id: str) -> Path:
    return home() / "chats" / f"{chat_id}.json"


def empty_chat(chat_id: str | None = None, **extra) -> dict:
    cid = chat_id or new_id()
    row = {
        "id": cid,
        "title": extra.get("title") or "New chat",
        "vendor": extra.get("vendor") or "grok",
        "model": extra.get("model") or "",
        "cwd": extra.get("cwd") or "",
        "session_id": extra.get("session_id") or str(uuid.uuid4()),
        "item_id": extra.get("item_id") or "",
        "created": now(),
        "updated": now(),
        "messages": [],
    }
    return row


def load_chat(chat_id: str) -> dict | None:
    path = _chat_path(chat_id)
    if not path.is_file():
        return None
    data = _read(path, {})
    return data if data.get("id") else None


def save_chat(chat: dict) -> dict:
    chat["updated"] = now()
    if not chat.get("id"):
        chat["id"] = new_id()
    _atomic(_chat_path(chat["id"]), chat)
    return chat


def list_chats(limit: int = 40) -> list[dict]:
    rows = []
    root = home() / "chats"
    for path in root.glob("*.json"):
        data = _read(path, {})
        if not data.get("id"):
            continue
        msgs = data.get("messages") or []
        last = ""
        if msgs:
            last = str((msgs[-1] or {}).get("text") or "")[:140]
        rows.append({
            "id": data["id"],
            "title": data.get("title") or "Chat",
            "vendor": data.get("vendor") or "",
            "model": data.get("model") or "",
            "cwd": data.get("cwd") or "",
            "item_id": data.get("item_id") or "",
            "updated": data.get("updated") or 0,
            "n": len(msgs),
            "last": last,
        })
    rows.sort(key=lambda r: r.get("updated") or 0, reverse=True)
    return rows[:limit]


def append_message(chat: dict, role: str, text: str, **meta) -> dict:
    chat.setdefault("messages", []).append({
        "role": role,
        "text": text,
        "ts": now(),
        **{k: v for k, v in meta.items() if v is not None},
    })
    if role == "user" and (chat.get("title") in ("", "New chat")):
        chat["title"] = (text or "Chat").strip().splitlines()[0][:72]
    return save_chat(chat)


def transcript(chat: dict, new_prompt: str, keep: int = 12) -> str:
    """Pack recent turns so vendors without --resume still see the thread."""
    msgs = list(chat.get("messages") or [])[-keep:]
    if not msgs:
        return new_prompt
    lines = ["Conversation so far:"]
    for m in msgs:
        role = "User" if m.get("role") == "user" else "Assistant"
        body = (m.get("text") or "").strip()
        if body:
            lines.append(f"{role}: {body}")
    lines.append(f"User: {new_prompt.strip()}")
    lines.append("Reply to the latest user message. Do not repeat the transcript.")
    return "\n".join(lines)


# ---- board -----------------------------------------------------------------
def _board_path() -> Path:
    return home() / "board.json"


def empty_board() -> dict:
    return {
        "id": "desk",
        "title": "Workman",
        "groups": [
            {"id": gid, "title": title, "color": color}
            for gid, title, color in GROUPS
        ],
        "items": [],
        "updated": now(),
    }


def load_board() -> dict:
    data = _read(_board_path(), empty_board())
    data.setdefault("groups", empty_board()["groups"])
    data.setdefault("items", [])
    data.setdefault("title", "Workman")
    return data


def save_board(board: dict) -> dict:
    board["updated"] = now()
    _atomic(_board_path(), board)
    return board


def find_item(board: dict, item_id: str) -> dict | None:
    for it in board.get("items") or []:
        if it.get("id") == item_id:
            return it
    return None


def upsert_item(fields: dict) -> dict:
    board = load_board()
    item_id = str(fields.get("id") or "")
    item = find_item(board, item_id) if item_id else None
    if item is None:
        item = {
            "id": item_id or new_id(),
            "title": "",
            "brief": "",
            "group": "backlog",
            "assignee": "",
            "cwd": "",
            "chat_id": "",
            "created": now(),
        }
        board["items"].append(item)
    for key in ("title", "brief", "group", "assignee", "cwd", "chat_id"):
        if key in fields and fields[key] is not None:
            item[key] = fields[key]
    group = item.get("group") or "backlog"
    if group not in {g[0] for g in GROUPS}:
        item["group"] = "backlog"
    agent = (item.get("assignee") or "").strip().lower()
    if agent and agent not in AGENTS:
        item["assignee"] = ""
    item["updated"] = now()
    save_board(board)
    return item


def delete_item(item_id: str) -> bool:
    board = load_board()
    before = len(board["items"])
    board["items"] = [i for i in board["items"] if i.get("id") != item_id]
    if len(board["items"]) == before:
        return False
    save_board(board)
    return True
