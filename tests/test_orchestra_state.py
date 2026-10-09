"""State, front end, the pre-action turn check, the session table and needs,
on a fake HOME and a fake /proc."""
from __future__ import annotations


import pytest

from workman.orchestra import common as ocommon
from workman.orchestra import registry as oregistry
from workman.orchestra import state as ostate
from workman import orchestra_tools as ot

from orchestra_fakes import (ASK, CALLER, LOCAL, NOW, SID_A, SID_B, SID_C, asst, lst, read_world, ts,
                             user, write)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return read_world(tmp_path, monkeypatch)


class TestClassify:
    def base(self, **kw):
        info = {"mtime": NOW - 600, "pending_questions": [], "denials": [], "api_error": None,
                "last_kind": "assistant_end"}
        info.update(kw)
        return info

    def test_idle_after_a_finished_turn(self):
        assert ostate.classify(self.base(), NOW)[0] == "idle"

    def test_working_when_fresh(self):
        assert ostate.classify(self.base(last_kind="assistant_tool", mtime=NOW - 5), NOW)[0] == "working"

    def test_silent_inside_a_tool_call(self):
        st, why = ostate.classify(self.base(last_kind="assistant_tool", mtime=NOW - 30 * 60), NOW)
        assert st == "silent" and "inside a tool call" in why

    def test_question_is_needs_input(self):
        q = [{"tool_use_id": "q", "questions": [{"question": "ok?", "options": []}]}]
        assert ostate.classify(self.base(pending_questions=q, last_kind="assistant_tool"),
                           NOW) == ("needs_input", "AskUserQuestion: ok?")

    def test_registry_waiting_is_needs_input(self):
        reg = {"alive": True, "status": "waiting", "waitingFor": "input needed"}
        assert ostate.classify(self.base(last_kind="assistant_tool"), NOW, reg)[0] == "needs_input"

    def test_denial_that_ended_the_turn_is_blocked(self):
        d = [{"tool": "Bash", "kind": "classifier", "reason": "[X]", "tool_use_id": "t"}]
        st, why = ostate.classify(self.base(denials=d), NOW)
        assert st == "blocked" and "Bash denied (classifier)" in why

    def test_denial_while_still_working_is_working(self):
        d = [{"tool": "Bash", "kind": "classifier", "reason": "", "tool_use_id": "t"}]
        assert ostate.classify(self.base(denials=d, last_kind="assistant_tool", mtime=NOW - 5),
                           NOW)[0] == "working"

    def test_limit_beats_a_question(self):
        q = [{"tool_use_id": "q", "questions": []}]
        err = {"error": "rate_limit", "text": "You've hit your weekly limit"}
        assert ostate.classify(self.base(pending_questions=q, api_error=err), NOW)[0] == "limit_hit"

    def test_a_limit_that_has_reset_is_no_longer_limit_hit(self):
        at = ocommon.parse_ts("2026-10-08T14:00:00Z")
        err = {"error": "rate_limit", "text": "You've hit your session limit · resets 3:50pm (UTC)",
               "ts": at}
        assert ostate.limit_reset(err["text"], at) == ocommon.parse_ts("2026-10-08T15:50:00Z")
        assert ostate.classify(self.base(api_error=err), at + 600)[0] == "limit_hit"
        st, why = ostate.classify(self.base(api_error=err), at + 3 * 3600)
        assert st == "idle" and "lifted" in why
        weekly = "You've hit your weekly limit · resets Oct 11, 5am (UTC)"
        assert ostate.limit_reset(weekly, at) == ocommon.parse_ts("2026-10-11T05:00:00Z")
        assert ostate.limit_reset("resets 1am (UTC)", at) == ocommon.parse_ts("2026-10-09T01:00:00Z")
        assert ostate.limit_reset("no time given", at) is None

    def test_no_records_at_all_reads_idle_once_quiet(self):
        assert ostate.classify(self.base(last_kind=None), NOW)[0] == "idle"
        assert ostate.classify(self.base(last_kind=None, mtime=NOW - 3), NOW)[0] == "working"

    def test_interrupted_reads_idle(self):
        assert ostate.classify(self.base(last_kind="interrupted"), NOW)[0] == "idle"

    def test_registry_busy_beats_an_old_end_turn_only_while_fresh(self):
        reg = {"alive": True, "status": "busy"}
        assert ostate.classify(self.base(mtime=NOW - 10), NOW, reg)[0] == "working"


