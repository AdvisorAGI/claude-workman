"""Durable learned keyboard shortcuts — the memory that never forgets chords.

The static table in ``shortcuts.py`` covers the OS and a few apps. Everything
else is learned the first time Workman focuses an application: harvest the
menu bar, normalise each item to an xdotool chord, upsert into
``~/.grok/workman-learn/shortcuts.jsonl``, and answer later lookups from that
file. Fleet mirrors under ``fleet/<device>/`` are read-only, same pattern as
recipes.

Resolution order used by ``shortcuts.resolve``:

  1. static app layer
  2. static system layer
  3. learned rows for this app (and platform)
  4. learned rows with a near action name

A blank is still better than a guessed chord: harvest only stores what the
menu bar actually exposed.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

LEARN_ROOT = Path(os.path.expanduser(
    os.environ.get("WORKMAN_LEARN_ROOT", "~/.grok/workman-learn")))
SHORTCUTS = LEARN_ROOT / "shortcuts.jsonl"
FLEET = LEARN_ROOT / "fleet"
META = LEARN_ROOT / "shortcuts-meta.json"

#: Re-harvest an app at most this often unless ``force=True``.
HARVEST_TTL_S = 7 * 24 * 3600

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def learn_root() -> Path:
    LEARN_ROOT.mkdir(parents=True, exist_ok=True)
    return LEARN_ROOT


def shortcuts_path() -> Path:
    return SHORTCUTS


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def slug(text: str) -> str:
    s = _SLUG_RE.sub("_", (text or "").strip().lower()).strip("_")
    return s or "unnamed"


def app_key(app: str) -> str:
    """Stable key for an app name (aliases match shortcuts._APP_ALIASES)."""
    from . import shortcuts as sc

    return sc._norm(app, sc._APP_ALIASES) or slug(app)


def _atomic_write_lines(path: Path, rows: list[dict]) -> None:
    learn_root()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".shortcuts.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _read_meta() -> dict:
    try:
        with open(META, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_meta(data: dict) -> None:
    learn_root()
    tmp = META.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, META)


def read_all(include_fleet: bool = True) -> list[dict]:
    rows = [r for r in _read_jsonl(SHORTCUTS) if r.get("kind", "shortcut") == "shortcut"]
    if not include_fleet or not FLEET.is_dir():
        return rows
    for device_dir in sorted(FLEET.iterdir()):
        if not device_dir.is_dir():
            continue
        for row in _read_jsonl(device_dir / "shortcuts.jsonl"):
            if row.get("kind", "shortcut") != "shortcut":
                continue
            row = dict(row)
            row["_fleet"] = device_dir.name
            rows.append(row)
    return rows


def _same(a: dict, b: dict) -> bool:
    return (
        str(a.get("app_key") or "") == str(b.get("app_key") or "")
        and str(a.get("platform") or "") == str(b.get("platform") or "")
        and str(a.get("action") or "") == str(b.get("action") or "")
    )


def upsert_many(entries: list[dict], *, source: str = "harvest") -> dict:
    """Merge harvested / taught chords into the durable store."""
    if not entries:
        return {"ok": True, "upserted": 0, "total": len(read_all(False)), "path": str(SHORTCUTS)}
    rows = _read_jsonl(SHORTCUTS)
    by_key = {(r.get("app_key"), r.get("platform"), r.get("action")): i
              for i, r in enumerate(rows) if r.get("kind", "shortcut") == "shortcut"}
    now = _now()
    upserted = 0
    for raw in entries:
        keys = (raw.get("keys") or "").strip()
        action = slug(str(raw.get("action") or raw.get("label") or ""))
        if not keys or not action:
            continue
        app = str(raw.get("app") or "").strip()
        platform = str(raw.get("platform") or "").strip()
        incoming = {
            "kind": "shortcut",
            "app": app,
            "app_key": str(raw.get("app_key") or app_key(app)),
            "platform": platform,
            "action": action,
            "label": str(raw.get("label") or action),
            "menu_path": list(raw.get("menu_path") or []),
            "keys": keys,
            "source": str(raw.get("source") or source),
            "ts": now,
            "uses": 1,
        }
        key = (incoming["app_key"], incoming["platform"], incoming["action"])
        if key in by_key:
            i = by_key[key]
            old = rows[i]
            rows[i] = {
                **old,
                **incoming,
                "uses": int(old.get("uses") or 1) + 1,
                "first_ts": old.get("first_ts") or old.get("ts") or now,
            }
        else:
            incoming["first_ts"] = now
            by_key[key] = len(rows)
            rows.append(incoming)
        upserted += 1
    _atomic_write_lines(SHORTCUTS, rows)
    return {"ok": True, "upserted": upserted, "total": len(rows), "path": str(SHORTCUTS)}


def list_for(app: str | None = None, platform: str | None = None) -> list[dict]:
    from . import shortcuts as sc

    where = sc._norm(platform, sc._PLATFORM_ALIASES) or sc.detect_platform()
    key = app_key(app) if app else None
    out = []
    for row in read_all(True):
        if platform and str(row.get("platform")) != where:
            continue
        if key and str(row.get("app_key")) != key and slug(str(row.get("app") or "")) != key:
            if key not in slug(str(row.get("app") or "")):
                continue
        out.append(row)
    return out


def lookup(action: str, app: str | None = None,
           platform: str | None = None) -> dict | None:
    """Best learned chord for an action, or None."""
    from . import shortcuts as sc

    name = sc._norm(action, sc._ACTION_ALIASES) or slug(action)
    where = sc._norm(platform, sc._PLATFORM_ALIASES) or sc.detect_platform()
    key = app_key(app) if app else None
    exact: dict | None = None
    fuzzy: dict | None = None
    for row in read_all(True):
        if str(row.get("platform") or "") != where:
            continue
        if key and str(row.get("app_key")) != key:
            # Allow substring match on display name (Google Chrome vs chrome).
            if key not in slug(str(row.get("app") or "")) and key not in str(row.get("app_key") or ""):
                continue
        act = str(row.get("action") or "")
        if act == name:
            exact = row
            break
        if name in act or act in name or name in slug(str(row.get("label") or "")):
            fuzzy = fuzzy or row
    return exact or fuzzy


def mark_harvested(app: str, platform: str, count: int, note: str = "") -> None:
    meta = _read_meta()
    apps = meta.setdefault("apps", {})
    apps[f"{app_key(app)}|{platform}"] = {
        "app": app,
        "platform": platform,
        "ts": _now(),
        "epoch": time.time(),
        "count": count,
        "note": note,
    }
    _write_meta(meta)


def needs_harvest(app: str, platform: str | None = None,
                  ttl_s: float = HARVEST_TTL_S) -> bool:
    from . import shortcuts as sc

    where = sc._norm(platform, sc._PLATFORM_ALIASES) or sc.detect_platform()
    meta = _read_meta().get("apps", {})
    row = meta.get(f"{app_key(app)}|{where}")
    if not row:
        return True
    try:
        return (time.time() - float(row.get("epoch") or 0)) > ttl_s
    except (TypeError, ValueError):
        return True


def ensure_learned(app: str, *, force: bool = False,
                   platform: str | None = None) -> dict:
    """Harvest and store an app's menu chords if we do not already know them."""
    from . import shortcut_harvest
    from . import shortcuts as sc

    app = (app or "").strip()
    if not app:
        return {"ok": False, "error": "app is required", "learned": False}
    where = sc._norm(platform, sc._PLATFORM_ALIASES) or sc.detect_platform()
    if not force and not needs_harvest(app, where):
        known = list_for(app, where)
        return {
            "ok": True,
            "learned": False,
            "skipped": "fresh",
            "app": app,
            "platform": where,
            "known": len(known),
            "path": str(SHORTCUTS),
        }
    harvested = shortcut_harvest.harvest(app, platform=where)
    if not harvested.get("ok"):
        mark_harvested(app, where, 0, note=str(harvested.get("error") or "failed"))
        return {**harvested, "learned": False, "path": str(SHORTCUTS)}
    entries = harvested.get("entries") or []
    stored = upsert_many(entries, source=str(harvested.get("source") or "harvest"))
    mark_harvested(app, where, stored.get("upserted", 0))
    return {
        "ok": True,
        "learned": True,
        "app": harvested.get("app") or app,
        "platform": where,
        "upserted": stored.get("upserted", 0),
        "total": stored.get("total", 0),
        "via": harvested.get("via"),
        "path": str(SHORTCUTS),
    }
