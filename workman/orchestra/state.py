"""A session's state and front end, the pre-action turn check, and `Scan`, one
pass over every source that yields the rows the tools print."""

from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import registry as rg
from .common import (CTX_WINDOW, NEED_STATES, QUIET_S, SILENT_MIN, TITLE_LEN, home_dir, iso,
                     redact)
from .registry import (accounts, ancestors, caller_ids, desktop_sessions, is_self, process_info,
                       registry, seat_runs)
from .transcripts import parse_transcript, transcripts


BLOCKING_STATUS = ("busy", "waiting")

def turn_ready(path: str | Path, quiet_s: float = QUIET_S, self_id: str = "",
               now: float | None = None, home: str | Path | None = None,
               proc_root: Path = Path("/proc"), after_compact: bool = False) -> tuple[bool, str]:
    """The full clean-turn check, run again right before any UI action: the
    caller is known and is not the target, quiet >= quiet_s, no open question,
    the MAIN chain's last record is an assistant end_turn (a subagent's
    end_turn never counts), and the live registry is neither busy nor waiting.

    after_compact (the resume line): /compact leaves its own local-command echo
    as the main chain's last record, so that, or an end_turn, is the clean
    state; anything else (a new prompt, a tool call) means someone is using it.
    """
    info = parse_transcript(path, count_compacts=False)
    if info.get("error"):
        return False, "transcript unreadable"
    now = time.time() if now is None else now
    sid = info["session_id"]
    ids = caller_ids(self_id, home, proc_root)
    if not ids:
        return False, ("caller unknown: " + (rg.bad_self_id(self_id) or "no session id in the "
                                              "env, the process tree or self_id"))
    if is_self(sid, ids=ids):
        return False, "the target is the caller's own session"
    age = now - info["mtime"]
    if age < quiet_s:
        return False, f"quiet {int(age)} s, need {int(quiet_s)} s"
    if info["pending_questions"]:
        return False, "AskUserQuestion open"
    ended = info["last_kind"] == "assistant_end" and info["stop_reason"] == "end_turn"
    if not ended and not (after_compact and info["last_kind"] == "local_command"):
        return False, f"turn not ended (last={info['last_kind']} stop={info['stop_reason']})"
    reg = registry(home, proc_root).get(sid) or {}
    if reg.get("status") in BLOCKING_STATUS:
        return False, f"registry {reg.get('status')}"
    return True, ""

RESET_RE = re.compile(r"resets\s+(?:([A-Z][a-z]{2})\s+(\d{1,2}),\s*)?(\d{1,2})(?::(\d{2}))?\s*"
                      r"(am|pm)\s*\(UTC\)", re.I)

MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                      "sep", "oct", "nov", "dec"), 1)}

def limit_reset(text: str, at: float | None) -> float | None:
    """When a "You've hit your ... limit · resets 3:50pm (UTC)" error lifts, as
    epoch seconds, or None when the text does not say."""
    m = RESET_RE.search(text or "")
    if not m or not at:
        return None
    base = datetime.fromtimestamp(at, timezone.utc)
    hour = int(m.group(3)) % 12 + (12 if m.group(5).lower() == "pm" else 0)
    minute = int(m.group(4) or 0)
    try:
        if m.group(1):
            month = MONTHS.get(m.group(1).lower())
            if not month:
                return None
            when = base.replace(month=month, day=int(m.group(2)), hour=hour, minute=minute,
                                second=0, microsecond=0)
            if when.timestamp() < at - 86400:
                when = when.replace(year=when.year + 1)
        else:
            when = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if when.timestamp() < at:
                when = datetime.fromtimestamp(when.timestamp() + 86400, timezone.utc)
    except ValueError:
        return None
    return when.timestamp()

