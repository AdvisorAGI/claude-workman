"""Pins the VO timing invariants of build_v3 / build_series: words land on their spoken onset,
VO never crosses a cut, takes are chosen by fit then read time; ElevenLabs calls are bounded
and never leak the key."""
import base64
import io
import json
import urllib.error

import pytest

from workman.studio import voice as v
from workman.studio.job import StudioError


def align(text, t0=0.1, step=0.05):
    chars = list(text)
    return {"characters": chars,
            "character_start_times_seconds": [round(t0 + k * step, 3) for k in range(len(chars))],
            "character_end_times_seconds": [round(t0 + (k + 1) * step, 3) for k in range(len(chars))]}


def test_onset_is_first_whole_word_not_substring():
    al = align("Everyone. Every dollar")
    assert v.word_onset(al, "Every") == al["character_start_times_seconds"][10]


def test_break_tag_is_not_speech_and_not_a_word():
    t = 'Look closer. <break time="0.7s" /> Every dollar.'
    al = align(t)
    assert v.spoken(t) == "Look closer. Every dollar."
    assert v.speech_span(al)[1] == al["character_end_times_seconds"][len(t) - 2]  # "r", not "."
    assert v.pauses(al) == [round(al["character_start_times_seconds"][t.index("Every")]
                                  - al["character_end_times_seconds"][t.index("r.")], 3)]
    with pytest.raises(ValueError):
        v.word_onset(align('Wait. <break time="0.6s" /> Now.'), "time")


def test_placement_lands_first_spoken_char_on_target_and_never_negative():
    al = align("Look", t0=0.12)
    assert abs(v.placement(5.6, al) + 0.12 - 5.6) < 1e-9
    assert v.placement(0.05, al) == 0.0


def test_overrun_uses_whole_take_against_cut():
    assert not v.overruns(0.8, 4.49, 127 / 24)
    assert v.overruns(0.8, 4.60, 127 / 24)


def test_vo_takes_add_breaths_only_at_full_stops_and_say_the_same_words():
    assert v.vo_texts("Not every click is a customer.") == ["Not every click is a customer."]
    t = v.vo_texts("Now you can ask it. In Claude, or ChatGPT.")
    assert t[0] == 'Now you can ask it. <break time="0.6s" /> In Claude, or ChatGPT.' and len(t) == 3
    assert {v.spoken(x) for x in t} == {"Now you can ask it. In Claude, or ChatGPT."}


def test_phrase_onset_ignores_punctuation_masks_breaks_and_needs_whole_words():
    al = align("Shield scores every paid click, and excludes")
    assert v.phrase_onset(al, "Shield scores every paid click.") == al["character_start_times_seconds"][0]
    t = "spend to end, end to end."
    assert v.phrase_onset(align(t), "end to end.") == align(t)["character_start_times_seconds"][t.rindex("end to end")]


def test_take_fit_and_choose_prefer_first_readable_fit():
    al = align("Now you can ask it.", t0=0.1)
    ok, read = v.take_fit(al, 1.2, 5.4, 10.0, "ask it.")
    assert ok and abs(read - (10.0 - v.HOLD - (5.3 + al["character_start_times_seconds"][12]))) < 1e-3
    assert not v.take_fit(al, 4.8, 5.4, 10.0, "ask it.")[0]
    assert v.choose([(True, 1.2), (True, 1.6), (True, 2.0)]) == 1
    assert v.choose([(True, 1.1), (True, 1.3), (False, 2.0)]) == 1
    assert v.choose([(False, 2.0)]) is None and v.choose([]) is None


def test_audible_end_and_attack_time():
    rate = 1000
    loud, hiss = [8000, -8000] * 5, [20, -20] * 5
    assert v.audible_end(loud * 30 + hiss * 40, rate) == 0.3
    with pytest.raises(ValueError):
        v.audible_end(hiss * 5, rate)
    assert v.attack_time([0, 5, -40, 1000, 200], 1000) == 0.003
    with pytest.raises(ValueError):
        v.attack_time([0, 0], 1000)


def test_text_never_appears_before_the_voice_is_heard():
    rate = 1000
    take = [0] * 120 + [9000, -9000] * 200        # alignment says 0.0 s; the voice is heard at 0.12 s
    heard = v.audible_onset(take, rate, 0.0)
    assert heard == 0.12
    assert v.text_onset(0.0, heard) == 0.12 and v.text_onset(0.0, heard, lead=0.05) == 0.17
    assert v.text_onset(0.3, 0.2) == 0.3              # heard earlier than aligned: keep the alignment
    assert v.audible_onset([0] * 2000, rate, 0.0) is None and v.text_onset(0.5, None) == 0.5
    assert v.audible_onset([0] * 600 + [9000], rate, 0.0) is None   # nothing within the 0.5 s search
    hiss = [100, -100] * 125 + [9000, -9000] * 50                  # -50 dBFS room tone is not the voice
    assert v.audible_onset(hiss, rate, 0.0) == 0.25


