#!/usr/bin/env python3
"""Yellow-zone docs harvest placeholder (allowlisted hosts only, stdlib).

Real network fetch is intentionally minimal: the live AX harvest on the host
is the primary teacher. This stage exists so the lane matches nightly-research
zoning and can later pull public shortcut cheat-sheets without executing them.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    payload = {
        "ok": True,
        "entries": [],
        "note": "no remote shortcut docs fetched this night; AX harvest is primary",
    }
    Path(args.out).write_text(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