def classify(info: dict, now: float, reg: dict | None = None,
             silent_min: float = SILENT_MIN, quiet_s: float = QUIET_S) -> tuple[str, str]:
    """(state, why). Order matters: a limit beats a question beats a block
    beats work; a live registry entry beats the transcript's guess."""
    age = now - (info.get("mtime") or now)
    reg = reg or {}
    alive = bool(reg.get("alive"))
    err = info.get("api_error")
    if err and "rate_limit" in err.get("error", ""):
        reset = limit_reset(err.get("text", ""), err.get("ts"))
        if reset is None or now < reset:
            return "limit_hit", err.get("text") or "usage limit"
        return "idle", f"usage limit lifted at {iso(reset)}, no turn since"
    if info.get("pending_questions"):
        q = info["pending_questions"][-1]["questions"]
        return "needs_input", "AskUserQuestion: " + (q[0]["question"] if q else "?")
    if alive and reg.get("status") == "waiting":
        return "needs_input", "waiting: " + redact(reg.get("waitingFor") or "input", 60)
    if info.get("denials") and (info.get("last_kind") == "assistant_end" or age >= quiet_s):
        d = info["denials"][-1]
        return "blocked", f"{d['tool']} denied ({d['kind']})" + (
            f": {d['reason']}" if d.get("reason") else "")
    if err:
        return "blocked", f"api error {err.get('error')}: {err.get('text')}"
    kind = info.get("last_kind")
    if kind in ("interrupted", "local_command"):
        if alive and reg.get("status") == "busy" and age < quiet_s:
            return "working", kind.replace("_", " ")
        return "idle", ("interrupted by the user" if kind == "interrupted"
                        else "local command ran") + f" {int(age // 60)} min ago"
    if kind == "assistant_end":
        if alive and reg.get("status") == "busy" and age < quiet_s:
            return "working", "turn ending"
        return "idle", f"turn ended {int(age // 60)} min ago"
    if alive and reg.get("status") == "idle" and age >= quiet_s:
        return "idle", "registry idle"
    if kind is None:
        return ("idle", "no conversation records") if age >= quiet_s else ("working", "starting")
    if age >= silent_min * 60 and kind in ("assistant_tool", "tool_result", "assistant_other",
                                          "user_prompt"):
        where = {"assistant_tool": "inside a tool call",
                 "tool_result": "after a tool result, no reply",
                 "user_prompt": "after a prompt, no reply"}.get(kind, "mid-turn")
        dead = "" if alive or not reg else ", process gone"
        return "silent", f"silent {int(age // 60)} min {where}{dead}"
    return "working", f"active {int(age)} s ago"

def front_end(info: dict, reg: dict | None, proc: dict | None, desk: dict | None,
              tmux_pids: set[int] | None = None, ancestry: list[int] | None = None,
              ccr_tip: bool = False, seat_unit: str | None = None) -> dict[str, Any]:
    """Which front end owns this session and how to reach it."""
    reg, proc = reg or {}, proc or {}
    if desk or reg.get("host_local_id") or reg.get("entrypoint") == "claude-desktop":
        return {"front_end": "desktop",
                "local_id": (desk or {}).get("sessionId") or reg.get("host_local_id") or ""}
    if proc.get("rc_name") or str(proc.get("unit", "")).startswith("claude-rc-"):
        return {"front_end": "remote_control", "rc_name": proc.get("rc_name") or ""}
    if proc and proc.get("print") and not proc.get("sdk_url"):
        return {"front_end": "headless", "unit": proc.get("unit") or ""}
    if tmux_pids and ancestry and tmux_pids.intersection(ancestry):
        return {"front_end": "tmux"}
    if reg.get("bridge") or ccr_tip:
        return {"front_end": "remote_control", "rc_name": ""}
    if seat_unit is not None:
        return {"front_end": "headless", "unit": seat_unit}
    if proc and info.get("entrypoint") == "cli":
        return {"front_end": "terminal"}
    if str(info.get("entrypoint") or "").startswith("sdk-"):
        # print mode or an SDK script: no window and no claude.ai page to reach
        return {"front_end": "headless", "unit": ""}
    return {"front_end": "unknown"}

