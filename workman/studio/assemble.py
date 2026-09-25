"""Spec-driven ad assembly: shots on the frame grid, words on their spoken onsets, one bed,
a chime on the logo's detected green frame, the dark end card, then the same checks as
build_v3 / build_partner / build_series.

Every font, size, colour, placement and timing of type comes from typesystem.TYPE (the
mirror of TYPE-SYSTEM.md); a spec cannot set them. The spec is <job>/spec.json, paths
relative to the job folder:

    {"name": "series-shield-v1", "fps": 24, "size": [1080, 1920], "end": 16.5,
     "renderer": "drawtext",                         (or "atmosmotion")
     "formats": ["1:1", "16:9"],                     (optional extra aspects, same takes)
     "shots": [{"clip": "clips/G_dune.mp4", "from": 1.0, "end_frame": 120, "horizon": 0.45},
               {"clip": "clips/C_amber.mp4", "from": 1.0, "end_frame": 240, "horizon": 0.50},
               {"logo": "clips/logo-led.mp4", "end_frame": 324},
               {"card": {"mark": "stills/ai-mark.png", "cta": "Shield click protection. Free tier.",
                         "url": "advisorppc.com", "facts": ["F58"]}, "end_frame": 396}],
     "vo": [{"shot": 0, "at": 0.8, "text": "Not every click is a customer.",
             "words": ["Not every click is a customer."], "opinion": true},
            {"shot": 1, "at": 5.4, "text": "Shield scores every paid click.",
             "words": ["Shield scores every paid click."], "facts": ["F58"]}],
     "bed": {"path": "audio/bed.mp3"}, "chime": {"path": "audio/chime.mp3"}}

Renderers: "drawtext" draws the words with ffmpeg; "atmosmotion" renders them with the
Atmosmotion engine (engine-native) to a white-on-black plate and alphamerges it over the
clips, and renders the end card as an engine segment.

Stages: `assets` (VO takes, bed, chime from ElevenLabs; the only network step), `render`
(reads saved takes only, heat-gated, one render at a time, never overwrites a render),
`verify` (probe, strip, onset bands, green frame, loudness, copy). A line that overruns
its shot gets a shorter written pause, never faster audio and never a moved cut.

    python -m workman.studio.assemble <job> plan|assets|render|verify|all [--spec spec.json] [--format 1:1]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import typesystem as ts
from . import voice
from .job import StudioError, cool_down, duration, ff, ffmpeg_cmd, probe, render_lock, resolve_job, sh, wait_turn

T = ts.TYPE
DEFAULTS = {"fps": 24, "size": [1080, 1920], "renderer": "drawtext", "hold": voice.HOLD, "read_min": voice.READ_MIN,
            "bed_lufs": -26.0, "chime_peak": -12.0, "onset_ok": [3.0, 10.0], "green_ok": [20, 200]}
TYPE_KEYS = {"fonts", "word_size", "word_gap", "split_w", "fade", "colors", "font"}   # refused in a spec
RENDERERS = ("drawtext", "atmosmotion")
MAX_SHOTS, MAX_LINES, MAX_END = 12, 8, 120.0
FORMATS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}
FPS_OK = (24, 25, 30)
ENGINE = "~/Documents/Codex/2026-09-23/continue-the-owner-s-motion-reference/work/atmosmotion-branding/packages"
ATMOS_SCRIPT = Path(__file__).with_name("atmos_render.mjs")
ATMOS_TIMEOUT = 20 * 60


# ------------------------------------------------------------------ pure: spec and geometry

def with_defaults(spec: dict) -> dict:
    return {**DEFAULTS, **spec}


def kind(shot: dict) -> str:
    return "logo" if "logo" in shot else "card" if "card" in shot else "clip"


def cuts(spec: dict) -> list[tuple[int, int]]:
    """(start frame, end frame) per shot on the fps grid."""
    out, prev = [], 0
    for sh_ in spec["shots"]:
        out.append((prev, int(sh_["end_frame"])))
        prev = int(sh_["end_frame"])
    return out


def even(x: float) -> int:
    return max(2, int(math.ceil(x / 2)) * 2)


def cover(iw: int, ih: int, W: int, H: int) -> dict:
    """Scale-to-cover then centre crop. A clip that already covers at scale 1 (grok's 1088x1920 into
    1080x1920) is only cropped, exactly as the builders did (crop=1080:1920:4:0)."""
    k = max(W / iw, H / ih)
    sw, sh_ = (iw, ih) if k <= 1 else (even(iw * k), even(ih * k))
    cx, cy = (sw - W) // 2, (sh_ - H) // 2
    vf = (f"scale={sw}:{sh_}," if (sw, sh_) != (iw, ih) else "") + f"crop={W}:{H}:{cx}:{cy}"
    return {"sw": sw, "sh": sh_, "cx": cx, "cy": cy, "vf": vf}


def fit(iw: int, ih: int, W: int, H: int) -> dict:
    """Scale-to-fit then pad on black, centred (the logo shot); 1920x1088 into 1080x1920 gives 1080x612."""
    k = min(W / iw, H / ih)
    lw, lh = min(W, even(iw * k)), min(H, even(ih * k))
    return {"w": lw, "h": lh, "x": (W - lw) // 2, "y": (H - lh) // 2,
            "vf": f"scale={lw}:{lh},pad={W}:{H}:{(W - lw) // 2}:{(H - lh) // 2}:black"}


def mapped_horizon(hz: float, geo: dict, H: int) -> float:
    """A source horizon fraction after scale-to-cover and crop, as a fraction of the output height."""
    return round((hz * geo["sh"] - geo["cy"]) / H, 4)


def derive(spec: dict, fmt: str) -> dict:
    """The same ad in another aspect: new size and name suffix, the base ad's VO takes reused
    (takes_name) so no new ElevenLabs characters are spent. Type scales by typesystem.scale."""
    if fmt not in FORMATS:
        raise ValueError(f"format {fmt!r} not in {sorted(FORMATS)}")
    s = with_defaults(spec)
    out = {**spec, "size": list(FORMATS[fmt]), "name": f"{s['name']}-{fmt.replace(':', 'x')}",
           "takes_name": s.get("takes_name", s["name"])}
    out.pop("formats", None)
    return out


def metrics(size: list[int]) -> dict:
    """The type system's px values at this frame size (short side 1080 = 1.0)."""
    k = ts.scale(size)
    return {"k": k, "gap": int(round(T["baseline_gap_px"] * k)), "split": int(round(T["split_px"] * k)),
            "pitch": int(round(T["line_pitch_px"] * k)), "rise": T["word_rise_px"] * k,
            "cta": int(round(T["cta_px"] * k)), "url": int(round(T["url_px"] * k))}


