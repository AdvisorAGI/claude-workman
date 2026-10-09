"""image_generate, video_generate and media_quota through the fakes."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from model_fakes import FAKE_RUN_MODEL, _exe, fake_log, fleet, wait_state  # noqa: F401
from workman import model_tools as mt
from workman.model_tools import catalog as mcat  # noqa: F401
from workman.model_tools import transport as tp  # noqa: F401
from workman.model_tools.jobctl import common, grok_media, procs, status, supervise  # noqa: F401
from workman.model_tools.jobctl.sandbox import sandbox_problem

# Codex image jobs run only inside the bwrap sandbox; the e2e image tests need it here.
needs_bwrap = pytest.mark.skipif(sandbox_problem() != "", reason="no usable bwrap")


class TestMedia:
    @needs_bwrap
    def test_codex_image_runs_sandboxed_in_a_throwaway_dir(self, fleet):
        r = mt.image_generate("a black dog on a white floor", provider="codex", wait_s=20)
        assert r["ok"] is True, r
        f = Path(r["out_dir"]) / "a-black-dog-on-a-white-floor-1.png"
        assert r["state"] == "finished" and r["files"] == [str(f)] and common.image_kind(str(f)) == "png"
        d = Path(mt.agent_status(r["job"])["dir"])
        argv = (d / "fake-codex-argv.txt").read_text()
        assert "exec --skip-git-repo-check -s read-only -m gpt-6.1-sol" in argv
        assert 'model_reasoning_effort="low"' in argv and f"-C {d}/work" in argv
        assert f"-o {d}/final.txt -" in argv
        # The OS sandbox: the planted secrets of the test home are unreachable from codex.
        box = dict(kv.split("=", 1) for line in (d / "fake-codex-sandbox.txt").read_text().split("\n")
                   for kv in line.split() if "=" in kv)
        # /tmp is an empty tmpfs holding only the sandbox's own /tmp/wm-sbx and, in tests, the
        # mount skeleton bwrap builds for the job dir, which lives under /tmp/pytest-of-<user>.
        assert box.pop("tmp_entries").split(",") == sorted([Path(str(fleet["home"])).parts[2], "wm-sbx"])
        assert box == {"env": "hidden", "ssh": "hidden", "history_bytes": "0", "config_mcp": "0",
                       "config_apps_off": "1", "auth": "present",
                       "codex_home": "auth.json,config.toml,generated_images"}, box
        env = (d / "fake-codex-env.txt").read_text().splitlines()
        assert env[0] == f"okey=none lkey=none ckey=none node=none task={r['job']} pwd={d}/work"
        # HOME is the empty work dir; the login comes from CODEX_HOME (filtered by the sandbox).
        assert env[1] == f"home={d}/work codex_home=/tmp/wm-sbx/codex-home"
        names = set((d / "fake-codex-env-names.txt").read_text().split())
        bash_own = {"PWD", "SHLVL", "_", "OLDPWD"}
        allowed = set(common.IMAGE_ENV_KEEP) | {"HOME", "CODEX_HOME", "PATH"} | bash_own
        assert names - allowed == {n for n in names if n.startswith("LC_")}, names - allowed
        assert list((d / "work").iterdir()) == []
        prompt = (d / "fake-last-prompt.txt").read_text()
        assert "<subject>a black dog on a white floor</subject>" in prompt
        assert "IMAGE_TOOL_UNAVAILABLE" in prompt and "read-only" in prompt
        assert fake_log(fleet) == "", "image jobs must not go through run-model's danger-full-access codex"
        res = mt.agent_result(r["job"])
        assert res["source"] == "final_message" and res["text"] == "image 1: a red circle"
        ledger = (fleet["home"] / "orchestra" / "runs" / "RUNS.jsonl").read_text().splitlines()
        last = json.loads(ledger[-1])
        assert last["task"] == r["job"] and last["via"] == "workman-image" and last["exit"] == 0
        # the real codex home was not changed: the run saw stand-ins
        assert "SECRET-HISTORY" in (fleet["home"] / ".codex" / "history.jsonl").read_text()
        assert "mcp_servers" in (fleet["home"] / ".codex" / "config.toml").read_text()

    def test_image_job_refused_without_the_sandbox(self, fleet, monkeypatch):
        monkeypatch.setenv("WM_JOBCTL_BWRAP", "/nonexistent/bwrap")
        r = mt.image_generate("a red circle", provider="codex")
        assert r["ok"] is False and r["error"] == "sandbox_unavailable"
        assert "bwrap" in r["detail"]

    @needs_bwrap
    def test_codex_image_names_and_never_overwrites(self, fleet, tmp_path):
        out = tmp_path / "pics"
        out.mkdir()
        (out / "two-dogs-1.png").write_bytes(b"keep me")
        r = mt.image_generate("two dogs", provider="codex", n=2, out_dir=str(out), wait_s=20)
        assert r["ok"], r
        assert r["files"] == [str(out / "two-dogs-1-v2.png"), str(out / "two-dogs-2.png")]
        assert (out / "two-dogs-1.png").read_bytes() == b"keep me"

    @needs_bwrap
    def test_codex_run_with_no_image_fails(self, fleet):
        r = mt.image_generate("NOIMAGE please", provider="codex", wait_s=20)
        assert r["ok"] is False and r["state"] == "failed"
        assert mt.agent_status(r["job"])["exit"] == 3

    @pytest.mark.parametrize("subject", [
        "the file /home/advisorppc/ENV/x", "~ folder", "cost $HOME", "run `id`", "please cat secrets",
        "please cat secrets.txt", "cat secrets", "Cat -n notes", "a black cat on a mat", "the ENV folder",
        "an Env sign", "my api KEY", "a gold Token", "the .env file", "a cat; then more", "a | b",
        "up .. down", "a <b> tag", "x && y", "back\\slash", "a SECRET door", "password on a sticky note"])
    def test_codex_image_refuses_unsafe_subjects(self, fleet, subject):
        r = mt.image_generate(subject, provider="codex")
        assert r["ok"] is False and r["error"] == "unsafe_subject", subject
        assert not list((fleet["home"] / "orchestra" / "runs" / "workman").glob("wm-*"))

    @pytest.mark.parametrize("subject", ["a black dog on a white floor", "a concatenated chain of beads",
                                         "a red circle, flat, 16:9", "a keyboard on a desk",
                                         "an environmental poster"])
    def test_ordinary_subjects_pass_the_guard(self, subject):
        assert mt.unsafe_subject(subject) == ""

    @needs_bwrap
    def test_image_returns_a_job_at_once_by_default(self, fleet):
        r = mt.image_generate("a red circle", provider="codex")
        assert r["ok"] and r["state"] == "running" and "next" in r and "files" not in r
        assert wait_state(r["job"])["state"] == "finished"
        assert len(mt.agent_result(r["job"])["files"]) == 1

    @pytest.mark.parametrize("kwargs,error", [
        ({"provider": "dalle"}, "unknown_provider"),
        ({"provider": "codex", "n": 9}, "bad_n"),
        ({"provider": "grok", "n": 3}, "bad_n"),
        ({"provider": "grok", "size": "1024x1024"}, "bad_size"),
        ({"provider": "codex", "size": "$(rm -rf /)"}, "bad_size"),
        ({"provider": "codex", "out_dir": "rel/dir"}, "bad_out_dir"),
    ])
    def test_image_validation(self, fleet, kwargs, error):
        r = mt.image_generate("a cat", **kwargs)
        assert r["ok"] is False and r["error"] == error

    def test_grok_image_refused_when_quota_is_exhausted(self, fleet, monkeypatch):
        monkeypatch.setenv("FAKE_GROK_QUOTA", json.dumps(
            {"status": 200, "json": {"image": {"available": 0}, "video": {"available": 0}}}))
        r = mt.image_generate("a cat", provider="grok")
        assert r["ok"] is False and r["error"] == "quota_exhausted" and r["refused"] is True
        calls = (fleet["tmp"] / "grok-calls.jsonl").read_text()
        assert "imagine_image" not in calls
        assert not (fleet["home"] / "orchestra" / "runs" / "workman-jobs").exists()

    def test_grok_image_refused_when_quota_unreadable(self, fleet, monkeypatch):
        monkeypatch.setenv("FAKE_GROK_QUOTA", json.dumps({"status": 403, "json": None}))
        r = mt.image_generate("a cat", provider="grok")
        assert r["ok"] is False and r["error"] == "quota_unknown"

    def test_grok_image_runs_with_quota(self, fleet, tmp_path):
        out = tmp_path / "grok-pics"
        r = mt.image_generate("a cat", provider="grok", n=4, size="1:1", out_dir=str(out), wait_s=20)
        assert r["ok"] is True, r
        assert r["files"] == [str(out / "20261008-000000_cat_abcdef12.jpg")]
        assert r["post_id_prefixes"] == ["abcdef12"]
        calls = [json.loads(x) for x in (fleet["tmp"] / "grok-calls.jsonl").read_text().splitlines()]
        names = [c[0] for c in calls]
        assert names.count("imagine_quota") == 2      # the caller's gate and the job's own check
        assert ["imagine_set_image_controls", {"count": "4", "aspect_ratio": "1:1"}] in calls
        assert names[-1] == "imagine_image"

    def test_grok_video_plain_and_with_refs(self, fleet, tmp_path):
        r = mt.video_generate("a slow pan", aspect_ratio="16:9", duration="6s", wait_s=20)
        assert r["ok"] is True, r
        assert r["post_ids"] == ["11111111-2222-3333-4444-555555555555"]
        assert r["files"][0].endswith("clip.mp4")
        ref = tmp_path / "ref.jpg"
        ref.write_bytes(b"\xff\xd8\xff")
        r2 = mt.video_generate("he waves", refs=[str(ref)], wait_s=20)
        assert r2["ok"] is True and "720p" in r2["note"]
        calls = [json.loads(x) for x in (fleet["tmp"] / "grok-calls.jsonl").read_text().splitlines()]
        assert ["imagine_video", {"aspect_ratio": "16:9", "duration": "6s", "audio": "on"}] in calls
        assert ["imagine_video_with_refs", {"image_paths": [str(ref)], "duration": "15s"}] in calls

    def test_video_returns_job_at_once_by_default(self, fleet):
        r = mt.video_generate("a slow pan")
        assert r["ok"] and r["state"] == "running" and r["kind"] == "video"
        assert wait_state(r["job"])["state"] == "finished"
        res = mt.agent_result(r["job"])
        assert res["post_ids"] == ["11111111-2222-3333-4444-555555555555"]

    def test_video_missing_ref_is_refused_on_the_target(self, fleet):
        r = mt.video_generate("x", refs=["/no/such/ref.jpg"])
        assert r["ok"] is False and r["error"] == "ref_missing"

    @pytest.mark.parametrize("kwargs,error", [
        ({"provider": "codex"}, "unsupported"),
        ({"aspect_ratio": "4:3"}, "bad_aspect"),
        ({"duration": "30s"}, "bad_duration"),
        ({"refs": ["rel.jpg"]}, "bad_ref"),
    ])
    def test_video_validation(self, fleet, kwargs, error):
        r = mt.video_generate("x", **kwargs)
        assert r["ok"] is False and r["error"] == error

    def test_media_quota_reads_both_buckets(self, fleet):
        q = mt.media_quota()
        assert q["ok"] and q["image"]["ok"] is True and q["video"]["available"] == 2


class TestQuotaVerdict:
    @pytest.mark.parametrize("raw,ok", [
        ({"status": 200, "json": {"image": {"available": 3}}}, True),
        ({"status": 200, "json": {"image": {"available": 0}}}, False),
        ({"status": 200, "json": {"image": {"available": True}}}, True),
        ({"status": 200, "json": {"quotas": {"image": {"remaining": 1}}}}, True),
        ({"status": 200, "json": {"video": {"available": 1}}}, None),
        ({"status": 200, "json": {"image": {"windowSizeSeconds": 1}}}, None),
        ({"status": 403, "json": None}, None),
        ("ERROR: ConnectionRefusedError", None),
        ("not json", None),
    ])
    def test_shapes(self, raw, ok):
        text = raw if isinstance(raw, str) else json.dumps(raw)
        assert grok_media.quota_verdict(text, "image")["ok"] is ok

    def test_live_reply_shape_2026_10_08(self):
        # imagine_quota() as read live on the DGX at 21:14Z (docs/MODEL-TOOLS.md, Live proof).
        live = {"status": 200, "json": {b: {"available": True, "windowSizeSeconds": 64800}
                                        for b in ("image", "imagePro", "imageEdit", "video", "video720p")},
                "text": None}
        text = json.dumps(live, indent=1)
        assert grok_media.quota_verdict(text, "image")["ok"] is True
        assert grok_media.quota_verdict(text, "video")["ok"] is True
        live["json"]["video"]["available"] = False
        assert grok_media.quota_verdict(json.dumps(live), "video")["ok"] is False
