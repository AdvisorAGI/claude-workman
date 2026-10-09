"""Front-end control of ANOTHER Claude Code session: compact it, or change its
model and effort, the way the owner would, only at a clean turn. The MCP tools
(registered in workman/server.py) and the CLI live here; the logic lives in
workman/orchestra/:

    guards        caller known and not the target, clean main-chain turn, quiet,
                  no question, registry neither busy nor waiting, no film,
                  frontend lock, the policy band; target lookup; logs
    drivers       the Chrome verbs (never Escape), page and focus checks
    compact       session_compact, the guarded session_front_compact, and the
                  Remote Control runner (RemoteCompact)
    picker        model ids and labels (Fable only by explicit choice), the
                  desktop AT-SPI picker and the claude.ai picker
    model_effort  session_set_model_effort

mode="dry_run" (the default) runs every guard and returns exactly what a run
would do, touching nothing on screen. Every outcome, dry runs included, is one
JSON line: compaction in ~/orchestra/compact-log.jsonl, model/effort in
~/orchestra/model-effort-log.jsonl.

Env:
  WORKMAN_MODEL_EFFORT_LOG   model/effort log (default ~/orchestra/model-effort-log.jsonl)
  WORKMAN_RC_URL_TEMPLATE    claude.ai page of an RC session, {suffix} is the
                             bridge id after "session_"/"cse_" (default
                             https://claude.ai/code/session_{suffix})
  WORKMAN_RC_COMPOSER        accessible name of the claude.ai composer (default "Reply")
"""

from __future__ import annotations

import argparse
import json
import sys

from .front_compact_cli import add_band_flags
from .orchestra.compact import (RemoteCompact, run_rc, session_compact,  # noqa: F401
                                session_front_compact, spawn_remote)
from .orchestra.drivers import ChromeDriver  # noqa: F401
from .orchestra.guards import COMPACT_POLICY, MODEL_POLICY, rc_url  # noqa: F401
from .orchestra.model_effort import session_set_model_effort
from .orchestra.picker import (DesktopPicker, effort_labels, model_label,  # noqa: F401
                               model_labels, normalize_model)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m workman.orchestra_control",
                                 description="Compact or re-model ANOTHER session from its front end.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compact")
    c.add_argument("session")
    c.add_argument("--run", action="store_true", help="act (default: dry run)")
    c.add_argument("--min-tokens", type=int, default=COMPACT_POLICY[0])
    c.add_argument("--max-tokens", type=int, default=COMPACT_POLICY[1])
    c.add_argument("--force", action="store_true")
    c.add_argument("--resume", default="")
    m = sub.add_parser("set-model")
    m.add_argument("session")
    m.add_argument("--model", default="")
    m.add_argument("--effort", default="")
    m.add_argument("--run", action="store_true", help="act (default: dry run)")
    m.add_argument("--allow-fable", action="store_true", help="the owner chose Fable explicitly")
    m.add_argument("--max-tokens", type=int, default=MODEL_POLICY[1])
    m.add_argument("--force", action="store_true")
    r = sub.add_parser("rc-compact", help="the detached Remote Control runner")
    r.add_argument("session_id")
    r.add_argument("--transcript", required=True)
    r.add_argument("--resume", default="")
    r.add_argument("--self-id", default="")
    add_band_flags(r)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = _parser()
    a = ap.parse_args(argv)
    if a.cmd == "rc-compact":
        if a.force and not a.reason.strip():
            ap.error("--force needs --reason (it is logged)")
        return run_rc(a.session_id, a.transcript, a.resume, a.self_id, a.min_tokens,
                      a.max_tokens, a.force, a.reason)
    if a.cmd == "compact":
        out = session_compact(a.session, "run" if a.run else "dry_run", a.min_tokens,
                              a.max_tokens, a.force, a.resume)
    else:
        out = session_set_model_effort(a.session, a.model, a.effort, "run" if a.run else "dry_run",
                                       allow_fable=a.allow_fable, max_tokens=a.max_tokens,
                                       force=a.force)
    print(json.dumps(out, indent=1, default=str))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
