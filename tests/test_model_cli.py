"""The MCP registration in server.py and the command line."""
from __future__ import annotations

import json


from model_fakes import FAKE_RUN_MODEL, _exe, fake_log, fleet, wait_state  # noqa: F401
from workman import model_tools as mt
from workman.model_tools import catalog as mcat  # noqa: F401
from workman.model_tools import transport as tp  # noqa: F401
from workman.model_tools.jobctl import common, grok_media, procs, status, supervise  # noqa: F401


class TestServer:
    NAMES = {"agent_run", "agent_status", "agent_result", "agent_cancel", "image_generate",
             "video_generate", "media_quota", "models_available"}

    def test_tools_are_registered(self):
        from workman import server
        assert self.NAMES <= set(server.mcp._tool_manager._tools)

    def test_tools_delegate_and_never_raise(self, fleet, monkeypatch):
        from workman import server
        assert server.agent_run("claude", "hi")["error"] == "unknown_cli"
        assert server.video_generate("x", provider="codex")["error"] == "unsupported"

        def boom(*_a, **_k):
            raise RuntimeError("kaput")
        monkeypatch.setattr(server._model_tools, "agent_status", boom)
        assert server.agent_status() == {"ok": False, "error": "RuntimeError: kaput"}

    def test_cli_run_and_status(self, fleet, capsys):
        assert mt.main(["run", "codex", "hello", "from", "cli", "--machine", "dgx", "--effort", "low"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] and out["effort"] == "low"
        wait_state(out["job"])
        assert mt.main(["result", out["job"]]) == 0
        assert json.loads(capsys.readouterr().out)["text"] == "FINAL ANSWER from codex"
        assert mt.main(["run", "nope", "x", "--machine", "dgx"]) == 1
