"""Voice and sound: ElevenLabs VO with timestamps, onsets by word, bed and SFX, loudness.

ElevenLabs is the only network API in the studio. The key is read from the
environment or ~/ENV/elevenlabs.local.env, never printed, logged or put in an error.

Onsets come from ElevenLabs character alignment, never by ear (CREATIVE-v3.md).
A line that overruns its shot gets a shorter written pause, never faster audio
and never a moved cut: `vo_texts` gives the takes in order of preference and
`take_fit` + `choose` pick one (build_series.py).

    python -m workman.studio.voice tts "<text>" out.mp3 [--previous T] [--next T]
    python -m workman.studio.voice sound "<prompt>" out.mp3 --seconds 20
    python -m workman.studio.voice loudness <file>
    python -m workman.studio.voice onset <take.json> "<phrase>"
"""
from __future__ import annotations

import argparse
import array
import base64
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .job import StudioError, duration, ff

ELEVEN_ENV = Path("~/ENV/elevenlabs.local.env").expanduser()
API = "https://api.elevenlabs.io"
BRIAN = "nPczCjzI2devNBz1zQrb"          # the male brand VO voice (Brian)
MODEL = "eleven_multilingual_v2"
SETTINGS = {"stability": 0.55, "similarity_boost": 0.75, "style": 0.15, "use_speaker_boost": True}
BED_PROMPT = ("one continuous low warm ambient drone, soft cinematic underscore, very slow swell, no drums, "
              "no melody, no percussion, seamless, calm, deep")
CHIME_PROMPT = ("a single soft clean glass chime, one gentle bell tone with a short natural decay, "
                "quiet, no music, no melody, no other sounds")
BR = '<break time="{}s" />'
PAUSES = (0.6, 0.3)                    # written breath at each full stop, longest first; then none
HOLD, READ_MIN = 0.25, 1.5             # words leave 0.25 s before the cut and should read for 1.5 s
VO_FLOOR, VO_TAIL = -60.0, 0.10        # a take ends at its last window above -60 dBFS, plus 0.1 s
MAX_TEXT = 600                         # characters per TTS call (ElevenLabs stitching window)
SOUND_SECONDS = (0.5, 22.0)            # sound-generation duration bounds
# Copy rules: no em dashes, no model names (tools/adbuild.py BANNED).
BANNED = re.compile(r"\u2014|\b(Opus|Sonnet|Haiku|Fable|GPT|Grok|Gemini|Llama|Qwen)\b")


# ------------------------------------------------------------------ pure: text and alignment

def spoken(text: str) -> str:
    """The words a listener hears: break tags removed, whitespace collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"<break[^>]*/>", " ", text)).strip()


def masked(al: dict) -> str:
    """Alignment text with break tags blanked out, same length so indices still map to times."""
    return re.sub(r"<break[^>]*/>", lambda m: " " * len(m.group(0)), "".join(al["characters"]))


def word_onset(al: dict, word: str) -> float:
    """Start time of the first character of the first whole-word `word` in an alignment."""
    s = masked(al)
    m = re.search(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])", s)
    if not m:
        raise ValueError(f"word {word!r} not in alignment text {''.join(al['characters'])!r}")
    return al["character_start_times_seconds"][m.start()]


def phrase_at(text: str, phrase: str) -> int:
    """Index where `phrase` starts in `text` as whole words (punctuation between words ignored), or -1."""
    toks = re.findall(r"[A-Za-z0-9]+", phrase)
    if not toks:
        return -1
    m = re.search(r"(?<![A-Za-z0-9])" + r"[^A-Za-z0-9]+".join(map(re.escape, toks)) + r"(?![A-Za-z0-9])", text)
    return m.start() if m else -1


def phrase_onset(al: dict, phrase: str) -> float:
    """Spoken start of `phrase` in a take, break tags masked so they can never match."""
    k = phrase_at(masked(al), phrase)
    if k < 0:
        raise ValueError(f"{phrase!r} is not spoken in {''.join(al['characters'])!r}")
    return al["character_start_times_seconds"][k]


def speech_span(al: dict) -> tuple[float, float]:
    """(first letter start, last letter end). Punctuation is skipped because the final
    full stop's end time absorbs the trailing silence."""
    idx = [k for k, c in enumerate(masked(al)) if c.isalnum()]
    if not idx:
        raise ValueError("alignment has no spoken characters")
    return al["character_start_times_seconds"][idx[0]], al["character_end_times_seconds"][idx[-1]]


