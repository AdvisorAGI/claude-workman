"""QC: the machine checks of a render, written as QC.md in the studio-qc format.

QC.md starts with `RESULT: PASS`, `RESULT: FAIL` or `RESULT: PENDING REVIEW`, then one row
per check: | # | check | PASS/FAIL | evidence | owner of the fix |. The eye checks (no
women, one continuous bed, male American voice, brand look) are not detectors: they stay
PENDING until the reviewer who looked at frames.jpg and audio.png passes them in, so a
render can never reach PASS on measurements alone. Traffic uploads only on RESULT: PASS.

Machine checks: copy rules (em dash, model names), every cited fact ID exists in FACTS.md
(IDs named in its Conflicts section are flagged), no number without a fact ID, probe,
loudness, words on their onsets (band test and, when a transcript exists, against the
spoken words), cuts on the plan within one frame, the spoken words equal the script.

    python -m workman.studio.qc <job> [--name NAME] [--spec spec.json] [--copy-only]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from . import voice
from .assemble import cuts, kind, load, with_defaults
from .inspect import letters, onset_misses, read_transcript
from .job import StudioError

FACTS = Path(os.environ.get("WORKMAN_STUDIO_FACTS", "~/advisorppc-ads-20260925/refs/FACTS.md")).expanduser()
FACT_ID = re.compile(r"\bF\d{2,3}\b")
EYE = {"women": ("No women, photoreal men only (frames.jpg, every tile)", "cinematographer"),
       "bed": ("One continuous bed, no melody or hits (audio.png spectrogram)", "sound"),
       "voice": ("Male brand voice, native American delivery (listen)", "sound"),
       "look": ("Brand look: black, off-white, one emerald accent, black Ai mark, no dot", "cinematographer")}
TP_MAX = -1.0      # build_series.loudness_errors; studio-qc.md asks for -1.5, see docs/STUDIO.md


# ------------------------------------------------------------------ pure

def row(check: str, ok: bool | None, evidence: str, owner: str, warn: bool = False) -> dict:
    result = "PENDING" if ok is None else ("PASS" if ok else ("WARN" if warn else "FAIL"))
    return {"check": check, "result": result, "evidence": evidence, "owner": owner}


def parse_facts(md: str) -> tuple[set[str], set[str]]:
    """(fact IDs defined in the tables, IDs mentioned in the Conflicts section)."""
    ids = set(re.findall(r"^\|\s*(F\d{2,3})\s*\|", md, re.M))
    m = re.search(r"^## Conflicts.*?(?=^## |\Z)", md, re.M | re.S)
    return ids, set(FACT_ID.findall(m.group(0))) if m else set()


def copy_lines(spec: dict) -> list[tuple[str, str, list[str], bool]]:
    """(where, text, fact IDs, opinion) for every piece of viewer-facing copy in the spec."""
    out = []
    for n, L in enumerate(spec.get("vo", [])):
        facts, opinion = list(L.get("facts", [])), bool(L.get("opinion", False))
        out.append((f"vo {n + 1}", voice.spoken(L["text"]), facts, opinion))
        out += [(f"vo {n + 1} words", w, facts, opinion) for w in L.get("words", [])]
    for s in spec.get("shots", []):
        if kind(s) == "card":
            c = s["card"]
            for key in ("cta", "wordmark", "url"):
                if c.get(key):
                    out.append((f"card {key}", str(c[key]), list(c.get("facts", [])), key != "cta"))
    return out


def copy_rows(spec: dict, docs: dict[str, str]) -> list[dict]:
    rows = []
    bad = [f"{where}: {voice.copy_problem(t)!r} in {t!r}"
           for where, t, _, _ in copy_lines(spec) if voice.copy_problem(t)]
    rows.append(row("Copy: no em dash, no model names (VO, words, card)", not bad, "; ".join(bad) or "spec copy clean",
                    "copywriter"))
    dash = [name for name, text in docs.items() if "\u2014" in text]
    rows.append(row("Copy: no em dash in the job documents", not dash, ", ".join(dash) or f"{', '.join(docs)} clean",
                    "copywriter"))
    return rows


def fact_rows(spec: dict, docs: dict[str, str], known: set[str], conflicts: set[str], facts_path: str) -> list[dict]:
    """Every cited ID exists; conflict IDs flagged; every claim carries an ID; no number without one."""
    rows = []
    cited = {name: sorted(set(FACT_ID.findall(text))) for name, text in docs.items()}
    cited["spec.json"] = sorted({i for _, _, ids, _ in copy_lines(spec) for i in ids})
    unknown = {name: [i for i in ids if i not in known] for name, ids in cited.items()}
    unknown = {k: v for k, v in unknown.items() if v}
    rows.append(row("Claims: every cited fact ID exists in FACTS.md", not unknown,
                    json.dumps(unknown) if unknown else f"{sum(map(len, cited.values()))} citations in {facts_path}",
                    "strategist"))
    hit = sorted({i for ids in cited.values() for i in ids} & conflicts)
    rows.append(row("Claims: no cited ID is in FACTS.md Conflicts", not hit,
                    f"{hit} named in Conflicts; check the ad uses the wording that section approves" if hit
                    else "none cited", "strategist", warn=True))
    bare = [where for where, _, ids, opinion in copy_lines(spec) if where.startswith("vo") and "words" not in where
            and not ids and not opinion]
    rows.append(row("Claims: every VO line has a fact ID or is marked opinion", not bare,
                    ", ".join(bare) or "all lines traced", "copywriter"))
    nums = [f"{where}: {t!r}" for where, t, ids, _ in copy_lines(spec) if re.search(r"\d", t) and not ids
            and where != "card url"]
    rows.append(row("Claims: no number without a fact ID", not nums, "; ".join(nums) or "no untraced numbers",
                    "copywriter"))
    return rows


def transcript_rows(spec: dict, timing: dict, transcript: str) -> list[dict]:
    """Spoken words equal the script; each phrase appears at most 0.1 s after its spoken onset and not before
    it beyond Scribe's 0.075 s jitter (inspect.onset_misses)."""
    words = read_transcript(transcript)
    if not words:
        why = transcript.strip().splitlines()[0] if transcript.strip() else "no transcript.txt"
        return [row("Words spoken equal the script (transcript)", None, why, "sound"),
                row("Words on screen land on the spoken onset (transcript)", None, why, "editor")]
    said, script = letters(" ".join(w for _, w in words)), letters(" ".join(voice.spoken(L["text"])
                                                                           for L in spec.get("vo", [])))
    bad = onset_misses(timing.get("words", []), words)
    return [row("Words spoken equal the script (transcript)", said == script,
                f"transcript {said[:80]!r} vs script {script[:80]!r}", "sound"),
            row("Words on screen land on the spoken onset (transcript)", not bad,
                "; ".join(bad) or "every phrase on its spoken word (-0.075..+0.1 s)", "editor")]


