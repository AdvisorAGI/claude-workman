"""Pins the assembly invariants (cuts on the frame grid, crop exactly as the builders did, words on
onsets and horizons, green frame by colour, formats reuse the takes) and the QC rules (fact IDs
exist, conflicts flagged, no untraced number, cuts within one frame, eye checks never auto-pass)."""
import copy
import json

import pytest

import studio_synth
from workman.studio import assemble as a
from workman.studio import inspect as ins
from workman.studio import qc


def spec():
    return a.with_defaults(studio_synth.spec())


def test_synthetic_spec_is_valid_and_cuts_are_on_the_grid():
    assert a.validate(studio_synth.spec()) == []
    assert a.cuts(spec()) == [(0, 32), (32, 60), (60, 66), (66, 72)]


def test_validate_catches_broken_specs():
    s = studio_synth.spec()
    bad = copy.deepcopy(s)
    bad["end"] = 2.9
    assert any("last cut frame" in e for e in a.validate(bad))
    bad = copy.deepcopy(s)
    bad["vo"][0]["words"] = ["Look away."]
    assert any("not spoken" in e for e in a.validate(bad))
    bad = copy.deepcopy(s)
    bad["vo"][1]["text"] = "Every dollar counts \u2014 really."
    bad["vo"][1]["words"] = ["Every dollar counts"]
    assert any("copy rule" in e for e in a.validate(bad))
    bad = copy.deepcopy(s)
    bad["vo"][0]["at"] = 1.5
    assert any("outside shot" in e for e in a.validate(bad))
    bad = copy.deepcopy(s)
    bad["shots"][1]["end_frame"] = 20
    assert any("strictly increasing" in e for e in a.validate(bad))
    bad = copy.deepcopy(s)
    bad["vo"][0]["words"] = ["Look: closer."]
    bad["vo"][0]["text"] = "Look: closer."
    assert any("escaping" in e for e in a.validate(bad))
    assert any("format" in e for e in a.validate({**s, "formats": ["4:3"]}))
    assert any("typesystem" in e for e in a.validate({**s, "fonts": {"serif": "x.ttf"}}))
    assert any("renderer" in e for e in a.validate({**s, "renderer": "html"}))
    bad = copy.deepcopy(s)
    bad["shots"][3]["card"]["style"] = "white"
    del bad["shots"][3]["card"]["mark"]
    errs = a.validate(bad)
    assert any("dark" in e for e in errs) and any("Ai mark" in e for e in errs)


def test_cover_crops_grok_clips_exactly_like_the_builders():
    assert a.cover(1088, 1920, 1080, 1920)["vf"] == "crop=1080:1920:4:0"   # build_v3: crop=1080:1920:4:0
    sq = a.cover(1088, 1920, 1080, 1080)
    assert sq["vf"] == "crop=1080:1080:4:420" and a.mapped_horizon(0.5, sq, 1080) == 0.5
    wide = a.cover(1088, 1920, 1920, 1080)
    assert wide["vf"].startswith("scale=1920:") and wide["sw"] == 1920
    assert a.fit(1920, 1088, 1080, 1920) == {"w": 1080, "h": 612, "x": 0, "y": 654,
                                             "vf": "scale=1080:612,pad=1080:1920:0:654:black"}   # partner LOGO_Y 654


def test_derive_reuses_the_base_takes_and_type_scales_on_the_short_side():
    d = a.derive(studio_synth.spec(), "1:1")
    assert d["size"] == [1080, 1080] and d["name"] == "synth-v1-1x1" and d["takes_name"] == "synth-v1"
    assert "formats" not in d and a.validate(d) == []
    assert a.metrics([1080, 1920])["gap"] == a.metrics([1920, 1080])["gap"] == 44
    assert a.metrics([540, 960]) == {"k": 0.5, "gap": 22, "split": 500, "pitch": 66, "rise": 3.0, "cta": 27, "url": 20}
    with pytest.raises(ValueError):
        a.derive(studio_synth.spec(), "4:3")


def test_lines_for_splits_where_the_wider_line_is_narrowest():
    width = len  # one px per character
    assert a.lines_for("end to end.", width, 1000) == ["end to end."]
    assert a.lines_for("Not every click is a customer.", width, 20) == ["Not every click", "is a customer."]
    with pytest.raises(ValueError):
        a.lines_for("Supercalifragilistic", width, 5)


