"""Accounts without secrets, and which session is the caller (env or process
tree). Fake HOME and fake /proc only."""
from __future__ import annotations

import json
import os

import pytest

from workman.orchestra import registry as oregistry
from workman import orchestra_tools as ot

from orchestra_fakes import (BRIDGE, NOW, SECRET, SID_A, SID_B, asst, lst, read_world, user)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return read_world(tmp_path, monkeypatch)


class TestAccounts:
    def test_email_per_dir_and_default_dir_falls_back_to_home_json(self, world):
        world.cfg(".claude", email="dir@example.com", uuid="acct-dir")
        (world.home / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"emailAddress": "home@example.com", "accountUuid": "acct-home"},
             "oauthToken": SECRET}))
        a = oregistry.accounts(world.home)
        assert a["by_dir"][".claude-a"] == {"email": "a@example.com",
                                            "source": "~/.claude-a/.claude.json"}
        assert a["by_dir"][".claude"]["email"] == "home@example.com"
        assert a["by_uuid"]["acct-dir"] == "dir@example.com"
        assert SECRET not in json.dumps(a)

    def test_a_dir_without_oauth_has_no_email(self, world):
        world.cfg(".claude-x", email="")
        assert oregistry.accounts(world.home)["by_dir"][".claude-x"]["email"] == ""

    def test_no_secret_reaches_any_output(self, world):
        world.transcript(SID_A, [user("go"), asst()])
        world.process(4242, ["/x/claude", "--print", "--sdk-url", "https://h/" + BRIDGE,
                             "--session-id", BRIDGE, "--model", "claude-opus-5-5"], ppid=4000,
                      unit="claude-rc-a.service")
        world.process(4000, ["/x/claude", "rc", "--name", "DGX A"])
        world.registry(4242, SID_A)
        world.desktop(SID_B)
        world.transcript(SID_B, [user("desk"), asst()])
        blob = json.dumps(lst(world)) + json.dumps(ot.session_needs(
            home=world.home, now=NOW, proc_root=world.proc))
        for secret in (SECRET, BRIDGE, "secret.sock"):
            assert secret not in blob


class TestCaller:
    def test_caller_found_through_the_process_tree(self, world):
        world.transcript(SID_A, [user("go"), asst()])
        me = os.getpid()
        world.process(4242, ["/x/claude", "--print"])
        world.process(me, ["python", "-m", "pytest"], ppid=4242)
        world.registry(4242, SID_A)
        assert oregistry.caller_ids(home=world.home, proc_root=world.proc) == [SID_A]
        assert lst(world)["sessions"][0]["is_self"] is True

    def test_no_caller_at_all_is_empty(self, world):
        assert oregistry.caller_ids(home=world.home, proc_root=world.proc) == []
        assert oregistry.caller_ids("a,b", home=world.home, proc_root=world.proc) == []
        both = f"{SID_A},{SID_B}"
        assert oregistry.caller_ids(both, home=world.home, proc_root=world.proc) == [SID_A, SID_B]
        assert oregistry.bad_self_id("owner") and not oregistry.bad_self_id(SID_A[:8])

    def test_env_ids_that_are_not_session_ids_are_dropped(self, world, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "owner")
        assert oregistry.self_ids() == []
