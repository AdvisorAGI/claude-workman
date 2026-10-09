"""The shared guards, driven through session_compact: caller known and not
the target, clean turn, quiet, question, registry busy/waiting, film, lock,
header, the clamped policy band. Fakes only; no display, Chrome or session."""
from __future__ import annotations

import fcntl
import os
import time

import pytest

from workman import front_compact as fc
from workman.orchestra import compact as ocompact

from orchestra_fakes import (ASK, CALLER, FakePicker, IN_BAND, LOCAL, SID_A, SID_B, _blocked, asst, compact, control_world, desktop_target, guard, headless_target, log_lines, result, set_model, user)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return control_world(tmp_path, monkeypatch)


class TestCompactGuards:
    def test_dry_run_reports_every_step_and_touches_nothing(self, world):
        desktop_target(world)
        out = compact(world)
        assert out["ok"] is True and out["mode"] == "dry_run"
        assert all(g["ok"] for g in out["guards"])
        assert any(fc.deep_link(LOCAL) in s for s in out["would"])
        assert out["session"]["front_end"] == "desktop"
        assert "would compact via desktop" in log_lines(world)[-1]["result"]

    def test_refuses_its_own_session_by_env(self, world, monkeypatch):
        desktop_target(world)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID_A)
        out = compact(world, mode="run")
        assert out["ok"] is False and guard(out, "not_self")["ok"] is False
        assert "not_self" in out["refused"]

    def test_refuses_its_own_session_by_explicit_id(self, world):
        desktop_target(world)
        assert compact(world, self_id=SID_A)["ok"] is False

    def test_band(self, world):
        desktop_target(world, [user("go"), asst(usage=(0, 200_000, 0, 1))])
        out = compact(world)
        assert out["ok"] is False and guard(out, "band")["ok"] is False
        forced = compact(world, force=True)
        assert forced["ok"] is True and "forced" in guard(forced, "band")["detail"]

    def test_a_caller_band_cannot_widen_the_policy(self, world):
        desktop_target(world, [user("go"), asst(usage=(0, 200_000, 0, 1))])
        out = compact(world, min_tokens=0, max_tokens=10 ** 9)
        assert out["ok"] is False
        assert "clamped to policy [300000, 400000]" in guard(out, "band")["detail"]
        narrower = compact(world, min_tokens=360_000, max_tokens=380_000)
        assert narrower["ok"] is False   # 350k is inside the policy but outside the caller's band
        assert compact(world, min_tokens=0, max_tokens=10 ** 9, force=True)["ok"] is True

    def test_registry_waiting_after_end_turn_blocks(self, world):
        desktop_target(world)
        world.process(4242, ["/x/claude"])
        world.registry(4242, SID_A, cfg=".claude-d", status="waiting", waiting="input needed")
        out = compact(world)
        assert out["ok"] is False and guard(out, "registry_ready")["ok"] is False

    def test_a_short_self_id_refuses(self, world, monkeypatch):
        desktop_target(world)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
        monkeypatch.setattr(fc, "spawn", _blocked)
        out = compact(world, mode="run", self_id="owner")
        assert out["ok"] is False and guard(out, "caller_known")["ok"] is False
        assert "'owner' is not a session id" in guard(out, "caller_known")["detail"]
        # a real caller in the env does not rescue a malformed explicit id
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CALLER)
        assert compact(world, mode="run", self_id="owner")["ok"] is False

    def test_an_unknown_caller_refuses_every_action(self, world, monkeypatch):
        desktop_target(world)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
        monkeypatch.setattr(fc, "spawn", _blocked)
        out = compact(world, mode="run")
        assert out["ok"] is False and guard(out, "caller_known")["ok"] is False
        assert "cannot tell which session is calling" in out["refused"]
        m = set_model(world, effort="high", mode="run", picker=FakePicker())
        assert m["ok"] is False and "caller_known" in m["refused"]
        assert ocompact.spawn_remote(SID_B, "x.jsonl")["ok"] is False

    def test_not_at_a_clean_turn(self, world):
        desktop_target(world, [user("go"), asst(usage=IN_BAND, stop="tool_use",
                                                 tools=[("t", "Bash", {})])])
        out = compact(world)
        assert out["ok"] is False and guard(out, "clean_turn")["ok"] is False

    def test_not_quiet_long_enough(self, world):
        desktop_target(world, age_s=5)
        out = compact(world, now=time.time())
        assert out["ok"] is False and guard(out, "quiet")["ok"] is False

    def test_pending_question(self, world):
        desktop_target(world, [user("go"), asst(usage=IN_BAND, stop="tool_use", tools=[ASK])])
        out = compact(world)
        assert out["ok"] is False and guard(out, "no_question")["ok"] is False

    def test_headless_is_refused_with_the_reason(self, world, monkeypatch):
        headless_target(world)
        monkeypatch.setattr(fc, "spawn", _blocked)
        out = compact(world, session=SID_B[:8], mode="run")
        assert out["ok"] is False and "headless" in out["refused"]
        assert "no front end" in out["refused"]

    def test_frontend_lock_busy(self, world):
        desktop_target(world)
        fd = os.open(fc.FRONTEND_LOCK, os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            out = compact(world)
        finally:
            os.close(fd)
        assert out["ok"] is False and guard(out, "frontend_lock")["ok"] is False

    def test_a_dry_run_creates_no_lock_file(self, world):
        desktop_target(world)
        assert not os.path.exists(fc.FRONTEND_LOCK)
        out = compact(world)
        assert guard(out, "frontend_lock")["ok"] is True
        assert not os.path.exists(fc.FRONTEND_LOCK)

    def test_header_never_learned(self, world):
        desktop_target(world, learn=False)
        out = compact(world)
        assert out["ok"] is False and "--learn" in guard(out, "header_learned")["detail"]

    def test_film_playing(self, world):
        desktop_target(world)
        out = compact(world, procs=lambda: ["bash", "vlc"])
        assert out["ok"] is False and guard(out, "no_film")["ok"] is False

    def test_unknown_session(self, world):
        out = compact(world, session="ffffffff")
        assert out["ok"] is False and "no session" in out["error"]

    def test_run_starts_the_desktop_runner(self, world, monkeypatch):
        p = desktop_target(world)
        seen = {}
        monkeypatch.setattr(fc, "spawn", lambda lid, tr, resume="", expect="", self_id="", **kw:
                            seen.update(lid=lid, tr=tr, resume=resume, self_id=self_id, **kw)
                            or {"ok": True, "pid": 99})
        out = compact(world, mode="run", resume="carry\non")
        assert out["ok"] is True and out["runner"]["pid"] == 99
        assert seen == {"lid": LOCAL, "tr": str(p), "resume": "carry on", "self_id": CALLER,
                        "min_tokens": 300_000, "max_tokens": 400_000, "force": False,
                        "reason": ""}
        assert log_lines(world)[-1]["result"] == "runner started pid 99"

    def test_run_with_a_failed_guard_starts_nothing(self, world, monkeypatch):
        desktop_target(world, [user("go"), asst(usage=(0, 10, 0, 1))])
        monkeypatch.setattr(fc, "spawn", _blocked)
        assert compact(world, mode="run")["ok"] is False

    def test_bad_mode(self, world):
        assert compact(world, mode="now")["ok"] is False