def test_words_plan_phrases_hand_over_on_onsets_and_hold_before_the_cut():
    s = spec()
    s["vo"][1]["text"] = "Every dollar. AdvisorPPC."
    s["vo"][1]["words"] = ["Every dollar.", "AdvisorPPC."]
    plan = a.words_plan(s, [[0.1], [1.4, 1.9]], lambda t, px: len(t), {1: 0.6})
    assert [(w["txt"], w["a"], w["b"]) for w in plan] == [("Look closer.", 0.1, round(32 / 24 - 0.25, 3)),
                                                          ("Every dollar.", 1.4, 1.9), ("AdvisorPPC.", 1.9, 2.25)]
    assert plan[1]["hz"] == 0.6 and plan[0]["hz"] == 0.5
    assert [w["size"] for w in plan] == [118, 118, 132]            # the brand word is 132 px (TYPE-SYSTEM.md)
    assert plan[0]["baseline"] == 960 - 44 and plan[1]["baseline"] == int(0.6 * 1920) - 44


def test_words_plan_two_lines_are_132_px_apart_and_scale_with_the_frame():
    s = spec()
    plan = a.words_plan(s, [[0.1], [1.4]], lambda t, px: len(t) * px / 2, {})
    two = [w for w in plan if w["vo"] == 1]
    assert [w["txt"] for w in two] == ["Every dollar", "counts."] and two[0]["lift"] - two[1]["lift"] == 132
    sq = a.words_plan(a.derive(studio_synth.spec(), "1:1"), [[0.1], [1.4]], lambda t, px: len(t) * px / 2, {})
    assert sq[0]["size"] == 118 and sq[0]["baseline"] == 540 - 44   # 1080 short side: same px as 9:16


def test_drawtext_follows_the_type_system_and_bed_shape_matches_the_builders():
    w = {"txt": "Look closer.", "a": 5.625, "b": 10.35, "baseline": 916, "size": 118}
    dt = a.drawtext(w, "F.ttf", 24, 6.0)
    assert "fontcolor=0xFFFFFF" in dt and "alpha='0.92*if(lt(t\\,5.625+0.3333)" in dt   # 92 percent, 8 frames
    assert "1-pow(1-(t-5.625)/0.3333\\,3)" in dt                                       # easeOutCubic in
    assert "y=916-ascent+6.00*pow(1-min(1\\,(t-5.625)/0.3333)\\,3)" in dt              # baseline + 6 px rise
    assert "enable='between(t\\,5.625\\,10.350)'" in dt
    assert a.bed_shape(16.5, 10.958).endswith("afade=t=out:st=15.000:d=1.500")
    assert "exp(-pow((t-10.958)/3.0\\,2))" in a.bed_shape(16.5, 10.958)


def test_green_detection_is_relative_and_ignores_red_and_yellow():
    px = bytes([200, 30, 30, 220, 200, 40, 20, 220, 60, 0, 0, 0])  # red, yellow, green, black
    assert a.green_counts(px, 4, 1) == [1]
    assert a.first_green([0, 0, 3, 290, 300, 460, 460]) == 3
    with pytest.raises(ValueError):
        a.first_green([0, 0])
    assert a.green_px([(20, 200, 90), (200, 200, 30), (220, 40, 40), (0, 0, 0)]) == 1


def test_probe_errors_vo_errors_and_onset_frames():
    good = {"streams": [{"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920,
                         "r_frame_rate": "24/1"}, {"codec_type": "audio", "codec_name": "aac"}],
            "format": {"duration": "16.500000"}}
    assert a.probe_errors(good, 1080, 1920, 24, 16.5) == []
    assert a.probe_errors({**good, "format": {"duration": "16.6"}}, 1080, 1920, 24, 16.5) == ["duration 16.6"]
    ok = {"n": 2, "place": 5.3, "vo_dur": 4.6, "shot": [5.0, 10.0]}
    assert a.vo_errors([ok]) == [] and a.vo_errors([{**ok, "vo_dur": 4.8}]) and a.vo_errors([{**ok, "place": 4.9}])
    before, after = a.onset_frames(1.4, 24)
    assert before / 24 < 1.4 and after == int(1.8 * 24)


def test_band_margins_scale_with_type():
    w = {"baseline": 916, "size": 118}
    assert a.band(w) == (916 + 8 - 118 - 40, 916 + 8)
    assert a.band({"baseline": 500, "size": 59}) == (500 + 4 - 59 - 20, 500 + 4)


def test_card_layout_is_the_dark_stack_with_cta_160_under_the_mark():
    L = a.card_layout([1080, 1920])
    assert L["mark"]["px"] == 300 and L["cta"]["y"] == L["mark"]["y"] + 300 + 160
    assert L["cta"]["px"] == 54 and L["url"]["px"] == 40 and L["url"]["y"] == L["cta"]["y"] + 54 + 24
    assert L["bead"]["y"] == L["url"]["y"] + 40 + 40 and L["mark"]["x"] == 390
    top, bottom = L["mark"]["y"], L["bead"]["y"] + L["bead"]["px"]
    assert abs((1920 - bottom) - top) <= 1                            # centred
    wide = a.card_layout([1920, 1080])
    assert wide["mark"]["px"] == 300 and wide["mark"]["x"] == 810


