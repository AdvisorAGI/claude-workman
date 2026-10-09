"""One Claude Code transcript (<config>/projects/<slug>/<session>.jsonl): its
records, its first prompt, and its state, read from head and tail without the
model. Sidechain (subagent) records never decide the main session's state."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable

from .common import (HEAD_BYTES, TAIL_BYTES, TEXT_LEN, TITLE_LEN, _read_span, config_dirs, parse_ts,
                     redact)


# Denial text (agent-watch's phrases). Only read on is_error tool results so a
# Read of a file that merely quotes them never fires.
CLASSIFIER = "auto mode classifier"

DENY_PHRASES = ("Permission for this action was denied", "Permission for this", "was denied",
                "requires approval")

DENIAL_KINDS = {"automode-blocked": "classifier", "permission-rule": "permission",
                "user-rejected": "user_rejected"}

TAG_RE = re.compile(r"<(system-reminder|local-command-stdout|local-command-stderr|"
                    r"command-message|command-args)>.*?</\1>", re.S)

CMD_RE = re.compile(r"<command-name>\s*(/?[\w:-]+)\s*</command-name>")

SIG_RE = re.compile(r"[0-9a-f]{8,}|\d+")

# User records the harness writes for a local slash command (/compact, /model,
# /effort): no model turn follows them. An interrupt ends the turn by hand.
LOCAL_CMD = ("<command-name>", "<local-command-stdout>", "<local-command-stderr>",
             "<local-command-caveat>")

INTERRUPT = "[Request interrupted by user"

BOUNDARY_RE = re.compile(rb'"subtype":\s*"compact_boundary"')

def _records(lines: Iterable[str]) -> list[dict]:
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # a malformed or cut line is skipped like any other
        if isinstance(rec, dict):
            out.append(rec)
    return out

def _content(rec: dict) -> Any:
    msg = rec.get("message")
    return msg.get("content") if isinstance(msg, dict) else None

def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(x.get("text") or "") for x in content
                        if isinstance(x, dict) and x.get("type") == "text")
    return ""

def is_tool_result(rec: dict) -> bool:
    c = _content(rec)
    return isinstance(c, list) and any(isinstance(x, dict) and x.get("type") == "tool_result"
                                       for x in c)

def user_kind(rec: dict) -> str:
    """"prompt", "interrupted", "local_command" or "" for a user record."""
    if (rec.get("type") != "user" or rec.get("isMeta") or rec.get("isCompactSummary")
            or rec.get("isSidechain") or is_tool_result(rec)):
        return ""
    text = _text_of(_content(rec)).lstrip()
    if not text:
        return ""
    if text.startswith(INTERRUPT):
        return "interrupted"
    if text.startswith(LOCAL_CMD):
        return "local_command"
    return "prompt"

def is_prompt(rec: dict) -> bool:
    """A user record that starts a model turn: not a tool result, not meta, not
    the compact summary, not a local slash command or an interrupt."""
    return user_kind(rec) == "prompt"

def prompt_text(rec: dict) -> str:
    """The human-readable part of a prompt: tags stripped, a slash command named."""
    raw = _text_of(_content(rec))
    cmd = CMD_RE.search(raw)
    text = TAG_RE.sub(" ", raw)
    text = re.sub(r"<[^>]{1,40}>", " ", text)
    text = " ".join(text.split())
    if not text and cmd:
        text = cmd.group(1)
    return text

def signature(text: str) -> str:
    """What makes two first prompts "the same work": lowercased, ids and
    numbers folded, first 200 chars."""
    return " ".join(SIG_RE.sub("#", (text or "").lower()).split())[:200]

def transcripts(home: str | Path | None = None, within_s: float | None = None,
                include_subagents: bool = False, now: float | None = None) -> list[Path]:
    """Every transcript, newest first, optionally only those written within_s."""
    now = time.time() if now is None else now
    found = []
    for d in config_dirs(home):
        pats = ["projects/*/*.jsonl"] + (["projects/*/*/subagents/*.jsonl"]
                                         if include_subagents else [])
        for pat in pats:
            for p in d.glob(pat):
                try:
                    m = p.stat().st_mtime
                except OSError:
                    continue
                if within_s is None or now - m <= within_s:
                    found.append((m, p))
    return [p for _, p in sorted(found, key=lambda x: -x[0])]

def _usage_ctx(usage: dict) -> int:
    return sum(int(usage.get(k) or 0) for k in
               ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))

def _denial(item: dict, rec: dict) -> tuple[str, str] | None:
    """(kind, reason) when this tool_result is a denial that waits on the
    owner, else None. The owner's own answer (a rejection, or aborting the
    prompt) is not one: it already came from the person it would go to."""
    pd = rec.get("permissionDecision")
    if isinstance(pd, dict) and str(pd.get("source") or "").startswith("user"):
        return None
    kind = DENIAL_KINDS.get(rec.get("toolDenialKind") or "")
    if kind == "user_rejected":
        return None
    text = item.get("content")
    if isinstance(text, list):
        text = " ".join(str(x.get("text") or "") for x in text if isinstance(x, dict))
    text = str(text or "")[:800]
    m = re.search(r"Reason:\s*(.{0,160})", text)
    reason = m.group(1).split(". If you have")[0].strip() if m else ""
    if kind:
        return kind, reason
    if item.get("is_error") is not True or text.lstrip().startswith("Exit code"):
        return None
    if CLASSIFIER in text[:300]:
        return "classifier", reason
    if any(p in text for p in DENY_PHRASES):
        return "permission", reason
    return None


TITLE_KEYS = (("custom-title", "customTitle", "custom"), ("ai-title", "aiTitle", "ai"),
              ("summary", "summary", "summary"), ("agent-name", "agentName", "agent"))
END_STOPS = ("end_turn", "stop_sequence", "refusal", "max_tokens")


def _new_info(p: Path, st: os.stat_result) -> dict[str, Any]:
    return {"session_id": p.stem, "path": str(p), "mtime": st.st_mtime, "size": st.st_size,
            "first_prompt": "", "title": "", "title_source": "", "started": None,
            "last_ts": None, "cwd": "", "entrypoint": "", "model": "", "effort": "",
            "ctx_tokens": 0, "ctx_stale": False, "last_kind": None, "stop_reason": None,
            "last_assistant_uuid": "", "pending_questions": [], "pending_tools": [],
            "denials": [], "api_error": None, "compacts": 0, "malformed": 0}


def _read_head(p: Path, info: dict, head_bytes: int) -> None:
    """The start time and the first real prompt."""
    for rec in _records(_read_span(p, 0, head_bytes)):
        if info["started"] is None and parse_ts(rec.get("timestamp")):
            info["started"] = parse_ts(rec.get("timestamp"))
        if not info["first_prompt"] and is_prompt(rec):
            info["first_prompt"] = prompt_text(rec)
        if info["started"] is not None and info["first_prompt"]:
            return


def _tail_records(p: Path, size: int, tail_bytes: int, info: dict) -> list[dict]:
    """The last tail_bytes as records; a malformed or cut line is counted, skipped."""
    recs = []
    for line in _read_span(p, max(0, size - tail_bytes), tail_bytes + 1):
        try:
            rec = json.loads(line)
        except ValueError:
            if line.strip():
                info["malformed"] += 1
            continue
        if isinstance(rec, dict):
            recs.append(rec)
    return recs


def _count_compacts(p: Path) -> int:
    try:
        with open(p, "rb") as fh:
            return sum(len(BOUNDARY_RE.findall(line)) for line in fh if b"compact_boundary" in line)
    except OSError:
        return 0


class _Walk:
    """Walks the tail records once and leaves the session state in `info`."""

    def __init__(self, info: dict):
        self.info = info
        self.uses: dict[str, tuple[str, Any]] = {}
        self.results: set[str] = set()
        self.denials: list[dict] = []
        self.titles: dict[str, str] = {}

    def record(self, rec: dict) -> None:
        ts = parse_ts(rec.get("timestamp"))
        self._meta(rec, ts)
        if rec.get("isSidechain"):
            return
        if rec.get("isCompactSummary") or (rec.get("type") == "system"
                                           and rec.get("subtype") == "compact_boundary"):
            self.info["ctx_stale"] = True
        elif rec.get("type") == "assistant":
            self._assistant(rec, ts)
        elif rec.get("type") == "user":
            self._user(rec, ts)

    def _meta(self, rec: dict, ts: float | None) -> None:
        if ts:
            self.info["last_ts"] = ts
        for key in ("cwd", "entrypoint"):
            if rec.get(key) and not rec.get("isSidechain"):
                self.info[key] = rec[key]
        for typ, field, name in TITLE_KEYS:
            if rec.get("type") == typ and rec.get(field):
                self.titles[name] = rec[field]
        if rec.get("type") == "last-prompt" and isinstance(rec.get("lastPrompt"), str):
            self.titles["last_prompt"] = rec["lastPrompt"]

    def _assistant(self, rec: dict, ts: float | None) -> None:
        info, content = self.info, _content(rec)
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        if rec.get("isApiErrorMessage") or rec.get("error"):
            info["api_error"] = {"error": str(rec.get("error") or "api_error")[:40],
                                 "text": redact(_text_of(content), TEXT_LEN),
                                 "ts": ts, "uuid": rec.get("uuid") or ""}
            info["last_kind"] = "api_error"
            return
        info["api_error"] = None
        if msg.get("model") and msg.get("model") != "<synthetic>":
            info["model"] = msg["model"]
        if rec.get("effort"):
            info["effort"] = str(rec["effort"])
        usage = msg.get("usage")
        if isinstance(usage, dict) and _usage_ctx(usage):
            info["ctx_tokens"], info["ctx_stale"] = _usage_ctx(usage), False
        has_tool = False
        for c in content if isinstance(content, list) else []:
            if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("id"):
                self.uses[c["id"]] = (str(c.get("name") or "?"), c.get("input"))
                has_tool = True
        info["stop_reason"] = msg.get("stop_reason")
        info["last_assistant_uuid"] = rec.get("uuid") or info["last_assistant_uuid"]
        info["last_kind"] = ("assistant_tool" if has_tool or msg.get("stop_reason") == "tool_use"
                             else "assistant_end" if msg.get("stop_reason") in END_STOPS
                             else "assistant_other")

    def _user(self, rec: dict, ts: float | None) -> None:
        if not is_tool_result(rec):
            kind = user_kind(rec)
            if kind:
                self.denials = []  # a new turn (or the owner stepped in): history
                self.info["last_kind"] = {"prompt": "user_prompt"}.get(kind, kind)
                self.info["stop_reason"] = None
            return
        for c in _content(rec):
            if not isinstance(c, dict) or c.get("type") != "tool_result":
                continue
            tid = c.get("tool_use_id") or ""
            self.results.add(tid)
            why = _denial(c, rec)
            if why:
                self.denials.append({"tool_use_id": tid, "tool": self.uses.get(tid, ("?",))[0][:60],
                                     "kind": why[0], "reason": redact(why[1], 160), "ts": ts})
        self.info["last_kind"] = "tool_result"

    def finish(self) -> dict:
        info = self.info
        for tid, (name, inp) in self.uses.items():
            if tid in self.results:
                continue
            if name == "AskUserQuestion":
                info["pending_questions"].append({"tool_use_id": tid, "questions": _questions(inp)})
            else:
                info["pending_tools"].append(name[:60])
        info["denials"] = self.denials
        for src in ("custom", "agent", "ai", "summary"):
            if self.titles.get(src):
                info["title"], info["title_source"] = redact(self.titles[src], TITLE_LEN), src
                break
        info["first_prompt"] = redact(info["first_prompt"], TITLE_LEN)
        info["last_prompt"] = redact(self.titles.get("last_prompt"), TITLE_LEN)
        return info


def _questions(inp: Any) -> list[dict]:
    """An AskUserQuestion input as question text and option labels, redacted."""
    out = []
    for q in (inp or {}).get("questions") or []:
        if isinstance(q, dict):
            out.append({"question": redact(q.get("question"), TEXT_LEN),
                        "header": redact(q.get("header"), 40),
                        "options": [redact(o.get("label"), 40) for o in q.get("options") or []
                                    if isinstance(o, dict)]})
    return out


def parse_transcript(path: str | Path, tail_bytes: int = TAIL_BYTES,
                     head_bytes: int = HEAD_BYTES, count_compacts: bool = True) -> dict[str, Any]:
    """The state of one session from its transcript, without the model.

    Reads the head for the first prompt and the start, the tail for the rest.
    Malformed lines are skipped. Sidechain (subagent) records never decide the
    main session's state.
    """
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return {"error": "unreadable", "path": str(p)}
    info = _new_info(p, st)
    _read_head(p, info, head_bytes)
    recs = _tail_records(p, st.st_size, tail_bytes, info)
    if count_compacts:
        info["compacts"] = _count_compacts(p)
    walk = _Walk(info)
    for rec in recs:
        walk.record(rec)
    return walk.finish()
