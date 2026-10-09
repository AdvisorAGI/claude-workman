"""Compaction routing: the Remote Control page guard and runner (never Escape,
full recheck), and the guarded low-level session_front_compact. Fakes only."""
from __future__ import annotations

import fcntl
import json
import os

import pytest

from workman import front_compact as fc
from workman.orchestra import compact as ocompact
from workman.orchestra import drivers as odrivers
from workman.orchestra import guards as oguards
from workman import server

from orchestra_fakes import (BRIDGE, CALLER, FakeDriver, GOOD, LOCAL, SID_A, SID_B, _blocked, asst, compact, control_world, desktop_target, guard, log_lines, rc_target, result, user)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return control_world(tmp_path, monkeypatch)


class TestCompactRemote:
    def test_dry_run_routes_by_page_and_hides_the_bridge_id(self, world):
        rc_target(world)
        out = compact(world, session=SID_B[:8])
        assert out["ok"] is True and out["session"]["front_end"] == "remote_control"
        assert guard(out, "rc_page")["ok"] and guard(out, "rc_title")["ok"]
        assert BRIDGE not in json.dumps(out) and BRIDGE not in json.dumps(log_lines(world))

    def test_a_dead_rc_process_has_no_page_to_open(self, world):
        rc_target(world)
        (world.proc / "4242" / "cmdline").unlink()
        out = compact(world, session=SID_B[:8])
        assert out["ok"] is False and guard(out, "rc_page")["ok"] is False

    def test_run_starts_the_remote_runner(self, world):
        p = rc_target(world)
        seen = {}
        out = compact(world, session=SID_B[:8], mode="run",
                      spawn_rc=lambda sid, tr, res, ids, **kw: seen.update(sid=sid, tr=tr, ids=ids)
                      or {"ok": True, "pid": 7})
        assert out["ok"] is True and seen == {"sid": SID_B, "tr": str(p), "ids": CALLER}

    def test_rc_url_template(self, monkeypatch):
        assert oguards.rc_url("cse_01ABC") == "https://claude.ai/code/session_01ABC"
        assert oguards.rc_url("session_01ABC") == "https://claude.ai/code/session_01ABC"
        monkeypatch.setenv("WORKMAN_RC_URL_TEMPLATE", "https://x/{suffix}")
        assert oguards.rc_url("cse_01ABC") == "https://x/01ABC"