def test_plate_and_card_comps_carry_the_type_system():
    w = {"txt": "Look closer.", "a": 1.0, "b": 3.0, "baseline": 916, "size": 118, "ink": 88}
    comp = a.plate_comp([w], [1080, 1920], 24, 4.0, {118: "/f/Fraunces-wght600-opsz118-SOFT50-WONK0.ttf"}, 6.0)
    assert comp["assets"]["headline118"]["src"].endswith("opsz118-SOFT50-WONK0.ttf")
    layer = comp["layers"][0]
    assert comp["background"]["color"] == "#000000" and layer["color"] == "#FFFFFF"
    assert layer["opacity"]["kf"][1] == {"at": 32.0, "value": 0.92, "easing": "easeOutCubic"}
    assert layer["opacity"]["kf"][-1]["at"] == 72.0 and layer["opacity"]["kf"][-2]["at"] == 64.0
    ys = layer["transform"]["y"]["kf"]
    assert ys[0]["value"] == 916 - 88 + 6 and ys[1]["value"] == 828
    card = a.card_comp({"cta": "Free account audit", "url": "advisorppc.com"}, [1080, 1920], 24, 72, "/m.png",
                       "/Geist-wght500.ttf", "/Geist-wght400.ttf")
    assert card["assets"]["cta"]["src"] == "/Geist-wght500.ttf" and card["assets"]["url"]["src"] == "/Geist-wght400.ttf"
    assert card["background"]["color"] == "#0F1112" and card["duration"] == 3.0
    ids = {x["id"]: x for x in card["layers"]}
    assert ids["cta"]["color"] == "#F5F4EF" and ids["url"]["color"] == "#A8A6A0" and ids["bead"]["fill"] == "#10B981"
    assert ids["cta"]["opacity"]["kf"][1]["at"] == 10          # card copy fades over 10 frames


# ------------------------------------------------------------------ qc

FACTS_MD = ("| ID | Fact |\n|---|---|\n| F01 | a |\n| F06 | b |\n| F12 | c |\n\n"
            "## Conflicts between pages\n- Ads only say it works inside Claude and ChatGPT (F06).\n\n"
            "## Claim trace\n| x | F99 |\n")


def test_parse_facts_reads_ids_and_conflict_section_only():
    known, conflicts = qc.parse_facts(FACTS_MD)
    assert known == {"F01", "F06", "F12"} and conflicts == {"F06"}


def test_fact_rows_unknown_ids_fail_conflicts_warn_untraced_numbers_fail():
    known, conflicts = qc.parse_facts(FACTS_MD)
    s = spec()
    rows = {r["check"]: r["result"] for r in qc.fact_rows(s, {"SCRIPT.md": "F01 F12"}, known, conflicts, "FACTS.md")}
    assert set(rows.values()) == {"PASS"}
    rows = {r["check"]: r for r in qc.fact_rows(s, {"BRIEF.md": "uses F06 and F77"}, known, conflicts, "F")}
    assert rows["Claims: every cited fact ID exists in FACTS.md"]["result"] == "FAIL"
    assert "F77" in rows["Claims: every cited fact ID exists in FACTS.md"]["evidence"]
    assert rows["Claims: no cited ID is in FACTS.md Conflicts"]["result"] == "WARN"
    s2 = copy.deepcopy(s)
    s2["vo"][0] = {**s2["vo"][0], "text": "Save 30 percent.", "words": ["Save 30 percent."], "opinion": True}
    s2["vo"][1].pop("facts")
    rows = {r["check"]: r["result"] for r in qc.fact_rows(s2, {}, known, conflicts, "F")}
    assert rows["Claims: no number without a fact ID"] == "FAIL"
    assert rows["Claims: every VO line has a fact ID or is marked opinion"] == "FAIL"


def test_cut_rows_one_frame_tolerance_and_no_cut_inside_a_shot():
    s = spec()
    planned = [32 / 24, 60 / 24, 66 / 24]
    ok = qc.cut_rows(s, [round(t + 0.03, 3) for t in planned])
    assert [r["result"] for r in ok] == ["PASS", "PASS"]
    bad = qc.cut_rows(s, planned[:2] + [2.0])
    assert [r["result"] for r in bad] == ["WARN", "FAIL"]      # undetectable cut warns, extra cut fails
    assert qc.parse_cuts("1.333 s  score 0.64\n2.500 s  score 1.00\n") == [1.333, 2.5]


