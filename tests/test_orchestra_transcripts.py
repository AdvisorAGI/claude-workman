"""Transcript parsing: malformed and cut lines, sidechains, interrupts, local
commands, denials, questions, titles and redaction. Fake HOME only."""
from __future__ import annotations


import pytest

from workman.orchestra import common as ocommon
from workman.orchestra import transcripts as otranscripts

from orchestra_fakes import (ASK, SECRET, SID_A, SID_B, asst, read_world, result, user)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return read_world(tmp_path, monkeypatch)


class TestParse:
    def test_malformed_lines_are_skipped_and_counted(self, world):
        p = world.transcript(SID_A, [user("fix the build"), asst()],
                             extra_lines=["{not json", "", '{"type": "assistant", "trunc'])
        info = otranscripts.parse_transcript(p)
        assert info["malformed"] == 2
        assert info["first_prompt"] == "fix the build"
        assert info["last_kind"] == "assistant_end"

    def test_context_is_input_plus_cache_of_the_last_assistant(self, world):
        p = world.transcript(SID_A, [user("go"), asst(usage=(1, 2, 3, 4), mid="m1"),
                                     asst(usage=(10, 300000, 5000, 99), mid="m2", ago=400)])
        info = otranscripts.parse_transcript(p)
        assert info["ctx_tokens"] == 10 + 300000 + 5000
        assert info["model"] == "claude-opus-5-5" and info["effort"] == "high"

    def test_a_compact_boundary_after_the_last_usage_marks_context_stale(self, world):
        p = world.transcript(SID_A, [user("go"), asst(),
                                     {"type": "system", "subtype": "compact_boundary"}])
        info = otranscripts.parse_transcript(p)
        assert info["ctx_stale"] is True and info["compacts"] == 1

    def test_titles_prefer_custom_over_ai_and_first_prompt(self, world):
        p = world.transcript(SID_A, [user("hello there"), asst(),
                                     {"type": "ai-title", "aiTitle": "AI name"},
                                     {"type": "custom-title", "customTitle": "Owner name"}])
        info = otranscripts.parse_transcript(p)
        assert info["title"] == "Owner name" and info["title_source"] == "custom"

    def test_open_question_has_text_and_option_labels(self, world):
        p = world.transcript(SID_A, [user("go"), asst(text="", stop="tool_use", tools=[ASK])])
        q = otranscripts.parse_transcript(p)["pending_questions"]
        assert q == [{"tool_use_id": "q1", "questions": [{
            "question": "Merge PR #6 into master?", "header": "Merge",
            "options": ["Approve once", "Skip"]}]}]

    def test_an_answered_question_is_not_pending(self, world):
        p = world.transcript(SID_A, [user("go"), asst(stop="tool_use", tools=[ASK]),
                                     result("q1", "Approve once"), asst(ago=300, mid="m2")])
        assert otranscripts.parse_transcript(p)["pending_questions"] == []

    def test_denials_from_the_kind_field_and_from_the_classifier_text(self, world):
        p = world.transcript(SID_A, [
            user("go"),
            asst(stop="tool_use", tools=[("t1", "Bash", {}), ("t2", "Edit", {})]),
            result("t1", "Permission for this action was denied by the Claude Code auto mode "
                         "classifier. Reason: [Production Reads]. If you have", is_error=True),
            result("t2", "nope", is_error=True, toolDenialKind="permission-rule"),
            asst(ago=300, mid="m2")])
        d = otranscripts.parse_transcript(p)["denials"]
        assert [(x["tool"], x["kind"]) for x in d] == [("Bash", "classifier"),
                                                       ("Edit", "permission")]
        assert d[0]["reason"] == "[Production Reads]"

    def test_the_owners_own_answer_is_no_denial(self, world):
        p = world.transcript(SID_A, [
            user("go"),
            asst(stop="tool_use", tools=[("t1", "AskUserQuestion", {}), ("t2", "Bash", {})]),
            result("t1", "Tool permission request aborted", is_error=True,
                   toolDenialKind="permission-rule",
                   permissionDecision={"decision": "reject", "source": "user_abort"}),
            result("t2", "no", is_error=True, toolDenialKind="user-rejected"),
            asst(ago=300, mid="m2")])
        assert otranscripts.parse_transcript(p)["denials"] == []

    def test_an_error_that_merely_quotes_the_phrase_in_a_normal_result_is_no_denial(self, world):
        p = world.transcript(SID_A, [user("go"), asst(stop="tool_use", tools=[("t1", "Read", {})]),
                                     result("t1", "doc says: was denied by the auto mode "
                                                  "classifier", is_error=False)])
        assert otranscripts.parse_transcript(p)["denials"] == []

    def test_a_new_prompt_clears_earlier_denials(self, world):
        p = world.transcript(SID_A, [
            user("go"), asst(stop="tool_use", tools=[("t1", "Bash", {})]),
            result("t1", "x", is_error=True, toolDenialKind="automode-blocked"),
            asst(ago=400, mid="m2"), user("next thing", ago=300)])
        assert otranscripts.parse_transcript(p)["denials"] == []

    def test_rate_limit_api_error_is_recorded(self, world):
        err = asst(text="You've hit your session limit · resets 3:50pm (UTC)", model="<synthetic>",
                   ago=100, mid="m9", isApiErrorMessage=True, error="rate_limit")
        p = world.transcript(SID_A, [user("go"), err])
        info = otranscripts.parse_transcript(p)
        assert info["api_error"]["error"] == "rate_limit"
        assert "session limit" in info["api_error"]["text"]
        assert info["model"] == ""  # a synthetic error is not the model that ran

    def test_interrupt_and_local_command_are_not_prompts(self, world):
        p = world.transcript(SID_A, [user("<command-name>/effort</command-name>"),
                                     user("real ask"), user("[Request interrupted by user]")])
        info = otranscripts.parse_transcript(p)
        assert info["first_prompt"] == "real ask"
        assert info["last_kind"] == "interrupted"
        q = world.transcript(SID_B, [user("go"), asst(),
                                     user("<local-command-stdout>Compacted </local-command-stdout>")])
        assert otranscripts.parse_transcript(q)["last_kind"] == "local_command"

    def test_sidechain_records_never_decide_the_state(self, world):
        p = world.transcript(SID_A, [user("go"), asst(),
                                     asst(stop="tool_use", ago=100, mid="side", isSidechain=True,
                                          tools=[("s1", "Bash", {})])])
        info = otranscripts.parse_transcript(p)
        assert info["last_kind"] == "assistant_end" and info["pending_tools"] == []

    def test_a_tail_read_drops_the_cut_line(self, world):
        big = [user("go")] + [asst(text="x" * 200, mid=f"m{i}", ago=500 - i) for i in range(50)]
        p = world.transcript(SID_A, big)
        info = otranscripts.parse_transcript(p, tail_bytes=3000)
        assert info["malformed"] == 0 and info["last_kind"] == "assistant_end"
        assert info["first_prompt"] == "go"

    def test_hyphenated_bridge_ids_are_redacted(self):
        text = ("open session_01a11d32-0dde-7771-837f-a5970bda522a and cse_01ABCdef-0123-4567-89ab "
                "and session_01ABCDEFGHJKMNPQRSTVWXYZ0 but keep session_id and session_compact")
        out = ocommon.redact(text, 400)
        assert "01a11d32" not in out and "01ABCdef" not in out and "01ABCDEFGHJK" not in out
        assert out.count("session_[redacted]") == 2 and "cse_[redacted]" in out
        assert "session_id" in out and "session_compact" in out

    def test_secrets_in_prompts_are_redacted(self, world):
        p = world.transcript(SID_A, [user(f"use token={SECRET} and Bearer abcdefghijklmnop")])
        fp = otranscripts.parse_transcript(p)["first_prompt"]
        assert SECRET not in fp and "abcdefghijklmnop" not in fp and "[redacted]" in fp
