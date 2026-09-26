"""grok Imagine: prompt builders and the Download > Save dialog > intake flow.

Prompts follow the owner lessons of 2026-09-25 (grok-imagine-browser skill):
locks first, one change per clip, the lock again at the end, no mood or camera
adjectives on a locked shot, the shortest duration that fits (6 s for one state
change, 15 s only for a continuous move harvested into cuts). Narration names the
exact line, when it starts, the voice (male only, Helios is the brand voice) and a
native General American delivery.

Downloads go through grok's own Download button only (Post actions > Download).
The GTK Save dialog that opens outside the pane is renamed and confirmed with
xdotool; browser cookies are never read. Then `intake` moves the file into the
job's clips/, probes it and makes a contact strip.

    python -m workman.studio.grok locked "<subject>" "<one change>" [--seconds 6]
    python -m workman.studio.grok narrate "<line>" --at 1 --seconds 6 [--voice Helios]
    python -m workman.studio.grok save <clip-name.mp4> [--display :0]
    python -m workman.studio.grok intake <downloaded.mp4> <job> <name>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .job import StudioError, cool_down, ff, probe, resolve_job

# Measured median pitch, deepest first (UI-MAP-v2.md). Female voices are refused.
MALE_VOICES = ("Orion", "Helios", "Perseus", "Rex", "Zenith", "Leo", "Altair", "Lux", "Castor", "Cosmo", "Zagan",
               "Rigel", "Kepler", "Atlas", "Naksh", "Sal", "Sirius", "Lumen")
FEMALE_VOICES = ("Eve", "Luna", "Iris", "Helix", "Ara", "Ursa", "Celeste", "Carina")
BRAND_VOICE = "Helios"
DURATIONS = (6, 10, 15)
ACCENT = "native American English accent, natural Hollywood film delivery, clear, warm, confident"
# Read as permission to move on a locked shot (owner lesson, logo LED clip).
MOTION_WORDS = re.compile(r"\b(cinematic|premium|dramatic|push-?ins?|orbit(?:s|ing)?|dolly|pan(?:s|ning)?|"
                          r"tilt(?:s|ing)?|zoom(?:s|ing)?|sweeping|dynamic|epic|glow(?:s|ing)?|rotat(?:e|es|ing)|"
                          r"spin(?:s|ning)?)\b", re.I)
PEOPLE_WORDS = re.compile(r"\b(woman|women|girl|girls|lady|ladies|female|she|her)\b", re.I)
WPS_MAX = 2.5                          # narration pace: about 2.5 words per second
DOWNLOADS = Path("~/Downloads").expanduser()
DIALOG_TITLE = "blob:https://grok.com"  # the pane's native save dialog title
GROK_CLIP = {"width": 1088, "height": 1920, "fps": 24.0}   # 1080p 9:16 as grok delivers it


# ------------------------------------------------------------------ pure: prompts

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def check_prompt(prompt: str) -> list[str]:
    """Rule problems in a prompt: em dash, women, female voices."""
    errs = []
    if "\u2014" in prompt:
        errs.append("em dash")
    if PEOPLE_WORDS.search(prompt):
        errs.append(f"woman in prompt ({PEOPLE_WORDS.search(prompt).group(0)!r}); men only")
    for v in FEMALE_VOICES:
        if re.search(rf"\b{v}\b", prompt):
            errs.append(f"female voice {v}")
    return errs


def pick_seconds(continuous_move: bool) -> int:
    """6 s for a single state change; 15 s only for a continuous move that will be harvested into cuts."""
    return 15 if continuous_move else 6


def locked_shot(subject: str, change: str, seconds: int = 6, locks: tuple[str, ...] = ()) -> str:
    """Locks first, the one change with its order and timing, the lock again. Raises on camera or mood words."""
    if seconds not in DURATIONS:
        raise ValueError(f"grok clips are {DURATIONS} s, got {seconds}")
    body = f"{subject} {change} {' '.join(locks)}"
    m = MOTION_WORDS.search(body)
    if m:
        raise ValueError(f"{m.group(0)!r} reads as permission to move on a locked shot")
    extra = " ".join(_clean(x).rstrip(".") + "." for x in locks)
    prompt = (f"Completely static shot. The camera does not move. {_clean(subject).rstrip('.')} does not move, "
              f"rotate, glow or change in any way. {extra} {_clean(change).rstrip('.')}. "
              f"Everything else stays exactly as in the image for the whole video.")
    errs = check_prompt(prompt)
    if errs:
        raise ValueError("; ".join(errs))
    return _clean(prompt)


def man(age: str, build: str) -> str:
    """The people clause every person prompt carries (owner rule: men only)."""
    return f"a man, {_clean(age)}, {_clean(build)}, photoreal and natural, no other people in frame"


def narration(line: str, at: float, seconds: int, voice: str = BRAND_VOICE) -> str:
    """Narration block: the exact line in quotes, when it starts, the voice named twice, the accent."""
    if voice not in MALE_VOICES:
        raise ValueError(f"voice {voice!r} is not one of the male voices {MALE_VOICES}")
    words = len(re.findall(r"[A-Za-z0-9']+", line))
    room = seconds - at
    if room <= 0 or words / room > WPS_MAX:
        raise ValueError(f"{words} words from {at} s in a {seconds} s clip is over {WPS_MAX} words per second")
    if '"' in line or "\u2014" in line:
        raise ValueError("narration line may not contain double quotes or an em dash")
    at_s = f"{at:g} second" + ("" if at == 1 else "s")
    return (f'A calm male narrator says, starting at {at_s}: "{_clean(line)}" '
            f"Use the {voice} voice exactly, no other voice. {ACCENT}.")


def sound(bed: bool = False, sync: tuple[str, ...] = ()) -> str:
    """Sound block: one soft continuous bed with no melody, or no music when the edit adds the bed."""
    base = "One soft continuous ambient bed, no melody." if bed else "No music."
    return _clean(base + " " + " ".join(_clean(s).rstrip(".") + "." for s in sync))


def clip_name(what: str, post_id: str, seconds: int) -> str:
    """grok-<what>-<postid8>-<dur>s.mp4: the rename every download gets in the Save dialog, so
    two clips from one conversation never overwrite each other."""
    slug = re.sub(r"[^a-z0-9]+", "-", what.lower()).strip("-")[:40]
    pid = re.sub(r"[^0-9a-f]", "", post_id.lower())[:8]
    if not slug or not pid or seconds not in DURATIONS:
        raise ValueError("clip_name needs a subject, a hex post id and a grok duration")
    return f"grok-{slug}-{pid}-{seconds}s.mp4"


def download_steps(post_id: str, name: str) -> list[dict]:
    """The pane lane, step by step, for the agent driving the built-in browser. Escape is never
    used on a post page (it navigates Back)."""
    return [
        {"step": "navigate", "url": f"https://grok.com/imagine/post/{post_id}"},
        {"step": "click", "target": "Post actions (sliders icon, top right; a bottom sheet in the compact pane)"},
        {"step": "click", "target": "Download (down-arrow)"},
        {"step": "save_dialog", "name": name, "how": f"python -m workman.studio.grok save {name}"},
        {"step": "intake", "how": f"python -m workman.studio.grok intake ~/Downloads/{name} <job> <clip-name>"},
    ]


def clip_errors(p: dict, want: dict = GROK_CLIP) -> list[str]:
    """A downloaded clip against grok's 1080p 9:16 delivery: 1088x1920, 24 fps, one video stream."""
    v = [s for s in p.get("streams", []) if s.get("codec_type") == "video"]
    if len(v) != 1:
        return [f"{len(v)} video streams"]
    errs = []
    if (v[0].get("width"), v[0].get("height")) != (want["width"], want["height"]):
        errs.append(f"size {v[0].get('width')}x{v[0].get('height')}")
    num, _, den = str(v[0].get("r_frame_rate", "0/1")).partition("/")
    try:
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    if abs(fps - want["fps"]) > 0.01:
        errs.append(f"fps {fps:g}")
    return errs


