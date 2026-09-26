"""Find a skill by query and load only that one.

A session that loads every SKILL.md up front spends its context on skills it
never uses. This module keeps a small index (name, description, headings) of
every skill on disk, ranks it against a query, and hands back the body of the
one skill that was asked for, or a single section of it.

Roots, searched in order:
  ~/.claude/skills
  every directory in $WORKMAN_SKILL_ROOTS (os.pathsep separated)

A skill is a directory holding a SKILL.md. Its name and description come from
the YAML-style frontmatter; without frontmatter the first `# heading` names it
and the first paragraph after it describes it.

The index is cached at ~/.cache/workman/skills-index.json and rebuilt whenever
the set of SKILL.md files, or any one's mtime, changes. Everything that touches
HOME is resolved at call time so a test (or a sandboxed HOME) is honoured.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
from pathlib import Path

INDEX_VERSION = 1
DESC_TRIM = 200
MAX_SIBLINGS = 50
_TOKEN = re.compile(r"[a-z0-9]+")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
# Field weights: a query word in the skill's name is the strongest signal.
_W_NAME, _W_HEAD, _W_DESC = 3.0, 1.5, 1.0


# ---- paths ------------------------------------------------------------------

def env_roots() -> list[Path]:
    raw = os.environ.get("WORKMAN_SKILL_ROOTS", "")
    return [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]


def roots() -> list[Path]:
    """Every root, de-duplicated, default first."""
    seen: set[str] = set()
    out: list[Path] = []
    for r in [Path.home() / ".claude" / "skills", *env_roots()]:
        key = os.path.normpath(str(r))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def cache_path() -> Path:
    return Path.home() / ".cache" / "workman" / "skills-index.json"


# ---- parsing ----------------------------------------------------------------

def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split `---` frontmatter from the body. Handles `key: value`, quoted
    values, CRLF, and block scalars (`>` / `|`) or indented continuation
    lines. Returns ({}, text) when there is no frontmatter."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("﻿"):
        text = text[1:]
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    block = text[4:end]
    rest = text[end + 4:]
    body = rest[1:] if rest.startswith("\n") else rest.split("\n", 1)[-1] if rest else ""
    meta: dict[str, str] = {}
    key = None
    for line in block.split("\n"):
        if not line.strip():
            continue
        if line[:1] in " \t" and key:
            meta[key] = (meta[key] + " " + line.strip()).strip()
            continue
        m = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if not m:
            key = None
            continue
        key = m.group(1).lower()
        value = m.group(2).strip()
        meta[key] = "" if value in (">", "|", ">-", "|-", ">+", "|+") else _unquote(value)
    return meta, body


def headings(body: str) -> list[str]:
    out = []
    in_fence = False
    for line in body.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING.match(line)
        if m:
            out.append(m.group(2))
    return out


def _fallback(body: str) -> tuple[str, str]:
    """(name, description) from the first heading and the paragraph after it."""
    lines = body.split("\n")
    name = ""
    desc: list[str] = []
    for i, line in enumerate(lines):
        m = _HEADING.match(line)
        if m and not name:
            name = m.group(2)
            for nxt in lines[i + 1:]:
                if not nxt.strip():
                    if desc:
                        break
                    continue
                if _HEADING.match(nxt):
                    break
                desc.append(nxt.strip())
            break
    return name, " ".join(desc)


def read_skill(skill_dir: Path, root: Path) -> dict:
    path = skill_dir / "SKILL.md"
    text = path.read_text(encoding="utf-8", errors="replace")
    meta, body = parse_frontmatter(text)
    fb_name, fb_desc = _fallback(body)
    return {
        "name": meta.get("name") or fb_name or skill_dir.name,
        "dir": skill_dir.name,
        "description": meta.get("description") or fb_desc,
        "headings": headings(body),
        "root": str(root),
        "path": str(path),
    }


# ---- index ------------------------------------------------------------------

def _discover() -> list[tuple[Path, Path]]:
    """(root, skill_dir) for every direct child holding a SKILL.md."""
    found = []
    for root in roots():
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                if child.is_dir() and (child / "SKILL.md").is_file():
                    found.append((root, child))
            except OSError:
                continue
    return found


def _fingerprint(found: list[tuple[Path, Path]]) -> list[list]:
    fp = []
    for _root, d in found:
        try:
            fp.append([str(d / "SKILL.md"), (d / "SKILL.md").stat().st_mtime_ns])
        except OSError:
            continue
    return sorted(fp)


def _build(found: list[tuple[Path, Path]], fp: list[list]) -> dict:
    skills, errors = [], []
    for root, d in found:
        try:
            skills.append(read_skill(d, root))
        except Exception as exc:  # one bad file must not hide the rest
            errors.append({"path": str(d / "SKILL.md"), "error": f"{type(exc).__name__}: {exc}"})
    return {"version": INDEX_VERSION, "roots": [str(r) for r in roots()],
            "fingerprint": fp, "skills": skills, "errors": errors}


def load_index(force: bool = False) -> dict:
    """The cached index, rebuilt if any SKILL.md was added, removed or touched."""
    found = _discover()
    fp = _fingerprint(found)
    cp = cache_path()
    current_roots = [str(r) for r in roots()]
    if not force:
        try:
            cached = json.loads(cp.read_text(encoding="utf-8"))
            if (cached.get("version") == INDEX_VERSION and cached.get("fingerprint") == fp
                    and cached.get("roots") == current_roots):
                cached["cached"] = True
                return cached
        except (OSError, ValueError):
            pass
    index = _build(found, fp)
    try:
        cp.parent.mkdir(parents=True, exist_ok=True)
        tmp = cp.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(index), encoding="utf-8")
        os.replace(tmp, cp)
    except OSError:
        pass  # a read-only cache dir only costs a rebuild next time
    index["cached"] = False
    return index


def reindex() -> dict:
    index = load_index(force=True)
    return {"ok": True, "count": len(index["skills"]), "roots": index["roots"],
            "errors": index["errors"], "cache": str(cache_path())}


# ---- search -----------------------------------------------------------------

def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _field_hit(q: str, toks: list[str]) -> float:
    """1.0 for an exact token, 0.6 for a prefix match (q of >=3 chars)."""
    best = 0.0
    for t in toks:
        if t == q:
            return 1.0
        if len(q) >= 3 and t.startswith(q):
            best = 0.6
    return best


def search(query: str, limit: int = 8) -> dict:
    index = load_index()
    skills = index["skills"]
    qtoks = list(dict.fromkeys(tokens(query)))
    if not qtoks:
        return {"ok": False, "error": "empty_query",
                "hint": "Pass a few words describing the task, e.g. 'stripe checkout'."}
    fields = []
    for s in skills:
        fields.append((tokens(s["name"] + " " + s.get("dir", "")),
                       tokens(" ".join(s.get("headings", []))),
                       tokens(s.get("description", ""))))
    n = len(skills) or 1
    # BM25-style idf: a word every skill mentions says little.
    idf = {}
    for q in qtoks:
        df = sum(1 for f in fields if any(_field_hit(q, part) for part in f))
        idf[q] = math.log(1 + (n - df + 0.5) / (df + 0.5))
    scored = []
    for s, (nt, ht, dt) in zip(skills, fields):
        score = 0.0
        for q in qtoks:
            w = (_W_NAME * _field_hit(q, nt) + _W_HEAD * _field_hit(q, ht)
                 + _W_DESC * _field_hit(q, dt))
            score += idf[q] * w
        # Whole query appearing in the name (e.g. "skill finder") is a strong hit.
        if len(qtoks) > 1 and " ".join(qtoks) in " ".join(nt):
            score += 2.0
        if score > 0:
            scored.append((score, s))
    scored.sort(key=lambda p: (-p[0], p[1]["name"]))
    limit = max(1, min(int(limit or 8), 50))
    results = []
    for score, s in scored[:limit]:
        desc = s.get("description", "")
        if len(desc) > DESC_TRIM:
            desc = desc[:DESC_TRIM - 1].rstrip() + "…"
        results.append({"name": s["name"], "description": desc, "root": s["root"],
                        "path": s["path"], "score": round(score, 3)})
    return {"ok": True, "query": query, "count": len(results), "total": len(skills),
            "results": results,
            "next": "skill_show(name) loads one skill; pass section= to load only one part."}


# ---- show -------------------------------------------------------------------

def _find(name: str, index: dict) -> dict | None:
    want = name.strip().lower()
    for s in index["skills"]:
        if s["name"].lower() == want or s.get("dir", "").lower() == want:
            return s
    return None


def extract_section(body: str, section: str) -> str | None:
    """The `## section` block (case-insensitive heading match, exact first,
    then prefix), up to the next heading of the same or a higher level."""
    lines = body.split("\n")
    want = section.strip().lstrip("#").strip().lower()
    found = []
    in_fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING.match(line)
        if m:
            found.append((i, len(m.group(1)), m.group(2).strip().lower()))
    pick = next((h for h in found if h[2] == want), None) or \
        next((h for h in found if h[2].startswith(want)), None)
    if pick is None:
        return None
    start, level, _ = pick
    end = len(lines)
    for i, lvl, _t in found:
        if i > start and lvl <= level:
            end = i
            break
    return "\n".join(lines[start:end]).rstrip() + "\n"


def _siblings(skill_dir: Path) -> tuple[list[str], bool]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(skill_dir):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for f in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, f), skill_dir)
            if rel == "SKILL.md" or f.startswith("."):
                continue
            out.append(rel.replace(os.sep, "/"))
            if len(out) >= MAX_SIBLINGS:
                return out, True
    return out, False


def show(name: str, section: str = "", max_chars: int = 20000) -> dict:
    index = load_index()
    s = _find(name, index)
    if s is None:
        near = search(name, limit=5)
        return {"ok": False, "error": "not_found", "name": name,
                "did_you_mean": [r["name"] for r in near.get("results", [])]}
    path = Path(s["path"])
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "error": "unreadable", "path": str(path), "detail": str(exc)}
    _meta, body = parse_frontmatter(text)
    if section:
        part = extract_section(body, section)
        if part is None:
            return {"ok": False, "error": "section_not_found", "name": s["name"],
                    "section": section, "sections": s.get("headings", [])}
        body = part
    max_chars = max(200, int(max_chars or 20000))
    total = len(body)
    truncated = total > max_chars
    if truncated:
        body = body[:max_chars]
    files, more = _siblings(path.parent)
    return {"ok": True, "name": s["name"], "description": s.get("description", ""),
            "path": str(path), "root": s["root"], "section": section or None,
            "body": body, "chars": total, "truncated": truncated,
            "files": files, "files_truncated": more,
            "sections": s.get("headings", [])}


# ---- add --------------------------------------------------------------------

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def add(src_dir: str, root: str = "") -> dict:
    """Copy a skill directory into a skill root. Refuses to overwrite."""
    src = Path(src_dir).expanduser()
    skill_md = src / "SKILL.md"
    if not skill_md.is_file():
        return {"ok": False, "error": "no_skill_md", "src_dir": str(src),
                "hint": "A skill is a directory with a SKILL.md at its top."}
    meta, _body = parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
    name, desc = meta.get("name", "").strip(), meta.get("description", "").strip()
    missing = [k for k, v in (("name", name), ("description", desc)) if not v]
    if missing:
        return {"ok": False, "error": "bad_frontmatter", "missing": missing,
                "hint": "SKILL.md must start with ---, name: ..., description: ..., ---"}
    if not _SAFE_NAME.match(name):
        return {"ok": False, "error": "bad_name", "name": name,
                "hint": "Use letters, digits, dot, dash or underscore only."}
    if root:
        dest_root = Path(root).expanduser()
    else:
        env = env_roots()
        if not env:
            return {"ok": False, "error": "no_target_root",
                    "hint": "Set WORKMAN_SKILL_ROOTS or pass root= explicitly."}
        dest_root = env[0]
    dest = dest_root / name
    try:
        if os.path.realpath(dest) == os.path.realpath(src):
            return {"ok": False, "error": "same_path", "path": str(dest)}
    except OSError:
        pass
    if dest.exists():
        return {"ok": False, "error": "exists", "path": str(dest),
                "hint": "Refusing to overwrite an existing skill."}
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    except (OSError, shutil.Error) as exc:
        return {"ok": False, "error": "copy_failed", "detail": str(exc)}
    index = load_index()
    return {"ok": True, "name": name, "path": str(dest / "SKILL.md"), "root": str(dest_root),
            "indexed": any(s["path"] == str(dest / "SKILL.md") for s in index["skills"])}
