#!/usr/bin/env python3
"""Harvest shortcuts for recently focused apps (desktop session)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running from ops/ without install.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    from workman import desktop, learned_shortcuts, shortcuts

    apps: list[str] = []
    try:
        for win in desktop.list_windows() or []:
            name = str((win or {}).get("app") or (win or {}).get("name") or "").strip()
            if name and name not in apps:
                apps.append(name)
    except Exception as exc:
        print(f"- harvest_local: list_windows failed: {exc}")
        apps = []
    try:
        active = desktop.active_window() or {}
        name = str(active.get("app") or active.get("name") or "").strip()
        if name and name not in apps:
            apps.insert(0, name)
    except Exception:
        pass

    results = []
    platform = shortcuts.detect_platform()
    for app in apps[:12]:
        if not learned_shortcuts.needs_harvest(app, platform):
            results.append({"app": app, "skipped": "fresh"})
            continue
        out = learned_shortcuts.ensure_learned(app)
        results.append(out)
        print(f"- harvest {app}: upserted={out.get('upserted', 0)} ok={out.get('ok')}")

    Path(args.out).write_text(json.dumps({"platform": platform, "results": results}, indent=2))
    print(f"- harvest_local: wrote {args.out} ({len(results)} apps)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
