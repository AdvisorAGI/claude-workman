"""Pins typesystem.TYPE to ~/advisorppc-ads-20260925/TYPE-SYSTEM.md: an owner edit to the file fails
this test until the mirror is updated, so no builder can drift from the one type system."""
import pytest

from studio_synth import fonts_present
from workman.studio import typesystem as ts


@pytest.mark.skipif(not ts.MD.is_file(), reason="TYPE-SYSTEM.md not on this machine")
def test_mirror_matches_the_type_system_file():
    assert ts.drift(ts.MD.read_text()) == {}


def test_parse_md_reads_every_mirrored_value_and_flags_a_change():
    md = ts.MD.read_text() if ts.MD.is_file() else pytest.skip("TYPE-SYSTEM.md not on this machine")
    got = ts.parse_md(md)
    assert set(got) == set(ts.TYPE) and got["words_opacity"] == 0.92 and got["dark"] == "#0F1112"
    assert ts.drift(md.replace("118 px at 1080 wide", "120 px at 1080 wide")) == {"headline_px": (118, 120)}
    with pytest.raises(ValueError):
        ts.parse_md("# not the type system")


def test_brand_word_and_scale():
    assert ts.headline_px("AdvisorPPC") == 132 and ts.headline_px("AdvisorPPC.") == 132
    assert ts.headline_px("AdvisorPPC shows you where.") == 118
    assert ts.scale([1080, 1920]) == ts.scale([1920, 1080]) == ts.scale([1080, 1080]) == 1.0
    assert ts.hex_rgb("#10B981") == (16, 185, 129)


@pytest.mark.skipif(not fonts_present(), reason="type system fonts not on this machine")
def test_headline_instance_is_weight_600_soft_display():
    from PIL import ImageFont
    p = ts.headline_font()
    assert p.is_file() and p == ts.headline_font()            # cached, cut once
    names = {a["name"] for a in ImageFont.truetype(str(p), 40).get_variation_axes()}
    assert b"Weight" not in names and b"Softness" not in names and b"Optical Size" not in names
