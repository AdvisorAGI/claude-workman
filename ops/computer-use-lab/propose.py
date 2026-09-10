#!/usr/bin/env python3
"""Propose computer-use skills from journal gaps + harvest (host / green)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--learn-root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--harvest", default="")
    args = ap.parse_args()
    learn = Path(args.learn_root)
    out = Path(args.out)
    proposals: list[dict] = []

    # Seed a canonical human skill if missing.
    proposals.append({
        "kind": "skill",
        "title": "Prefer shortcut then paste",
        "app": "",
        "platform": "",
        "steps_v2": [
            {"action": "key", "value": "shortcut lookup"},
            {"action": "key", "value": "press chord"},
            {"action": "paste", "value": "long text via clipboard"},
            {"action": "verify", "value": "screenshot after UI change"},
        ],
        "notes": "baseline human habit; lab keeps it alive",
        "source": "lab-propose",
    })

    harvest_path = Path(args.harvest) if args.harvest else None
    if harvest_path and harvest_path.is_file():
        try:
            data = json.loads(harvest_path.read_text())
        except Exception:
            data = {}
        for row in data.get("results") or []:
            if not row.get("learned"):
                continue
            app = row.get("app") or ""
            if not app:
                continue
            proposals.append({
                "kind": "skill",
                "title": f"Use harvested shortcuts in {app}",
                "app": app,
                "platform": row.get("platform") or "",
                "steps_v2": [
                    {"action": "key", "value": "shortcut_learn if stale"},
                    {"action": "key", "value": "shortcut(action) before mouse"},
                ],
                "notes": f"auto from harvest upserted={row.get('upserted', 0)}",
                "source": "lab-propose",
            })

    # Chord proposals already live in shortcuts.jsonl via harvest; nothing else.
    with out.open("w", encoding="utf-8") as fh:
        for p in proposals:
            fh.write(json.dumps(p) + "\n")
    print(f"- propose: {len(proposals)} rows -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