def placement(at: float, al: dict) -> float:
    """Where the take starts on the timeline so its first spoken character lands at `at`."""
    return max(0.0, at - speech_span(al)[0])


def overruns(place: float, dur: float, end: float) -> bool:
    """True when the placed audio (length `dur`) does not finish by the cut at `end`."""
    return place + dur > end + 1e-6


def pauses(al: dict) -> list[float]:
    """Silence between the last letter before each full stop and the next spoken letter, in seconds."""
    s = masked(al)
    st, en = al["character_start_times_seconds"], al["character_end_times_seconds"]
    out = []
    for m in re.finditer(r"([A-Za-z0-9])\.", s):
        nxt = re.search(r"[A-Za-z0-9]", s[m.end():])
        if nxt:
            out.append(round(st[m.end() + nxt.start()] - en[m.start()], 3))
    return out


def wps(text: str, al: dict) -> float:
    """Words per second over the whole spoken line, pauses included."""
    a, b = speech_span(al)
    return len(spoken(text).split()) / (b - a)


def articulation_wps(text: str, al: dict) -> float:
    """Words per second with the full-stop pauses taken out."""
    a, b = speech_span(al)
    return len(spoken(text).split()) / (b - a - sum(pauses(al)))


def vo_texts(vo: str, breaths: tuple[float, ...] = PAUSES) -> list[str]:
    """Written takes in order of preference: a breath at each full stop, a shorter one, none."""
    parts = re.split(r"(?<=\.) (?=[A-Z])", vo)
    return [f" {BR.format(p)} ".join(parts) for p in breaths] + [vo] if len(parts) > 1 else [vo]


def take_fit(al: dict, dur: float, at: float, cut: float, phrase: str, hold: float = HOLD) -> tuple[bool, float]:
    """(take of length `dur` ends by the cut, seconds its phrase stays on screen) with speech placed at `at`."""
    place = placement(at, al)
    return not overruns(place, dur, cut), round(cut - hold - place - phrase_onset(al, phrase), 3)


def choose(fits: list[tuple[bool, float]], read_min: float = READ_MIN) -> int | None:
    """First take that fits and reads for read_min, else the fitting take that reads longest, else None."""
    for k, (ok, read) in enumerate(fits):
        if ok and read >= read_min:
            return k
    ok = [k for k, (f, _) in enumerate(fits) if f]
    return max(ok, key=lambda k: fits[k][1]) if ok else None


def copy_problem(text: str, extra: re.Pattern | None = None) -> str:
    """The first copy-rule violation (em dash, model name, or `extra`) in `text`, or ''."""
    m = BANNED.search(text) or (extra.search(text) if extra else None)
    return m.group(0) if m else ""


# ------------------------------------------------------------------ pure: samples