def parse_cuts(text: str) -> list[float]:
    return [float(m.group(1)) for m in re.finditer(r"^\s*([\d.]+) s\s+score", text, re.M)]


def cut_rows(spec: dict, detected: list[float], planned: list[dict] | None) -> list[dict]:
    """Planned cuts: seen on the render's own frames (verify's frame-difference check, 1 frame). Unplanned
    cuts: any scene-detected cut more than one frame from a planned one."""
    fps = spec["fps"]
    plan_t = [c1 / fps for _, c1 in cuts(spec)][:-1]
    tol = 1 / fps + 1e-6
    extra = [t for t in detected if not any(abs(t - p) <= tol for p in plan_t)]
    if planned is None:
        first = row("Cuts: every planned cut is a hard cut on the render", None,
                    "verify.json has no frame-difference check; re-run verify", "editor")
    else:
        missed = [f"{c['t']}s change {c.get('diff')} vs median {c.get('median')}" for c in planned if not c["seen"]]
        first = row("Cuts: every planned cut is a hard cut on the render", not missed,
                    "; ".join(missed) or f"{len(planned)} cuts, each at least 1.8x the shot's frame-to-frame change",
                    "editor")
    return [first, row("Cuts: no cut inside a shot", not extra,
                       f"unplanned {extra}" if extra else f"scene cuts {detected} all on the plan", "editor")]


