"""Account discovery: which config dir, which identity, and what must never
come back out.

What is checked here is what would actually hurt: a config dir whose account
file is looked for in the wrong place (the two layouts on the fleet differ), a
transcript path that resolves to the wrong config dir so a child lane runs as
the wrong account, an alternate config dir silently reported as the home-root
account, an identity read that requires a field one real account file does not
have, and above all any credential-shaped value escaping the module.
"""
from __future__ import annotations

import json
import os

import pytest

from workman import account, server


def write_account(path, oauth: dict | None, extra: dict | None = None) -> None:
    """A .claude.json shaped like the real thing: oauthAccount plus the rest of
    the document, which on a real machine carries mcpServers and their tokens."""
    body: dict = dict(extra or {})
    if oauth is not None:
        body["oauthAccount"] = oauth
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")


FULL = {
    "accountUuid": "acc-1", "billingType": "subscription",
    "displayName": "Tariqul", "emailAddress": "owner@example.com",
    "fullName": "Tariqul Islam", "organizationName": "Example Org",
    "organizationUuid": "org-1", "seatTier": "max",
}

# Verified on the DGX: ~/.claude/.claude.json has NO organizationName and no
# displayName. A discovery routine that requires either drops that account.
SHORT = {"accountUuid": "acc-2", "emailAddress": "alt@example.com",
         "seatTier": "pro", "billingType": "subscription"}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """A private HOME: this module reads the owner's real ~/.claude otherwise."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    (tmp_path / ".claude").mkdir()
    return tmp_path


class TestAccountFileLayouts:
    """Both fleet layouts, verified 2026-09-05: the mini keeps its only account
    at ~/.claude.json, the DGX keeps one inside most config dirs as well."""

    def test_default_dir_falls_back_to_the_home_root_file(self, home):
        write_account(home / ".claude.json", FULL)
        out = account.describe(home / ".claude")
        assert out["account_file"] == str(home / ".claude.json")
        assert out["account"]["emailAddress"] == "owner@example.com"

    def test_a_dirs_own_file_wins_over_the_home_root_file(self, home):
        write_account(home / ".claude.json", FULL)
        write_account(home / ".claude" / ".claude.json", SHORT)
        out = account.describe(home / ".claude")
        assert out["account"]["emailAddress"] == "alt@example.com"

    def test_an_alternate_dir_never_borrows_the_root_account(self, home):
        """The trap: ~/.claude-team has no account file, so it is LOGGED OUT.
        Reporting the home-root identity for it would name the wrong account."""
        write_account(home / ".claude.json", FULL)
        (home / ".claude-team").mkdir()
        out = account.describe(home / ".claude-team")
        assert out["logged_in"] is False
        assert out["account_file"] == "" and out["account"] == {}

    def test_a_stub_config_with_no_oauth_is_logged_out(self, home):
        write_account(home / ".claude-alt" / ".claude.json", None,
                      extra={"numStartups": 3})
        assert account.describe(home / ".claude-alt")["logged_in"] is False

    def test_missing_organization_name_still_reads(self, home):
        write_account(home / ".claude-advisor" / ".claude.json", SHORT)
        out = account.describe(home / ".claude-advisor")
        assert out["logged_in"] is True
        assert "organizationName" not in out["account"]


class TestConfigDirFromTranscript:
    def test_three_levels_above_the_jsonl(self, home):
        path = home / ".claude-team" / "projects" / "-Users-x-Repo" / "sid.jsonl"
        assert account.config_dir_from_transcript(str(path)) == \
            str(home / ".claude-team")

    def test_a_cwd_with_spaces_is_not_a_problem(self, home):
        """Claude Code turns spaces into dashes too, so a rebuilt slug misses.
        Deriving from the path cannot: the slug is never parsed."""
        path = (home / ".claude" / "projects"
                / "-Users-x-New-Clean-Projects-Atmosphere-Web-App" / "sid.jsonl")
        assert account.config_dir_from_transcript(str(path)) == str(home / ".claude")

    def test_a_path_of_the_wrong_shape_yields_nothing(self, home):
        assert account.config_dir_from_transcript("/tmp/a/b/c.jsonl") == ""

    def test_empty_in_empty_out(self):
        assert account.config_dir_from_transcript("") == ""


class TestDiscover:
    def test_default_when_nothing_says_otherwise(self, home):
        write_account(home / ".claude.json", FULL)
        out = account.discover()
        assert out["source"] == "default"
        assert out["config_dir"] == str(home / ".claude")

    def test_env_wins_over_the_default(self, home, monkeypatch):
        write_account(home / ".claude-owner" / ".claude.json", SHORT)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude-owner"))
        out = account.discover()
        assert out["source"] == "env"
        assert out["account"]["emailAddress"] == "alt@example.com"

    def test_transcript_selects_the_dir_that_owns_the_session(self, home):
        """The bug this module exists to close: a hook child resolving the
        default dir instead of the one the parent session runs under."""
        write_account(home / ".claude.json", FULL)
        write_account(home / ".claude-support" / ".claude.json", SHORT)
        path = home / ".claude-support" / "projects" / "-tmp-repo" / "sid.jsonl"
        out = account.discover(transcript_path=str(path))
        assert out["source"] == "transcript"
        assert out["account"]["emailAddress"] == "alt@example.com"

    def test_env_and_transcript_disagreeing_is_reported_not_hidden(self, home, monkeypatch):
        write_account(home / ".claude-owner" / ".claude.json", SHORT)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude-owner"))
        path = home / ".claude-support" / "projects" / "-tmp-repo" / "sid.jsonl"
        out = account.discover(transcript_path=str(path))
        assert out["config_dir"] == str(home / ".claude-owner")
        assert out["transcript_config_dir"] == str(home / ".claude-support")

    def test_an_explicit_argument_beats_everything(self, home, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude-owner"))
        write_account(home / ".claude-pick" / ".claude.json", FULL)
        out = account.discover(config_dir=str(home / ".claude-pick"))
        assert out["source"] == "argument"
        assert out["config_dir"] == str(home / ".claude-pick")

    def test_a_machine_with_no_account_at_all_does_not_raise(self, home):
        out = account.discover()
        assert out["ok"] is True and out["logged_in"] is False


class TestEnumerate:
    def test_finds_every_config_dir_and_marks_the_active_one(self, home, monkeypatch):
        write_account(home / ".claude.json", FULL)
        write_account(home / ".claude-support" / ".claude.json", SHORT)
        (home / ".claude-team").mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude-support"))
        out = account.enumerate_accounts()
        dirs = {entry["config_dir"] for entry in out["accounts"]}
        assert dirs == {str(home / d) for d in (".claude", ".claude-support",
                                                ".claude-team")}
        active = [e for e in out["accounts"] if e["active"]]
        assert len(active) == 1
        assert active[0]["config_dir"] == str(home / ".claude-support")
        assert out["signed_in"] == 2 and out["count"] == 3

    def test_a_directory_is_listed_once_however_it_was_reached(self, home, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
        out = account.enumerate_accounts()
        dirs = [entry["config_dir"] for entry in out["accounts"]]
        assert len(dirs) == len(set(dirs))

    def test_a_config_dir_env_naming_a_missing_dir_is_still_reported(self, home, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude-gone"))
        out = account.enumerate_accounts()
        assert out["active_config_dir"] == str(home / ".claude-gone")
        assert any(e["active"] for e in out["accounts"])


class TestGrokSeat:
    @pytest.fixture(autouse=True)
    def only_home_candidates(self, home, monkeypatch):
        """Pin the search to HOME so the test does not see whatever grok this
        machine happens to have in /usr/local/bin."""
        monkeypatch.setattr(account, "GROK_CLI_CANDIDATES", ("~/.grok/bin/grok",))

    def test_absent_grok_is_reported_not_an_error(self, home):
        out = account.grok_seat()
        assert out["ok"] is True
        assert out["installed"] is False and out["logged_in"] is False

    def test_seat_marker_is_the_login_test(self, home):
        grok = home / ".grok"
        grok.mkdir()
        (grok / "auth.json").write_text(
            json.dumps({"https://auth.x.ai::b1a0-not-a-real-id": {"a": 1}}))
        (grok / "models_cache.json").write_text(json.dumps({"auth_method": "session"}))
        out = account.grok_seat()
        assert out["logged_in"] is True and out["auth_method"] == "session"

    def test_an_auth_file_without_the_marker_is_not_a_seat(self, home):
        grok = home / ".grok"
        grok.mkdir()
        (grok / "auth.json").write_text(json.dumps({"something-else": 1}))
        assert account.grok_seat()["logged_in"] is False

    def test_an_exported_key_is_flagged_as_presence_only(self, home, monkeypatch):
        monkeypatch.setenv("XAI_API_KEY", "xai-THIS-VALUE-MUST-NOT-ESCAPE")
        out = account.grok_seat()
        assert out["api_key_env_set"] is True
        assert "THIS-VALUE-MUST-NOT-ESCAPE" not in json.dumps(out)

    def test_the_cli_is_found_by_path_and_executable_bit(self, home):
        binary = home / ".grok" / "bin" / "grok"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")
        assert account.grok_seat()["installed"] is False   # not executable yet
        os.chmod(binary, 0o755)
        out = account.grok_seat()
        assert out["installed"] is True and out["cli"] == str(binary)


class TestNothingCredentialShapedEscapes:
    """The property that matters most. An allowlist is what makes it provable:
    a field added upstream is simply not copied."""

    POISON = {
        "accessToken": "sk-ant-oat01-POISON-ACCESS",
        "refreshToken": "sk-ant-ort01-POISON-REFRESH",
        "apiKey": "sk-POISON-API-KEY",
        "sessionKey": "sessionKey-POISON",
        "scopes": ["user:inference"],
    }

    def _poisoned_home(self, home):
        write_account(home / ".claude.json", {**FULL, **self.POISON}, extra={
            "mcpServers": {"secretive": {
                "env": {"AUTH_TOKEN": "Bearer POISON-MCP-TOKEN",
                        "ANTHROPIC_API_KEY": "sk-ant-POISON-ENV"}}},
            "oauthAccountBackup": {"accessToken": "POISON-BACKUP"},
        })
        grok = home / ".grok"
        grok.mkdir(exist_ok=True)
        (grok / "auth.json").write_text(json.dumps(
            {"https://auth.x.ai::abc": {"access_token": "POISON-GROK-TOKEN",
                                        "refresh_token": "POISON-GROK-REFRESH"}}))

    @pytest.mark.parametrize("call", [
        lambda: account.discover(),
        lambda: account.active(),
        lambda: account.enumerate_accounts(),
        lambda: account.lanes(),
        lambda: account.grok_seat(),
    ])
    def test_no_poison_value_reaches_the_caller(self, home, call):
        self._poisoned_home(home)
        blob = json.dumps(call(), default=str)
        assert "POISON" not in blob
        # And the identity that SHOULD come through still does.
        assert "sk-" not in blob and "Bearer" not in blob

    def test_only_allowlisted_fields_are_copied(self, home):
        self._poisoned_home(home)
        keys = set(account.discover()["account"])
        assert keys <= set(account.IDENTITY_FIELDS)
        assert "fullName" not in keys      # present in the file, not on the list

    def test_the_allowlist_holds_no_credential_shaped_names(self):
        banned = ("token", "key", "secret", "password", "credential", "cookie",
                  "auth", "bearer")
        for field in account.IDENTITY_FIELDS:
            assert not any(word in field.lower() for word in banned), field

    def test_a_nested_value_on_an_allowlisted_name_is_dropped(self, home):
        """A dict or list smuggled in under an allowed name would carry whatever
        is inside it. Only scalars are copied."""
        write_account(home / ".claude.json",
                      {"emailAddress": {"real": "a@b.c", "token": "POISON-NEST"},
                       "seatTier": ["POISON-LIST"], "accountUuid": "acc-9"})
        out = account.discover()
        assert "POISON" not in json.dumps(out["account"], default=str)
        assert out["account"] == {"accountUuid": "acc-9"}

    def test_the_human_line_carries_no_secret(self, home):
        self._poisoned_home(home)
        assert "POISON" not in account._line(account.active())


class TestToolsAreRegistered:
    """Declared on the server, not just importable from account."""

    @pytest.mark.parametrize("name", ["account_lanes", "account_active"])
    def test_tool_exists_on_the_server(self, name):
        assert callable(getattr(server, name, None))

    def test_active_tool_delegates(self, home):
        write_account(home / ".claude.json", FULL)
        out = server.account_active()
        assert out["account"]["emailAddress"] == "owner@example.com"
        assert out["grok"]["ok"] is True

    def test_lanes_tool_carries_both_lanes(self, home):
        write_account(home / ".claude.json", FULL)
        out = server.account_lanes()
        assert out["claude"]["count"] >= 1 and out["grok"]["ok"] is True

    def test_a_tool_returns_a_dict_rather_than_raising(self, home, monkeypatch):
        monkeypatch.setattr(account, "config_dirs",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        out = server.account_lanes()
        assert out["ok"] is False and "boom" in out["error"]


class TestCli:
    def test_the_human_line_names_the_account_and_the_seat(self, home, capsys):
        write_account(home / ".claude.json", FULL)
        assert account.main([]) == 0
        line = capsys.readouterr().out.strip()
        assert "owner@example.com" in line and "grok:" in line
        assert "\n" not in line          # exactly one line

    def test_json_mode_is_parseable(self, home, capsys):
        write_account(home / ".claude.json", FULL)
        account.main(["--json"])
        data = json.loads(capsys.readouterr().out)
        assert data["account"]["emailAddress"] == "owner@example.com"

    def test_all_lists_every_dir_and_marks_one(self, home, capsys):
        write_account(home / ".claude.json", FULL)
        write_account(home / ".claude-support" / ".claude.json", SHORT)
        account.main(["--all"])
        out = capsys.readouterr().out
        assert out.count("*") == 1 and "alt@example.com" in out

    def test_transcript_flag_selects_the_lane(self, home, capsys):
        write_account(home / ".claude-support" / ".claude.json", SHORT)
        path = home / ".claude-support" / "projects" / "-tmp-r" / "sid.jsonl"
        account.main(["--transcript", str(path)])
        assert "alt@example.com" in capsys.readouterr().out

    def test_a_missing_transcript_value_does_not_crash(self, home, capsys):
        assert account.main(["--transcript"]) == 0
