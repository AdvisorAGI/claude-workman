"""The AdvisorPPC video type system: the one source of fonts, sizes, colours, placement and motion.

Mirrors ~/advisorppc-ads-20260925/TYPE-SYSTEM.md (owner order 2026-09-25: one font, one colour
set, no random). assemble.py reads every type value from TYPE and nowhere else; a spec cannot
override a font, size, colour or timing. `parse_md` reads the same values out of the markdown so
tests/studio pins TYPE to the file: when the owner edits the file, the test fails until this
mirror is updated.

Sizes are px on the short side of 1080 (1080x1920, 1080x1080 and 1920x1080 all use 118 px words).
Fraunces ships as a variable font whose default instance is weight 900, optical size 9; the
headline instance (weight 600, soft, display optical size) is cut once with fontTools into
~/.cache/workman-studio/fonts and used by both renderers, so drawtext and Atmosmotion draw the
same glyphs.

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

TYPE = MappingProxyType({
    "headline_font": "Fraunces.ttf", "headline_weight": 600, "headline_px": 118, "brand_px": 132,
    "brand_word": "AdvisorPPC", "split_px": 1000, "line_pitch_px": 132,
    "sans_font": "Geist.ttf", "cta_px": 54, "url_px": 40, "label_px": 44,
    "words_color": "#FFFFFF", "words_opacity": 0.92, "dark": "#0F1112", "offwhite": "#FAF9F5",
    "card_copy": "#F5F4EF", "secondary": "#A8A6A0", "graphite": "#1F1E1D", "accent": "#10B981",
    "led_red": "#EF4444", "led_yellow": "#F5C84C", "led_green": "#10B981",
    "baseline_gap_px": 44, "cta_below_mark_px": 160, "card_hold_s": 3,
    "word_in_frames": 8, "word_rise_px": 6, "word_easing": "easeOutCubic", "word_out_frames": 8,
    "card_copy_rise_px": 6, "card_copy_frames": 10, "led_step_s": 0.2,
    "master_lufs": -14, "master_tp": -1.5,
})
# Fraunces axes for the headline instance: the file says weight 600, "optical size soft";
# read as SOFT 100 at the display optical size (118 px is display type). WONK stays default.
HEADLINE_AXES = {"wght": 600, "SOFT": 100, "opsz": 144}
# Card layout not fixed by the file, chosen once here: mark 300 px, URL 24 px under the CTA,
# bead 40 px under the URL, the stack centred on the frame.
CARD = MappingProxyType({"mark_px": 300, "url_gap_px": 24, "bead_gap_px": 40, "bead_px": 14})


def font_dir() -> Path:
    return Path(os.environ.get("WORKMAN_STUDIO_FONTS", "~/advisorppc-ads-20260925/tools/fonts")).expanduser()


def cache_dir() -> Path:
    return Path(os.environ.get("WORKMAN_STUDIO_CACHE", "~/.cache/workman-studio")).expanduser()


def scale(size: list[int] | tuple[int, int]) -> float:
    """Type scale for a frame: sizes are defined on a 1080 px short side."""
    return min(size) / 1080


def headline_px(phrase: str) -> int:
    """118 px, or 132 px when the phrase is the brand word on its own."""
    return TYPE["brand_px"] if re.sub(r"[^A-Za-z]", "", phrase) == TYPE["brand_word"] else TYPE["headline_px"]


def hex_rgb(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def sans_font() -> Path:
    p = font_dir() / TYPE["sans_font"]
    if not p.is_file():
        raise StudioError(f"type system font {p} is missing")
    return p


_INSTANCE = ("import sys\nfrom fontTools.ttLib import TTFont\nfrom fontTools.varLib.instancer import "
             "instantiateVariableFont\nf = TTFont(sys.argv[1])\naxes = {a.axisTag for a in f['fvar'].axes}\n"
             "want = {k: float(v) for k, v in (x.split('=') for x in sys.argv[3:])}\n"
             "instantiateVariableFont(f, {k: v for k, v in want.items() if k in axes}, inplace=True)\n"
             "f.save(sys.argv[2])\n")


def headline_font() -> Path:
    """Fraunces cut to the headline instance (weight 600, soft, display opsz), cached. Fails closed when
    fontTools is not available: drawing the variable default would be weight 900, off the type system."""
    src = font_dir() / TYPE["headline_font"]
    if not src.is_file():
        raise StudioError(f"type system font {src} is missing")
    tag = "-".join(f"{k}{v}" for k, v in HEADLINE_AXES.items())
    dst = cache_dir() / "fonts" / f"Fraunces-{tag}-{int(src.stat().st_mtime)}.ttf"
    if dst.is_file():
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp.ttf")
    args = [str(src), str(tmp), *(f"{k}={v}" for k, v in HEADLINE_AXES.items())]
    for py in (sys.executable, "/usr/bin/python3"):
        try:
            r = subprocess.run([py, "-c", _INSTANCE, *args], capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0 and tmp.is_file():
            tmp.replace(dst)
            return dst
    raise StudioError("cannot cut the Fraunces headline instance: fontTools is not importable "
                      "(pip install fonttools, or apt install python3-fonttools)")


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
    out["led_step_s"] = _one(r"hard steps, ([\d.]+) s each", md, float)
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
