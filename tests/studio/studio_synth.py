"""A 3 s synthetic studio job built with ffmpeg only: no network, no ElevenLabs, no grok.

Two flat clips (1088x1920, the size grok delivers), a logo clip whose small box turns
from red to green at frame 3, a pink-noise bed, a sine chime, two VO takes (sine tones)
with canned character alignment, a black Ai-mark PNG for the dark card, and a tiny
FACTS.md. Type (fonts, sizes, colours, motion) comes from workman.studio.typesystem.
Used by the e2e test and by the release dry run:

    python tests/studio/studio_synth.py <dir>      prints the job folder it built
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

NAME = "synth-v1"
LINES = [("Look closer.", 0.7, 220), ("Every dollar counts.", 0.6, 180)]


def _ff(args: list[str]) -> None:
    subprocess.run(["nice", "-n", "19", "ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y", "-threads", "4",
                    *args], check=True, timeout=120)


def alignment(text: str, t0: float, t1: float) -> dict:
    """Characters spread evenly over t0..t1, the shape ElevenLabs with-timestamps returns."""
    n = len(text)
    step = (t1 - t0) / n
    return {"characters": list(text),
            "character_start_times_seconds": [round(t0 + k * step, 4) for k in range(n)],
            "character_end_times_seconds": [round(t0 + (k + 1) * step, 4) for k in range(n)]}


def transcript(words: list[tuple[float, float, str]]) -> dict:
    """A Scribe-shaped JSON for the canned speech-to-text."""
    return {"language_code": "eng", "text": " ".join(w for *_, w in words),
            "words": [{"start": a, "end": b, "type": "word", "text": w} for a, b, w in words]}


def spec(formats: list[str] | None = None, renderer: str = "drawtext") -> dict:
    return {"name": NAME, "fps": 24, "size": [1080, 1920], "end": 3.0, "renderer": renderer,
            "shots": [{"clip": "clips/a.mp4", "from": 0.0, "end_frame": 32, "horizon": 0.5},
                      {"clip": "clips/b.mp4", "from": 0.25, "end_frame": 60, "horizon": 0.55},
                      {"logo": "clips/logo.mp4", "end_frame": 66},
                      {"card": {"cta": "Free account audit", "url": "advisorppc.com",
                                "mark": "stills/logo.png", "facts": ["F12"]}, "end_frame": 72}],
            "vo": [{"shot": 0, "at": 0.1, "text": "Look closer.", "words": ["Look closer."], "opinion": True},
                   {"shot": 1, "at": 1.4, "text": "Every dollar counts.", "words": ["Every dollar counts."],
                    "facts": ["F01"]}],
            "bed": {"path": "audio/bed.mp3"}, "chime": {"path": "audio/chime.mp3"},
            **({"formats": formats} if formats else {})}


def build(root: Path, formats: list[str] | None = None, renderer: str = "drawtext") -> Path:
    job = root / "20260925-synth"
    for sub in ("clips", "stills", "audio", "ident", "out", "review", "refs"):
        (job / sub).mkdir(parents=True, exist_ok=True)
    _ff(["-f", "lavfi", "-i", "color=c=0x101010:s=1088x1920:r=24:d=1.5", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         str(job / "clips/a.mp4")])
    _ff(["-f", "lavfi", "-i", "color=c=0x505050:s=1088x1920:r=24:d=1.5", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         str(job / "clips/b.mp4")])
    box = "x=840:y=484:w=240:h=120:t=fill"
    _ff(["-f", "lavfi", "-i", "color=c=black:s=1920x1088:r=24:d=1",
         "-vf", f"drawbox={box}:color=red:enable='lt(n,3)',drawbox={box}:color=0x00C040:enable='gte(n,3)'",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(job / "clips/logo.mp4")])
    _ff(["-f", "lavfi", "-i", "anoisesrc=d=4:c=pink:a=0.2,lowpass=f=400", "-ac", "2", "-ar", "44100",
         str(job / "audio/bed.mp3")])
    _ff(["-f", "lavfi", "-i", "sine=f=1320:d=0.6,afade=t=out:st=0.05:d=0.55", "-ar", "44100",
         str(job / "audio/chime.mp3")])
    for n, (text, dur, hz) in enumerate(LINES, 1):
        _ff(["-f", "lavfi", "-i", f"sine=f={hz}:d={dur}", "-ar", "44100", str(job / f"audio/{NAME}_vo{n}_0.mp3")])
        (job / f"audio/{NAME}_vo{n}_0.json").write_text(json.dumps(
            {"text": text, "voice": "synthetic", "alignment": alignment(text, 0.05, dur - 0.05)}))
    from PIL import Image
    Image.new("RGBA", (64, 64), (245, 244, 239, 255)).save(job / "stills/logo.png")
    (job / "refs/FACTS.md").write_text("# facts\n\n| ID | Fact | Source |\n|---|---|---|\n"
                                       "| F01 | A fact. | S1 |\n| F12 | Free audit. | S2 |\n\n"
                                       "## Conflicts between pages\n- nothing cited here\n")
    (job / "SCRIPT.md").write_text("# synth\n\n| 1 | 0.0 | a | Look closer. | (opinion) |\n"
                                   "| 2 | 1.0 | b | Every dollar counts. | F01 |\n")
    (job / "spec.json").write_text(json.dumps(spec(formats, renderer), indent=1))
    return job


def fonts_present() -> bool:
    """The type system's pinned instances exist or can be made (Fraunces, Geist via ~/bin/font-instance)."""
    from workman.studio import typesystem
    try:
        typesystem.headline_font(118), typesystem.headline_font(132)
        typesystem.sans_font("cta"), typesystem.sans_font("url")
    except Exception:   # noqa: BLE001 - any missing piece means the fonts are not usable here
        return False
    return True


def canned_transcript() -> dict:
    """What Scribe would hear: each line's first word at its on-screen onset (0.1 s and 1.4 s)."""
    return transcript([(0.1, 0.35, "Look"), (0.35, 0.6, "closer."), (1.4, 1.55, "Every"), (1.55, 1.7, "dollar"),
                       (1.7, 1.9, "counts.")])


if __name__ == "__main__":
    args = sys.argv[2:]
    rend = "atmosmotion" if "--atmosmotion" in args else "drawtext"
    print(build(Path(sys.argv[1]), [a for a in args if a != "--atmosmotion"] or None, rend))
