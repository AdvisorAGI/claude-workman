"""The guards every front-end action shares (caller known and not the target,
clean main-chain turn, quiet, no question, registry neither busy nor waiting,
no film, frontend lock, the policy band), target lookup and the logs."""

from __future__ import annotations

import fcntl
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .. import front_compact as fc
from . import common as cm
from . import registry as rg
from . import state as st
from . import transcripts as tr


# Token bands, the owner's policy. A caller narrows them; only force widens.
COMPACT_POLICY = cm.COMPACT_BAND            # compact only at 300k-400k

MODEL_POLICY = (0, cm.COMPACT_BAND[1])      # a model switch re-reads the whole context

# run-claude-seat seat names for the config dirs that do not follow .claude-<seat>
SEAT_OF_DIR = {".claude": "main", ".claude-advisor-support": "support",
               ".claude-hello-advisorppc": "hello"}

RESOLVE_TITLE_WITHIN_MIN = 24 * 60

BRIDGE_PREFIXES = ("session_", "cse_")

def model_log_path() -> Path:
    return Path(os.environ.get("WORKMAN_MODEL_EFFORT_LOG")
                or cm.home_dir() / "orchestra" / "model-effort-log.jsonl")

def log_line(path: Path, **fields: Any) -> None:
    """One JSON line with ts first; the same {ts,target,result} shape as
    front_compact.log, plus the tool's own fields."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **fields}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")

def norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())

def seat_of(config_dir: str) -> str:
    return SEAT_OF_DIR.get(config_dir, config_dir.replace(".claude-", "", 1))

def find_session(session: str, home: Any = None, now: float | None = None,
                 proc_root: Path = Path("/proc")) -> tuple[dict | None, str]:
    """(row, error) for a full or prefix session id (>= 6 chars), a
    local_<uuid>, or an exact title among sessions active in the last 24 h."""
    want = (session or "").strip()
    if not want:
        return None, "no session given"
    scan = st.Scan(home, now, proc_root)
    paths: list[Path] = []
    if want.startswith("local_"):
        sid = next((k for k, v in scan.desk.items() if v.get("sessionId") == want), "")
        if not sid:
            return None, f"no desktop session {want}"
        want_id = sid
    else:
        want_id = want
    if re.fullmatch(r"[0-9a-f-]{6,36}", want_id):
        for d in cm.config_dirs(scan.home):
            paths.extend(d.glob(f"projects/*/{want_id}*.jsonl"))
    if not paths and not want.startswith("local_"):
        for p in tr.transcripts(scan.home, RESOLVE_TITLE_WITHIN_MIN * 60, now=scan.now):
            info = tr.parse_transcript(p)
            if norm(info.get("title")) and norm(info.get("title")) == norm(want):
                paths.append(p)
    uniq = sorted({p.stem: p for p in paths}.values())
    if not uniq:
        return None, f"no session matches {want!r}"
    if len(uniq) > 1:
        return None, "ambiguous: " + ", ".join(p.stem[:8] for p in uniq[:8])
    row = scan.row(uniq[0])
    if row.get("error"):
        return None, f"transcript unreadable: {uniq[0]}"
    row["_scan"] = scan
    return row, ""

def _guard(name: str, ok: bool, detail: str = "") -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail}

def lock_free(path: str = "") -> bool:
    """The frontend lock is free right now (probe and release at once). A
    missing lock file is free and is not created: a dry run writes nothing
    outside its log."""
    path = path or fc.FRONTEND_LOCK
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)

def turn_guards(row: dict, self_id: str = "", procs: Callable[[], list[str]] | None = None,
                quiet_s: float = cm.QUIET_S) -> list[dict]:
    """The guards every front-end action shares."""
    scan = row.get("_scan")
    ids = rg.caller_ids(self_id, scan.home if scan else None,
                        scan.proc_root if scan else Path("/proc"))
    mine = rg.is_self(row["session_id"], local_id=row.get("local_id", ""), ids=ids)
    g = [_guard("caller_known", bool(ids), "" if ids else
                rg.bad_self_id(self_id) or
                "cannot tell which session is calling (no CLAUDE_SESSION_ID / "
                "CLAUDE_CODE_SESSION_ID, no Claude ancestor in the registry, no self_id): "
                "refusing so a session can never act on itself"),
         _guard("not_self", bool(ids) and not mine,
                "the target is the caller's own session" if mine else "")]
    fe = row.get("front_end")
    g.append(_guard("front_end", fe in ("desktop", "remote_control"), fe or "unknown"))
    g.append(_guard("clean_turn", row.get("last_kind") == "assistant_end"
                    and row.get("stop_reason") == "end_turn",
                    f"last={row.get('last_kind')} stop={row.get('stop_reason')}"))
    g.append(_guard("quiet", row.get("age_s", 0) >= quiet_s,
                    f"quiet {row.get('age_s', 0)} s, need {int(quiet_s)} s"))
    g.append(_guard("no_question", not row.get("pending_questions"),
                    "AskUserQuestion open" if row.get("pending_questions") else ""))
    status = row.get("registry_status")
    g.append(_guard("registry_ready", status not in st.BLOCKING_STATUS,
                    f"registry {status}" + (" (input or a permission prompt pending)"
                                            if status == "waiting" else "")))
    film = fc.film_playing((procs or fc.process_names)())
    g.append(_guard("no_film", not film, "a film player is running" if film else ""))
    free = lock_free()
    g.append(_guard("frontend_lock", free, "" if free else f"{fc.FRONTEND_LOCK} is held"))
    if fe == "desktop":
        lid = row.get("local_id") or ""
        g.append(_guard("local_id", bool(fc.LOCAL_ID_RE.match(lid)), lid or "none"))
        has = bool(lid and fc.stored_header(lid))
        g.append(_guard("header_learned", has, "" if has else
                        f"run `python -m workman.front_compact {lid} --learn` first"))
    elif fe == "remote_control":
        reg = row["_scan"].reg.get(row["session_id"]) if row.get("_scan") else None
        bridge = (reg or {}).get("_bridge") or ""
        known = bridge.startswith(BRIDGE_PREFIXES) and bool((reg or {}).get("alive"))
        g.append(_guard("rc_page", known, "page known" if known else
                        "no live registry entry with a bridge id"))
        g.append(_guard("rc_title", bool(expected_titles(row)), "no title to verify the page by"
                        if not expected_titles(row) else ""))
    return g

def band_guard(ctx: int, min_tokens: int, max_tokens: int, force: bool,
               policy: tuple[int, int]) -> tuple[dict, tuple[int, int]]:
    """(guard, band). The caller's band is clamped into the policy band, so a
    min of 0 or a huge max cannot widen it; only force skips the check."""
    lo, hi = max(int(min_tokens), policy[0]), min(int(max_tokens), policy[1])
    clamped = (lo, hi) != (int(min_tokens), int(max_tokens))
    note = f" (clamped to policy [{policy[0]}, {policy[1]}])" if clamped else ""
    if force:
        return _guard("band", True, f"ctx {ctx}, band check skipped: forced"), (lo, hi)
    if lo > hi:
        return _guard("band", False, f"empty band [{lo}, {hi}]{note}"), (lo, hi)
    ok = lo <= ctx <= hi
    return _guard("band", ok, f"ctx {ctx} {'in' if ok else 'outside'} [{lo}, {hi}]{note}"), (lo, hi)

def headless_refusal(row: dict, what: str, model: str = "", effort: str = "") -> str:
    if what == "compact":
        return ("headless `claude -p` session: there is no front end to type /compact into; "
                "it compacts on its own at the window, or start a fresh seat from a handoff")
    flags = (f" --model {model}" if model else "") + (f" --effort {effort}" if effort else "")
    return ("headless `claude -p` session: no front end, the change applies only at the next "
            f"resume: run-claude-seat {seat_of(row.get('config_dir', ''))}{flags} "
            f"--resume {row['session_id']} -- \"<prompt>\"")

def expected_titles(row: dict) -> list[str]:
    """What the claude.ai page must show for this session: its title and, for a
    Remote Control seat, the session name the registry holds."""
    out = []
    reg = row["_scan"].reg.get(row["session_id"]) if row.get("_scan") else None
    for t in (row.get("title") if row.get("title") != row.get("first_prompt") else "",
              (reg or {}).get("name")):
        if t and len(norm(t)) >= 4:
            out.append(str(t))
    return out

def rc_url(bridge: str) -> str:
    """The claude.ai page of a bridged session. The live registry holds
    session_<id>; the CLI's --session-id flag spells the same id cse_<id>."""
    suffix = bridge.split("_", 1)[1] if bridge.startswith(BRIDGE_PREFIXES) else bridge
    tpl = os.environ.get("WORKMAN_RC_URL_TEMPLATE", "https://claude.ai/code/session_{suffix}")
    return tpl.format(suffix=suffix)

def _public(row: dict) -> dict:
    keys = ("id", "session_id", "account", "title", "front_end", "local_id", "rc_name", "pid",
            "model", "effort", "front_model", "front_effort", "ctx_tokens", "state", "age_s")
    return {k: row.get(k) for k in keys if k in row}
