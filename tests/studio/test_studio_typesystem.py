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


def test_axes_follow_the_owner_rule():
    assert ts.headline_axes(118) == {"wght": 600, "opsz": 118, "SOFT": 50, "WONK": 0}
    assert ts.headline_axes(200)["opsz"] == 144 and ts.headline_axes(4)["opsz"] == 9     # clamped 9..144
    assert ts.sans_axes("cta") == {"wght": 500} and ts.sans_axes("url") == ts.sans_axes("label") == {"wght": 400}
    with pytest.raises(ValueError):
        ts.sans_axes("headline")


def test_instance_name_encodes_the_axes_like_font_instance():
    assert ts.instance_name("Fraunces", ts.headline_axes(118)) == "Fraunces-wght600-opsz118-SOFT50-WONK0.ttf"
    assert ts.instance_name("Geist", ts.sans_axes("cta")) == "Geist-wght500.ttf"
    assert ts.instance_name("Fraunces", {"opsz": 12.5}) == "Fraunces-opsz12.5.ttf"


@pytest.mark.skipif(not fonts_present(), reason="type system fonts not on this machine")
def test_pinned_instances_are_the_files_named_for_the_axes():
    from PIL import ImageFont
    for px in (118, 132):
        p = ts.headline_font(px)
        assert p.name == ts.instance_name("Fraunces", ts.headline_axes(px)) and p.parent.name == "instances"
        f = ImageFont.truetype(str(p), px)
        try:
            axes = f.get_variation_axes()
        except OSError:      # a fully pinned instance has no variation tables at all
            axes = []
        assert axes == [], axes
    assert ts.sans_font("cta").name == "Geist-wght500.ttf" and ts.sans_font("url").name == "Geist-wght400.ttf"
    with pytest.raises(ts.StudioError):
        ts.instance("Helvetica", {"wght": 400})