def verify_rows(v: dict, tp_max: float = TP_MAX) -> list[dict]:
    lo = v.get("loudness", {})
    i, tp = lo.get("integrated"), lo.get("true_peak")
    loud = voice.loudness_errors(i, tp, tp_max=tp_max) if isinstance(i, (int, float)) and isinstance(tp, (int, float)) \
        else ["no loudness measured"]
    rows = [row("Format: probe (codec, size, fps, duration)", not v.get("probe"), "; ".join(v.get("probe", []))
                or "h264 + aac, size, fps and length as the spec", "editor"),
            row(f"Loudness: -14 LUFS +-0.5, true peak <= {tp_max} dBTP", not loud,
                f"I {i} LUFS, TP {tp} dBTP" + (f"; {loud}" if loud else ""), "sound"),
            row("VO: every take inside its shot", not v.get("vo"), "; ".join(v.get("vo", [])) or "all inside", "sound"),
            row("Copy: VO says the script", not v.get("copy"), "; ".join(v.get("copy", [])) or "every line matches",
                "sound")]
    bad = [f"{o['txt']!r} -0.1s {o['diff_before']} +0.4s {o['diff_after']}" for o in v.get("onsets", []) if not o["ok"]]
    rows.append(row("Onsets: text band clean before, drawn after (band test)", not bad and bool(v.get("onsets")),
                    "; ".join(bad) or f"{len(v.get('onsets', []))} lines ok", "editor"))
    if v.get("green") is not None:
        g = v["green"]
        rows.append(row("Chime frame: logo reads green on the planned frame", g["ok"],
                        f"green px before {g['frame_before']}, on {g['frame_on']}", "editor"))
    if v.get("deviations"):
        rows.append(row("Readability: words on screen 1.5 s or more", False, "; ".join(v["deviations"]), "copywriter",
                        warn=True))
    return rows


def eye_rows(eye: dict | None, frames: str) -> list[dict]:
    rows = []
    for key, (check, owner) in EYE.items():
        e = (eye or {}).get(key)
        if not isinstance(e, dict) or "pass" not in e:
            rows.append(row(check, None, f"eye review pending: look at {frames}", owner))
        else:
            rows.append(row(check, bool(e["pass"]), str(e.get("evidence", ""))[:300] or "reviewer gave no evidence",
                            owner))
    return rows


def verdict(rows: list[dict]) -> str:
    results = {r["result"] for r in rows}
    return "FAIL" if "FAIL" in results else "PENDING REVIEW" if "PENDING" in results else "PASS"


def render_md(name: str, rows: list[dict], result: str) -> str:
    lines = [f"RESULT: {result}", "", f"# {name}: QC (studio-qc machine checks, {time.strftime('%Y-%m-%d %H:%M')})", "",
             "| # | check | PASS/FAIL | evidence (file, time, value) | owner of the fix |", "|---|---|---|---|---|"]
    for n, r in enumerate(rows, 1):
        ev = str(r["evidence"]).replace("|", "/").replace("\n", " ")
        lines.append(f"| {n} | {r['check']} | {r['result']} | {ev} | {r['owner']} |")
    lines += ["", "Owner still hears the final cut.", ""]
    return "\n".join(lines)


# ------------------------------------------------------------------ io

def _docs(d: Path) -> dict[str, str]:
    return {n: (d / n).read_text(errors="replace") for n in ("BRIEF.md", "SCRIPT.md") if (d / n).is_file()}