def validate(spec: dict) -> list[str]:
    """Everything that would make a render wrong, before any file is touched."""
    s, errs = with_defaults(spec), []
    if TYPE_KEYS & set(spec):
        errs.append(f"{sorted(TYPE_KEYS & set(spec))}: type comes from typesystem.py (TYPE-SYSTEM.md), not the spec")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,80}", str(s.get("name", ""))):
        errs.append("name must be a-z, 0-9, dot, dash, underscore")
    if s["fps"] not in FPS_OK:
        errs.append(f"fps {s['fps']} not in {FPS_OK}")
    if s["renderer"] not in RENDERERS:
        errs.append(f"renderer {s['renderer']!r} not in {RENDERERS}")
    w, h = (list(s["size"]) + [0, 0])[:2]
    if not (isinstance(w, int) and isinstance(h, int) and 0 < w <= 4096 and 0 < h <= 4096 and w % 2 == 0 == h % 2):
        errs.append(f"size {s['size']} must be two even ints up to 4096")
    shots = s.get("shots") or []
    if not 0 < len(shots) <= MAX_SHOTS:
        errs.append(f"1..{MAX_SHOTS} shots")
        return errs
    ends = [sh_.get("end_frame") for sh_ in shots]
    if not all(isinstance(e, int) for e in ends) or ends != sorted(set(ends)) or ends[0] <= 0:
        errs.append(f"end_frame must be strictly increasing positive ints, got {ends}")
        return errs
    end = s.get("end")
    if not isinstance(end, (int, float)) or not 0 < end <= MAX_END:
        errs.append(f"end must be 0..{MAX_END} s")
    elif abs(ends[-1] - end * s["fps"]) > 1e-6:
        errs.append(f"last cut frame {ends[-1]} is not end {end} s x {s['fps']} fps")
    for i, sh_ in enumerate(shots):
        k = kind(sh_)
        if k == "clip" and not (0.0 < float(sh_.get("horizon", 0)) < 1.0 and float(sh_.get("from", 0)) >= 0):
            errs.append(f"shot {i}: clip needs horizon 0..1 and from >= 0")
        if k == "card":
            c = sh_["card"]
            if c.get("style", "dark") != "dark":
                errs.append(f"shot {i}: the card is dark #0F1112 (TYPE-SYSTEM.md: no white card on a dark film)")
            if not c.get("mark"):
                errs.append(f"shot {i}: the card needs the Ai mark image (card.mark)")
            for key in ("cta", "url"):
                if voice.copy_problem(str(c.get(key, ""))) or re.search(r"['%:\\]", str(c.get(key, ""))):
                    errs.append(f"shot {i}: card {key} breaks the copy rules or needs escaping")
        if k == "logo" and i == 0:
            errs.append("shot 0 cannot be the logo shot (the chime would land before any picture)")
    lines = s.get("vo") or []
    if len(lines) > MAX_LINES:
        errs.append(f"at most {MAX_LINES} VO lines")
    cs = cuts(s)
    for n, L in enumerate(lines):
        i = L.get("shot")
        if not isinstance(i, int) or not 0 <= i < len(shots) or kind(shots[i]) != "clip":
            errs.append(f"vo {n}: shot must index a clip shot")
            continue
        a, b = cs[i][0] / s["fps"], cs[i][1] / s["fps"]
        if not a <= float(L.get("at", -1)) < b:
            errs.append(f"vo {n}: at {L.get('at')} is outside shot {i} ({a:.3f}..{b:.3f})")
        text = str(L.get("text", ""))
        if voice.copy_problem(text):
            errs.append(f"vo {n}: copy rule ({voice.copy_problem(text)!r})")
        words = L.get("words") or []
        if not words:
            errs.append(f"vo {n}: no words on screen")
        for p in words:
            if voice.phrase_at(text, p) < 0:
                errs.append(f"vo {n}: {p!r} is not spoken in {text!r}")
            if re.search(r"['%:\\]", p):
                errs.append(f"vo {n}: {p!r} needs drawtext escaping")
            if voice.copy_problem(p):
                errs.append(f"vo {n}: copy rule in words {p!r}")
    for f in s.get("formats", []):
        if f not in FORMATS:
            errs.append(f"format {f!r} not in {sorted(FORMATS)}")
    return errs


def lines_for(txt: str, width_of, split_w: float) -> list[str]:
    """`txt` on one line when at most split_w px wide, else two lines split where the wider one is narrowest."""
    if width_of(txt) <= split_w:
        return [txt]
    ws = txt.split()
    if len(ws) < 2:
        raise ValueError(f"{txt!r} is wider than {split_w}px and cannot be split")
    k = min(range(1, len(ws)), key=lambda k: max(width_of(" ".join(ws[:k])), width_of(" ".join(ws[k:]))))
    out = [" ".join(ws[:k]), " ".join(ws[k:])]
    if max(width_of(x) for x in out) > split_w:
        raise ValueError(f"{txt!r} does not fit in two lines of {split_w}px")
    return out


def words_plan(spec: dict, onsets: list[list[float]], width_of, horizons: dict | None = None) -> list[dict]:
    """Word lines: each phrase from its spoken onset to the next phrase's onset in the same VO line, the last
    one to `hold` s before its cut; baseline 44 px above its clip's horizon; two centred lines 132 px apart
    when wider than 1000 px. width_of(text, px) measures in the headline font. Sizes from TYPE."""
    s = with_defaults(spec)
    m, H = metrics(s["size"]), s["size"][1]
    cs, out, horizons = cuts(s), [], horizons or {}
    for n, L in enumerate(s.get("vo", [])):
        i = L["shot"]
        hz = horizons.get(i, float(s["shots"][i]["horizon"]))
        cut = round(cs[i][1] / s["fps"] - s["hold"], 3)
        for j, phrase in enumerate(L["words"]):
            b = onsets[n][j + 1] if j + 1 < len(L["words"]) else cut
            px = int(round(ts.headline_px(phrase) * m["k"]))
            ls = lines_for(phrase, lambda t, px=px: width_of(t, px), m["split"])
            for k, txt in enumerate(ls):
                lift = m["pitch"] * (len(ls) - 1 - k)
                out.append({"txt": txt, "a": onsets[n][j], "b": b, "hz": hz, "size": px, "lift": lift,
                            "baseline": int(hz * H) - m["gap"] - lift, "shot": i, "vo": n})
    return out


def ease_in_alpha(a: float, b: float, d_in: float, d_out: float, peak: float) -> str:
    """peak x (easeOutCubic over d_in from a; linear out over d_out to b), as a drawtext expression."""
    return (f"{peak}*if(lt(t\\,{a:.3f}+{d_in:.4f})\\,1-pow(1-(t-{a:.3f})/{d_in:.4f}\\,3)\\,"
            f"if(gt(t\\,{b:.3f}-{d_out:.4f})\\,({b:.3f}-t)/{d_out:.4f}\\,1))")


def drawtext(w: dict, font: str, fps: int, rise: float) -> str:
    """One word line (TYPE: white at 92 percent, 8 frame easeOutCubic fade in with a 6 px rise, 8 frame
    fade out), its baseline on w['baseline'] (drawtext puts the baseline `ascent` below y)."""
    d_in, d_out = T["word_in_frames"] / fps, T["word_out_frames"] / fps
    return (f"drawtext=fontfile={font}:text='{w['txt']}':fontsize={w['size']}:fontcolor=0x{T['words_color'][1:]}:"
            f"alpha='{ease_in_alpha(w['a'], w['b'], d_in, d_out, T['words_opacity'])}':x=(w-text_w)/2:"
            f"y={w['baseline']}-ascent+{rise:.2f}*pow(1-min(1\\,(t-{w['a']:.3f})/{d_in:.4f})\\,3):"
            f"enable='between(t\\,{w['a']:.3f}\\,{w['b']:.3f})'")