class Scan:
    """One pass over the machine's sessions; every source read once."""

    def __init__(self, home: str | Path | None = None, now: float | None = None,
                 proc_root: Path = Path("/proc"), run: Callable[..., Any] = subprocess.run):
        self.home = home_dir(home)
        self.now = time.time() if now is None else now
        self.proc_root = proc_root
        self.accts = accounts(self.home)
        self.reg = registry(self.home, proc_root)
        self.desk = desktop_sessions(self.home)
        self.seats = seat_runs(self.home)
        self.callers = caller_ids(home=self.home, proc_root=proc_root)
        self._run = run
        self._tmux: set[int] | None = None

    def tmux(self) -> set[int]:
        if self._tmux is None:
            self._tmux = rg.tmux_pane_pids(self._run)
        return self._tmux

    def account_of(self, cfg: str, desk: dict | None) -> tuple[str, str]:
        if desk and self.accts["by_uuid"].get(desk.get("account_uuid") or ""):
            return self.accts["by_uuid"][desk["account_uuid"]], "desktop store"
        d = self.accts["by_dir"].get(cfg) or {}
        return d.get("email") or "", d.get("source") or "none"

    def row(self, path: Path, silent_min: float = SILENT_MIN) -> dict[str, Any]:
        info = parse_transcript(path)
        if info.get("error"):
            return info
        sid = info["session_id"]
        is_sub = path.parent.name == "subagents"
        if is_sub:
            sid = path.parent.parent.name + "/" + path.stem
        cfg = path.parents[4 if is_sub else 2].name
        reg = None if is_sub else self.reg.get(info["session_id"])
        desk = None if is_sub else self.desk.get(info["session_id"])
        proc = process_info(reg["pid"], self.proc_root) if reg and reg.get("alive") else {}
        anc = ancestors(reg["pid"], self.proc_root) if reg and reg.get("alive") else []
        tmux = self.tmux() if proc and not proc.get("rc_name") and not desk else set()
        fe = front_end(info, reg, proc, desk, tmux, anc,
                       ccr_tip=(path.parent / path.stem / "ccr-tip.json").exists(),
                       seat_unit=self.seats.get(info["session_id"]))
        state, why = classify(info, self.now, reg, silent_min)
        email, src = self.account_of(cfg, desk)
        ctx = 0 if info["ctx_stale"] else info["ctx_tokens"]
        front_model = (desk or {}).get("model") or proc.get("model") or ""
        front_effort = (desk or {}).get("effort") or proc.get("effort") or ""
        title = redact((desk or {}).get("title"), TITLE_LEN) or info["title"]
        return {
            "id": info["session_id"][:8], "session_id": sid, "subagent": is_sub,
            "account": email, "account_source": src, "config_dir": cfg,
            "model": info["model"] or front_model, "effort": info["effort"] or front_effort,
            "front_model": front_model, "front_effort": front_effort,
            "model_source": ("transcript" if info["model"] else
                             "desktop store" if desk else "process args" if proc else ""),
            "ctx_tokens": ctx, "ctx_pct": round(100.0 * ctx / CTX_WINDOW, 1),
            "ctx_stale": info["ctx_stale"], "compacts": info["compacts"],
            "state": state, "why": why,
            "age_s": int(self.now - info["mtime"]),
            "title": title or info["first_prompt"] or info["last_prompt"],
            "first_prompt": info["first_prompt"],
            "cwd": info["cwd"] or (reg or {}).get("cwd") or "",
            **fe,
            "pid": (reg or {}).get("pid") if reg and reg.get("alive") else None,
            "registry_status": (reg or {}).get("status") if reg else None,
            "stop_reason": info["stop_reason"], "last_kind": info["last_kind"],
            "pending_questions": info["pending_questions"],
            "pending_tools": info["pending_tools"], "denials": info["denials"],
            "api_error": info["api_error"], "last_assistant_uuid": info["last_assistant_uuid"],
            "path": info["path"], "mtime": info["mtime"], "malformed": info["malformed"],
            "is_self": is_self(info["session_id"], local_id=fe.get("local_id", ""),
                               ids=self.callers),
        }

    def rows(self, within_s: float, include_subagents: bool = False,
             silent_min: float = SILENT_MIN) -> list[dict]:
        out = []
        for p in transcripts(self.home, within_s, include_subagents, self.now):
            r = self.row(p, silent_min)
            if not r.get("error"):
                out.append(r)
        return out

BRIEF_KEYS = ("id", "account", "model", "effort", "ctx_tokens", "ctx_pct", "state", "why",
              "age_s", "title", "cwd", "front_end", "local_id", "rc_name", "unit", "pid",
              "front_model", "front_effort", "compacts", "is_self", "session_id", "subagent")

def brief(row: dict) -> dict:
    return {k: row[k] for k in BRIEF_KEYS if k in row}

def need_of(row: dict) -> dict[str, Any] | None:
    """What this session waits on, or None."""
    if row["state"] not in NEED_STATES:
        return None
    need: dict[str, Any] = {k: row.get(k) for k in ("id", "session_id", "account", "title",
                                                     "front_end", "local_id", "pid", "cwd",
                                                     "age_s", "state", "why", "model")}
    if row["pending_questions"]:
        need["need"] = "question"
        need["questions"] = row["pending_questions"][-1]["questions"]
    elif row["state"] == "needs_input":
        need["need"] = "permission"
        need["pending_tools"] = row["pending_tools"]
    elif row["state"] == "limit_hit":
        need["need"] = "limit_hit"
        need["limit"] = (row.get("api_error") or {}).get("text", "")
    elif row["state"] == "blocked":
        need["need"] = "blocked"
        need["denials"] = row["denials"][-3:]
        if row.get("api_error"):
            need["api_error"] = row["api_error"]
    else:
        need["need"] = "silent"
        need["pending_tools"] = row["pending_tools"]
    return need
