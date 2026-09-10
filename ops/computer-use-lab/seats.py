#!/usr/bin/env python3
"""Presence-only CLI seat probe for the DGX computer-use tuner.

Never calls a metered API. Never prints token material. A seat is "up" when
the binary exists and the login file that makes it usable is present.

Leader order (owner, 2026-09-10): Fable, then ChatGPT Astra (Codex CLI),
then Grok 4.6, then Cursor agent, then local Qwen3.8-27B.
Verifier: first different available seat, cheaper first (grok, sonnet, qwen).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path.home()


def _has(bin_name: str) -> str | None:
    return shutil.which(bin_name)


def _readable(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def grok_usable() -> bool:
    p = HOME / ".grok" / "auth.json"
    try:
        data = json.loads(p.read_text())
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    for v in data.values():
        if isinstance(v, dict) and v.get("refresh_token"):
            return True
    return False


def claude_logged_in() -> bool:
    return _readable(HOME / ".claude.json") or _readable(
        HOME / ".claude" / "settings.json")


def codex_logged_in() -> bool:
    return _readable(HOME / ".codex" / "auth.json")


def astra_model() -> str:
    env = (os.environ.get("CU_TUNE_ASTRA_MODEL") or "").strip()
    if env:
        return env
    cfg = HOME / ".codex" / "config.toml"
    try:
        for line in cfg.read_text().splitlines():
            s = line.strip()
            if s.startswith("model") and "=" in s and not s.startswith("["):
                val = s.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    except OSError:
        pass
    return "gpt-5.3-codex-spark"


def fable_model() -> str:
    return (os.environ.get("CU_TUNE_FABLE_MODEL") or "claude-fable-5-1").strip()


def qwen_up() -> bool:
    url = os.environ.get("DGX_QWEN_URL") or "http://127.0.0.1:8001/v1/models"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "cu-tune-seat/1"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode() or "{}")
        rows = data.get("data") or data.get("models") or []
        return bool(rows)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return False


def collect() -> dict:
    seats = {
        "fable": {
            "id": "fable",
            "bin": _has("claude"),
            "model": fable_model(),
            "usable": bool(_has("claude") and claude_logged_in()),
        },
        "astra": {
            "id": "astra",
            "bin": _has("codex") or _has("chatgpt"),
            "model": astra_model(),
            "usable": bool((_has("codex") or _has("chatgpt")) and codex_logged_in()),
        },
        "grok": {
            "id": "grok",
            "bin": _has("grok"),
            "model": "grok-4.6",
            "usable": bool(_has("grok") and grok_usable()),
        },
        "cursor": {
            "id": "cursor",
            "bin": _has("cursor-agent"),
            "model": os.environ.get("CU_TUNE_CURSOR_MODEL") or "",
            "usable": bool(_has("cursor-agent")),
        },
        "qwen": {
            "id": "qwen",
            "bin": "http://127.0.0.1:8001/v1",
            "model": os.environ.get("DGX_QWEN_MODEL") or "qwen3.8-27b",
            "usable": qwen_up(),
        },
        "sonnet": {
            "id": "sonnet",
            "bin": _has("claude"),
            "model": "sonnet",
            "usable": bool(_has("claude") and claude_logged_in()),
        },
    }
    return {
        "host": os.uname().nodename,
        "seats": {k: {**v, "bin": bool(v["bin"]) if k != "qwen" else v["bin"]}
                  for k, v in seats.items()},
    }


def probe(mode: str = "learn") -> dict:
    return choose_rungs(collect(), mode=mode)


def choose_rungs(info: dict, mode: str = "learn") -> dict:
    """Pick leader + a different verifier.

    learn: Fable -> Astra -> Grok -> Cursor -> Qwen (owner ladder).
    recheck: cheaper first. A known skill does not spend Fable.
    skip: no seats.
    """
    seats = info.get("seats") or {}
    if mode == "skip":
        info["leader"] = None
        info["verifier"] = None
        info["mode"] = "skip"
        return info
    if mode == "recheck":
        leader_order = ("grok", "qwen", "cursor", "astra", "fable")
    else:
        leader_order = ("fable", "astra", "grok", "cursor", "qwen")
    verify_order = ("grok", "sonnet", "qwen", "astra", "cursor")
    leader = next((seats[k] for k in leader_order if seats.get(k, {}).get("usable")), None)
    verifier = None
    if leader:
        for k in verify_order:
            cand = seats.get(k)
            if not cand or not cand.get("usable"):
                continue
            if k == leader["id"]:
                continue
            verifier = cand
            break
    info["leader"] = leader
    info["verifier"] = verifier
    info["mode"] = mode
    return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--mode", default="learn",
                    choices=("learn", "recheck", "skip"))
    args = ap.parse_args()
    info = probe(args.mode)
    print(json.dumps(info, indent=2 if args.json else None))
    if args.mode == "skip":
        return 0
    return 0 if info.get("leader") else 2


if __name__ == "__main__":
    raise SystemExit(main())