class TestRemoteRunner:
    def runner(self, world, drv=None, resume="", titles=("proj-ab",)):
        p = rc_target(world)
        drv = drv or FakeDriver(str(p))
        r = ocompact.RemoteCompact(SID_B, str(p), "https://claude.ai/code/session_x", list(titles),
                             resume=resume, driver=drv, sleep=lambda s: None, procs=lambda: [])
        return r, drv, p

    def results(self, world):
        return [x["result"] for x in log_lines(world)]

    def test_compacts_and_never_presses_escape(self, world):
        r, drv, _ = self.runner(world)
        assert r.run() == fc.OK
        assert drv.typed == ["/compact"] and drv.keys == ["Return"]
        assert self.results(world) == ["compacted"]

    def test_sends_the_resume_line(self, world):
        r, drv, _ = self.runner(world, resume="carry\n on")
        assert r.run() == fc.OK
        assert drv.typed == ["/compact", "carry on"]
        assert self.results(world)[-1] == "resume sent"

    def test_wrong_page_types_nothing(self, world):
        p = rc_target(world)
        drv = FakeDriver(str(p), page_title="some other chat")
        r = ocompact.RemoteCompact(SID_B, str(p), "u", ["proj-ab"], driver=drv,
                             sleep=lambda s: None, procs=lambda: [])
        assert r.run() == fc.HEADER_MISMATCH
        assert drv.typed == [] and drv.keys == []

    def test_focus_not_editable_types_nothing(self, world):
        p = rc_target(world)
        drv = FakeDriver(str(p), focus_role="push button")
        r = ocompact.RemoteCompact(SID_B, str(p), "u", ["proj-ab"], driver=drv,
                             sleep=lambda s: None, procs=lambda: [])
        assert r.run() == fc.HEADER_MISMATCH and drv.typed == []
        assert "not an editable field" in self.results(world)[-1]

    def test_lock_busy_opens_nothing(self, world, monkeypatch):
        r, drv, _ = self.runner(world)
        monkeypatch.setattr(fc, "LOCK_WAIT_S", 0)
        fd = os.open(fc.FRONTEND_LOCK, os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            assert r.run() == fc.LOCK_BUSY
        finally:
            os.close(fd)
        assert drv.calls == []

    def test_no_boundary_is_a_failure(self, world, monkeypatch):
        monkeypatch.setattr(fc, "BOUNDARY_POLLS", 2)
        p = rc_target(world)
        drv = FakeDriver(str(p), compacts=False)
        r = ocompact.RemoteCompact(SID_B, str(p), "u", ["proj-ab"], driver=drv,
                             sleep=lambda s: None, procs=lambda: [])
        assert r.run() == fc.NO_BOUNDARY

    def test_busy_target_never_opens(self, world, monkeypatch):
        monkeypatch.setattr(fc, "IDLE_POLLS", 2)
        p = rc_target(world, age_s=0)
        drv = FakeDriver(str(p))
        r = ocompact.RemoteCompact(SID_B, str(p), "u", ["proj-ab"], driver=drv,
                             sleep=lambda s: None, procs=lambda: [])
        assert r.run() == fc.NEVER_IDLE and drv.calls == []

    def test_the_default_recheck_is_the_full_turn_guard(self, world, monkeypatch):
        monkeypatch.setattr(fc, "IDLE_POLLS", 2)
        p = rc_target(world)
        world.registry(4242, SID_B, status="waiting", waiting="input needed")
        drv = FakeDriver(str(p))
        r = ocompact.RemoteCompact(SID_B, str(p), "u", ["proj-ab"], driver=drv,
                             sleep=lambda s: None, procs=lambda: [])
        assert r.run() == fc.NEVER_IDLE and drv.calls == []

    def test_a_turn_that_restarts_after_the_page_opens_types_nothing(self, world):
        p = rc_target(world)
        drv = FakeDriver(str(p))
        answers = iter([(True, ""), (False, "registry busy")])
        r = ocompact.RemoteCompact(SID_B, str(p), "u", ["proj-ab"], driver=drv,
                             sleep=lambda s: None, procs=lambda: [],
                             ready=lambda: next(answers))
        assert r.run() == fc.BUSY_AGAIN and drv.typed == []
        assert "registry busy" in self.results(world)[-1]

    def test_a_question_after_the_compact_skips_the_resume(self, world, monkeypatch):
        monkeypatch.setattr(fc, "RESUME_POLLS", 2)
        p = rc_target(world)
        ask = json.dumps({"type": "assistant", "message": {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "q9", "name": "AskUserQuestion", "input": {}}]}}) + "\n"
        drv = FakeDriver(str(p), after_compact=ask)
        r = ocompact.RemoteCompact(SID_B, str(p), "u", ["proj-ab"], resume="carry on", driver=drv,
                                   sleep=lambda s: None, procs=lambda: [])
        assert r.run() == fc.OK
        assert drv.typed == ["/compact"]   # the resume line was never typed
        assert self.results(world)[-1] == "resume skipped: AskUserQuestion open"

    def test_the_rc_cli_refuses_outside_the_band_without_force(self, world):
        p = rc_target(world, [user("go"), asst(usage=(0, 120_000, 0, 1))])
        drv = FakeDriver(str(p))
        code = ocompact.run_rc(SID_B, str(p), self_id=CALLER, home=world.home,
                               proc_root=world.proc, driver=drv)
        assert code == fc.REFUSED and drv.calls == []
        last = log_lines(world)[-1]
        assert last["tool"] == "runner_check" and "band" in last["result"]
        forced = ocompact.run_rc(SID_B, str(p), self_id=CALLER, home=world.home,
                                 proc_root=world.proc, driver=drv, force=True, reason="test")
        assert forced == fc.OK and "band forced: test" in [
            x["result"] for x in log_lines(world) if x.get("tool") == "runner_check"][-1]
        with pytest.raises(SystemExit):
            from workman import orchestra_control
            orchestra_control.main(["rc-compact", SID_B, "--transcript", str(p), "--force"])

    def test_the_real_driver_refuses_escape(self):
        with pytest.raises(ValueError):
            odrivers.ChromeDriver().key("Escape")


class TestServer:
    def test_tools_are_registered(self):
        for name in ("sessions_list", "session_needs", "session_usage", "session_events",
                     "session_compact", "session_set_model_effort"):
            assert callable(getattr(server, name, None)), name

    def test_low_level_front_compact_refuses_self(self, world, monkeypatch):
        p = desktop_target(world)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID_A)
        monkeypatch.setattr(fc, "spawn", _blocked)
        out = server.session_front_compact(LOCAL, str(p), expect=GOOD)
        assert out["ok"] is False and "not_self" in out["refused"]

    def test_low_level_front_compact_goes_through_every_guard(self, world, monkeypatch):
        monkeypatch.setattr(fc, "spawn", _blocked)
        p = desktop_target(world, [user("go"), asst(usage=(0, 120_000, 0, 1))])
        out = ocompact.session_front_compact(LOCAL, str(p), home=world.home, proc_root=world.proc,
                                       procs=lambda: [])
        assert out["ok"] is False and guard(out, "band")["ok"] is False
        other = "local_aaaaaaaa-5555-4555-8555-555555555555"
        out = ocompact.session_front_compact(other, str(p), home=world.home, proc_root=world.proc,
                                       procs=lambda: [])
        assert out["ok"] is False and guard(out, "local_id_matches")["ok"] is False

    def test_low_level_front_compact_spawns_when_every_guard_passes(self, world, monkeypatch):
        p = desktop_target(world, learn=False)
        seen = {}
        monkeypatch.setattr(fc, "spawn", lambda lid, tr, resume="", expect="", self_id="", **kw:
                            seen.update(lid=lid, expect=expect, self_id=self_id)
                            or {"ok": True, "pid": 5})
        out = ocompact.session_front_compact(LOCAL, str(p), expect=GOOD, home=world.home,
                                       proc_root=world.proc, procs=lambda: [])
        assert out["ok"] is True and seen == {"lid": LOCAL, "expect": GOOD, "self_id": CALLER}