# ------------------------------------------------------------------ io: save dialog and intake

def _x(cmd: list[str], display: str, timeout: float = 10) -> subprocess.CompletedProcess:
    env = dict(os.environ, DISPLAY=display)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        raise StudioError(f"{cmd[0]} timed out") from e
    except FileNotFoundError as e:
        raise StudioError(f"{cmd[0]} is not installed") from e


def find_dialog(display: str, run=_x) -> str:
    """X window id of the pane's save dialog (title starts blob:https://grok.com), or ''."""
    r = run(["wmctrl", "-l"], display)
    for line in r.stdout.splitlines():
        if DIALOG_TITLE in line:
            return line.split()[0]
    return ""


def landed(path: Path, wait: float, sleep=time.sleep) -> bool:
    """The file exists and its size held still for one poll (the browser finished writing it)."""
    t0, last = time.monotonic(), -1
    while time.monotonic() - t0 < wait:
        size = path.stat().st_size if path.exists() else -1
        if size > 0 and size == last:
            return True
        last = size
        sleep(1)
    return False


def save_dialog(name: str, display: str = os.environ.get("WORKMAN_DISPLAY", ":0"), wait: float = 20,
                folder: Path = DOWNLOADS, land_wait: float = 120, run=_x, sleep=time.sleep) -> dict:
    """Rename the file in the pane's open Save dialog to `name` and press Save (save-dialog.sh plus the
    rename). GTK opens a Save dialog with the Name field focused and its text selected, so ctrl+a then
    typing replaces the conversation-id default. Proof is the file itself: `folder/name` must appear
    and stop growing, else this fails closed (the dialog may have saved under another name)."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}\.(mp4|jpg|png)", name):
        raise ValueError(f"file name {name!r} must be plain [A-Za-z0-9._-] with .mp4/.jpg/.png")
    target = folder / name
    if target.exists():
        return {"ok": False, "error": "exists", "detail": f"{target} exists; pick another name"}
    wid, t0 = "", time.monotonic()
    while not wid and time.monotonic() - t0 < wait:
        wid = find_dialog(display, run)
        if not wid:
            sleep(1)
    if not wid:
        return {"ok": False, "error": "no_dialog", "detail": f"no '{DIALOG_TITLE}' window in {wait:.0f}s"}
    for cmd in (["xdotool", "windowactivate", "--sync", wid], ["xdotool", "key", "--clearmodifiers", "ctrl+a"],
                ["xdotool", "type", "--clearmodifiers", "--delay", "20", name], ["xdotool", "key", "Return"]):
        r = run(cmd, display)
        if r.returncode:
            return {"ok": False, "error": "xdotool", "detail": f"{' '.join(cmd[:2])} exit {r.returncode}"}
        sleep(0.4)
    sleep(3)
    if find_dialog(display, run) == wid:
        return {"ok": False, "window": wid, "error": "dialog_still_open"}
    if not landed(target, land_wait, sleep):
        return {"ok": False, "window": wid, "error": "not_saved",
                "detail": f"{target} did not appear in {land_wait:.0f}s; check {folder} for the default name"}
    return {"ok": True, "window": wid, "path": str(target), "bytes": target.stat().st_size}


def strip(clip: Path, dst: Path) -> Path:
    """Ten frames across the clip in one row (intake.sh contact strip)."""
    cool_down()
    ff(["-i", str(clip), "-vf", "fps=2/3,scale=216:384,tile=10x1", "-frames:v", "1", "-update", "1", str(dst)])
    return dst


def intake(src: str | Path, job: str | Path, name: str) -> dict:
    """Move a downloaded clip into <job>/clips/<name>.mp4 (never overwriting), probe it, strip it."""
    s = Path(src).expanduser()
    if not s.is_file():
        raise StudioError(f"missing {s}")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", name):
        raise ValueError(f"clip name {name!r} must be plain [A-Za-z0-9._-]")
    d = resolve_job(job) / "clips"
    d.mkdir(parents=True, exist_ok=True)
    dst = d / (name if name.endswith(".mp4") else name + ".mp4")
    if dst.exists():
        raise StudioError(f"{dst} exists; pick another name")
    shutil.move(str(s), str(dst))
    info = probe(dst)
    (d / "sheets").mkdir(exist_ok=True)
    sheet = strip(dst, d / "sheets" / (dst.stem + ".jpg"))
    return {"ok": True, "clip": str(dst), "sheet": str(sheet), "probe": info, "problems": clip_errors(info)}


def last_frame(clip: str | Path, dst: str | Path) -> str:
    """The clip's last frame as a still, to use as First frame of the next clip (chaining shots)."""
    cool_down()
    ff(["-sseof", "-0.05", "-i", str(clip), "-frames:v", "1", "-update", "1", str(dst)])
    return str(dst)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m workman.studio.grok", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    lk = sub.add_parser("locked")
    lk.add_argument("subject")
    lk.add_argument("change")
    lk.add_argument("--seconds", type=int, default=6)
    nr = sub.add_parser("narrate")
    nr.add_argument("line")
    nr.add_argument("--at", type=float, default=1.0)
    nr.add_argument("--seconds", type=int, default=6)
    nr.add_argument("--voice", default=BRAND_VOICE)
    sv = sub.add_parser("save")
    sv.add_argument("name")
    sv.add_argument("--display", default=os.environ.get("WORKMAN_DISPLAY", ":0"))
    it = sub.add_parser("intake")
    it.add_argument("src")
    it.add_argument("job")
    it.add_argument("name")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "locked":
            out = {"ok": True, "prompt": locked_shot(a.subject, a.change, a.seconds)}
        elif a.cmd == "narrate":
            out = {"ok": True, "prompt": narration(a.line, a.at, a.seconds, a.voice)}
        elif a.cmd == "save":
            out = save_dialog(a.name, a.display)
        else:
            out = intake(a.src, a.job, a.name)
    except (StudioError, ValueError, OSError) as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 1
    print(json.dumps(out, indent=1))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