def audible_end(samples, rate: int, floor_db: float = VO_FLOOR, win: float = 0.01) -> float:
    """End of the last `win` s window whose RMS is above floor_db (s16 full scale): where a take stops being heard."""
    n = max(1, int(rate * win))
    lim = (10 ** (floor_db / 20) * 32768) ** 2 * n
    for k in range(len(samples) // n - 1, -1, -1):
        if sum(v * v for v in samples[k * n:(k + 1) * n]) > lim:
            return (k + 1) * n / rate
    raise ValueError("take is silent")


def attack_time(samples, rate: int, frac: float = 0.1) -> float:
    """Seconds to the first sample reaching `frac` of the peak: where a chime is heard to start."""
    peak = max((abs(s) for s in samples), default=0)
    if not peak:
        raise ValueError("silent audio")
    return next(k for k, s in enumerate(samples) if abs(s) >= frac * peak) / rate


def loudness_errors(i: float, tp: float, target: float = -14.0, tol: float = 0.5, tp_max: float = -1.0) -> list[str]:
    """-14 LUFS integrated within 0.5 LU, true peak at or under -1.0 dBTP."""
    return (([f"integrated {i} LUFS"] if not abs(i - target) <= tol else [])
            + ([f"true peak {tp} dBTP"] if not tp <= tp_max else []))


def parse_ebur128(stderr: str) -> tuple[float, float]:
    """(integrated LUFS, true peak dBTP) from ffmpeg ebur128=peak=true output."""
    summ = stderr[stderr.rfind("Summary:"):]
    m = re.search(r"I:\s+(-?[\d.]+|-inf) LUFS", summ)
    if not m:
        raise StudioError("ebur128 summary has no integrated loudness")
    tp = re.search(r"Peak:\s+(-?[\d.]+|-inf) dBFS", summ)
    return float(m.group(1)), float(tp.group(1)) if tp else float("nan")


LIMIT_DB = -2.5    # AAC adds a few tenths of inter-sample peak; limit under the -1.5 dBTP target (build_partner)


def master_chain(gain_db: float, end: float, limit_db: float = LIMIT_DB) -> str:
    """Static gain, then a 4x-oversampled lookahead limiter (sample peak at 192 kHz tracks true peak),
    back to 48 kHz and trimmed to the spot length (build_partner.master_chain)."""
    return (f"volume={gain_db:.2f}dB,aresample=192000,alimiter=limit={10 ** (limit_db / 20):.4f}:level=false:"
            f"latency=true:attack=5:release=80,aresample=48000,atrim=0:{end}")


def solve_gain(measure, target: float = -14.0, tries: int = 4) -> tuple[float, list]:
    """Gain that brings the limited premix to `target` LUFS within 0.1 LU, in at most `tries` measured
    passes (build_partner.master_gain). measure(gain_or_None) -> (integrated, true_peak)."""
    i0, _ = measure(None)
    gain, passes = target - i0, []
    for _ in range(max(1, min(int(tries), 6))):
        i, tp = measure(gain)
        passes.append([round(gain, 2), i, tp])
        if abs(i - target) < 0.1:
            return round(gain, 2), passes
        gain += target - i
    raise StudioError(f"master gain did not converge on {target} LUFS: {passes}")


# ------------------------------------------------------------------ io: loudness

def ebur128(args: list[str], af: str = "") -> tuple[float, float]:
    """(integrated LUFS, true peak dBTP) of the input described by ffmpeg args."""
    return parse_ebur128(ff(args + ["-af", (af + "," if af else "") + "ebur128=peak=true", "-f", "null", "-"]))


def master_gain(premix: str | Path, end: float, target: float = -14.0) -> tuple[float, list]:
    """solve_gain measured with ebur128 on the premix through master_chain."""
    return solve_gain(lambda g: ebur128(["-i", str(premix)], master_chain(g, end) if g is not None else ""), target)


def max_volume(path: str | Path, start: float | None = None, dur: float | None = None) -> float:
    """Sample peak in dBFS (volumedetect), optionally of a window."""
    win = (["-ss", f"{start:.3f}"] if start is not None else []) + (["-t", f"{dur}"] if dur is not None else [])
    err = ff(win + ["-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"])
    m = re.search(r"max_volume: (-?[\d.]+) dB", err)
    if not m:
        raise StudioError(f"volumedetect found no audio in {path}")
    return float(m.group(1))


def pcm(path: str | Path, rate: int = 48000) -> array.array:
    """Mono s16 samples of an audio file."""
    try:
        r = subprocess.run(["nice", "-n", "19", "ffmpeg", "-nostdin", "-v", "error", "-threads", "4", "-i",
                            str(path), "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
                           capture_output=True, timeout=120)
    except subprocess.TimeoutExpired as e:
        raise StudioError(f"decode of {path} timed out") from e
    if r.returncode:
        raise StudioError(f"ffmpeg decode of {path} exit {r.returncode}: {r.stderr[-400:]!r}")
    a = array.array("h")
    a.frombytes(r.stdout[: len(r.stdout) // 2 * 2])
    return a


# ------------------------------------------------------------------ io: ElevenLabs

def eleven_key(env_path: Path = ELEVEN_ENV) -> str:
    """ELEVENLABS_API_KEY from the environment or the ENV file. The value never leaves this function
    except into the request header."""
    k = os.environ.get("ELEVENLABS_API_KEY", "")
    if not k and env_path.exists():
        m = re.search(r"^(?:export\s+)?ELEVENLABS_API_KEY=(\S+)", env_path.read_text(), re.M)
        k = m.group(1).strip("'\"") if m else ""
    if not k:
        raise StudioError(f"ELEVENLABS_API_KEY not found (environment or {env_path})")
    return k


def _request(path: str, data: bytes | None, ctype: str) -> urllib.request.Request:
    return urllib.request.Request(API + path, method="POST" if data is not None else "GET", data=data,
                                  headers={"xi-api-key": eleven_key(), "content-type": ctype})


def eleven(path: str, body: dict | None = None, raw: bool = False, timeout: float = 180, tries: int = 3,
           opener=urllib.request.urlopen, sleep=time.sleep, data: bytes | None = None,
           ctype: str = "application/json"):
    """ElevenLabs call with a timeout and bounded retry (5 s, 10 s backoff) on 429/5xx/network errors.
    Errors name the endpoint and status, never the key."""
    tries = max(1, min(int(tries), 3))
    payload = data if data is not None else (json.dumps(body).encode() if body is not None else None)
    endpoint = path.split("?")[0]
    for attempt in range(tries):
        req = _request(path, payload, ctype)
        try:
            with opener(req, timeout=timeout) as r:
                out = r.read()
            return out if raw else json.loads(out)
        except urllib.error.HTTPError as e:
            detail = e.read()[:400].decode(errors="replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                sleep(5 * 2 ** attempt)
                continue
            raise StudioError(f"ElevenLabs {endpoint} HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
            if attempt < tries - 1:
                sleep(5 * 2 ** attempt)
                continue
            raise StudioError(f"ElevenLabs {endpoint} unreachable: {type(e).__name__}") from None
    raise StudioError(f"ElevenLabs {endpoint}: no attempt made")


def tts_body(text: str, previous_text: str = "", next_text: str = "", model: str = MODEL,
             settings: dict | None = None) -> dict:
    """The with-timestamps request body; neighbouring lines ride along so separate takes read as one."""
    if not text.strip() or len(text) > MAX_TEXT:
        raise ValueError(f"TTS text must be 1..{MAX_TEXT} characters, got {len(text)}")
    bad = copy_problem(spoken(text))
    if bad:
        raise ValueError(f"VO breaks the copy rules ({bad!r}): {text!r}")
    body = {"text": text, "model_id": model, "voice_settings": dict(settings or SETTINGS)}
    if previous_text:
        body["previous_text"] = previous_text[-MAX_TEXT:]
    if next_text:
        body["next_text"] = next_text[:MAX_TEXT]
    return body


def tts(text: str, mp3: str | Path, meta_path: str | Path, voice: str = BRIAN, previous_text: str = "",
        next_text: str = "", model: str = MODEL, settings: dict | None = None, call=eleven) -> dict:
    """One VO take with character alignment: writes the mp3 and a JSON of text, voice and alignment."""
    body = tts_body(text, previous_text, next_text, model, settings)
    r = call(f"/v1/text-to-speech/{voice}/with-timestamps?output_format=mp3_44100_128", body)
    if not isinstance(r, dict) or not r.get("audio_base64") or not r.get("alignment"):
        raise StudioError("ElevenLabs with-timestamps returned no audio or alignment")
    Path(mp3).write_bytes(base64.b64decode(r["audio_base64"]))
    meta = {"text": text, "model": model, "voice": voice, "settings": body["voice_settings"],
            "alignment": r["alignment"], "normalized_alignment": r.get("normalized_alignment")}
    Path(meta_path).write_text(json.dumps(meta))
    return meta


def sound(path: str | Path, prompt: str, seconds: float, influence: float = 0.5, call=eleven) -> dict:
    """One sound-generation take (bed or SFX); an existing file is kept, never regenerated."""
    p = Path(path)
    if p.exists():
        return {"path": str(p), "generated": False}
    lo, hi = SOUND_SECONDS
    if not lo <= seconds <= hi:
        raise ValueError(f"sound seconds must be {lo}..{hi}, got {seconds}")
    if not prompt.strip() or len(prompt) > 450:
        raise ValueError("sound prompt must be 1..450 characters")
    audio = call("/v1/sound-generation", {"text": prompt, "duration_seconds": seconds,
                                          "prompt_influence": influence}, raw=True, timeout=300)
    p.write_bytes(audio)
    return {"path": str(p), "generated": True, "seconds": round(duration(p), 3)}


def multipart(fields: dict, file_field: str, filename: str, content: bytes, ctype: str) -> tuple[bytes, str]:
    """A multipart/form-data body (stdlib has no encoder); returns (body, content-type)."""
    b = uuid.uuid4().hex
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items()]
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                 f"Content-Type: {ctype}\r\n\r\n".encode() + content + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={b}"


def stt(audio: str | Path, call=eleven) -> dict:
    """ElevenLabs Scribe word timings of an audio file (the video-inspect transcript request)."""
    data, ctype = multipart({"model_id": "scribe_v1", "timestamps_granularity": "word", "tag_audio_events": "true"},
                            "file", Path(audio).name, Path(audio).read_bytes(), "audio/mpeg")
    return call("/v1/speech-to-text", data=data, ctype=ctype, timeout=300)


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m workman.studio.voice", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tts")
    t.add_argument("text")
    t.add_argument("out")
    t.add_argument("--voice", default=BRIAN)
    t.add_argument("--previous", default="")
    t.add_argument("--next", default="")
    s = sub.add_parser("sound")
    s.add_argument("prompt")
    s.add_argument("out")
    s.add_argument("--seconds", type=float, required=True)
    s.add_argument("--influence", type=float, default=0.5)
    lo = sub.add_parser("loudness")
    lo.add_argument("path")
    on = sub.add_parser("onset")
    on.add_argument("take")
    on.add_argument("phrase")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "tts":
            meta = tts(a.text, a.out, str(Path(a.out).with_suffix(".json")), a.voice, a.previous, a.next)
            out = {"ok": True, "mp3": a.out, "spoken": spoken(meta["text"]), "seconds": round(duration(a.out), 3)}
        elif a.cmd == "sound":
            out = {"ok": True, **sound(a.out, a.prompt, a.seconds, a.influence)}
        elif a.cmd == "loudness":
            i, tp = ebur128(["-i", a.path, "-vn"])
            out = {"ok": True, "integrated_lufs": i, "true_peak_dbtp": tp, "errors": loudness_errors(i, tp)}
        else:
            al = json.loads(Path(a.take).read_text())["alignment"]
            out = {"ok": True, "phrase": a.phrase, "onset": phrase_onset(al, a.phrase), "span": speech_span(al)}
    except (StudioError, ValueError, OSError, KeyError) as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 1
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
