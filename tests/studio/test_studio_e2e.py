"""End to end on a 3 s synthetic job, no network: assemble (render + verify, 9:16 and 1:1), inspect with a
canned transcript, qc to RESULT: PASS once the eye checks are passed in; the same through the Atmosmotion
renderer. The real heat gate stays on: a hot board skips instead of rendering (owner rule)."""
import json
import shutil
from pathlib import Path

import pytest

import studio_synth
from studio_synth import fonts_present
from workman.studio import assemble, inspect, job, qc

ENGINE = Path(assemble.ENGINE).expanduser()
pytestmark = [
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed"),
    pytest.mark.skipif(not fonts_present(), reason="type system fonts (Fraunces, Geist) not on this machine"),
]


@pytest.fixture(autouse=True)
def _cool_board():
    if job.board_temp() >= job.HEAT_MAX:
        pytest.skip(f"board at {job.board_temp():.1f} C; renders wait for the heat gate, not the test suite")


def ink_bottom(png: Path, y0: int, y1: int, thresh: int = 128) -> int:
    """Lowest row in y0..y1 with a pixel brighter than thresh (white words on the dark synthetic clip)."""
    from PIL import Image
    im = Image.open(png).convert("L")
    rows = [y for y in range(y0, y1) if max(im.crop((0, y, im.width, y + 1)).tobytes()) > thresh]
    return rows[-1] if rows else -1


def run_job(tmp_path: Path, formats=None, renderer="drawtext") -> tuple[Path, dict]:
    d = studio_synth.build(tmp_path, formats, renderer)
    return d, assemble.run(d, "all")


def test_drawtext_job_renders_verifies_inspects_and_passes_qc(tmp_path):
    d, r = run_job(tmp_path, ["1:1"])
    for fmt in ("spec", "1:1"):
        v = r["formats"][fmt]["verify"]
        assert v["probe"] == [] and v["vo"] == [] and v["copy"] == [], v
        assert all(o["ok"] for o in v["onsets"]), v["onsets"]
        assert v["green"]["ok"] and v["loudness"]["errors"] == [], (v["green"], v["loudness"])
    assert r["ok"] and Path(r["contact"]).is_file()
    tm = json.loads((d / "out" / "synth-v1.timing.json").read_text())
    assert tm["logo"]["green_frame"] == 3 and tm["chime"]["at"] == pytest.approx(63 / 24, abs=0.01)
    w = tm["words"][0]
    frame = assemble.grab(d / "out" / "synth-v1.mp4", int((w["a"] + 0.4) * 24), 24, tmp_path / "w0.png")
    assert abs(ink_bottom(frame, w["baseline"] - 150, w["baseline"] + 30) - w["baseline"]) <= 3   # on the baseline
    with pytest.raises(job.StudioError, match="never overwritten"):
        assemble.render(d)
    ins = inspect.inspect(d / "out" / "synth-v1.mp4", d / "out" / "synth-v1.inspect",
                          stt=lambda _p: studio_synth.canned_transcript())
    assert {"probe", "frames", "cuts", "loudness", "audio", "transcript"} <= set(ins["files"])
    pending = qc.qc(d, facts_path=d / "refs" / "FACTS.md")
    assert pending["result"] == "PENDING REVIEW", [x for x in pending["rows"] if x["result"] == "FAIL"]
    eye = {k: {"pass": True, "evidence": "frames.jpg all tiles"} for k in qc.EYE}
    done = qc.qc(d, eye=eye, facts_path=d / "refs" / "FACTS.md")
    assert done["result"] == "PASS", [x for x in done["rows"] if x["result"] != "PASS"]
    assert (d / "QC.md").read_text().startswith("RESULT: PASS")
    assert job.status(d)["qc"] == "PASS"


@pytest.mark.skipif(shutil.which("node") is None or not (ENGINE / "engine-native" / "dist" / "index.js").is_file(),
                    reason="Atmosmotion engine-native dist or node not available")
def test_atmosmotion_renderer_plate_lands_words_on_the_same_baseline(tmp_path):
    d, r = run_job(tmp_path, renderer="atmosmotion")
    v = r["formats"]["spec"]["verify"]
    assert v["renderer"] == "atmosmotion" and all(o["ok"] for o in v["onsets"]), v["onsets"]
    assert v["probe"] == [] and v["green"]["ok"], v
    assert (d / "out" / "synth-v1-plate.mkv").is_file() and (d / "out" / "synth-v1-card.mp4").is_file()
    tm = json.loads((d / "out" / "synth-v1.timing.json").read_text())
    w = tm["words"][0]
    frame = assemble.grab(d / "out" / "synth-v1.mp4", int((w["a"] + 0.4) * 24), 24, tmp_path / "a0.png")
    assert abs(ink_bottom(frame, w["baseline"] - 150, w["baseline"] + 30) - w["baseline"]) <= 3
