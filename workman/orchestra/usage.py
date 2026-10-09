"""Usage per session or account from transcripts: tokens (once per message
id), images, turns, API calls, and the first prompts that several sessions
started from. Pure parsing, zero model calls."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .common import TEXT_LEN, TITLE_LEN, home_dir, iso, redact
from .registry import accounts, desktop_sessions
from .transcripts import _content, is_prompt, prompt_text, signature, transcripts


REPEAT_MIN_CHARS = 20   # "hi" twice is not the same work done twice
USAGE_FIELDS = ("input", "cache_read", "cache_creation", "output", "total", "images", "turns",
                "api_calls")


def _images(content: Any) -> int:
    """Image blocks in a user record, including those inside tool results."""
    n = 0
    for c in content if isinstance(content, list) else []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "image":
            n += 1
        elif c.get("type") == "tool_result" and isinstance(c.get("content"), list):
            n += sum(1 for x in c["content"] if isinstance(x, dict) and x.get("type") == "image")
    return n


def _records_since(fh: Any, out: dict) -> Any:
    """Assistant-with-usage and user records, parsed; a malformed line is counted."""
    for raw in fh:
        if not ((b'"assistant"' in raw and b'"usage"' in raw) or b'"user"' in raw):
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            out["malformed"] += 1
            continue
        if isinstance(rec, dict):
            yield rec


def _add_usage(out: dict, per_msg: dict) -> dict:
    for m in per_msg.values():
        u = m["u"]
        out["input"] += int(u.get("input_tokens") or 0)
        out["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
        out["cache_creation"] += int(u.get("cache_creation_input_tokens") or 0)
        out["output"] += int(u.get("output_tokens") or 0)
        out["models"][m["model"]] = out["models"].get(m["model"], 0) + 1
    out["api_calls"] = len(per_msg)
    out["total"] = out["input"] + out["cache_read"] + out["cache_creation"] + out["output"]
    out["first_prompt"] = redact(out["first_prompt"], TEXT_LEN)
    return out


def usage_of(path: str | Path, since: float) -> dict[str, Any]:
    """Tokens, images, turns and API calls in one transcript since `since`.

    Pure parsing. An assistant message is written once per content block with
    the same usage, so usage is counted once per message id.
    """
    since_iso = iso(since)[:19]
    per_msg: dict[str, dict] = {}
    out = {"input": 0, "cache_read": 0, "cache_creation": 0, "output": 0, "images": 0,
           "turns": 0, "api_calls": 0, "first_prompt": "", "malformed": 0, "models": {}}
    try:
        fh = open(path, "rb")
    except OSError:
        return {**out, "error": "unreadable"}
    with fh:
        for rec in _records_since(fh, out):
            if rec.get("type") == "user" and not out["first_prompt"] and is_prompt(rec):
                out["first_prompt"] = prompt_text(rec)
            ts = rec.get("timestamp")
            if not isinstance(ts, str) or ts[:19] < since_iso:
                continue
            if rec.get("type") == "assistant":
                msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
                u = msg.get("usage")
                if isinstance(u, dict) and msg.get("model") != "<synthetic>":
                    key = msg.get("id") or rec.get("requestId") or rec.get("uuid") or str(len(per_msg))
                    per_msg[key] = {"u": u, "model": msg.get("model") or "?"}
            elif rec.get("type") == "user":
                out["images"] += _images(_content(rec))
                out["turns"] += 1 if is_prompt(rec) else 0
    return _add_usage(out, per_msg)


def _session_rows(h: Path, since: float, window_s: float, include_subagents: bool,
                  now: float) -> dict[str, dict]:
    """{session_id: usage row}; a subagent transcript adds to its parent session."""
    accts, desk = accounts(h), desktop_sessions(h)
    rows: dict[str, dict] = {}
    for p in transcripts(h, window_s, include_subagents, now):
        is_sub = p.parent.name == "subagents"
        sid = p.parent.parent.name if is_sub else p.stem
        cfg = p.parents[4 if is_sub else 2].name
        u = usage_of(p, since)
        if not u["api_calls"] and not u["turns"]:
            continue
        d = desk.get(sid)
        email = (accts["by_uuid"].get((d or {}).get("account_uuid") or "")
                 or (accts["by_dir"].get(cfg) or {}).get("email") or cfg)
        r = rows.setdefault(sid, {"id": sid[:8], "session_id": sid, "account": email,
                                  "config_dir": cfg, "first_prompt": "", "subagent_files": 0,
                                  **{f: 0 for f in USAGE_FIELDS}, "models": {}})
        for f in USAGE_FIELDS:
            r[f] += u[f]
        for m, n in u["models"].items():
            r["models"][m] = r["models"].get(m, 0) + n
        if is_sub:
            r["subagent_files"] += 1
        elif u["first_prompt"]:
            r["first_prompt"] = u["first_prompt"]
    return rows


def _repeats(rows: dict[str, dict]) -> list[dict]:
    """First prompts that more than one session started from; marks each row."""
    groups: dict[str, dict] = {}
    for r in rows.values():
        sig = signature(r["first_prompt"])
        if len(sig) < REPEAT_MIN_CHARS:
            continue
        g = groups.setdefault(sig, {"prompt": r["first_prompt"][:TITLE_LEN], "sessions": 0,
                                    "total": 0, "accounts": set(), "ids": []})
        g["sessions"] += 1
        g["total"] += r["total"]
        g["accounts"].add(r["account"])
        g["ids"].append(r["id"])
    for r in rows.values():
        sig = signature(r["first_prompt"])
        g = groups.get(sig) if len(sig) >= REPEAT_MIN_CHARS else None
        r["repeat"] = g["sessions"] if g else 1
        r["first_prompt"] = r["first_prompt"][:TITLE_LEN]
    return sorted(({**g, "accounts": sorted(g["accounts"]), "ids": g["ids"][:12]}
                   for g in groups.values() if g["sessions"] > 1),
                  key=lambda g: (-g["total"], -g["sessions"]))


def _by_account(rows: dict[str, dict]) -> list[dict]:
    agg: dict[str, dict] = {}
    for r in rows.values():
        a = agg.setdefault(r["account"], {"account": r["account"], "sessions": 0,
                                          **{f: 0 for f in USAGE_FIELDS}})
        a["sessions"] += 1
        for f in USAGE_FIELDS:
            a[f] += r[f]
    return sorted(agg.values(), key=lambda a: -a["total"])


def session_usage(window_h: float = 24, by: str = "session", top: int = 25,
                  include_subagents: bool = False, home: str | Path | None = None,
                  now: float | None = None) -> dict[str, Any]:
    """Usage per session or account over the window, ranked by total tokens,
    with the first prompts that more than one session started from."""
    if by not in ("session", "account"):
        return {"ok": False, "error": "by must be 'session' or 'account'"}
    h = home_dir(home)
    now = time.time() if now is None else now
    since = now - window_h * 3600
    rows = _session_rows(h, since, window_h * 3600, include_subagents, now)
    repeats = _repeats(rows)
    totals = {f: sum(r[f] for r in rows.values()) for f in USAGE_FIELDS}
    ranked = (_by_account(rows) if by == "account"
              else sorted(rows.values(), key=lambda r: -r["total"]))
    return {"ok": True, "window_h": window_h, "by": by, "since": iso(since), "now": iso(now),
            "sessions": len(rows), "totals": totals, "rows": ranked[:max(1, int(top))],
            "repeats": repeats[:max(1, int(top))]}
