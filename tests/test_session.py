"""Session continuity: the token read, the zones, and the files that survive a
compaction.

What is checked here is what would actually hurt: a token count that disagrees
with the shell hook computing the same thing, a zone boundary off by one, a
memory directory hardcoded instead of derived from the cwd, a verdict file
written somewhere the plugin half does not look, and an append that overwrites
the ledger it was supposed to extend.
"""
from __future__ import annotations

import json
import os

import pytest

from workman import server, session


def usage_line(input_tokens=0, cache_creation=0, cache_read=0, output=0,
               role="assistant") -> str:
    return json.dumps({"type": role, "message": {"role": role, "usage": {
        "input_tokens": input_tokens,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
        "output_tokens": output}}})


def transcript(home, project: str, session_id: str, lines: list[str]) -> str:
    directory = home / ".claude" / "projects" / project
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """A private HOME and verdict dir: these tools write to the owner's real
    ~/.claude and /tmp otherwise."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WORKMAN_VERDICT_DIR", str(tmp_path / "verdicts"))
    monkeypatch.delenv("COMPACT_FLOOR", raising=False)
    return tmp_path


class TestContextTokens:
    def test_sums_the_four_usage_counters(self, home):
        path = transcript(home, "-tmp-repo", "sid", [usage_line(10, 20, 30, 40)])
        assert session.context_tokens(path) == 100

    def test_reads_the_last_turn_not_the_first(self, home):
        path = transcript(home, "-tmp-repo", "sid",
                          [usage_line(1, 1, 1, 1), usage_line(100, 200, 300, 400)])
        assert session.context_tokens(path) == 1000

    def test_skips_lines_that_are_not_json(self, home):
        path = transcript(home, "-tmp-repo", "sid",
                          [usage_line(5, 0, 0, 5), "{ this is not json"])
        assert session.context_tokens(path) == 10

    def test_missing_counters_count_as_zero(self, home):
        directory = home / ".claude" / "projects" / "-tmp-repo"
        directory.mkdir(parents=True)
        path = directory / "sid.jsonl"
        path.write_text(json.dumps(
            {"message": {"usage": {"input_tokens": 7, "output_tokens": None}}}) + "\n")
        assert session.context_tokens(str(path)) == 7

    def test_missing_file_is_zero_not_an_exception(self, home):
        assert session.context_tokens(str(home / "nope.jsonl")) == 0

    def test_matches_the_hook_computation_verbatim(self, home):
        """The reference read, copied from context-step.sh. If this drifts, the
        hook and the tool tell the session two different numbers."""
        path = transcript(home, "-tmp-repo", "sid", [
            json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}),
            usage_line(1200, 34000, 210000, 900),
            json.dumps({"type": "summary", "summary": "no usage here"}),
        ])
        ctx = 0
        with open(path, "rb") as fh:
            fh.seek(max(0, os.path.getsize(path) - 600000))
            tail = fh.read().decode("utf-8", "replace")
        for line in reversed(tail.splitlines()):
            try:
                d = json.loads(line)
            except Exception:
                continue
            u = (d.get("message") or {}).get("usage") if isinstance(d, dict) else None
            if u:
                ctx = sum(int(u.get(k) or 0) for k in (
                    "input_tokens", "cache_creation_input_tokens",
                    "cache_read_input_tokens", "output_tokens"))
                break
        assert session.context_tokens(path) == ctx == 246100


class TestWindowAndFloor:
    def test_window_comes_from_settings(self, home):
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"autoCompactWindow": 300000}))
        assert session.window() == 300000

    def test_window_falls_back_when_settings_are_missing(self, home):
        assert session.window() == session.DEFAULT_WINDOW

    def test_window_falls_back_when_settings_are_unreadable(self, home):
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        (home / ".claude" / "settings.json").write_text("{ not json")
        assert session.window() == session.DEFAULT_WINDOW

    def test_floor_honours_the_env_the_hooks_honour(self, monkeypatch):
        monkeypatch.setenv("COMPACT_FLOOR", "180000")
        assert session.floor() == 180000

    def test_garbage_floor_falls_back(self, monkeypatch):
        monkeypatch.setenv("COMPACT_FLOOR", "loads")
        assert session.floor() == session.DEFAULT_FLOOR


class TestZones:
    @pytest.mark.parametrize("context,expected", [
        (0, "quiet"), (249_999, "quiet"), (250_000, "judged"),
        (399_999, "judged"), (400_000, "backstop"), (450_000, "backstop"),
    ])
    def test_boundaries(self, context, expected):
        assert session.zone_for(context, 400_000, 250_000) == expected


class TestMemoryDir:
    def test_derived_from_the_cwd_never_hardcoded(self, home):
        assert session.memory_dir("/Users/x/Repo") == (
            home / ".claude" / "projects" / "-Users-x-Repo" / "memory")

    def test_a_different_cwd_gives_a_different_dir(self, home):
        assert session.memory_dir("/home/monzurul/x") != session.memory_dir("/Users/x/Repo")

    def test_empty_cwd_uses_the_process_cwd(self, home):
        assert session.memory_dir("") == session.memory_dir(os.getcwd())


class TestContextStatus:
    def test_resolves_the_transcript_from_the_session_id(self, home):
        transcript(home, "-tmp-a", "older", [usage_line(1, 0, 0, 1)])
        transcript(home, "-tmp-b", "wanted", [usage_line(100_000, 100_000, 100_000, 0)])
        out = session.context_status(session_id="wanted")
        assert out["ok"] is True
        assert out["context"] == 300_000
        assert out["zone"] == "judged"
        assert out["percent"] == 75.0
        assert out["session_id"] == "wanted"

    def test_falls_back_to_the_newest_transcript(self, home):
        old = transcript(home, "-tmp-a", "old", [usage_line(1, 0, 0, 1)])
        new = transcript(home, "-tmp-b", "new", [usage_line(2, 0, 0, 2)])
        os.utime(old, (1, 1))
        out = session.context_status()
        assert out["transcript"] == new
        assert out["session_id"] == "new"          # a transcript is named for its session

    def test_explicit_path_wins(self, home):
        transcript(home, "-tmp-a", "other", [usage_line(9, 9, 9, 9)])
        path = transcript(home, "-tmp-b", "mine", [usage_line(1, 1, 1, 1)])
        assert session.context_status(transcript_path=path)["context"] == 4

    def test_reports_the_zone_at_the_backstop(self, home):
        transcript(home, "-tmp-a", "sid", [usage_line(400_000, 0, 0, 0)])
        assert session.context_status(session_id="sid")["zone"] == "backstop"

    def test_carries_the_verdict_when_one_exists(self, home):
        transcript(home, "-tmp-a", "sid", [usage_line(260_000, 0, 0, 0)])
        session.compact_verdict("sid", compact_now=True, reason="bulk output")
        out = session.context_status(session_id="sid")
        assert out["verdict"]["compact_now"] is True
        assert out["verdict"]["reason"] == "bulk output"

    def test_no_verdict_is_none_not_an_error(self, home):
        transcript(home, "-tmp-a", "sid", [usage_line(10, 0, 0, 0)])
        assert session.context_status(session_id="sid")["verdict"] is None

    def test_memory_dir_follows_the_cwd_argument(self, home):
        transcript(home, "-tmp-a", "sid", [usage_line(10, 0, 0, 0)])
        out = session.context_status(session_id="sid", cwd="/Users/x/Repo")
        assert out["memory_dir"].endswith("/-Users-x-Repo/memory")

    def test_no_transcript_anywhere_is_a_structured_error(self, home):
        out = session.context_status()
        assert out["ok"] is False
        assert "error" in out and out["window"] == session.DEFAULT_WINDOW


class TestCompactVerdict:
    def test_reading_a_missing_verdict_is_not_a_failure(self, home):
        out = session.compact_verdict("sid")
        assert out["ok"] is True and out["exists"] is False and out["verdict"] is None

    def test_writes_the_contract_shape(self, home):
        transcript(home, "-tmp-a", "sid", [usage_line(300_000, 0, 0, 0)])
        out = session.compact_verdict("sid", compact_now=False, reason="mid debug",
                                      blockers=["grok lane running"],
                                      handoff_current=True)
        assert out["ok"] is True
        written = json.loads((home / "verdicts" / "compact-verdict.sid.json")
                             .read_text(encoding="utf-8"))
        assert written == out["verdict"]
        assert set(written) == {"compact_now", "context", "window", "reason",
                                "blockers", "handoff_current"}
        assert written["compact_now"] is False
        assert written["context"] == 300_000
        assert written["window"] == 400_000
        assert written["blockers"] == ["grok lane running"]
        assert written["handoff_current"] is True

    def test_the_path_is_the_one_the_hooks_read(self, home):
        out = session.compact_verdict("abc-123", compact_now=True)
        assert out["path"].endswith("compact-verdict.abc-123.json")
        assert not list((home / "verdicts").glob("*.tmp")), "atomic write left a temp file"

    def test_default_verdict_dir_is_tmp(self, monkeypatch):
        monkeypatch.delenv("WORKMAN_VERDICT_DIR", raising=False)
        assert str(session.verdict_path("sid")) == "/tmp/compact-verdict.sid.json"

    def test_overwriting_replaces_the_previous_verdict(self, home):
        session.compact_verdict("sid", compact_now=True, reason="first",
                                blockers=["a"], handoff_current=True)
        session.compact_verdict("sid", compact_now=False, reason="second",
                                blockers=[], handoff_current=False)
        verdict = session.read_verdict("sid")
        assert verdict["reason"] == "second"
        assert verdict["blockers"] == []
        assert verdict["compact_now"] is False

    def test_unset_handoff_current_keeps_the_previous_answer(self, home):
        session.compact_verdict("sid", compact_now=False, handoff_current=True)
        session.compact_verdict("sid", compact_now=True, reason="clean break")
        assert session.read_verdict("sid")["handoff_current"] is True

    def test_session_id_is_required_to_write(self, home):
        out = session.compact_verdict("", compact_now=True)
        assert out["ok"] is False and "session_id" in out["error"]


class TestLedger:
    def test_creates_the_ledger_with_a_dated_block(self, home):
        out = session.ledger_append("/Users/x/Repo", ["ran gate: 14 failed", "PR #80 merged"])
        assert out["ok"] is True and out["bullets"] == 2 and out["created"] is True
        text = (home / ".claude" / "projects" / "-Users-x-Repo" / "memory"
                / "turn-ledger.md").read_text(encoding="utf-8")
        assert text.startswith("## ")
        assert "- ran gate: 14 failed\n" in text
        assert "- PR #80 merged\n" in text

    def test_appends_without_losing_the_previous_block(self, home):
        session.ledger_append("/Users/x/Repo", ["first"])
        out = session.ledger_append("/Users/x/Repo", ["second"])
        text = open(out["path"], encoding="utf-8").read()
        assert "- first" in text and "- second" in text
        assert text.count("## ") == 2
        assert out["created"] is False

    def test_a_multiline_entry_stays_one_bullet(self, home):
        out = session.ledger_append("/Users/x/Repo", ["line one\nline two"])
        text = open(out["path"], encoding="utf-8").read()
        assert "- line one line two\n" in text
        assert out["bullets"] == 1

    def test_blank_entries_are_dropped(self, home):
        out = session.ledger_append("/Users/x/Repo", ["real", "", "   "])
        assert out["bullets"] == 1

    def test_nothing_to_write_is_reported_not_written(self, home):
        out = session.ledger_append("/Users/x/Repo", [])
        assert out["ok"] is False
        assert not (home / ".claude" / "projects" / "-Users-x-Repo").exists()

    def test_writes_under_the_project_derived_from_cwd(self, home):
        out = session.ledger_append("/home/monzurul/agentx", ["dgx side"])
        assert "/-home-monzurul-agentx/memory/turn-ledger.md" in out["path"]


class TestHandoff:
    def test_reading_a_missing_handoff_is_not_a_failure(self, home):
        out = session.handoff("/Users/x/Repo")
        assert out["ok"] is True and out["exists"] is False and out["content"] == ""

    def test_write_then_read_round_trips(self, home):
        written = session.handoff("/Users/x/Repo", content="DONE: nothing yet")
        assert written["ok"] is True and written["created"] is True
        assert written["bytes"] == len("DONE: nothing yet\n".encode("utf-8"))
        read = session.handoff("/Users/x/Repo")
        assert read["content"] == "DONE: nothing yet\n"
        assert read["exists"] is True

    def test_writing_replaces_wholesale(self, home):
        session.handoff("/Users/x/Repo", content="old plan")
        session.handoff("/Users/x/Repo", content="new plan")
        assert session.handoff("/Users/x/Repo")["content"] == "new plan\n"

    def test_lands_beside_the_ledger(self, home):
        ledger = session.ledger_append("/Users/x/Repo", ["a fact"])["path"]
        handoff = session.handoff("/Users/x/Repo", content="NEXT: run the gate")["path"]
        assert os.path.dirname(ledger) == os.path.dirname(handoff)
        assert handoff.endswith("session-handoff-latest.md")


class TestNeverRaises:
    """An MCP tool that raises tells the model nothing it can act on."""

    def test_status_on_a_directory_instead_of_a_transcript(self, home):
        out = session.context_status(transcript_path=str(home))
        assert isinstance(out, dict) and "ok" in out

    def test_ledger_with_a_non_string_entry(self, home):
        assert session.ledger_append("/Users/x/Repo", [{"not": "a string"}])["ok"] is True

    def test_ledger_on_an_unwritable_path(self, home, monkeypatch):
        monkeypatch.setattr(session, "memory_dir", lambda cwd="": home / "file" / "memory")
        (home / "file").write_text("I am a file, not a directory")
        out = session.ledger_append("/Users/x/Repo", ["x"])
        assert out["ok"] is False and "error" in out

    def test_handoff_on_an_unwritable_path(self, home, monkeypatch):
        monkeypatch.setattr(session, "memory_dir", lambda cwd="": home / "file" / "memory")
        (home / "file").write_text("I am a file, not a directory")
        out = session.handoff("/Users/x/Repo", content="x")
        assert out["ok"] is False and "error" in out

    def test_verdict_in_an_unwritable_dir(self, home, monkeypatch):
        (home / "file").write_text("I am a file, not a directory")
        monkeypatch.setenv("WORKMAN_VERDICT_DIR", str(home / "file" / "verdicts"))
        out = session.compact_verdict("sid", compact_now=True)
        assert out["ok"] is False and "error" in out


class TestToolsAreRegistered:
    """The tools are declared on the server, not just importable from session."""

    @pytest.mark.parametrize("name", ["session_context_status", "session_compact_verdict",
                                      "session_ledger_append", "session_handoff"])
    def test_tool_exists_on_the_server(self, name):
        assert callable(getattr(server, name, None))

    def test_status_tool_delegates(self, home):
        transcript(home, "-tmp-a", "sid", [usage_line(10, 0, 0, 5)])
        assert server.session_context_status(session_id="sid")["context"] == 15

    def test_verdict_tool_round_trips(self, home):
        server.session_compact_verdict("sid", compact_now=True, reason="clean break")
        out = server.session_compact_verdict("sid")
        assert out["verdict"]["compact_now"] is True

    def test_ledger_and_handoff_tools_write(self, home):
        assert server.session_ledger_append("/Users/x/Repo", ["fact"])["bullets"] == 1
        assert server.session_handoff("/Users/x/Repo", content="NEXT: nothing")["written"]
        assert server.session_handoff("/Users/x/Repo")["content"] == "NEXT: nothing\n"