class TestTurnReady:
    def ready(self, world, path, **kw):
        kw.setdefault("self_id", CALLER)
        return ostate.turn_ready(path, now=NOW, home=world.home, proc_root=world.proc, **kw)

    def test_no_caller_at_all_refuses(self, world):
        p = world.transcript(SID_A, [user("go"), asst()], age_s=120)
        ok, why = self.ready(world, p, self_id="")
        assert ok is False and why.startswith("caller unknown")

    def test_a_short_self_id_refuses(self, world):
        p = world.transcript(SID_A, [user("go"), asst()], age_s=120)
        ok, why = self.ready(world, p, self_id="owner")
        assert ok is False and "'owner' is not a session id" in why

    def test_after_compact_the_compact_echo_is_clean_but_a_new_prompt_is_not(self, world):
        echo = user("<local-command-stdout>Compacted </local-command-stdout>", ago=100)
        p = world.transcript(SID_A, [user("go"), asst(), {"type": "system",
                                                         "subtype": "compact_boundary"}, echo],
                             age_s=120)
        assert self.ready(world, p)[0] is False
        assert self.ready(world, p, after_compact=True) == (True, "")
        q = world.transcript(SID_B, [user("go"), asst(), echo, user("new work", ago=90)], age_s=120)
        assert self.ready(world, q, after_compact=True)[0] is False

    def test_clean_turn_is_ready(self, world):
        assert self.ready(world, world.transcript(SID_A, [user("go"), asst()], age_s=120)) == (True, "")

    def test_a_subagent_end_turn_does_not_count(self, world):
        p = world.transcript(SID_A, [user("go"), asst(stop="tool_use", tools=[("t", "Agent", {})]),
                                     asst(ago=100, mid="s", isSidechain=True)], age_s=120)
        ok, why = self.ready(world, p)
        assert ok is False and "turn not ended" in why

    @pytest.mark.parametrize("status", ["busy", "waiting"])
    def test_registry_busy_or_waiting_blocks(self, world, status):
        p = world.transcript(SID_A, [user("go"), asst()], age_s=120)
        world.process(4242, ["/x/claude"])
        world.registry(4242, SID_A, status=status)
        assert self.ready(world, p) == (False, f"registry {status}")

    def test_quiet_question_and_self(self, world, monkeypatch):
        p = world.transcript(SID_A, [user("go"), asst()], age_s=30)
        assert "quiet" in self.ready(world, p)[1]
        q = world.transcript(SID_B, [user("go"), asst(stop="end_turn", tools=[ASK])], age_s=120)
        assert self.ready(world, q) == (False, "AskUserQuestion open")
        r = world.transcript(SID_C, [user("go"), asst()], age_s=120)
        assert self.ready(world, r, self_id=SID_C)[1] == "the target is the caller's own session"


