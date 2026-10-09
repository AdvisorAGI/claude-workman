"""Usage: dedupe per message id, images, turns, the window, repeats, by account."""
from __future__ import annotations


import pytest

from workman import orchestra_tools as ot

from orchestra_fakes import (NOW, SID_A, SID_B, SID_C, SID_D, asst, read_world, ts, user)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return read_world(tmp_path, monkeypatch)


class TestUsage:
    def test_dedupes_per_message_counts_images_turns_and_window(self, world):
        img = {"type": "user", "timestamp": ts(300), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": [
                {"type": "image", "source": {}}, {"type": "text", "text": "x"}]}]}}
        world.transcript(SID_A, [
            user("long enough first prompt for the group", ago=900),
            asst(mid="m1", usage=(1, 100, 10, 5), ago=800),
            asst(mid="m1", usage=(1, 100, 10, 5), ago=799),   # same message, second block
            img, user("second prompt", ago=200),
            asst(mid="m2", usage=(2, 200, 20, 7), ago=100),
            asst(mid="old", usage=(1000, 1000, 1000, 1000), ago=3 * 86400)])
        out = ot.session_usage(window_h=24, home=world.home, now=NOW)
        r = out["rows"][0]
        assert (r["input"], r["cache_read"], r["cache_creation"], r["output"]) == (3, 300, 30, 12)
        assert r["total"] == 345 and r["api_calls"] == 2
        assert r["images"] == 1 and r["turns"] == 2

    def test_repeated_first_prompts_are_exposed(self, world):
        prompt = "Run the nightly audit of repo 1234 and report"
        world.transcript(SID_A, [user(prompt), asst(mid="a")])
        world.transcript(SID_B, [user(prompt.replace("1234", "5678")), asst(mid="b")])
        world.transcript(SID_C, [user("hi"), asst(mid="c")])
        world.transcript(SID_D, [user("hi"), asst(mid="d")])
        out = ot.session_usage(window_h=24, home=world.home, now=NOW)
        assert len(out["repeats"]) == 1  # "hi" twice is not repeated work
        rep = out["repeats"][0]
        assert rep["sessions"] == 2 and sorted(rep["ids"]) == [SID_A[:8], SID_B[:8]]
        assert {r["id"]: r["repeat"] for r in out["rows"]}[SID_A[:8]] == 2

    def test_by_account(self, world):
        world.cfg(".claude-b", email="b@example.com", uuid="acct-b")
        world.transcript(SID_A, [user("one"), asst(mid="a", usage=(1, 1, 1, 1))])
        world.transcript(SID_B, [user("two"), asst(mid="b", usage=(1, 1, 1, 1))])
        world.transcript(SID_C, [user("three"), asst(mid="c", usage=(5, 5, 5, 5))], cfg=".claude-b")
        out = ot.session_usage(window_h=24, by="account", home=world.home, now=NOW)
        assert [(a["account"], a["sessions"], a["total"]) for a in out["rows"]] == [
            ("b@example.com", 1, 20), ("a@example.com", 2, 8)]

    def test_rejects_a_bad_grouping(self, world):
        assert ot.session_usage(by="model", home=world.home, now=NOW)["ok"] is False
