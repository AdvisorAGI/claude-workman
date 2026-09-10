#!/usr/bin/env python3
"""Red-zone verify: offline checklist, no network, no seat."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def verify(row: dict) -> tuple[bool, str]:
    # Red zone rules: must be self-contained, human-mode friendly.
    if row.get("kind") == "skill":
        steps = row.get("steps_v2") or []
        actions = [str(s.get("action") or "") for s in steps]
        if not actions:
            return False, "empty"
        if "bridge_dom_click" in actions or "bridge_dom_type" in actions:
            return False, "dom"
        # Must prefer keys/paste over pure coordinate scripts.
        if all(a == "click" for a in actions):
            return False, "mouse-only"
    if row.get("kind") == "shortcut":
        keys = str(row.get("keys") or "")
        if "+" not in keys and keys not in {"Return", "Escape", "Tab", "space"}:
            # Bare letter chords are fine with modifiers missing only for rare cases;
            # require something that looks like a real binding.
            if len(keys) > 1 and not keys[0].isalnum():
                return False, "odd-keys"
    return True, "pass"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    kept = dropped = 0
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
            good, reason = verify(row)
            if not good:
                dropped += 1
                continue
            row["red_verify"] = reason
            fout.write(json.dumps(row) + "\n")
            kept += 1
    print(f"- red verify: kept={kept} dropped={dropped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
