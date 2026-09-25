"""Break a video open so an agent can SEE and READ it (~/bin/video-inspect as a function).

Writes into <outdir> (default <file>.inspect/):
    probe.json       streams, codecs, fps, duration (ffprobe)
    frames.jpg       contact sheet, 4 frames per second, time-stamped
    cuts.txt         hard cuts found by scene detection (time, score)
    loudness.txt     loudness every 0.5 s (momentary LUFS) + integrated / true peak
    audio.png        waveform (top) and spectrogram (bottom) of the soundtrack
    transcript.txt   ElevenLabs Scribe word timings: what is actually said and when

Heat-gated before every ffmpeg pass, nice 19, 4 threads. ElevenLabs is the only API.

    python -m workman.studio.inspect <file.mp4> [outdir] [--no-transcript]
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

from .job import StudioError, cool_down, ff, probe

FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
COLS, SHEET_FPS = 8, 4
MAX_SECONDS = 20 * 60        # a contact sheet of a longer file is unreadable and slow; refuse


# ------------------------------------------------------------------ pure

def sheet_grid(dur: float, fps: int = SHEET_FPS, cols: int = COLS) -> tuple[int, int]:
    """(cols, rows) of the contact sheet: fps frames per second, `cols` wide."""
    n = max(1, math.ceil(dur * fps))
    return cols, (n + cols - 1) // cols


def sheet_filter(dur: float, font: Path = FONT) -> str:
    cols, rows = sheet_grid(dur)
    stamp = (f"drawtext=fontfile={font}:" if font.exists() else "drawtext=") + \
        "text='%{pts\\:hms}':x=6:y=6:fontsize=16:fontcolor=yellow:box=1:boxcolor=black@0.6,"
    return f"fps={SHEET_FPS},scale=240:-2,{stamp}tile={cols}x{rows}:padding=2"


def parse_scenes(metadata: str) -> list[tuple[float, float]]:
    """(time, score) pairs from ffmpeg metadata=print output of select='gt(scene,0.3)'."""
    out, t = [], None
    for line in metadata.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            t = float(m.group(1))
        m = re.search(r"lavfi\.scene_score=([\d.]+)", line)
        if m and t is not None:
            out.append((t, float(m.group(1))))
    return out


def format_cuts(cuts: list[tuple[float, float]]) -> str:
    return "".join(f"{t:.3f} s  score {s:.2f}\n" for t, s in cuts)


def parse_loudness(stderr: str, step: float = 0.5) -> str:
    """Momentary loudness every `step` s plus the integrated and peak summary lines (video-inspect awk)."""
    lines, last = [], None
    for ln in stderr.splitlines():
        m = re.search(r"\bt:\s*([\d.]+).*?\bM:\s*(-?[\d.]+|-inf)", ln)
        if m:
            t = float(m.group(1))
            if last is None or t - last >= step:
                lines.append(f"{t:6.2f} s  M {m.group(2):>6} LUFS")
                last = t
    summ = stderr[stderr.rfind("Integrated loudness"):] if "Integrated loudness" in stderr else ""
    lines += [ln for ln in summ.splitlines() if re.search(r"\bI:|Peak:", ln)]
    return "\n".join(lines) + "\n"


def format_transcript(d: dict) -> str:
    """Scribe JSON as 'language | text' then one 'start-end type word' line per word."""
    out = [f"language: {d.get('language_code')} | text: {(d.get('text') or '').strip()}"]
    for w in d.get("words", []):
        if w.get("type") == "spacing":
            continue
        kind = str(w.get("type", ""))[:5]
        out.append(f"{w.get('start', 0):6.2f}-{w.get('end', 0):6.2f}  {kind:5}  {w.get('text', '')}")
    return "\n".join(out) + "\n"


def transcript_words(d: dict) -> list[tuple[float, float, str]]:
    """(start, end, word) for spoken words only."""
    return [(float(w.get("start", 0)), float(w.get("end", 0)), str(w.get("text", "")))
            for w in d.get("words", []) if w.get("type") == "word"]


# ------------------------------------------------------------------ io

def inspect(path: str | Path, outdir: str | Path | None = None, transcribe: bool = True, stt=None) -> dict:
    """Write the six inspection files for one video and return their paths plus the transcript text."""
    src = Path(path).expanduser().resolve()
    if not src.is_file():
        raise StudioError(f"no file at {src}")
    out = Path(outdir).expanduser() if outdir else src.with_suffix(".inspect")
    out.mkdir(parents=True, exist_ok=True)
    info = probe(src)
    (out / "probe.json").write_text(json.dumps(info, indent=1))
    dur = float(info.get("format", {}).get("duration", 0) or 0)
    if not 0 < dur <= MAX_SECONDS:
        raise StudioError(f"{src.name} lasts {dur:.1f}s; inspect takes 0..{MAX_SECONDS}s")
    files = {"probe": str(out / "probe.json")}

    cool_down()
    ff(["-i", str(src), "-vf", sheet_filter(dur), "-frames:v", "1", "-update", "1", str(out / "frames.jpg")])
    files["frames"] = str(out / "frames.jpg")

    cool_down()
    meta = out / ".scene.txt"
    try:
        ff(["-i", str(src), "-vf", f"select='gt(scene,0.3)',metadata=print:file={meta}", "-an", "-f", "null", "-"])
        cuts = parse_scenes(meta.read_text()) if meta.exists() else []
    finally:
        meta.unlink(missing_ok=True)
    (out / "cuts.txt").write_text(format_cuts(cuts))
    files["cuts"] = str(out / "cuts.txt")

    transcript = ""
    if any(s.get("codec_type") == "audio" for s in info.get("streams", [])):
        cool_down()
        # ff() logs at info level, which is where ebur128 prints its per-frame lines
        err = ff(["-i", str(src), "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"])
        (out / "loudness.txt").write_text(parse_loudness(err))
        cool_down()
        ff(["-i", str(src), "-filter_complex",
            "[0:a]aformat=channel_layouts=mono,asplit[a][b];[a]showwavespic=s=1600x240:colors=0x10B981[w];"
            "[b]showspectrumpic=s=1600x400:legend=0:scale=log[s];[w][s]vstack", "-frames:v", "1", "-update", "1",
            str(out / "audio.png")])
        files["audio"] = str(out / "audio.png")
        if transcribe:
            from . import voice
            mp3 = out / ".stt.mp3"
            try:
                ff(["-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(mp3)])
                d = (stt or voice.stt)(mp3)
                transcript = format_transcript(d)
            except StudioError as e:
                transcript = f"speech-to-text failed: {e}\n"
            finally:
                mp3.unlink(missing_ok=True)
            (out / "transcript.txt").write_text(transcript)
            files["transcript"] = str(out / "transcript.txt")
    else:
        (out / "loudness.txt").write_text("no audio stream\n")
    files["loudness"] = str(out / "loudness.txt")
    return {"ok": True, "outdir": str(out), "files": files, "duration": dur, "cuts": cuts, "transcript": transcript}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m workman.studio.inspect", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("outdir", nargs="?")
    ap.add_argument("--no-transcript", action="store_true")
    a = ap.parse_args(argv)
    try:
        r = inspect(a.path, a.outdir, transcribe=not a.no_transcript)
    except StudioError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    print(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
