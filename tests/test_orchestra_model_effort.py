"""session_set_model_effort: Fable by explicit choice, the 400k ceiling for a
model switch, headless resume line, picker run and verification. Fakes only."""
from __future__ import annotations

import json
import subprocess

import pytest

from workman import front_compact as fc
from workman.orchestra import model_effort as omodel
from workman.orchestra import picker as opicker

from orchestra_fakes import (FakePicker, IN_BAND, LOCAL, SID_A, SID_B, asst, control_world, desktop_target, fake_desk, guard, headless_target, log_lines, rc_target, result, set_model, user)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return control_world(tmp_path, monkeypatch)


class TestModelEffort:
    @pytest.mark.parametrize("model", ["fable", "claude-fable-5-1", "Claude-Fable-5"])
    def test_fable_is_refused_without_an_explicit_choice(self, world, model):
        out = set_model(world, model=model)
        assert out["ok"] is False and "allow_fable=True" in out["error"]
        assert "explicit choice" in log_lines(world, "model-effort-log.jsonl")[-1]["result"]

    def test_fable_by_explicit_choice(self, world):
        desktop_target(world)
        out = set_model(world, model="fable", allow_fable=True)
        assert out["ok"] is True and out["target"]["model"] == "claude-fable-5-1"
        assert opicker.normalize_model("claude-fable-5-1", allow_fable=True) == ("claude-fable-5-1", "")

    def test_a_model_switch_is_refused_above_the_ceiling(self, world):
        desktop_target(world, [user("go"), asst(usage=(0, 450_000, 0, 1))])
        out = set_model(world, model="sonnet")
        assert out["ok"] is False and guard(out, "band")["ok"] is False
        assert set_model(world, model="sonnet", max_tokens=10 ** 9)["ok"] is False  # clamped
        assert set_model(world, model="sonnet", force=True)["ok"] is True
        effort_only = set_model(world, effort="high")
        assert effort_only["ok"] is True and not any(g["name"] == "band"
                                                     for g in effort_only["guards"])

    def test_bad_inputs(self, world):
        assert "effort must be" in set_model(world, effort="turbo")["error"]
        assert "unknown model" in set_model(world, model="gpt-6")["error"]
        assert "give a model" in set_model(world)["error"]

    def test_headless_names_the_resume_command(self, world):
        headless_target(world)
        out = set_model(world, session=SID_B[:8], model="opus", effort="high", mode="run")
        assert out["ok"] is False
        assert (f"run-claude-seat a --model claude-opus-5-5 --effort high --resume {SID_B}"
                in out["refused"])

    def test_no_change_needed(self, world):
        desktop_target(world)  # store: claude-opus-5-5 / xhigh; transcript effort high
        out = set_model(world, model="claude-opus-5-5", effort="xhigh")
        assert out["ok"] is True and out["result"].startswith("no change")

    def test_desktop_dry_run(self, world):
        desktop_target(world)
        out = set_model(world, model="sonnet", effort="high")
        assert out["ok"] is True and out["mode"] == "dry_run"
        assert out["current"] == {"model": "claude-opus-5-5", "effort": "xhigh"}
        assert out["target"] == {"model": "claude-sonnet-5-5", "effort": "high"}
        assert any("'Opus 5.5'" in s and "'Sonnet 5.5'" in s for s in out["would"])
        assert any("'xhigh'" in s and "'high'" in s for s in out["would"])

    def test_rc_current_value_is_the_last_turns_not_the_launch_flags(self, world):
        rc_target(world, [user("go"), asst(usage=IN_BAND, model="claude-sonnet-5-5",
                                           effort="low")])
        out = set_model(world, session=SID_B[:8], effort="high")
        assert out["current"] == {"model": "claude-sonnet-5-5", "effort": "low"}
        assert out["ok"] is True and out["mode"] == "dry_run"

    def test_self_is_refused(self, world, monkeypatch):
        desktop_target(world)
        monkeypatch.setenv("CLAUDE_SESSION_ID", SID_A)
        out = set_model(world, effort="high", mode="run", picker=FakePicker())
        assert out["ok"] is False and "not_self" in out["refused"]

    def test_desktop_run_picks_and_verifies(self, world, monkeypatch):
        desktop_target(world)
        monkeypatch.setattr(fc, "_run", fake_desk)
        pk = FakePicker()
        out = set_model(world, model="sonnet", effort="high", mode="run", picker=pk)
        assert out["ok"] is True and out["changed"] == ["model", "effort"]
        assert pk.picks == [("Opus 5.5", "Sonnet 5.5"), ("xhigh", "high")]

    def test_desktop_run_verifies_through_the_store(self, world, monkeypatch):
        desktop_target(world)
        monkeypatch.setattr(fc, "_run", fake_desk)

        def store_updates(target):
            f = next((world.home / ".config").rglob(f"{LOCAL}.json"))
            d = json.loads(f.read_text())
            d["effort"] = "high"
            f.write_text(json.dumps(d))
        out = set_model(world, effort="high", mode="run",
                        picker=FakePicker(shows=False, on_pick=store_updates))
        assert out["ok"] is True and out["verified"] == {"effort": True}

    def test_unconfirmed_change_is_a_failure(self, world, monkeypatch):
        desktop_target(world)
        monkeypatch.setattr(fc, "_run", fake_desk)
        monkeypatch.setattr(omodel, "VERIFY_POLLS", 2)
        out = set_model(world, effort="high", mode="run", picker=FakePicker(shows=False))
        assert out["ok"] is False and "not confirmed" in out["result"]

    def test_picker_failure_aborts(self, world, monkeypatch):
        desktop_target(world)
        monkeypatch.setattr(fc, "_run", fake_desk)
        out = set_model(world, effort="high", mode="run", picker=FakePicker(ok=False))
        assert out["ok"] is False and out["result"].startswith("abort: effort")

    def test_wrong_header_aborts_before_the_picker(self, world, monkeypatch):
        desktop_target(world)
        monkeypatch.setattr(fc, "_run", lambda argv, timeout: subprocess.CompletedProcess(
            argv, 0, b"another session" if argv[0] == "ffmpeg" else b"", b""))
        pk = FakePicker()
        out = set_model(world, effort="high", mode="run", picker=pk)
        assert out["ok"] is False and "header" in out["result"] and pk.picks == []

    def test_labels(self):
        assert opicker.model_label("claude-opus-5-5") == "Opus 5.5"
        assert opicker.model_label("claude-haiku-4-5-20251001") == "Haiku 4.5"
        assert opicker.model_label("claude-opus-5") == "Opus 5"
        assert opicker.normalize_model("opus") == ("claude-opus-5-5", "")
        assert opicker.normalize_model("claude-sonnet-5-5[1m]") == ("claude-sonnet-5-5[1m]", "")