def test_transcript_rows_compare_letters_and_onsets():
    tr = ins.format_transcript(studio_synth.canned_transcript())
    tm = {"words": [{"txt": "Look closer.", "a": 0.1, "vo": 0},
                    {"txt": "Every dollar", "a": 1.4, "vo": 1}, {"txt": "counts.", "a": 1.4, "vo": 1}]}
    assert [r["result"] for r in qc.transcript_rows(spec(), tm, tr)] == ["PASS", "PASS"]
    late = {"words": [{"txt": "Look closer.", "a": 0.25, "vo": 0}]}
    assert qc.transcript_rows(spec(), late, tr)[1]["result"] == "FAIL"
    early = {"words": [{"txt": "Look closer.", "a": 0.05, "vo": 0}]}
    assert qc.transcript_rows(spec(), early, tr)[1]["result"] == "FAIL"   # never before the voice
    assert [r["result"] for r in qc.transcript_rows(spec(), tm, "")] == ["PENDING", "PENDING"]
    assert qc.letters("AdvisorPPC shows") == qc.letters("Advisor PPC shows.")


def test_eye_checks_never_pass_on_measurements_alone():
    rows = qc.eye_rows(None, "frames.jpg")
    assert {r["result"] for r in rows} == {"PENDING"}
    assert qc.verdict([qc.row("x", True, "", "e")] + rows) == "PENDING REVIEW"
    done = qc.eye_rows({k: {"pass": True, "evidence": "tiles 1-24"} for k in qc.EYE}, "f")
    assert qc.verdict(done) == "PASS"
    assert qc.verdict(done + [qc.row("y", False, "", "e", warn=True)]) == "PASS"      # WARN does not fail
    assert qc.verdict(done + [qc.row("z", False, "", "e")]) == "FAIL"


def test_qc_md_first_line_is_result_and_rows_are_pipe_safe():
    md = qc.render_md("ad-v1", [qc.row("c", False, "a|b", "editor")], "FAIL")
    lines = md.splitlines()
    assert lines[0] == "RESULT: FAIL" and "| 1 | c | FAIL | a/b | editor |" in md
    assert lines[-1] == "Owner still hears the final cut."


# ------------------------------------------------------------------ inspect parsers

def test_inspect_parsers():
    assert ins.sheet_grid(3.0) == (8, 2) and ins.sheet_grid(0.1) == (8, 1)
    meta = "frame:0    pts:12288  pts_time:1.333\nlavfi.scene_score=0.640000\nframe:1 pts:1 pts_time:2.5\n" \
           "lavfi.scene_score=1.000000\n"
    assert ins.parse_scenes(meta) == [(1.333, 0.64), (2.5, 1.0)]
    assert ins.format_cuts([(1.333, 0.64)]) == "1.333 s  score 0.64\n"
    err = ("[Parsed_ebur128_0] t: 0.1  TARGET:-23 LUFS    M: -30.1 S:-120.7     I: -30.1 LUFS\n"
           "[Parsed_ebur128_0] t: 0.3  TARGET:-23 LUFS    M: -29.0 S:-120.7     I: -29.9 LUFS\n"
           "[Parsed_ebur128_0] t: 0.7  TARGET:-23 LUFS    M: -20.0 S:-120.7     I: -25.0 LUFS\n"
           "  Integrated loudness:\n    I:         -14.0 LUFS\n  True peak:\n    Peak:       -1.4 dBFS\n")
    out = ins.parse_loudness(err)
    assert "  0.10 s  M  -30.1 LUFS" in out and "0.30 s" not in out and "0.70 s" in out
    assert "I:         -14.0 LUFS" in out and "Peak:" in out
    tr = ins.format_transcript({"language_code": "eng", "text": "Hi.", "words": [
        {"start": 0.1, "end": 0.3, "type": "word", "text": "Hi."}, {"type": "spacing", "text": " "}]})
    assert tr == "language: eng | text: Hi.\n  0.10-  0.30  word   Hi.\n"
    assert qc.transcript_words(tr) == [(0.1, "Hi.")]


def test_spec_json_round_trips():
    assert json.loads(json.dumps(studio_synth.spec(["1:1"])))["formats"] == ["1:1"]


def test_source_shot_follows_the_output_frame_across_cuts():
    s = spec()
    assert a.source_shot(s, 31) == 0 and a.source_shot(s, 32) == 1       # before-frame near a cut: previous clip
    assert a.source_shot(s, 60) is None and a.source_shot(s, 71) is None  # logo and card have no source clip
    assert list(a.rgb_triples(bytes([1, 2, 3, 4, 5, 6]))) == [(1, 2, 3), (4, 5, 6)]
