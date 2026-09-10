#!/usr/bin/env python3
"""Deterministic gate for computer-use proposals."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

_SECRET = re.compile(r"(?i)(password|secret|token|api[_-]?key|bearer\s+[a-z0-9])")
_BAD_ACTIONS = {"bridge_dom_click", "bridge_dom_type"}


def ok_row(row: dict) -> tuple[bool, str]:
    if row.get("kind") not in {"skill", "shortcut"}:
        return False, "kind"
    blob = json.dumps(row)
    if _SECRET.search(blob):
        return False, "secret"
    if row.get("kind") == "skill":
        steps = row.get("steps_v2") or []
        if not steps:
            return False, "no-steps"
        for step in steps:
            act = str((step or {}).get("action") or "")
            if act in _BAD_ACTIONS:
                return False, "dom-injection"
            val = str((step or {}).get("value") or "")
            if act == "key" and val and "teleport" in val:
                return False, "teleport"
    if row.get("kind") == "shortcut":
        if not row.get("keys") or not row.get("action"):
            return False, "incomplete-chord"
    return True, "ok"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    kept = 0
    dropped = 0
    with open(args.inp, encoding="utf-8") as fin, open(args.out, "w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                dropped += 1
                continue
            good, reason = ok_row(row)
            if not good:
                dropped += 1
                continue
            row["gate"] = reason
            fout.write(json.dumps(row) + "\n")
            kept += 1
    print(f"- gate: kept={kept} dropped={dropped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
