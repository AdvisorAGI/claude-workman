"""The event feed: `collect` appends new events to
~/orchestra/session-events.jsonl (deduped by key, `<epoch>:<seq>` cursor), and
`session_events` reads it back after a cursor."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .common import (COMPACT_BAND, EVENT_KINDS, FINISHED_RECENT_S, QUIET_S, SILENT_MIN, SILENT_SPAN_MIN,
                     TEXT_LEN, _read_json, _read_span, home_dir, iso, orchestra_dir, parse_ts, redact)
from .state import Scan


FLEET_BUCKET_S = 600         # hook-feed permission prompts: one event per session per 10 min

# The CLI's own permission-prompt notifications, nothing else: a message that
# merely contains the word "permission" is not a prompt.
FLEET_PERMISSION_RE = re.compile(r"^(?:Tool permission requested|"
                                 r"Claude needs your permission to use (\S+))$")

EVENTS_KEEP_LINES = 4000

SEEN_KEEP = 20000

def events_path(home: str | Path | None = None) -> Path:
    return orchestra_dir(home) / "session-events.jsonl"

def _state_path(home: str | Path | None = None) -> Path:
    return orchestra_dir(home) / "session-events.state.json"

def fleet_feed(home: str | Path | None = None) -> Path:
    env = os.environ.get("WORKMAN_FLEET_FEED")
    return Path(env) if env and not home else home_dir(home) / ".claude" / "fleet-session-events.jsonl"

def events_for(row: dict, now: float, band: tuple[int, int] = COMPACT_BAND,
               silent_min: float = SILENT_MIN, quiet_s: float = QUIET_S) -> list[dict]:
    """Every event this session's state implies, each with a dedupe key that is
    stable until the underlying fact changes."""
    sid, out = row["session_id"], []
    base = {"session": row["id"], "session_id": sid, "account": row["account"],
            "title": row["title"], "front_end": row["front_end"],
            "local_id": row.get("local_id", ""), "pid": row.get("pid")}

    def ev(kind: str, key: str, detail: str, **extra: Any) -> None:
        out.append({**base, "kind": kind, "key": f"{sid}:{kind}:{key}", "detail": detail, **extra})

    for q in row["pending_questions"]:
        qs = q["questions"]
        ev("question", q["tool_use_id"],
           " | ".join(x["question"] for x in qs)[:TEXT_LEN] or "AskUserQuestion",
           questions=qs)
    if row["state"] == "needs_input" and not row["pending_questions"]:
        ev("permission", row.get("last_assistant_uuid") or str(int(row["mtime"])), row["why"],
           pending_tools=row["pending_tools"])
    # Every denial in the current turn, at once: the session may keep working
    # around it, but the owner still has to hear about it (doctrine: ask, never
    # work around).
    for d in row["denials"]:
        ev("blocked", d["tool_use_id"] or str(d.get("ts")),
           f"{d['tool']} denied ({d['kind']})" + (f": {d['reason']}" if d["reason"] else ""))
    if row["state"] == "blocked" and not row["denials"] and row.get("api_error"):
        ev("blocked", row["api_error"].get("uuid") or "api", row["why"])
    if row["state"] == "limit_hit":
        ev("limit_hit", (row.get("api_error") or {}).get("uuid") or str(int(row["mtime"])),
           row["why"])
    age = row["age_s"]
    if row["state"] == "silent" and age <= (silent_min + SILENT_SPAN_MIN) * 60:
        ev("silent", str(int(row["mtime"])), row["why"])
    clean = (row["last_kind"] == "assistant_end" and age >= quiet_s
             and not row["pending_questions"])
    if clean and not row["ctx_stale"] and band[0] <= row["ctx_tokens"] <= band[1]:
        ev("compact_ready", f"{row['compacts']}:{row['ctx_tokens'] // 50000}",
           f"ctx {row['ctx_tokens'] // 1000}k, turn ended {age // 60} min ago",
           ctx_tokens=row["ctx_tokens"])
    if clean and row["state"] == "idle" and age <= FINISHED_RECENT_S:
        ev("finished", row.get("last_assistant_uuid") or str(int(row["mtime"])),
           f"turn ended {age // 60} min ago")
    return out

def _fleet_events(home: str | Path | None, st: dict, now: float, known: dict[str, dict]
                  ) -> list[dict]:
    """New permission prompts from the Notification hook feed. Questions come
    from the transcript (with their text), and Stop lines are `finished`."""
    path = fleet_feed(home)
    try:
        s = path.stat()
    except OSError:
        return []
    cur = st.get("fleet") or {}
    start = cur.get("off", 0) if cur.get("ino") == s.st_ino and cur.get("off", 0) <= s.st_size else 0
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read(4 << 20)
    except OSError:
        return []
    end = data.rfind(b"\n")
    if end < 0:
        return []
    st["fleet"] = {"ino": s.st_ino, "off": start + end + 1}
    out = []
    for raw in data[:end + 1].splitlines():
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("event") != "Notification":
            continue
        msg = str(d.get("msg") or "")
        m = FLEET_PERMISSION_RE.match(msg.strip())
        if not m or m.group(1) == "AskUserQuestion":
            continue  # questions come from the transcript, with their text
        t = parse_ts(d.get("ts")) or 0
        if now - t > FINISHED_RECENT_S:
            continue  # history: the first read of a long feed must not flood
        sid = str(d.get("session_id") or "")
        row = known.get(sid) or {}
        out.append({"session": sid[:8], "session_id": sid, "account": row.get("account", ""),
                    "title": row.get("title", ""), "front_end": row.get("front_end", "unknown"),
                    "local_id": row.get("local_id", ""), "pid": row.get("pid"),
                    "kind": "permission",
                    # one per session and message per 10 min: a worker that asks
                    # for six tools in a row is one thing to look at, not six
                    "key": "fleet:%s:%s:%d" % (sid[:36], hashlib.sha1(msg.encode()).hexdigest()[:8],
                                               int(t // FLEET_BUCKET_S)),
                    "detail": redact(msg, TEXT_LEN), "cwd": redact(d.get("cwd"), 160),
                    "source": "fleet-session-events"})
    return out

def _seen_from_file(path: Path) -> list[str]:
    keys = []
    for line in _read_span(path, max(0, (path.stat().st_size if path.exists() else 0) - (2 << 20)),
                           2 << 20):
        try:
            keys.append(json.loads(line)["key"])
        except (ValueError, KeyError, TypeError):
            continue
    return keys

def _feed_position(home: str | Path | None, path: Path, now: float) -> tuple[dict, int, str]:
    """(state, seq, epoch). A lost state file is rebuilt from the feed's tail;
    a new feed gets a new epoch, so a cursor from an earlier feed is never
    read as one of ours."""
    st = _read_json(_state_path(home)) or {}
    st = st if isinstance(st, dict) else {}
    seq, epoch = int(st.get("seq") or 0), str(st.get("epoch") or "")
    if (not seq or not epoch) and path.exists():
        for line in _read_span(path, max(0, path.stat().st_size - 65536), 65536):
            try:
                e = json.loads(line)
                seq = max(seq, int(e.get("seq") or 0))
                epoch = epoch or str(e.get("epoch") or "")
            except (ValueError, TypeError, AttributeError):
                continue
    return st, seq, epoch or "%x%s" % (int(now), os.urandom(2).hex())


def _found(scan: Scan, home: str | Path | None, st: dict, active_within_min: float,
           silent_min: float, band: tuple[int, int], include_self: bool) -> tuple[list, int]:
    """(every event the current state implies, number of sessions scanned)."""
    rows = scan.rows(active_within_min * 60, silent_min=silent_min)
    found: list[dict] = []
    for r in rows:
        if not r["is_self"] or include_self:
            found.extend(events_for(r, scan.now, band, silent_min))
    found.extend(_fleet_events(home, st, scan.now, {r["session_id"]: r for r in rows}))
    return found, len(rows)


def _trimmed_seen(seen_list: list[str], found: list[dict]) -> list[str]:
    """The newest SEEN_KEEP keys, plus every key whose condition is still live:
    dropping a live key would re-emit the same event on the next pass."""
    live = {e["key"] for e in found}
    keys = list(dict.fromkeys(seen_list))
    return [k for k in keys if k not in live][-SEEN_KEEP:] + [k for k in keys if k in live]


def _save_state(home: str | Path | None, st: dict) -> None:
    tmp = _state_path(home).with_suffix(".tmp")
    tmp.write_text(json.dumps(st), encoding="utf-8")
    os.replace(tmp, _state_path(home))


def collect(home: str | Path | None = None, now: float | None = None,
            active_within_min: float = 180, silent_min: float = SILENT_MIN,
            band: tuple[int, int] = COMPACT_BAND, proc_root: Path = Path("/proc"),
            include_self: bool = False) -> dict[str, Any]:
    """One zero-model pass: append every new event to session-events.jsonl,
    deduped by key. Safe to run from a timer; a second collector waits its turn."""
    odir = orchestra_dir(home)
    odir.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(odir / "session-events.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"ok": False, "busy": True, "error": "another collector is running"}
        scan = Scan(home, now, proc_root)
        path = events_path(home)
        st, seq, epoch = _feed_position(home, path, scan.now)
        seen_list = list(st.get("seen") or []) or _seen_from_file(path)
        seen = set(seen_list)
        found, sessions = _found(scan, home, st, active_within_min, silent_min, band, include_self)
        new = []
        for e in found:
            if e["key"] not in seen:
                seen.add(e["key"])
                seen_list.append(e["key"])
                seq += 1
                new.append({"seq": seq, "epoch": epoch, "ts": iso(scan.now), **e})
        if new:
            with path.open("a", encoding="utf-8") as fh:
                fh.writelines(json.dumps(e, separators=(",", ":")) + "\n" for e in new)
            _rotate(path)
        st.update(seen=_trimmed_seen(seen_list, found), seq=seq, epoch=epoch,
                  last_collect=iso(scan.now))
        _save_state(home, st)
        return {"ok": True, "now": iso(scan.now), "sessions": sessions, "new": len(new),
                "cursor": f"{epoch}:{seq}", "events": new, "path": str(path)}
    finally:
        os.close(lock_fd)


def _rotate(path: Path) -> None:
    try:
        if path.stat().st_size < (8 << 20):
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-EVENTS_KEEP_LINES:]
        tmp = path.with_suffix(".rot")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass

def session_events(since_cursor: str = "", kinds: str = "", limit: int = 50,
                   collect_first: bool = True, home: str | Path | None = None,
                   now: float | None = None, proc_root: Path = Path("/proc")) -> dict[str, Any]:
    """New events after `since_cursor` (a seq number). An empty cursor returns
    the newest `limit` events. Runs one collector pass first unless told not
    to, so it works with or without the timer."""
    collected = None
    if collect_first:
        collected = collect(home, now, proc_root=proc_root)
    cur = str(since_cursor or "").strip()
    cur_epoch, _, cur_seq = cur.rpartition(":")
    try:
        after = int(cur_seq) if cur else None
    except ValueError:
        return {"ok": False, "error": f"cursor must be '<epoch>:<seq>' or a number, got "
                                      f"{since_cursor!r}"}
    want = {k.strip() for k in (kinds or "").split(",") if k.strip()}
    bad = want - set(EVENT_KINDS)
    if bad:
        return {"ok": False, "error": f"unknown kinds {sorted(bad)}; known {list(EVENT_KINDS)}"}
    path = events_path(home)
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                rows.append(e)
    top = max((int(e.get("seq") or 0) for e in rows), default=0)
    st = _read_json(_state_path(home))
    epoch = str((st or {}).get("epoch") or "") if isinstance(st, dict) else ""
    epoch = epoch or next((str(e.get("epoch")) for e in reversed(rows) if e.get("epoch")), "")
    # A cursor from another feed (the file was reset) or past this feed's end
    # has seen none of these events: start over from the first, never skip.
    reset = after is not None and ((cur_epoch and epoch and cur_epoch != epoch) or after > top)
    if reset:
        after = 0
    events = [e for e in rows if (after is None or int(e.get("seq") or 0) > after)
              and (not want or e.get("kind") in want)]
    last = top
    more = False
    if after is None:
        events = events[-max(1, int(limit)):]
    elif len(events) > limit:
        events, more = events[:max(1, int(limit))], True
        last = int(events[-1]["seq"])
    out = {"ok": True, "cursor": f"{epoch}:{last}", "count": len(events), "more": more,
           "events": events, "path": str(path)}
    if reset:
        out["cursor_reset"] = True
    if collected is not None:
        out["collect"] = {k: collected.get(k) for k in ("ok", "new", "sessions", "busy", "error")
                          if k in collected}
    return out
