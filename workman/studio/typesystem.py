"""The AdvisorPPC video type system: the one source of fonts, sizes, colours, placement and motion.

Mirrors ~/advisorppc-ads-20260925/TYPE-SYSTEM.md (owner order 2026-09-25: one font, one colour
set, no random). assemble.py reads every type value from TYPE and nowhere else; a spec cannot
override a font, size, colour or timing. `parse_md` reads the same values out of the markdown so
tests/studio pins TYPE to the file: when the owner edits the file, the test fails until this
mirror is updated.

Sizes are px on the short side of 1080 (1080x1920, 1080x1080 and 1920x1080 all use 118 px words).
Both fonts are variable and are used through their axes, never the default instance (Fraunces
defaults to wght 900, opsz 9, WONK 1) and never a hand-made cut: Fraunces wght 600, opsz = the
pixel size clamped 9..144, SOFT 50, WONK 0; Geist wght 500 for the CTA, 400 for URL and labels.
ffmpeg drawtext cannot set axes, so it gets the owner's pinned instances from ~/bin/font-instance
(files in tools/fonts/instances/, the axes in the name). The Atmosmotion path uses the same pinned
instances until its text layer takes fontVariation (the follow-up to atmosmotion PR 70).

    python -m workman.studio.typesystem [TYPE-SYSTEM.md]    compare the mirror with the file
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType

from .job import StudioError

MD = Path("~/advisorppc-ads-20260925/TYPE-SYSTEM.md").expanduser()

T_ = {
    "headline_font": "Fraunces.ttf", "headline_weight": 600, "headline_px": 118, "brand_px": 132,
    "brand_word": "AdvisorPPC", "split_px": 1000, "line_pitch_px": 132,
    "sans_font": "Geist.ttf", "cta_px": 54, "url_px": 40, "label_px": 44,
    "words_color": "#FFFFFF", "words_opacity": 0.92, "dark": "#0F1112", "offwhite": "#FAF9F5",
    "card_copy": "#F5F4EF", "secondary": "#A8A6A0", "graphite": "#1F1E1D", "accent": "#10B981",
    "led_red": "#EF4444", "led_yellow": "#F5C84C", "led_green": "#10B981",
    "baseline_gap_px": 44, "cta_below_mark_px": 160, "card_hold_s": 3,
    "word_in_frames": 8, "word_rise_px": 6, "word_easing": "easeOutCubic", "word_out_frames": 8,
    "card_copy_rise_px": 6, "card_copy_frames": 10,
    "master_lufs": -14, "master_tp": -1.5,
    "headline_wght": 600, "headline_soft": 50, "headline_wonk": 0, "opsz_min": 9, "opsz_max": 144,
    "cta_wght": 500, "url_wght": 400,
}
TYPE = MappingProxyType(T_)
# Which end-card mark may follow which logo shot (TYPE-SYSTEM.md "Logo signal", logo-kit/): the
# status-hold-green clips end on the green signal, so the card shows the green mark; the status clips
# whose hexagon dissolves end on the plain Ai, so the card shows the plain mark. Matched on the file
# path, first family wins; a clip or mark that matches no family fails validation.
LOGO_FAMILIES = (
    ("plain", (r"dissolve", r"/status-plain/", r"ai-logo-led-.*-plain"),
     (r"ai-plain-", r"ai-logo-3d-[a-z]+-plain-")),
    ("hold-green", (r"logo-led-signal-clean\.mp4$", r"/status-hold-green/", r"ai-logo-led-[a-z0-9-]+\.(mp4|mov|webm)$"),
     (r"ai-logo-3d-[a-z]+-green-", r"ai-lockup-led-green-")),
)


def logo_family(clip: str) -> str | None:
    """The family of a logo shot's clip, or None."""
    return next((f for f, clips, _ in LOGO_FAMILIES if any(re.search(p, clip) for p in clips)), None)


def mark_family(mark: str) -> str | None:
    """The family of an end-card mark image, or None."""
    return next((f for f, _, marks in LOGO_FAMILIES if any(re.search(p, mark) for p in marks)), None)