def test_loudness_gates_and_parsers():
    assert v.loudness_errors(-14.3, -1.2) == [] and len(v.loudness_errors(-13.2, -0.8)) == 2
    assert v.loudness_errors(float("nan"), -2.0)  # an unmeasured file never passes
    err = ("junk\n[Parsed_ebur128_0] Summary:\n  Integrated loudness:\n    I:         -14.1 LUFS\n"
           "  True peak:\n    Peak:       -1.6 dBFS\n")
    assert v.parse_ebur128(err) == (-14.1, -1.6)


def test_master_gain_converges_within_a_tenth_and_fails_closed():
    measured = []

    def measure(g):   # a limiter that loses 0.6 dB of whatever gain it gets
        measured.append(g)
        return (-20.0, -9.0) if g is None else (-20.0 + g - 0.6, -2.6)

    gain, passes = v.solve_gain(measure)
    assert abs(-20.0 + gain - 0.6 + 14.0) < 0.1 and len(passes) == 2 and measured[0] is None
    with pytest.raises(StudioError, match="did not converge"):
        v.solve_gain(lambda g: (-30.0, -9.0), tries=3)
    chain = v.master_chain(6.5, 16.5)
    assert chain.startswith("volume=6.50dB,aresample=192000,alimiter=limit=0.7499") and chain.endswith("atrim=0:16.5")


def test_copy_rules_reject_em_dash_and_model_names_but_not_chatgpt():
    for bad in ("Built on GPT", "Partner \u2014 now", "Ask Opus."):
        assert v.copy_problem(bad), bad
    assert v.copy_problem("In Claude, or ChatGPT.") == ""


def test_tts_body_bounds_and_copy_rules():
    b = v.tts_body("Hello there.", previous_text="x" * 900, next_text="y" * 900)
    assert len(b["previous_text"]) == len(b["next_text"]) == v.MAX_TEXT and b["model_id"] == v.MODEL
    with pytest.raises(ValueError):
        v.tts_body("")
    with pytest.raises(ValueError):
        v.tts_body("Built on GPT.")


def test_tts_writes_take_and_alignment(tmp_path):
    al = align("Look closer.")
    calls = []

    def fake(path, body):
        calls.append((path, body))
        return {"audio_base64": base64.b64encode(b"ID3fake").decode(), "alignment": al}

    meta = v.tts("Look closer.", tmp_path / "a.mp3", tmp_path / "a.json", call=fake)
    assert calls[0][0].startswith(f"/v1/text-to-speech/{v.BRIAN}/with-timestamps")
    assert (tmp_path / "a.mp3").read_bytes() == b"ID3fake"
    assert json.loads((tmp_path / "a.json").read_text())["alignment"] == al == meta["alignment"]
    with pytest.raises(StudioError):
        v.tts("Look closer.", tmp_path / "b.mp3", tmp_path / "b.json", call=lambda *a: {"alignment": al})


def test_sound_keeps_existing_file_and_bounds_duration(tmp_path):
    p = tmp_path / "bed.mp3"
    p.write_bytes(b"x")
    kept = v.sound(p, "bed", 20, call=lambda *a, **k: pytest.fail("regenerated"))
    assert kept == {"path": str(p), "generated": False}
    with pytest.raises(ValueError):
        v.sound(tmp_path / "long.mp3", "bed", 60, call=lambda *a, **k: b"")


def test_eleven_retries_bounded_and_error_never_holds_the_key():
    tries, sleeps = [], []

    def opener(req, timeout):
        tries.append(req.get_header("Xi-api-key"))
        raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, io.BytesIO(b"overloaded"))

    with pytest.raises(StudioError) as e:
        v.eleven("/v1/sound-generation", {"text": "x"}, opener=opener, sleep=sleeps.append, tries=9)
    assert len(tries) == 3 and sleeps == [5, 10]       # tries capped at 3, backoff 5 s then 10 s
    assert "test-key-not-real" not in str(e.value) and "HTTP 503" in str(e.value)


def test_eleven_key_from_env_file_only_when_env_empty(monkeypatch, tmp_path):
    f = tmp_path / "e.env"
    f.write_text("export ELEVENLABS_API_KEY='abc123'\n")
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    assert v.eleven_key(f) == "abc123"
    with pytest.raises(StudioError) as e:
        v.eleven_key(tmp_path / "missing.env")
    assert "abc123" not in str(e.value)


def test_multipart_body_carries_fields_and_file():
    body, ctype = v.multipart({"model_id": "scribe_v1"}, "file", "a.mp3", b"\x00\x01", "audio/mpeg")
    boundary = ctype.split("boundary=")[1]
    assert body.startswith(f"--{boundary}".encode()) and body.endswith(f"--{boundary}--\r\n".encode())
    assert b'name="model_id"\r\n\r\nscribe_v1' in body and b'filename="a.mp3"' in body and b"\x00\x01" in body