def card_layout(size: list[int]) -> dict:
    """The dark card as a centred stack: mark, CTA 160 px under it, URL, one emerald bead (TYPE, CARD)."""
    W, H = size
    m, c = metrics(size), ts.CARD
    k = m["k"]
    mark = int(round(c["mark_px"] * k))
    gap, ug, bg, bead = (int(round(v * k)) for v in (T["cta_below_mark_px"], c["url_gap_px"], c["bead_gap_px"],
                                                      c["bead_px"]))
    top = (H - (mark + gap + m["cta"] + ug + m["url"] + bg + bead)) // 2
    cta_y = top + mark + gap
    url_y = cta_y + m["cta"] + ug
    return {"mark": {"x": (W - mark) // 2, "y": top, "px": mark}, "cta": {"y": cta_y, "px": m["cta"]},
            "url": {"y": url_y, "px": m["url"]}, "bead": {"x": (W - bead) // 2, "y": url_y + m["url"] + bg, "px": bead},
            "rise": T["card_copy_rise_px"] * k}


def card_drawtext(text: str, font: str, px: int, y: int, color: str, a: float, b: float, fps: int,
                  rise: float) -> str:
    """Card copy: rise 6 px with a fade over 10 frames from the card's first frame (TYPE), box top at y."""
    d = T["card_copy_frames"] / fps
    return (f"drawtext=fontfile={font}:text='{text}':fontsize={px}:fontcolor=0x{color[1:]}:"
            f"alpha='if(lt(t\\,{a:.3f}+{d:.4f})\\,1-pow(1-(t-{a:.3f})/{d:.4f}\\,3)\\,1)':x=(w-text_w)/2:"
            f"y={y}+{rise:.2f}*pow(1-min(1\\,(t-{a:.3f})/{d:.4f})\\,3):enable='between(t\\,{a:.3f}\\,{b:.3f})'")


def bed_shape(end: float, swell_t: float) -> str:
    """Fade in 0.4 s, gentle swell peaking at swell_t, fade out over the last 1.5 s (or half the spot)."""
    out = min(1.5, end / 2)
    return (f"atrim=0:{end},asetpts=PTS-STARTPTS,apad=whole_dur={end},"
            f"volume='0.6+0.4*exp(-pow((t-{swell_t:.3f})/3.0\\,2))':eval=frame,"
            f"afade=t=in:d=0.4,afade=t=out:st={end - out:.3f}:d={out:.3f}")


def is_green(r: int, g: int, b: int) -> bool:
    return g > 100 and g > r * 1.5 and g > b * 1.2


def green_counts(raw: bytes, w: int, h: int) -> list[int]:
    """Per frame, how many pixels of an rgb24 frame stream are LED green."""
    size = w * h * 3
    out = []
    for k in range(len(raw) // size):
        f = raw[k * size:(k + 1) * size]
        out.append(sum(1 for i in range(0, size, 3) if is_green(f[i], f[i + 1], f[i + 2])))
    return out


def first_green(counts: list[int], frac: float = 0.5) -> int:
    """First frame whose green count reaches `frac` of the clip's most green frame (resolution independent)."""
    min_px = max(1, int(max(counts, default=0) * frac))
    for k, c in enumerate(counts):
        if c >= min_px:
            return k
    raise ValueError("the light never turns green")


def rgb_triples(raw: bytes):
    """(r, g, b) per pixel of rgb24 bytes."""
    return zip(raw[0::3], raw[1::3], raw[2::3], strict=True)


def green_px(pixels) -> int:
    """Pixels that read as the LED's green (build_series rule), from an iterable of (r, g, b)."""
    return sum(1 for r, g, b in pixels if g > 90 and g > r + 40 and g > b + 20)


def vo_errors(lines: list[dict]) -> list[str]:
    """Every VO take, as trimmed onto the timeline (vo_dur), lies inside its own shot."""
    return [f"vo{L['n']} {L['place']:.3f}..{L['place'] + L['vo_dur']:.3f} leaves shot {L['shot']}"
            for L in lines if L["place"] < L["shot"][0] - 1e-6 or voice.overruns(L["place"], L["vo_dur"], L["shot"][1])]


def probe_errors(p: dict, w: int, h: int, fps: int, end: float) -> list[str]:
    """Delivery spec: one h264 WxH video at fps, one aac audio, the spot's length within 0.05 s."""
    v = [s for s in p.get("streams", []) if s.get("codec_type") == "video"]
    a = [s for s in p.get("streams", []) if s.get("codec_type") == "audio"]
    errs = []
    if len(v) != 1 or (v[0].get("codec_name"), v[0].get("width"), v[0].get("height"), v[0].get("r_frame_rate")) \
            != ("h264", w, h, f"{fps}/1"):
        got = [(s.get("codec_name"), s.get("width"), s.get("height"), s.get("r_frame_rate")) for s in v]
        errs.append(f"video {got}")
    if len(a) != 1 or a[0].get("codec_name") != "aac":
        errs.append(f"audio {[s.get('codec_name') for s in a]}")
    dur = float(p.get("format", {}).get("duration", 0) or 0)
    if abs(dur - end) > 0.05:
        errs.append(f"duration {dur}")
    return errs


def strip_times(end: float, n: int = 7) -> list[float]:
    return [round((k + 0.5) * end / n, 2) for k in range(n)]


def band(w: dict) -> tuple[int, int]:
    """Rows of a word's text band (build_v3.verify: 8 px under the line, the glyph size plus 40 px above
    it at 118 px), the margins scaled with the type so every format keeps the same text coverage."""
    k = w["size"] / T["headline_px"]
    y1 = w["baseline"] + int(round(8 * k))
    return max(0, y1 - w["size"] - int(round(40 * k))), y1


def source_shot(spec: dict, frame: int) -> int | None:
    """Index of the clip shot that output `frame` shows, or None on the logo or card (build_v3.source_frame)."""
    for i, (c0, c1) in enumerate(cuts(spec)):
        if c0 <= frame < c1:
            return i if kind(spec["shots"][i]) == "clip" else None
    return None


def onset_frames(a: float, fps: int) -> tuple[int, int]:
    """(frame 0.1 s before the onset, strictly before it; frame 0.4 s after)."""
    n = int((a - 0.1) * fps)
    if n / fps >= a:
        n -= 1
    return max(0, n), int((a + 0.4) * fps)


# ------------------------------------------------------------------ pure: Atmosmotion compositions

def _kf(*pts) -> dict:
    return {"kf": [{"at": round(at, 3), "value": round(v, 3), **({"easing": e} if e else {})} for at, v, e in pts]}


def plate_comp(words: list[dict], size: list[int], fps: int, end: float, fonts: dict, rise: float) -> dict:
    """White-on-black word plate for engine-native: the same lines, opacity and rise as drawtext, each text
    box top placed `ink` px above its baseline (the engine's box top is the ink top, measured 2026-09-25).
    fonts maps px to the pinned Fraunces instance for that px.
    TODO(atmosmotion-fontvariation): pass the axes (typesystem.headline_axes) on the text layer instead of
    a pinned instance once the follow-up to atmosmotion PR 70 adds fontVariation."""
    W, H = size
    layers = []
    for n, w in enumerate(words):
        a, b = w["a"] * fps, w["b"] * fps
        i, o = T["word_in_frames"], T["word_out_frames"]
        top = w["baseline"] - w["ink"]
        layers.append({"id": f"w{n}", "type": "text", "content": w["txt"], "font": "Fraunces",
                       "fontAsset": f"headline{w['size']}",
                       "size": w["size"], "color": T["words_color"], "align": "center", "in": round(a, 3),
                       "out": round(b, 3),
                       "opacity": _kf((a, 0, None), (a + i, T["words_opacity"], T["word_easing"]),
                                      (max(a + i, b - o), T["words_opacity"], "linear"), (b, 0, "linear")),
                       "transform": {"x": W / 2, "y": _kf((a, top + rise, None), (a + i, top, T["word_easing"])),
                                     "anchor": {"x": 0.5, "y": 0}}})
    return {"version": "1.0", "meta": {"name": "studio-word-plate"}, "size": {"w": W, "h": H}, "fps": fps,
            "duration": end, "coordSystem": "top-left", "timeUnit": "frames",
            "background": {"kind": "color", "color": "#000000"},
            "assets": {f"headline{px}": {"kind": "font", "family": "Fraunces", "src": str(src)}
                       for px, src in sorted(fonts.items())}, "layers": layers}


def card_comp(card: dict, size: list[int], fps: int, frames: int, mark_png: str, cta_font: str,
              url_font: str) -> dict:
    """The dark end card as an engine segment: mark (pre-sized PNG, pasted 1:1), CTA and URL rising 6 px
    with a 10 frame fade from the card's first frame, one emerald bead."""
    L = card_layout(size)
    W = size[0]
    f, r = T["card_copy_frames"], L["rise"]

    def copy(i: str, text: str, y: int, px: int, color: str) -> dict:
        return {"id": i, "type": "text", "content": text, "font": "Geist", "fontAsset": i, "size": px,
                "color": color, "align": "center", "opacity": _kf((0, 0, None), (f, 1, T["word_easing"])),
                "transform": {"x": W / 2, "y": _kf((0, y + r, None), (f, y, T["word_easing"])),
                              "anchor": {"x": 0.5, "y": 0}}}

    mk, bd = L["mark"], L["bead"]
    return {"version": "1.0", "meta": {"name": "studio-card"}, "size": {"w": W, "h": size[1]}, "fps": fps,
            "duration": frames / fps, "coordSystem": "top-left", "timeUnit": "frames",
            "background": {"kind": "color", "color": T["dark"]},
            "assets": {"mark": {"kind": "image", "src": mark_png},
                       "cta": {"kind": "font", "family": "Geist", "src": cta_font},
                       "url": {"kind": "font", "family": "Geist", "src": url_font}},
            "layers": [{"id": "mark", "type": "image", "asset": "mark", "fit": "contain",
                        "transform": {"x": mk["x"] + mk["px"] / 2, "y": mk["y"] + mk["px"] / 2,
                                      "anchor": {"x": 0.5, "y": 0.5}}},
                       copy("cta", card.get("cta", ""), L["cta"]["y"], L["cta"]["px"], T["card_copy"]),
                       copy("url", card.get("url", "advisorppc.com"), L["url"]["y"], L["url"]["px"], T["secondary"]),
                       {"id": "bead", "type": "shape", "shape": "ellipse", "size": {"w": bd["px"], "h": bd["px"]},
                        "fill": T["accent"], "transform": {"x": bd["x"], "y": bd["y"], "anchor": {"x": 0, "y": 0}}}]}


# ------------------------------------------------------------------ io helpers

def _p(job: Path, rel: str) -> Path:
    p = Path(rel).expanduser()
    return p if p.is_absolute() else job / p


def load(job: str | Path, spec_name: str = "spec.json") -> tuple[Path, dict]:
    d = resolve_job(job)
    sp = _p(d, spec_name)
    try:
        spec = json.loads(sp.read_text())
    except (OSError, ValueError) as e:
        raise StudioError(f"cannot read spec {sp}: {e}") from e
    errs = validate(spec)
    if errs:
        raise StudioError(f"spec {sp} is invalid: {errs}")
    return d, with_defaults(spec)


def vo_paths(job: Path, name: str, n: int, k: int) -> tuple[Path, Path]:
    return job / "audio" / f"{name}_vo{n}_{k}.mp3", job / "audio" / f"{name}_vo{n}_{k}.json"


def grab(path: Path, frame: int, fps: int, dst: Path, scale: int = 0) -> Path:
    """Exactly frame `frame` of a constant-fps file (the seek lands on the first frame at or after the time)."""
    vf = ["-vf", f"scale={scale}:-2"] if scale else []
    ff(["-ss", f"{(frame - 0.25) / fps:.4f}", "-i", str(path), "-frames:v", "1", *vf, "-q:v", "2", "-update", "1",
        str(dst)])
    return dst


def font_measure(path_for):
    """width_of(text, px) and ink(text, px) (baseline to ink top), each px in the font path_for(px)."""
    from PIL import ImageFont
    cache: dict = {}

    def face(px: int):
        if px not in cache:
            path = path_for(px)
            try:
                cache[px] = ImageFont.truetype(str(path), px)
            except OSError as e:
                raise StudioError(f"font {path} cannot be read: {e}") from e
        return cache[px]

    return (lambda t, px: face(px).getlength(t)), (lambda t, px: -face(px).getbbox(t, anchor="ls")[1])


def takes(job: Path, spec: dict, generate: bool, tts=voice.tts) -> list[dict]:
    """The chosen take per VO line. generate=True fills missing takes from ElevenLabs in preference order and
    stops at the first that fits and reads; False reads only saved takes, so render repeats that choice."""
    s, cs, chosen = spec, cuts(spec), []
    texts = [L["text"] for L in s.get("vo", [])]
    for n, L in enumerate(s.get("vo", [])):
        cut = cs[L["shot"]][1] / s["fps"]
        fits, cands = [], []
        for k, text in enumerate(voice.vo_texts(L["text"])):
            mp3, js = vo_paths(job, s.get("takes_name", s["name"]), n + 1, k)
            meta = json.loads(js.read_text()) if js.exists() and mp3.exists() else None
            if meta is None or meta.get("text") != text:  # missing, or the script changed under a saved take
                if not generate:
                    break
                meta = tts(text, mp3, js, voice=L.get("voice", voice.BRIAN),
                           previous_text=" ".join(texts[:n]), next_text=" ".join(texts[n + 1:]))
            dur = duration(mp3)
            occ = round(min(dur, voice.audible_end(voice.pcm(mp3), 48000) + voice.VO_TAIL), 3)
            fit_ = voice.take_fit(meta["alignment"], occ, float(L["at"]), cut, L["words"][-1], s["hold"])
            fits.append(fit_)
            cands.append({"k": k, "text": text, "meta": meta, "mp3": str(mp3), "dur": dur, "occ": occ, "fit": fit_})
            if fit_[0] and fit_[1] >= s["read_min"]:
                break
        c = voice.choose(fits, s["read_min"])
        if c is None:
            raise StudioError(f"VO line {n + 1}: no saved take fits its shot "
                              f"({'every written pause overruns' if generate else 'run assets first'})")
        chosen.append(cands[c])
    return chosen


def timing(spec: dict, chosen: list[dict]) -> dict:
    cs, lines, onsets = cuts(spec), [], []
    for n, (L, t) in enumerate(zip(spec.get("vo", []), chosen, strict=True)):
        al = t["meta"]["alignment"]
        place = voice.placement(float(L["at"]), al)
        a, b = voice.speech_span(al)
        onsets.append([round(place + voice.phrase_onset(al, p), 3) for p in L["words"]])
        i = L["shot"]
        lines.append({"n": n + 1, "take": t["k"], "text": t["text"], "spoken": voice.spoken(t["text"]), "mp3": t["mp3"],
                      "place": round(place, 3), "mp3_dur": round(t["dur"], 3), "vo_dur": t["occ"],
                      "speech": [round(place + a, 3), round(place + b, 3)], "speech_dur": round(b - a, 3),
                      "onset": onsets[-1][0], "read": t["fit"][1], "wps": round(voice.wps(t["text"], al), 2),
                      "pauses": voice.pauses(al),
                      "shot": [round(cs[i][0] / spec["fps"], 3), round(cs[i][1] / spec["fps"], 3)]})
    return {"lines": lines, "onsets": onsets}


def logo_facts(job: Path, spec: dict) -> dict | None:
    """Green frame of the logo shot by colour, its time on the timeline, and where the logo sits."""
    idx = next((i for i, s_ in enumerate(spec["shots"]) if kind(s_) == "logo"), None)
    if idx is None:
        return None
    clip = _p(job, spec["shots"][idx]["logo"])
    v = next((s_ for s_ in probe(clip).get("streams", []) if s_.get("codec_type") == "video"), None)
    if not v:
        raise StudioError(f"logo clip {clip} has no video stream")
    iw, ih = int(v["width"]), int(v["height"])
    w = 240
    h = max(2, int(round(w * ih / iw / 2)) * 2)
    try:
        raw = subprocess.run(ffmpeg_cmd(["-v", "error", "-i", str(clip), "-vf", f"scale={w}:{h}", "-f", "rawvideo",
                                         "-pix_fmt", "rgb24", "-"]), capture_output=True, timeout=600).stdout
    except subprocess.TimeoutExpired as e:
        raise StudioError(f"decoding {clip} timed out") from e
    g = first_green(green_counts(raw, w, h))
    c0, c1 = cuts(spec)[idx]
    if g >= c1 - c0:
        raise StudioError(f"logo turns green at clip frame {g}, after the logo shot's {c1 - c0} frames")
    geo = fit(iw, ih, *spec["size"])
    return {"shot": idx, "green_frame": g, "green_t": round((c0 + g) / spec["fps"], 3),
            "band": [geo["x"], geo["y"], geo["x"] + geo["w"], geo["y"] + geo["h"]], "clip": str(clip)}


def card_mark(job: Path, card: dict, size: list[int], dst: Path) -> Path:
    """The Ai mark pre-sized to the card's mark px (the engine pastes images 1:1, render-ident.mjs)."""
    from PIL import Image
    px = card_layout(size)["mark"]["px"]
    try:
        Image.open(_p(job, card["mark"])).convert("RGBA").resize((px, px), Image.LANCZOS).save(dst)
    except OSError as e:
        raise StudioError(f"card mark {card['mark']} cannot be read: {e}") from e
    return dst


def card_plate(job: Path, card: dict, size: list[int], dst: Path) -> Path:
    """drawtext renderer: the card background, mark and bead as one still; its copy is drawn animated."""
    from PIL import Image, ImageDraw
    L = card_layout(size)
    im = Image.new("RGB", tuple(size), ts.hex_rgb(T["dark"]))
    mk = Image.open(card_mark(job, card, size, dst.with_name(dst.stem + "-mark.png")))
    im.paste(mk, (L["mark"]["x"], L["mark"]["y"]), mk)
    b = L["bead"]
    ImageDraw.Draw(im).ellipse((b["x"], b["y"], b["x"] + b["px"], b["y"] + b["px"]), fill=ts.hex_rgb(T["accent"]))
    im.save(dst)
    return dst


def atmos(comp: dict, out: Path, mode: str, timeout: float = ATMOS_TIMEOUT) -> Path:
    """Render one composition with engine-native through atmos_render.mjs (heat-gated, nice 19, bounded)."""
    engine = Path(os.environ.get("WORKMAN_ATMOS_ENGINE", ENGINE)).expanduser()
    if not (engine / "engine-native" / "dist" / "index.js").is_file():
        raise StudioError(f"Atmosmotion engine not built at {engine} (engine-native/dist missing)")
    cj = out.with_suffix(".cm.json")
    cj.write_text(json.dumps(comp, indent=1))
    cool_down()
    env = dict(os.environ, ATMOS_ENGINE=str(engine))
    try:
        r = subprocess.run(["nice", "-n", "19", "node", str(ATMOS_SCRIPT), str(cj), str(out), mode],
                           capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        raise StudioError(f"Atmosmotion {mode} render timed out after {timeout:.0f}s") from e
    except FileNotFoundError as e:
        raise StudioError("node is not installed") from e
    if r.returncode or not out.is_file():
        raise StudioError(f"Atmosmotion {mode} render exit {r.returncode}: {(r.stderr or r.stdout)[-600:]}")
    return out


def geometry(job: Path, spec: dict) -> tuple[dict, list[str]]:
    """Per shot index: its crop/scale filter and (for clips) the horizon in output coordinates; plus the
    problems found: a missing clip, too few frames for its window, a horizon cropped out of frame."""
    W, H = spec["size"]
    geo, errs = {}, []
    for i, ((c0, c1), s_) in enumerate(zip(cuts(spec), spec["shots"], strict=True)):
        k = kind(s_)
        if k == "card":
            continue
        clip = _p(job, s_["logo"] if k == "logo" else s_["clip"])
        if not clip.is_file():
            errs.append(f"shot {i}: missing {clip}")
            continue
        p = probe(clip)
        v = next((x for x in p.get("streams", []) if x.get("codec_type") == "video"), None)
        if not v:
            errs.append(f"shot {i}: {clip.name} has no video stream")
            continue
        iw, ih = int(v["width"]), int(v["height"])
        have = float(p.get("format", {}).get("duration", 0) or 0) * spec["fps"]
        need = (int(round(float(s_.get("from", 0)) * spec["fps"])) if k == "clip" else 0) + c1 - c0
        if need > have + 0.5:
            errs.append(f"shot {i}: needs {need} frames, {clip.name} has {have:.0f}")
        if k == "logo":
            geo[i] = fit(iw, ih, W, H)
            continue
        g = cover(iw, ih, W, H)
        g["hz"] = mapped_horizon(float(s_["horizon"]), g, H)
        if not 0.05 < g["hz"] < 0.95:
            errs.append(f"shot {i}: horizon lands at {g['hz']} of the {W}x{H} frame after cropping")
        geo[i] = g
    return geo, errs


def _words(s: dict, tm: dict, geo: dict) -> list[dict]:
    width_of, ink = font_measure(ts.headline_font)
    words = words_plan(s, tm["onsets"], width_of, {i: g["hz"] for i, g in geo.items() if "hz" in g})
    for w in words:
        w["ink"] = int(round(ink(w["txt"], w["size"])))
    return words


# ------------------------------------------------------------------ stages

def assets(job: str | Path, spec_name: str = "spec.json", tts=voice.tts, sound=voice.sound) -> dict:
    """VO takes (with-timestamps), bed and chime. The only stage that calls ElevenLabs."""
    d, s = load(job, spec_name)
    (d / "audio").mkdir(exist_ok=True)
    made = []
    bed = s.get("bed", {})
    made.append(sound(_p(d, bed.get("path", "audio/bed.mp3")), bed.get("prompt", voice.BED_PROMPT),
                      float(bed.get("seconds", min(22.0, math.ceil(s["end"] + 0.5)))), 0.5))
    if any(kind(x) == "logo" for x in s["shots"]):
        ch = s.get("chime", {})
        made.append(sound(_p(d, ch.get("path", "audio/chime.mp3")), ch.get("prompt", voice.CHIME_PROMPT),
                          float(ch.get("seconds", 2.5)), 0.7))
    chosen = takes(d, s, generate=True, tts=tts)
    return {"ok": True, "sounds": made, "takes": [{"line": n + 1, "take": t["k"], "fits": t["fit"][0],
                                                     "read": t["fit"][1]} for n, t in enumerate(chosen)]}


def plan(job: str | Path, spec_name: str = "spec.json") -> dict:
    """Timing and word lines from saved takes; renders nothing."""
    d, s = load(job, spec_name)
    geo, errs = geometry(d, s)
    tm = timing(s, takes(d, s, generate=False))
    return {"ok": not errs and not vo_errors(tm["lines"]), "clip_errors": errs, "vo_errors": vo_errors(tm["lines"]),
            **tm, "words": _words(s, tm, geo)}


def filtergraph(job: Path, s: dict, geo: dict, words: list[dict], card: Path | None,
                plate: Path | None) -> tuple[list, list]:
    """(video inputs, video filters) ending in [vout]."""
    W, H = s["size"]
    fps, end, inputs, fc, labels = s["fps"], s["end"], [], [], []
    atmos_card = card is not None and card.suffix == ".mp4"
    for i, ((c0, c1), sh_) in enumerate(zip(cuts(s), s["shots"], strict=True)):
        k, n = kind(sh_), c1 - c0
        if k == "clip":
            inputs += ["-i", str(_p(job, sh_["clip"]))]
            s0 = int(round(float(sh_.get("from", 0)) * fps))
            fc.append(f"[{i}:v]trim=start_frame={s0}:end_frame={s0 + n},setpts=PTS-STARTPTS,"
                      f"{geo[i]['vf']},setsar=1,format=yuv420p[v{i}]")
        elif k == "logo":
            inputs += ["-i", str(_p(job, sh_["logo"]))]
            fc.append(f"[{i}:v]trim=start_frame=0:end_frame={n},setpts=PTS-STARTPTS,{geo[i]['vf']},"
                      f"setsar=1,format=yuv420p[v{i}]")
        elif atmos_card:
            inputs += ["-i", str(card)]
            fc.append(f"[{i}:v]trim=start_frame=0:end_frame={n},setpts=PTS-STARTPTS,setsar=1,format=yuv420p[v{i}]")
        else:
            inputs += ["-i", str(card)]
            fc.append(f"[{i}:v]loop=loop={n - 1}:size=1:start=0,setpts=N/{fps}/TB,fps={fps},setsar=1,"
                      f"format=yuv420p[v{i}]")
        labels.append(f"[v{i}]")
    fc.append("".join(labels) + f"concat=n={len(labels)}:v=1:a=0[vcat]")
    m = metrics(s["size"])
    draws = [] if plate else [drawtext(w, str(ts.headline_font(w["size"])), fps, m["rise"]) for w in words]
    cidx = next((i for i, x in enumerate(s["shots"]) if kind(x) == "card"), None)
    if cidx is not None and not atmos_card:
        L, c = card_layout(s["size"]), s["shots"][cidx]["card"]
        a0, b0 = cuts(s)[cidx][0] / fps, end
        draws += [card_drawtext(c.get("cta", ""), str(ts.sans_font("cta")), L["cta"]["px"], L["cta"]["y"],
                                T["card_copy"], a0, b0, fps, L["rise"]),
                  card_drawtext(c.get("url", "advisorppc.com"), str(ts.sans_font("url")), L["url"]["px"],
                                L["url"]["y"], T["secondary"], a0, b0, fps, L["rise"])]
    fc.append("[vcat]" + ",".join(draws + ["format=yuv420p"]) + ("[vb]" if plate else "[vout]"))
    if plate:
        # TODO(atmosmotion-alpha): engine-native encodes yuv420p MP4 only, so the words come as a white-on-black
        # plate and become alpha here. Switch to the engine's own alpha output (render --format mov --codec
        # prores, ProRes 4444) once that lands on the canonical engine, and drop this alphamerge.
        p = len(s["shots"])
        inputs += ["-i", str(plate)]
        fc.append(f"[{p}:v]format=gray,setsar=1[pm]")
        fc.append(f"color=c=0x{T['words_color'][1:]}:s={W}x{H}:r={fps}:d={end},format=rgba[wc]")
        fc.append("[wc][pm]alphamerge[wt]")
        fc.append("[vb][wt]overlay=0:0:shortest=1:format=auto,format=yuv420p[vout]")
    return inputs, fc


def audio_graph(job: Path, s: dict, tm: dict, logo: dict | None, bed_gain: float, chime: dict | None,
                vo_gain: dict) -> tuple[list, list]:
    """(audio inputs, filters) ending in [pre]: the bed shaped and set to -26 LUFS, the chime on its frame,
    each VO take trimmed at its audible end and placed on its onset (build_series/build_partner premix)."""
    end, inputs = s["end"], []
    bed = _p(job, s.get("bed", {}).get("path", "audio/bed.mp3"))
    swell = logo["green_t"] if logo else float(s.get("bed", {}).get("swell_at", end / 2))
    inputs += ["-i", str(bed)]
    afc, mix = [f"[0:a]{bed_shape(end, swell)},volume={bed_gain:.2f}dB[bed]"], ["[bed]"]
    ai = 1
    if chime:
        inputs += ["-i", chime["path"]]
        afc.append(f"[{ai}:a]afade=t=in:d=0.005,volume={chime['gain']:.2f}dB,"
                   f"adelay={int(round(chime['at'] * 1000))}:all=1[chime]")
        mix.append("[chime]")
        ai += 1
    for L in tm["lines"]:
        inputs += ["-i", L["mp3"]]
        afc.append(f"[{ai}:a]atrim=0:{L['vo_dur']:.3f},afade=t=out:st={max(0.0, L['vo_dur'] - 0.05):.3f}:d=0.05,"
                   f"volume={vo_gain[L['n']]:.2f}dB,adelay={int(round(L['place'] * 1000))}:all=1[vo{L['n']}]")
        mix.append(f"[vo{L['n']}]")
        ai += 1
    afc.append("".join(mix) + f"amix=inputs={len(mix)}:normalize=0:duration=longest:dropout_transition=0,"
               f"aresample=48000,atrim=0:{end}[pre]")
    return inputs, afc


def render(job: str | Path, spec_name: str = "spec.json", fmt: str = "") -> dict:
    """out/<name>.mp4 from saved takes: heat gate and one render at a time before the encode; an existing
    render is never overwritten (a new cut is a new v<n>)."""
    d, s = load(job, spec_name)
    if fmt:
        s = with_defaults(derive(s, fmt))
    name, out = s["name"], d / "out" / f"{s['name']}.mp4"
    if out.exists():
        raise StudioError(f"{out} exists; a new cut is a new v<n>, earlier renders are never overwritten")
    geo, errs = geometry(d, s)
    if errs:
        raise StudioError(f"clips: {errs}")
    tm = timing(s, takes(d, s, generate=False))
    if vo_errors(tm["lines"]):
        raise StudioError(f"VO: {vo_errors(tm['lines'])}")
    words = _words(s, tm, geo)
    (d / "out").mkdir(exist_ok=True)
    (d / "review").mkdir(exist_ok=True)
    atm = s["renderer"] == "atmosmotion"
    cidx = next((i for i, x in enumerate(s["shots"]) if kind(x) == "card"), None)
    logo = logo_facts(d, s)
    bed = _p(d, s.get("bed", {}).get("path", "audio/bed.mp3"))
    swell = logo["green_t"] if logo else float(s.get("bed", {}).get("swell_at", s["end"] / 2))
    bed_i, _ = voice.ebur128(["-i", str(bed)], bed_shape(s["end"], swell))
    bed_gain = s["bed_lufs"] - bed_i
    chime = None
    if logo:
        cp = _p(d, s.get("chime", {}).get("path", "audio/chime.mp3"))
        at = logo["green_t"] - voice.attack_time(voice.pcm(cp), 48000)
        if at < cuts(s)[logo["shot"]][0] / s["fps"]:
            raise StudioError(f"chime attack would start it before the logo shot ({at:.3f}s)")
        chime = {"path": str(cp), "at": round(at, 3), "gain": round(s["chime_peak"] - voice.max_volume(cp), 2)}
    vo_gain = {L["n"]: round(-6.0 - voice.max_volume(L["mp3"]), 2) for L in tm["lines"]}
    with render_lock():
        wait_turn(d)
        card = plate = None
        if cidx is not None:
            c, n = s["shots"][cidx]["card"], cuts(s)[cidx][1] - cuts(s)[cidx][0]
            if atm:
                mark = card_mark(d, c, s["size"], d / "out" / f"{name}-mark.png")
                card = atmos(card_comp(c, s["size"], s["fps"], n, str(mark), str(ts.sans_font("cta")),
                                       str(ts.sans_font("url"))),
                             d / "out" / f"{name}-card.mp4", "opaque")
            else:
                card = card_plate(d, c, s["size"], d / "out" / f"{name}-card.png")
        if atm and words:
            fonts = {w["size"]: ts.headline_font(w["size"]) for w in words}
            plate = atmos(plate_comp(words, s["size"], s["fps"], s["end"], fonts,
                                     metrics(s["size"])["rise"]), d / "out" / f"{name}-plate.mkv", "plate")
        # Master (build_partner): premix at the set levels, then one static gain solved on the premix plus a
        # 4x oversampled limiter. build_series' two-pass loudnorm missed -14 LUFS on programmes near its 3 s
        # window (3 s synthetic job: -13.4); a solved gain converges on any length and cannot pump a VO-free shot.
        ain, afc = audio_graph(d, s, tm, logo, bed_gain, chime, vo_gain)
        premix = d / "audio" / f"{name}_premix.wav"
        ff(ain + ["-filter_complex", ";".join(afc), "-map", "[pre]", "-c:a", "pcm_f32le", str(premix)])
        gain, passes = voice.master_gain(premix, s["end"], target=float(T["master_lufs"]))
        inputs, vfc = filtergraph(d, s, geo, words, card, plate)
        pi = len(inputs) // 2          # every input is an ("-i", path) pair: the premix is the next index
        inputs += ["-i", str(premix)]
        fc = vfc + [f"[{pi}:a]{voice.master_chain(gain, s['end'])}[aout]"]
        cmd = ffmpeg_cmd(["-v", "error"] + inputs + [
            "-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]",
            "-t", str(s["end"]), "-r", str(s["fps"]),
            "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(out)])
        (d / "out" / f"{name}.cmd").write_text(" ".join(cmd))
        timing_doc = {**tm, "words": words, "geometry": {str(i): g for i, g in geo.items()}, "logo": logo,
                      "chime": chime, "renderer": s["renderer"], "bed_raw_lufs": bed_i,
                      "bed_gain_db": round(bed_gain, 2), "vo_gain_db": vo_gain,
                      "master_gain_db": gain, "master_passes": passes}
        (d / "out" / f"{name}.timing.json").write_text(json.dumps(timing_doc, indent=1))
        cool_down()
        t0 = time.monotonic()
        sh(cmd, timeout=3600)
    return {"ok": True, "out": str(out), "seconds": round(time.monotonic() - t0, 1),
            "timing": str(d / "out" / f"{name}.timing.json")}


def band_diff(a: Path, b: Path, y0: int, y1: int) -> float:
    """Mean absolute luma difference of rows y0..y1 between two same-size frames."""
    from PIL import Image
    A = Image.open(a).convert("L")
    B = Image.open(b).convert("L")
    pa, pb = A.crop((0, y0, A.width, y1)).tobytes(), B.crop((0, y0, B.width, y1)).tobytes()
    if len(pa) != len(pb) or not pa:
        raise StudioError(f"band {y0}..{y1} differs in size between {a.name} and {b.name}")
    return sum(abs(x - y) for x, y in zip(pa, pb, strict=True)) / len(pa)


def verify(job: str | Path, spec_name: str = "spec.json", fmt: str = "") -> dict:
    """Every check the three builders ran, into review/<name>_verify.json; `ok` is their conjunction."""
    from PIL import Image
    d, s = load(job, spec_name)
    if fmt:
        s = with_defaults(derive(s, fmt))
    W, H = s["size"]
    fps, name = s["fps"], s["name"]
    out = d / "out" / f"{name}.mp4"
    if not out.is_file():
        raise StudioError(f"no render at {out}")
    tm = json.loads((d / "out" / f"{name}.timing.json").read_text())
    rv = d / "review" / name
    rv.mkdir(parents=True, exist_ok=True)
    tiles = [Image.open(grab(out, int(round(t * fps)), fps, rv / f"f_{t}.jpg", 360)) for t in strip_times(s["end"])]
    strip = Image.new("RGB", (sum(i.width for i in tiles), max(i.height for i in tiles)), "black")
    for n, im in enumerate(tiles):
        strip.paste(im, (n * im.width, 0))
    strip_path = d / "review" / f"{name}_strip.jpg"
    strip.save(strip_path, quality=90)
    lo, hi = s["onset_ok"]
    cs, onsets = cuts(s), []
    for k, w in enumerate(tm["words"]):
        y0, y1 = band(w)
        row = []
        for tag, n in zip(("before", "after"), onset_frames(w["a"], fps), strict=True):
            o = grab(out, n, fps, rv / f"on{k}_{tag}.png")
            i = source_shot(s, n)   # 0.1 s before an onset near a cut is still the previous clip (build_v3)
            if i is None:
                raise StudioError(f"frame {n} of {w['txt']!r} is not on a clip shot; the onset band test needs one")
            shot = s["shots"][i]
            m = int(round(float(shot.get("from", 0)) * fps)) + n - cs[i][0]
            src = rv / f"src{k}_{tag}.png"
            ff(["-i", str(_p(d, shot["clip"])), "-map", "0:v:0", "-vf",
                f"select=eq(n\\,{m}),{tm['geometry'][str(i)]['vf']}", "-frames:v", "1", "-update", "1",
                str(src)])
            row.append(round(band_diff(o, src, y0, y1), 2))
        onsets.append({"txt": w["txt"], "onset": w["a"], "diff_before": row[0], "diff_after": row[1],
                       "ok": row[0] < lo and row[1] > hi})
    green = None
    if tm.get("logo"):
        lg = tm["logo"]
        f0 = cs[lg["shot"]][0] + lg["green_frame"]
        g = [green_px(rgb_triples(Image.open(grab(out, f0 + dd, fps, rv / f"green{dd:+d}.png")).convert("RGB")
                                  .crop(tuple(lg["band"])).resize((480, 272)).tobytes())) for dd in (-1, 0)]
        gmax, gmin = s["green_ok"]
        green = {"frame_before": g[0], "frame_on": g[1], "ok": g[0] < gmax and g[1] > gmin}
    whole = voice.ebur128(["-i", str(out), "-vn"])
    copy = [f"vo{L['n']} copy rule {voice.copy_problem(L['spoken'])!r}" for L in tm["lines"]
            if voice.copy_problem(L["spoken"])]
    copy += [f"vo{L['n']} says {L['spoken']!r}, script says {V['text']!r}"
             for L, V in zip(tm["lines"], s.get("vo", []), strict=True) if L["spoken"] != V["text"]]
    deviations = [f"vo{L['n']} words read {L['read']:.2f}s, under {s['read_min']}s" for L in tm["lines"]
                  if L["read"] < s["read_min"]]
    cidx = next((i for i, x in enumerate(s["shots"]) if kind(x) == "card"), None)
    if cidx is not None and (cs[cidx][1] - cs[cidx][0]) / fps < T["card_hold_s"]:
        deviations.append(f"card held {(cs[cidx][1] - cs[cidx][0]) / fps:.2f}s, TYPE-SYSTEM.md holds it "
                          f"{T['card_hold_s']}s")
    res = {"name": name, "out": str(out), "renderer": tm.get("renderer", "drawtext"),
           "probe": probe_errors(probe(out), W, H, fps, s["end"]), "onsets": onsets,
           "vo": vo_errors(tm["lines"]), "green": green, "copy": copy, "strip": str(strip_path),
           "loudness": {"integrated": whole[0], "true_peak": whole[1], "errors": voice.loudness_errors(*whole)},
           "deviations": deviations}
    res["ok"] = not (res["probe"] or res["vo"] or res["loudness"]["errors"] or res["copy"]) \
        and all(o["ok"] for o in onsets) and (green is None or green["ok"])
    (d / "review" / f"{name}_verify.json").write_text(json.dumps(res, indent=1))
    return res


def contact(strips: list[tuple[str, Path]], dst: Path) -> Path | None:
    """One strip per render, each under its label (build_series.contact)."""
    from PIL import Image, ImageDraw, ImageFont
    rows = [(label, Image.open(p)) for label, p in strips if Path(p).exists()]
    if not rows:
        return None
    lab = 44
    try:
        f = ImageFont.truetype(str(ts.sans_font("label")), 28)
    except (OSError, StudioError):
        f = ImageFont.load_default()
    sheet = Image.new("RGB", (max(im.width for _, im in rows), sum(im.height + lab for _, im in rows)), "black")
    dr, y = ImageDraw.Draw(sheet), 0
    for label, im in rows:
        dr.text((12, y + 8), label, font=f, fill="white")
        sheet.paste(im, (0, y + lab))
        y += im.height + lab
    sheet.save(dst, quality=88)
    return dst


def formats_of(spec: dict, only: str = "") -> list[str]:
    """'' (the spec's own size) then each extra format the spec asks for, or just `only`."""
    return [only] if only else [""] + list(spec.get("formats", []))


def run(job: str | Path, stage: str, spec_name: str = "spec.json", only: str = "") -> dict:
    """render and/or verify every format of the spec, one render at a time; stops at the first error."""
    d, s = load(job, spec_name)
    out: dict = {"ok": True, "formats": {}}
    for fmt in formats_of(s, only):
        r: dict = {}
        if stage in ("render", "all"):
            r["render"] = render(d, spec_name, fmt)
        if stage in ("verify", "all"):
            r["verify"] = verify(d, spec_name, fmt)
            out["ok"] = out["ok"] and r["verify"]["ok"]
        out["formats"][fmt or "spec"] = r
    if stage in ("verify", "all"):
        names = [with_defaults(derive(s, f))["name"] if f else s["name"] for f in formats_of(s, only)]
        sheet = contact([(n, d / "review" / f"{n}_strip.jpg") for n in names],
                        d / "review" / f"{s['name']}_contact.jpg")
        out["contact"] = str(sheet) if sheet else None
    return out


def start_background(job: str | Path, spec_name: str = "spec.json", stage: str = "all", only: str = "") -> dict:
    """Launch `python -m workman.studio.assemble <job> <stage>` detached, log to out/, record its PID in the
    job's ledger. Refuses while this job's previous render still runs (one render at a time)."""
    from .job import render_state, track_pid
    d, s = load(job, spec_name)
    if stage not in ("render", "verify", "all"):
        raise StudioError(f"stage {stage!r} is render, verify or all")
    if only and only not in FORMATS:
        raise StudioError(f"format {only!r} not in {sorted(FORMATS)}")
    st = render_state(d)
    if st.get("state") == "running":
        return {"ok": False, "error": "render_running", "pid": st["pid"], "log": st.get("log")}
    (d / "out").mkdir(exist_ok=True)
    log = d / "out" / f"{s['name']}.{time.strftime('%Y%m%d-%H%M%S')}.log"
    argv = [sys.executable, "-m", "workman.studio.assemble", str(d), stage, "--spec", spec_name]
    if only:
        argv += ["--format", only]
    pkg_parent = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(x for x in (pkg_parent, os.environ.get("PYTHONPATH", "")) if x))
    with open(log, "w") as f:
        p = subprocess.Popen(argv, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=str(d),
                             env=env, start_new_session=True)
    track_pid(d, p.pid, f"render {s['name']} {stage}", "workman.studio.assemble", str(log))
    return {"ok": True, "pid": p.pid, "log": str(log), "job": str(d), "stage": stage,
            "formats": formats_of(s, only), "next": "studio_status(job) until render.state is finished"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m workman.studio.assemble", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job")
    ap.add_argument("stage", choices=["plan", "assets", "render", "verify", "all"])
    ap.add_argument("--spec", default="spec.json")
    ap.add_argument("--format", default="", choices=["", *FORMATS],
                    help="only this aspect (default: the spec's size, then each of its formats)")
    a = ap.parse_args(argv)
    try:
        if a.stage == "plan":
            out = plan(a.job, a.spec)
        elif a.stage == "assets":
            out = assets(a.job, a.spec)
        else:
            out = run(a.job, a.stage, a.spec, a.format)
    except (StudioError, ValueError, OSError, KeyError) as e:
        print(json.dumps({"ok": False, "stage": a.stage, "error": f"{type(e).__name__}: {e}"}), flush=True)
        return 1
    print(json.dumps(out, indent=1), flush=True)
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
