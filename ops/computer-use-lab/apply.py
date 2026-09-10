#!/usr/bin/env python3
"""Apply verified proposals into the durable learn stores."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _repo_root() -> Path:
    env = os.environ.get("WORKMAN_ROOT")
    if env:
        p = Path(env)
        if (p / "workman").is_dir():
            return p
    here = Path(__file__).resolve().parent
    parents = list(here.parents)
    for cand in (*(parents[i] for i in range(min(3, len(parents)))), Path.home() / "workman"):
        if (cand / "workman").is_dir():
            return cand
    return parents[1] if len(parents) > 1 else here


ROOT = _repo_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--learn-root", required=True)
    args = ap.parse_args()
    os.environ["WORKMAN_LEARN_ROOT"] = args.learn_root

    from workman import cu_skills, learned_shortcuts

    skills = chords = 0
    with open(args.inp, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("unchanged"):
                continue
            if row.get("kind") == "skill":
                cu_skills.teach(
                    row.get("title") or "untitled",
                    row.get("steps_v2") or row.get("steps") or "",
                    app=row.get("app") or "",
                    platform=row.get("platform") or "",
                    notes=row.get("notes") or "",
                    source=row.get("source") or "lab",
                    task_id=row.get("task_id") or "",
                )
                skills += 1
            elif row.get("kind") == "shortcut":
                learned_shortcuts.upsert_many([row], source=row.get("source") or "lab")
                chords += 1
    print(f"- apply: skills={skills} shortcuts={chords}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
