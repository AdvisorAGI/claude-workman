"""Codex CLI stills: the proven 2026-09-25 lane for a first frame or a logo still.

Codex CLI has a built-in image tool on the ChatGPT plan login, so a still is plan
usage, never an API key: OPENAI_API_KEY is stripped from the child environment so
a stray key can never switch it to metered billing. The prompt goes on stdin
because `-i` is variadic and would swallow a trailing prompt:

    echo "<prompt>" | codex exec --skip-git-repo-check -C <dir> -i ref.png

Tickets name no image model. Use the still as a grok First frame.

    python -m workman.studio.still make <dir> <name.png> "<subject>" [--aspect 9:16] [-i ref.png ...]
    python -m workman.studio.still variants <dir> <src.png> [--aspects 9:16 16:9 1:1]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .job import StudioError

ASPECTS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "4:5": (1080, 1350)}
BRAND = ("Photoreal and natural. Monochrome look: black, graphite #1F1E1D, white, warm off-white #FAF9F5, "
         "silver and stone grey, at most one emerald #10B981 accent. No text, no letters, no numbers, "
         "no logos unless the reference image has one, no watermark. No women or girls anywhere.")
TIMEOUT = 15 * 60
MAX_REFS = 5
MAGIC = {b"\x89PNG": "png", b"\xff\xd8\xff": "jpg", b"RIFF": "webp"}


def codex_cmd(workdir: Path, refs: list[Path]) -> list[str]:
    """The argv (the prompt goes on stdin)."""
    cmd = ["codex", "exec", "--skip-git-repo-check", "-C", str(workdir)]
    for r in refs:
        cmd += ["-i", str(r)]
    return cmd


def still_prompt(subject: str, name: str, aspect: str = "9:16", keep_reference: bool = False) -> str:
    """One-image ticket: what to make, where to save it, the aspect, the brand rules, then look at it."""
    if aspect not in ASPECTS:
        raise ValueError(f"aspect {aspect!r} not in {sorted(ASPECTS)}")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}\.(png|jpg)", name):
        raise ValueError(f"output name {name!r} must be plain [A-Za-z0-9._-] ending .png or .jpg")
    if "\u2014" in subject:
        raise ValueError("no em dashes in a ticket")
    w, h = ASPECTS[aspect]
    keep = (" Keep the subject of the attached reference image exactly as it is: same shape, same marks, "
            "same colours; only recompose the frame.") if keep_reference else ""
    return (f"Create one image and save it as {name} in the current directory. {subject.strip().rstrip('.')}. "
            f"Aspect {aspect}, about {w}x{h} pixels.{keep} {BRAND} "
            f"Then open {name} to check it matches this ticket, and say in one line what you see.")


def image_kind(head: bytes) -> str:
    """'png' / 'jpg' / 'webp' from a file's first bytes, or ''."""
    return next((k for sig, k in MAGIC.items() if head.startswith(sig)), "")


def child_env() -> dict:
    """The environment for codex with every OpenAI API key variable removed (plan login only)."""
    return {k: v for k, v in os.environ.items() if not re.match(r"OPENAI_(API_KEY|ORG|PROJECT)", k)}


def make(workdir: str | Path, name: str, subject: str, aspect: str = "9:16", refs: list[str] | None = None,
         keep_reference: bool = False, timeout: float = TIMEOUT, run=subprocess.run) -> dict:
    """Run one Codex still ticket and check the file it saved is a real image. No retry: a try spends plan quota."""
    wd = Path(workdir).expanduser().resolve()
    wd.mkdir(parents=True, exist_ok=True)
    paths = [Path(r).expanduser().resolve() for r in (refs or [])]
    if len(paths) > MAX_REFS or any(not p.is_file() for p in paths):
        raise StudioError(f"refs must be at most {MAX_REFS} existing files: {refs}")
    out = wd / name
    if out.exists():
        raise StudioError(f"{out} exists; pick another name")
    prompt = still_prompt(subject, name, aspect, keep_reference)
    try:
        r = run(codex_cmd(wd, paths), input=prompt, capture_output=True, text=True, timeout=timeout, env=child_env())
    except subprocess.TimeoutExpired as e:
        raise StudioError(f"codex still {name} timed out after {timeout:.0f}s") from e
    except FileNotFoundError as e:
        raise StudioError("codex CLI is not installed") from e
    tail = (r.stdout or "").strip().splitlines()[-3:]
    if r.returncode:
        raise StudioError(f"codex exit {r.returncode}: {(r.stderr or '')[-400:]}")
    if not out.is_file():
        raise StudioError(f"codex finished but saved no {name}; last output: {tail}")
    kind = image_kind(out.read_bytes()[:8])
    if not kind:
        raise StudioError(f"{out} is not an image")
    return {"ok": True, "path": str(out), "kind": kind, "aspect": aspect, "codex_said": tail}


def variants(workdir: str | Path, src: str | Path, aspects: list[str], run=subprocess.run) -> dict:
    """The same still recomposed per aspect, each ticket with the source as its -i reference."""
    s = Path(src).expanduser()
    out, errors = [], {}
    for a in aspects:
        name = f"{s.stem}-{a.replace(':', 'x')}.png"
        try:
            out.append(make(workdir, name, "The attached image, recomposed for this aspect", a, [str(s)],
                            keep_reference=True, run=run))
        except (StudioError, ValueError) as e:
            errors[a] = str(e)
    return {"ok": not errors, "stills": out, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m workman.studio.still", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    mk = sub.add_parser("make")
    mk.add_argument("dir")
    mk.add_argument("name")
    mk.add_argument("subject")
    mk.add_argument("--aspect", default="9:16")
    mk.add_argument("-i", dest="refs", action="append", default=[])
    va = sub.add_parser("variants")
    va.add_argument("dir")
    va.add_argument("src")
    va.add_argument("--aspects", nargs="+", default=["9:16", "16:9", "1:1"])
    a = ap.parse_args(argv)
    try:
        out = make(a.dir, a.name, a.subject, a.aspect, a.refs) if a.cmd == "make" else variants(a.dir, a.src, a.aspects)
    except (StudioError, ValueError) as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 1
    print(json.dumps(out, indent=1))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
