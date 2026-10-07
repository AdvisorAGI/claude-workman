"""The Desk orchestration contract is pure: no real model or desktop calls."""
from __future__ import annotations

from workman.desk import orchestrator


def _passing_runner(calls):
    def run(lane, prompt, emit):
        calls.append((lane, prompt))
        emit(f"{lane.name} evidence\n")
        return 0, f"{lane.name} complete\nVERDICT: PASS\n"
    return run


def test_chatgpt_lanes_are_primary_and_grok_is_a_lens():
    calls, output = [], []
    records = orchestrator.run("add a connector", _passing_runner(calls), output.append,
                               grok_available=True)
    assert [lane.name for lane, _ in calls] == [
        "architecture", "implementation", "verify-terra", "verify-luna", "grok-lens",
    ]
    assert [lane.model for lane, _ in calls[:4]] == [
        "gpt-6-astra", "gpt-6.1-sol", "gpt-5.6-terra", "gpt-6-luna",
    ]
    assert records[-1]["optional"] is True
    assert orchestrator.may_publish(records) is True


def test_failed_architecture_stops_before_a_write_lane():
    calls = []

    def fail_architecture(lane, prompt, emit):
        calls.append(lane)
        return 1, "could not inspect\nVERDICT: FAIL\n"

    records = orchestrator.run("change code", fail_architecture, lambda _text: None,
                               grok_available=True)
    assert [lane.name for lane in calls] == ["architecture"]
    assert records[0]["passed"] is False


def test_publish_needs_both_chatgpt_verifiers():
    records = [
        {"name": "verify-terra", "passed": True},
        {"name": "verify-luna", "passed": False},
        {"name": "grok-lens", "passed": True, "optional": True},
    ]
    assert orchestrator.may_publish(records) is False


def test_publish_is_explicit_and_requires_a_pr_url():
    calls, output = [], []
    records = orchestrator.run("ship connector", _passing_runner(calls), output.append,
                               grok_available=False, publish=True)
    assert [lane.name for lane, _ in calls][-1] == "release"
    assert records[-1]["name"] == "release"
    assert records[-1]["passed"] is False

    def published(lane, prompt, emit):
        calls.append((lane, prompt))
        return 0, "PR: https://github.com/example/repo/pull/1\nVERDICT: PASS\n"

    records = orchestrator.run("ship connector", published, lambda _text: None,
                               grok_available=False, publish=True)
    assert records[-1]["passed"] is True
