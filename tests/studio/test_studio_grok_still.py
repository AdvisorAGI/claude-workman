"""Pins the grok prompt rules (locks first, no motion words, men only, male voices, pace), the
Save-dialog rename that proves itself by the saved file, and the Codex still lane (stdin prompt,
-i reference, no API key)."""
import subprocess

import pytest

from workman.studio import grok as g
from workman.studio import still as s
from workman.studio.job import StudioError


def test_locked_shot_opens_and_closes_with_the_lock():
    p = g.locked_shot("The logo", "The bar switches on red, then yellow, then green, about half a second each")
    assert p.startswith("Completely static shot. The camera does not move. The logo does not move")
    assert p.endswith("Everything else stays exactly as in the image for the whole video.")
    for bad in ("slow push-in on the logo", "a cinematic glow", "the camera orbits"):
        with pytest.raises(ValueError):
            g.locked_shot("The logo", bad)
    with pytest.raises(ValueError):
        g.locked_shot("The logo", "the light turns green", seconds=8)


def test_prompts_refuse_women_and_female_voices():
    with pytest.raises(ValueError):
        g.locked_shot("A woman at a desk", "she looks up")
    assert "no other people in frame" in g.man("in his forties", "lean")
    with pytest.raises(ValueError):
        g.narration("Look closer.", 1, 6, voice="Eve")
    assert g.check_prompt("use the Luna voice") == ["female voice Luna"]


def test_narration_names_voice_accent_and_keeps_pace():
    p = g.narration("Every ad account looks fine.", 1, 6)
    assert 'starting at 1 second: "Every ad account looks fine."' in p and "Helios voice exactly" in p
    assert "native American English accent" in p
    with pytest.raises(ValueError):
        g.narration("one two three four five six seven eight nine ten eleven twelve thirteen", 1, 6)


def test_clip_name_and_download_steps():
    assert g.clip_name("logo LED", "383d28fe-aaaa-bbbb", 6) == "grok-logo-led-383d28fe-6s.mp4"
    steps = g.download_steps("383d28fe", "grok-x-383d28fe-6s.mp4")
    assert [x["step"] for x in steps] == ["navigate", "click", "click", "save_dialog", "intake"]
    assert not any("cookie" in str(x).lower() for x in steps)


def test_clip_errors_against_grok_delivery():
    good = {"streams": [{"codec_type": "video", "width": 1088, "height": 1920, "r_frame_rate": "24/1"}]}
    assert g.clip_errors(good) == []
    assert g.clip_errors({"streams": [{"codec_type": "video", "width": 720, "height": 1280,
                                       "r_frame_rate": "30/1"}]}) == ["size 720x1280", "fps 30"]


class FakeX:
    """wmctrl lists the dialog until Return; Return 'saves' the file."""

    def __init__(self, folder, save=True):
        self.folder, self.save, self.open, self.calls = folder, save, True, []

    def __call__(self, cmd, display, timeout=10):
        self.calls.append(cmd)
        out = ""
        if cmd[0] == "wmctrl":
            out = "0x04a00007  0 spark blob:https://grok.com/5f1e save\n" if self.open else ""
        if cmd[:2] == ["xdotool", "key"] and cmd[-1] == "Return":
            self.open = False
            if self.save:
                (self.folder / self.typed).write_bytes(b"\x00" * 64)
        if cmd[:2] == ["xdotool", "type"]:
            self.typed = cmd[-1]
        return subprocess.CompletedProcess(cmd, 0, out, "")


def test_save_dialog_renames_then_proves_by_the_saved_file(tmp_path):
    x = FakeX(tmp_path)
    r = g.save_dialog("grok-a-383d28fe-6s.mp4", folder=tmp_path, run=x, sleep=lambda s: None, land_wait=5)
    assert r["ok"] and r["path"] == str(tmp_path / "grok-a-383d28fe-6s.mp4")
    keys = [c for c in x.calls if c[0] == "xdotool"]
    assert keys[0][:2] == ["xdotool", "windowactivate"] and keys[1][-1] == "ctrl+a"
    assert keys[2][-1] == "grok-a-383d28fe-6s.mp4" and keys[3][-1] == "Return"


def test_save_dialog_fails_closed(tmp_path):
    none = FakeX(tmp_path)
    none.open = False
    assert g.save_dialog("a.mp4", folder=tmp_path, run=none, sleep=lambda s: None, wait=0)["error"] == "no_dialog"
    lost = FakeX(tmp_path, save=False)
    r = g.save_dialog("b.mp4", folder=tmp_path, run=lost, sleep=lambda s: None, land_wait=0.01)
    assert r["error"] == "not_saved"
    (tmp_path / "c.mp4").write_bytes(b"x")
    assert g.save_dialog("c.mp4", folder=tmp_path, run=FakeX(tmp_path))["error"] == "exists"
    with pytest.raises(ValueError):
        g.save_dialog("../x.mp4", folder=tmp_path)


def test_still_prompt_and_cmd_follow_the_codex_recipe(tmp_path):
    p = s.still_prompt("A graphite macro surface", "first.png", "9:16")
    assert p.startswith("Create one image and save it as first.png") and "Then open first.png" in p
    assert "No women" in p and "#10B981" in p
    assert s.codex_cmd(tmp_path, [tmp_path / "ref.png"]) == [
        "codex", "exec", "--skip-git-repo-check", "-C", str(tmp_path), "-i", str(tmp_path / "ref.png")]
    with pytest.raises(ValueError):
        s.still_prompt("x", "../evil.png")


def test_still_make_pipes_prompt_on_stdin_strips_api_keys_and_checks_image(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-reach-codex")
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\n")
    seen = {}

    def run(cmd, input, capture_output, text, timeout, env):
        seen.update(cmd=cmd, input=input, env=env)
        (tmp_path / "out" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n rest")
        return subprocess.CompletedProcess(cmd, 0, "saved a.png\nit shows graphite", "")

    r = s.make(tmp_path / "out", "a.png", "Graphite", refs=[str(ref)], run=run)
    assert r["ok"] and r["kind"] == "png"
    assert seen["cmd"][-2:] == ["-i", str(ref)] and "Create one image" in seen["input"]
    assert "OPENAI_API_KEY" not in seen["env"]
    with pytest.raises(StudioError, match="exists"):
        s.make(tmp_path / "out", "a.png", "Graphite", run=run)

    def no_file(cmd, **k):
        return subprocess.CompletedProcess(cmd, 0, "done", "")

    with pytest.raises(StudioError, match="saved no"):
        s.make(tmp_path / "out", "b.png", "Graphite", run=no_file)
