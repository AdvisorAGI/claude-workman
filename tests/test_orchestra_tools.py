"""The orchestra_tools facade: the CLI and the ssh machine guard. The logic is
tested per module (test_orchestra_transcripts, _registry, _state, _usage,
_events). Fake HOME only."""
from __future__ import annotations

import json
import os

import pytest

from workman import orchestra_tools as ot

from orchestra_fakes import NOW, SID_A, asst, read_world, user


@pytest.fixture
def world(tmp_path, monkeypatch):
    return read_world(tmp_path, monkeypatch)


class TestCli:
    def test_list_and_usage_json(self, world, capsys):
        world.transcript(SID_A, [user("go"), asst()], age_s=60)
        os.utime(world.home / ".claude-a" / "projects" / "-home-x-proj" / f"{SID_A}.jsonl")
        assert ot.main(["list", "--json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["count"] == 1 and out["sessions"][0]["id"] == SID_A[:8]
        assert ot.main(["usage", "--window-h", "1000000"]) == 0
        assert json.loads(capsys.readouterr().out)["ok"] is True

    def test_remote_machine_name_is_validated(self):
        assert ot.sessions_list(machine="bad host;rm")["ok"] is False


class TestFacade:
    def test_the_server_block_is_unchanged(self):
        from workman import server
        for name in ("sessions_list", "session_needs", "session_usage", "session_events",
                     "session_compact", "session_set_model_effort", "session_front_compact"):
            assert callable(getattr(server, name, None)), name

    def test_usage_by_machine_validates_the_name(self):
        assert ot.session_usage(machine="bad host;rm")["ok"] is False
