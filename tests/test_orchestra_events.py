"""The event feed: dedupe across passes, epoch cursors, kinds, the fleet hook
feed, the collector lock, lost state, live keys kept past the trim."""
from __future__ import annotations

import fcntl
import json
import os

import pytest

from workman.orchestra import common as ocommon
from workman.orchestra import events as oevents

from orchestra_fakes import (ASK, NOW, SID_A, SID_B, SID_C, SID_D, asst, read_world, result, ts, user)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return read_world(tmp_path, monkeypatch)


class TestEvents:
    def collect(self, world, **kw):
        return oevents.collect(home=world.home, now=NOW, proc_root=world.proc, **kw)

    def test_dedupe_across_passes_and_cursor(self, world):
        world.transcript(SID_A, [user("go"), asst(stop="tool_use", tools=[ASK])], age_s=100)
        first = self.collect(world)
        assert first["new"] == 1 and first["events"][0]["kind"] == "question"
        assert first["events"][0]["questions"][0]["options"] == ["Approve once", "Skip"]
        assert self.collect(world)["new"] == 0
        world.transcript(SID_B, [user("go"), asst(usage=(0, 350000, 0, 1))], age_s=120)
        third = self.collect(world)
        assert {e["kind"] for e in third["events"]} == {"compact_ready", "finished"}
        feed = oevents.session_events(since_cursor=first["cursor"], collect_first=False,
                                 home=world.home)
        assert [e["seq"] for e in feed["events"]] == [2, 3]
        epoch = first["cursor"].split(":")[0]
        assert epoch and feed["cursor"] == f"{epoch}:3"
        again = oevents.session_events(since_cursor=feed["cursor"], collect_first=False, home=world.home)
        assert again["events"] == [] and again["cursor"] == f"{epoch}:3"

    def test_a_cursor_from_a_reset_feed_starts_over_and_skips_nothing(self, world):
        world.transcript(SID_A, [user("go"), asst(stop="tool_use", tools=[ASK])], age_s=100)
        world.transcript(SID_B, [user("go"), asst(usage=(0, 350000, 0, 1))], age_s=120)
        old = self.collect(world)["cursor"]                   # epoch1:3
        odir = world.home / "orchestra"
        for f in ("session-events.jsonl", "session-events.state.json"):
            (odir / f).unlink()
        world.transcript(SID_C, [user("go"), asst(stop="tool_use", tools=[ASK])], age_s=100)
        fresh = self.collect(world)                            # epoch2: seqs 1..4
        assert fresh["cursor"].split(":")[0] != old.split(":")[0]
        got = oevents.session_events(since_cursor=old, collect_first=False, home=world.home)
        assert got["cursor_reset"] is True
        assert [e["seq"] for e in got["events"]] == [1, 2, 3, 4]

    def test_a_numeric_cursor_past_the_end_starts_over(self, world):
        world.transcript(SID_A, [user("go"), asst(stop="tool_use", tools=[ASK])], age_s=100)
        self.collect(world)
        got = oevents.session_events(since_cursor="9", collect_first=False, home=world.home)
        assert got["cursor_reset"] is True and [e["seq"] for e in got["events"]] == [1]
        assert got["cursor"].endswith(":1")

    def test_live_keys_survive_the_seen_trim(self, world, monkeypatch):
        monkeypatch.setattr(oevents, "SEEN_KEEP", 1)
        world.transcript(SID_A, [user("go"), asst(stop="tool_use", tools=[ASK])], age_s=100)
        assert self.collect(world)["new"] == 1
        for i in range(3):   # other, short-lived keys push the question's key out of the window
            world.transcript(SID_B, [user("go"), asst(usage=(0, 350000, 0, 1), mid=f"x{i}",
                                                      ago=50 + i)], age_s=120)
            self.collect(world)
        again = self.collect(world)
        assert again["new"] == 0
        lines = (world.home / "orchestra" / "session-events.jsonl").read_text().splitlines()
        assert sum(1 for x in lines if json.loads(x)["kind"] == "question") == 1

    def test_kinds_filter_and_unknown_kind(self, world):
        world.transcript(SID_A, [user("go"), asst(stop="tool_use", tools=[ASK])], age_s=100)
        world.transcript(SID_B, [user("go"), asst(usage=(0, 350000, 0, 1))], age_s=120)
        self.collect(world)
        got = oevents.session_events(kinds="compact_ready", collect_first=False, home=world.home)
        assert [e["kind"] for e in got["events"]] == ["compact_ready"]
        assert oevents.session_events(kinds="bogus", collect_first=False,
                                 home=world.home)["ok"] is False
        assert oevents.session_events(since_cursor="x", collect_first=False,
                                 home=world.home)["ok"] is False

    def test_compact_ready_only_inside_the_band_and_at_a_clean_turn(self, world):
        world.transcript(SID_A, [user("go"), asst(usage=(0, 250000, 0, 1))], age_s=120)
        world.transcript(SID_B, [user("go"), asst(usage=(0, 450000, 0, 1))], age_s=120)
        world.transcript(SID_C, [user("go"), asst(usage=(0, 350000, 0, 1))], age_s=10)
        world.transcript(SID_D, [user("go"), asst(usage=(0, 350000, 0, 1), stop="tool_use",
                                                  tools=[("t", "Bash", {})])], age_s=120)
        kinds = [(e["session"], e["kind"]) for e in self.collect(world)["events"]]
        assert not any(k == "compact_ready" for _, k in kinds)

    def test_denial_fires_blocked_once_even_while_working(self, world):
        world.transcript(SID_A, [user("go"), asst(stop="tool_use", tools=[("t1", "Bash", {})]),
                                 result("t1", "x", is_error=True,
                                        toolDenialKind="automode-blocked")], age_s=5)
        ev = self.collect(world)["events"]
        assert [e["kind"] for e in ev] == ["blocked"]
        assert self.collect(world)["new"] == 0

    def test_the_callers_own_session_is_skipped(self, world, monkeypatch):
        world.transcript(SID_A, [user("go"), asst(stop="tool_use", tools=[ASK])], age_s=100)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID_A)
        assert self.collect(world)["new"] == 0

    def test_fleet_feed_permission_prompts_are_ingested_once(self, world):
        feed = world.home / ".claude" / "fleet-session-events.jsonl"
        feed.parent.mkdir(parents=True, exist_ok=True)
        lines = [{"ts": ocommon.iso(NOW - 60), "event": "Notification", "session_id": SID_D,
                  "cwd": "/w", "msg": "Claude needs your permission to use Bash"},
                 {"ts": ocommon.iso(NOW - 50), "event": "Notification", "session_id": SID_D,
                  "cwd": "/w", "msg": "Claude needs your permission to use Bash"},
                 {"ts": ocommon.iso(NOW - 60), "event": "Notification", "session_id": SID_D,
                  "cwd": "/w", "msg": "Claude needs your permission to use AskUserQuestion"},
                 {"ts": ocommon.iso(NOW - 60), "event": "Stop", "session_id": SID_D, "cwd": "/w",
                  "msg": ""},
                 {"ts": ocommon.iso(NOW - 7200), "event": "Notification", "session_id": SID_D,
                  "cwd": "/w", "msg": "Tool permission requested"},
                 {"ts": ocommon.iso(NOW - 60), "event": "Notification", "session_id": SID_C,
                  "cwd": "/w", "msg": "Background task completed: permission audit"},
                 {"ts": ocommon.iso(NOW - 60), "event": "Notification", "session_id": SID_C,
                  "cwd": "/w", "msg": "Tool permission requested"}]
        feed.write_text("".join(json.dumps(x) + "\n" for x in lines))
        ev = self.collect(world)["events"]
        assert [(e["kind"], e["session"], e["detail"]) for e in ev] == [
            ("permission", SID_D[:8], "Claude needs your permission to use Bash"),
            ("permission", SID_C[:8], "Tool permission requested")]
        assert all(e["source"] == "fleet-session-events" for e in ev)
        assert self.collect(world)["new"] == 0

    def test_a_second_collector_waits_its_turn(self, world):
        odir = world.home / "orchestra"
        odir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(odir / "session-events.lock"), os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            out = self.collect(world)
        finally:
            os.close(fd)
        assert out["ok"] is False and out["busy"] is True

    def test_a_lost_state_file_does_not_replay_events(self, world):
        world.transcript(SID_A, [user("go"), asst(stop="tool_use", tools=[ASK])], age_s=100)
        assert self.collect(world)["new"] == 1
        (world.home / "orchestra" / "session-events.state.json").unlink()
        again = self.collect(world)
        assert again["new"] == 0 and again["cursor"].endswith(":1")
        first_epoch = json.loads((world.home / "orchestra" / "session-events.jsonl")
                                 .read_text().splitlines()[0])["epoch"]
        assert again["cursor"] == f"{first_epoch}:1"   # the epoch is rebuilt from the feed

    def test_silent_fires_once_and_only_inside_its_window(self, world):
        world.transcript(SID_A, [user("go"), asst(stop="tool_use", tools=[("t", "Bash", {})])],
                         age_s=30 * 60)
        world.transcript(SID_B, [user("go"), asst(stop="tool_use", tools=[("t", "Bash", {})])],
                         age_s=150 * 60)
        ev = self.collect(world)["events"]
        assert [(e["session"], e["kind"]) for e in ev] == [(SID_A[:8], "silent")]