def _facts(facts_path: Path) -> tuple[set[str], set[str]]:
    try:
        return parse_facts(facts_path.read_text())
    except OSError as e:
        raise StudioError(f"cannot read {facts_path}: {e}") from e


def copy_check(job: str | Path, spec_name: str = "spec.json", facts_path: Path = FACTS) -> dict:
    """The copy and claim rows only, for the copywriter before anything is made. Writes nothing."""
    d, s = load(job, spec_name)
    known, conflicts = _facts(facts_path)
    rows = copy_rows(s, _docs(d)) + fact_rows(s, _docs(d), known, conflicts, str(facts_path))
    return {"ok": verdict(rows) != "FAIL", "result": verdict(rows), "rows": rows}


def qc(job: str | Path, name: str = "", spec_name: str = "spec.json", eye: dict | None = None,
       facts_path: Path = FACTS, tp_max: float = TP_MAX) -> dict:
    """All machine checks of one render into QC.md (and review/<name>_qc.json); eye checks stay PENDING
    unless `eye` passes them in."""
    d, s = load(job, spec_name)
    name = name or s["name"]
    if name != s["name"]:
        from .assemble import derive
        fmt = next((f for f in s.get("formats", []) if name == f"{s['name']}-{f.replace(':', 'x')}"), None)
        if fmt is None:
            raise StudioError(f"render {name!r} is not the spec's {s['name']!r} or one of its formats")
        s = with_defaults(derive(s, fmt))
    out, review = d / "out", d / "review"
    vpath, tpath, insp = review / f"{name}_verify.json", out / f"{name}.timing.json", out / f"{name}.inspect"
    for p in (vpath, tpath):
        if not p.is_file():
            raise StudioError(f"missing {p}; run studio_assemble (render + verify) first")
    v, tm = json.loads(vpath.read_text()), json.loads(tpath.read_text())
    known, conflicts = _facts(facts_path)
    docs = _docs(d)
    rows = copy_rows(s, docs) + fact_rows(s, docs, known, conflicts, str(facts_path)) + verify_rows(v, tp_max)
    cuts_txt = insp / "cuts.txt"
    if cuts_txt.is_file():
        rows += cut_rows(s, parse_cuts(cuts_txt.read_text()), v.get("cuts"))
    else:
        rows.append(row("Cuts: detected cuts match the plan", None, f"no {cuts_txt}; run studio_inspect", "editor"))
    tr = insp / "transcript.txt"
    rows += transcript_rows(s, tm, tr.read_text() if tr.is_file() else "")
    rows += eye_rows(eye, str(insp / "frames.jpg"))
    result = verdict(rows)
    (d / "QC.md").write_text(render_md(name, rows, result))
    (review / f"{name}_qc.json").write_text(json.dumps({"result": result, "rows": rows}, indent=1))
    return {"ok": result == "PASS", "result": result, "qc_md": str(d / "QC.md"), "rows": rows}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m workman.studio.qc", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job")
    ap.add_argument("--name", default="")
    ap.add_argument("--spec", default="spec.json")
    ap.add_argument("--facts", type=Path, default=FACTS)
    ap.add_argument("--copy-only", action="store_true")
    ap.add_argument("--eye", default="", help='JSON: {"women": {"pass": true, "evidence": "..."}, ...}')
    a = ap.parse_args(argv)
    try:
        eye = json.loads(a.eye) if a.eye else None
        out = copy_check(a.job, a.spec, a.facts) if a.copy_only else qc(a.job, a.name, a.spec, eye, a.facts)
    except (StudioError, ValueError, OSError, KeyError) as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 1
    print(json.dumps({k: v for k, v in out.items() if k != "rows"} | {"rows": [f"{r['result']:7} {r['check']}"
                                                                           for r in out["rows"]]}, indent=1))
    return 0 if out.get("result") != "FAIL" else 1


if __name__ == "__main__":
    sys.exit(main())