class TestFrontEnd:
    def test_desktop_from_the_store_with_its_account_and_settings(self, world):
        (world.home / ".claude-desk").mkdir()
        world.cfg(".claude-d", email="desk@example.com", uuid="acct-desk")
        world.desktop(SID_A)
        world.transcript(SID_A, [user("go"), asst()], cfg=".claude-d")
        row = lst(world)["sessions"][0]
        assert row["front_end"] == "desktop" and row["local_id"] == LOCAL
        assert row["account"] == "desk@example.com"
        assert (row["front_model"], row["front_effort"]) == ("claude-opus-5-5", "xhigh")
        assert row["title"] == "Desk title"

    def test_remote_control_from_the_parent_rc_process(self, world):
        world.transcript(SID_A, [user("go"), asst()])
        world.process(4000, ["/x/claude", "rc", "--name", "DGX A", "--permission-mode", "default"])
        world.process(4242, ["/x/claude", "--print", "--sdk-url", "u", "--model",
                             "claude-fable-5-1", "--effort", "high"], ppid=4000,
                      unit="claude-rc-a.service")
        world.registry(4242, SID_A)
        row = lst(world)["sessions"][0]
        assert (row["front_end"], row["rc_name"], row["pid"]) == ("remote_control", "DGX A", 4242)
        assert (row["front_model"], row["front_effort"]) == ("claude-fable-5-1", "high")

    def test_headless_print_seat_with_its_unit(self, world):
        world.transcript(SID_A, [user("go"), asst()])
        world.process(5000, ["claude", "-p", "--model", "claude-sonnet-5-5", "--effort", "medium"],
                      unit="run-claude-seat-x.service")
        world.registry(5000, SID_A, bridge=False, status="busy")
        row = lst(world)["sessions"][0]
        assert (row["front_end"], row["unit"]) == ("headless", "run-claude-seat-x.service")

    def test_tmux_by_ancestry(self, world, monkeypatch):
        world.transcript(SID_A, [user("go"), asst()])
        world.process(6000, ["tmux: server"])
        world.process(6100, ["bash"], ppid=6000)
        world.process(6200, ["/x/claude"], ppid=6100)
        world.registry(6200, SID_A, bridge=False, entrypoint="cli")
        monkeypatch.setattr(oregistry, "tmux_pane_pids", lambda run=None: {6100})
        assert lst(world)["sessions"][0]["front_end"] == "tmux"

    def test_a_dead_pid_is_not_alive(self, world):
        world.transcript(SID_A, [user("go"), asst()])
        world.registry(7777, SID_A)
        reg = oregistry.registry(world.home, world.proc)
        assert reg[SID_A]["alive"] is False
        assert lst(world)["sessions"][0]["pid"] is None


class TestList:
    def test_window_and_subagents(self, world):
        world.transcript(SID_A, [user("recent"), asst()], age_s=600)
        world.transcript(SID_B, [user("old"), asst()], age_s=10 * 3600)
        write(world.home / ".claude-a" / "projects" / "-home-x-proj" / SID_A / "subagents" /
              "agent-1.jsonl", [user("sub"), asst()], 600)
        out = lst(world, active_within_min=180)
        assert [r["id"] for r in out["sessions"]] == [SID_A[:8]]
        subs = lst(world, active_within_min=180, include_subagents=True)["sessions"]
        assert any(r["subagent"] for r in subs) and len(subs) == 2

    def test_row_fields(self, world):
        world.transcript(SID_A, [user("build the thing"),
                                 asst(usage=(1, 349000, 999, 5))])
        r = lst(world)["sessions"][0]
        assert r["ctx_tokens"] == 350000 and r["ctx_pct"] == 35.0
        assert r["account"] == "a@example.com" and r["state"] == "idle"
        assert r["title"] == "build the thing" and r["cwd"] == "/home/x/proj"
        assert r["age_s"] == 600


class TestNeeds:
    def test_only_waiting_sessions_with_question_details(self, world):
        world.transcript(SID_A, [user("go"), asst(stop="tool_use", tools=[ASK])], age_s=100)
        world.transcript(SID_B, [user("go"), asst()], age_s=100)
        err = asst(text="You've hit your weekly limit", model="<synthetic>", mid="e",
                   isApiErrorMessage=True, error="rate_limit")
        world.transcript(SID_C, [user("go"), err], age_s=100)
        out = ot.session_needs(home=world.home, now=NOW, proc_root=world.proc)
        assert [(n["id"], n["need"]) for n in out["needs"]] == [(SID_A[:8], "question"),
                                                                (SID_C[:8], "limit_hit")]
        assert out["needs"][0]["questions"][0]["options"] == ["Approve once", "Skip"]