FONT_INSTANCE = Path(os.environ.get("WORKMAN_FONT_INSTANCE", "~/bin/font-instance")).expanduser()
# Card layout not fixed by the file, chosen once here: mark 300 px, URL 24 px under the CTA,
# bead 40 px under the URL, the stack centred on the frame.
CARD = MappingProxyType({"mark_px": 300, "url_gap_px": 24, "bead_gap_px": 40, "bead_px": 14})


def font_dir() -> Path:
    return Path(os.environ.get("WORKMAN_STUDIO_FONTS", "~/advisorppc-ads-20260925/tools/fonts")).expanduser()


def scale(size: list[int] | tuple[int, int]) -> float:
    """Type scale for a frame: sizes are defined on a 1080 px short side."""
    return min(size) / 1080


def headline_px(phrase: str) -> int:
    """118 px, or 132 px when the phrase is the brand word on its own."""
    return TYPE["brand_px"] if re.sub(r"[^A-Za-z]", "", phrase) == TYPE["brand_word"] else TYPE["headline_px"]


def hex_rgb(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def headline_axes(px: int) -> dict:
    """Fraunces axes for words drawn at `px`: wght 600, opsz = px clamped 9..144, SOFT 50, WONK 0.
    Ordered as font-instance names them (wght, opsz, SOFT, WONK)."""
    return {"wght": T_["headline_wght"], "opsz": max(T_["opsz_min"], min(T_["opsz_max"], int(px))),
            "SOFT": T_["headline_soft"], "WONK": T_["headline_wonk"]}


def sans_axes(role: str) -> dict:
    """Geist axes: wght 500 for the CTA, 400 for URL and labels."""
    if role not in ("cta", "url", "label"):
        raise ValueError(f"Geist role {role!r} is cta, url or label")
    return {"wght": T_["cta_wght"] if role == "cta" else T_["url_wght"]}


def instance_name(family: str, axes: dict) -> str:
    """The file ~/bin/font-instance writes for these axes: <Family>-<tag><value>-....ttf (ints unpadded)."""
    tag = "-".join(f"{k}{int(v) if float(v) == int(v) else v}" for k, v in axes.items()) or "default"
    return f"{family}-{tag}.ttf"


def instance(family: str, axes: dict, timeout: float = 120) -> Path:
    """The pinned instance for these axes, made by ~/bin/font-instance when it is not there yet.
    Fails closed: without the instance the renderer would draw the default instance."""
    if family not in ("Fraunces", "Geist"):
        raise StudioError(f"type system fonts are Fraunces and Geist, not {family!r}")
    out = font_dir() / "instances"
    dst = out / instance_name(family, axes)
    if dst.is_file():
        return dst
    if not FONT_INSTANCE.is_file():
        raise StudioError(f"{dst.name} is missing and {FONT_INSTANCE} is not installed")
    args = [str(FONT_INSTANCE), family, *(f"{k}={v}" for k, v in axes.items()), "--out", str(out)]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise StudioError(f"font-instance {family} {axes} failed: {type(e).__name__}") from e
    if r.returncode or not dst.is_file():
        raise StudioError(f"font-instance {family} {axes} exit {r.returncode}: {(r.stdout + r.stderr)[-300:]}")
    return dst


def headline_font(px: int) -> Path:
    return instance("Fraunces", headline_axes(px))


def sans_font(role: str) -> Path:
    return instance("Geist", sans_axes(role))


# ------------------------------------------------------------------ the markdown

def _one(pattern: str, md: str, cast=str):
    m = re.search(pattern, md, re.S)
    if not m:
        raise ValueError(f"TYPE-SYSTEM.md no longer matches /{pattern}/")
    return tuple(cast(g) for g in m.groups()) if len(m.groups()) > 1 else cast(m.group(1))


def parse_md(md: str) -> dict:
    """The TYPE values as TYPE-SYSTEM.md states them."""
    out: dict = {}
    out["headline_font"] = Path(_one(r"\*\*Fraunces\*\* `([^`]+)`", md)).name
    out["headline_weight"] = _one(r"weight (\d+)\)", md, int)
    out["headline_px"], out["brand_px"] = _one(r"(\d+) px at 1080 wide \((\d+) px for the brand word", md, int)
    out["brand_word"] = _one(r'brand word "([^"]+)"', md)
    out["split_px"], out["line_pitch_px"] = _one(r"wider than (\d+) px, (\d+) px line spacing", md, int)
    out["sans_font"] = Path(_one(r"\*\*Geist\*\* `([^`]+)`", md)).name
    out["cta_px"], out["url_px"], out["label_px"] = _one(r"CTA line (\d+) px, URL (\d+) px, small labels (\d+) px",
                                                          md, int)
    color, pct = _one(r"\| words on picture \| white (#[0-9A-Fa-f]{6}) at (\d+) percent", md)
    out["words_color"], out["words_opacity"] = color, int(pct) / 100
    rows = {"dark": r"\| dark background, cards, logo plate \| (#[0-9A-Fa-f]{6})",
            "offwhite": r"\| off-white background[^|]*\| (#[0-9A-Fa-f]{6})",
            "card_copy": r"\| card copy on dark \| (#[0-9A-Fa-f]{6})",
            "secondary": r"\| secondary copy on dark[^|]*\| (#[0-9A-Fa-f]{6})",
            "graphite": r"\| graphite copy on light \| (#[0-9A-Fa-f]{6})",
            "accent": r"\| the one accent[^|]*\| emerald (#[0-9A-Fa-f]{6})"}
    out.update({k: _one(p, md) for k, p in rows.items()})
    out["led_red"], out["led_yellow"], out["led_green"] = _one(
        r"red (#[0-9A-Fa-f]{6}), yellow (#[0-9A-Fa-f]{6}), green (#[0-9A-Fa-f]{6})", md)
    out["baseline_gap_px"] = _one(r"baseline (\d+) px above the horizon", md, int)
    out["cta_below_mark_px"] = _one(r"CTA (\d+) px below the mark", md, int)
    out["card_hold_s"] = _one(r"Held still (\d+) s", md, int)
    w = _one(r"fade in (\d+) frames with a (\d+) px rise, (\w+); fade out (\d+) frames", md)
    out["word_in_frames"], out["word_rise_px"], out["word_easing"], out["word_out_frames"] = \
        int(w[0]), int(w[1]), w[2], int(w[3])
    out["card_copy_rise_px"], out["card_copy_frames"] = _one(r"rise (\d+) px with fade over (\d+) frames", md, int)
    ax = _one(r"\| words on picture, \d+ px \| wght (\d+), opsz \d+ \(= pixel size, cap (\d+)\), SOFT (\d+), "
              r"WONK (\d+)", md, int)
    out["headline_wght"], out["opsz_max"], out["headline_soft"], out["headline_wonk"] = ax
    out["opsz_min"] = _one(r"clamped (\d+)\.\.\d+", md, int)
    out["cta_wght"] = _one(r"\| card CTA \d+ px \| \| (\d+) \|", md, int)
    out["url_wght"] = _one(r"\| URL \d+ px, labels \d+ px \| \| (\d+) \|", md, int)
    out["master_lufs"], out["master_tp"] = _one(r"Master (-?\d+) LUFS, TP (-?\d+(?:\.\d+)?)", md, float)
    out["master_lufs"] = int(out["master_lufs"])
    return out


def drift(md: str) -> dict:
    """{key: (mirror, file)} for every value that differs; {} when the mirror matches the file."""
    got = parse_md(md)
    return {k: (TYPE[k], got[k]) for k in got if TYPE.get(k) != got[k]}


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else MD
    d = drift(path.read_text())
    print(json.dumps({"ok": not d, "file": str(path), "drift": {k: list(v) for k, v in d.items()}}, indent=1))
    sys.exit(1 if d else 0)
