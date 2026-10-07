"""workman.ac: the office AC tool is a bounded shell over the DGX driver.

The driver itself (protocol, lock) is tested beside it in ~/bin/test_ac.py.
Here: only public verbs pass, nothing reaches a shell, off-DGX names the
remote route, and the tool is registered. conftest points the driver at a
missing path, so no test can reach the real AC.
"""
from __future__ import annotations

import asyncio
import os
import stat
import subprocess

import pytest

from workman import ac, server


def _fake_driver(tmp_path, body: str) -> str:
    path = tmp_path / "ac"
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class TestParseCmd:
    def test_public_forms(self):
        assert ac.parse_cmd("") == ["status"]
        assert ac.parse_cmd("temp 26") == ["temp", "26"]
        assert ac.parse_cmd("  lock   off ") == ["lock", "off"]
        assert ac.parse_cmd("cool-quiet") == ["cool-quiet"]

    @pytest.mark.parametrize("cmd", ["_loop", "status; rm -rf ~", "temp 26 27", "TEMP 26", "temp ../x",
                                     "lock $(id)", "x" * 41, "reboot"])
    def test_refuses_everything_else(self, cmd):
        with pytest.raises(ValueError):
            ac.parse_cmd(cmd)

    def test_refuses_non_string(self):
        with pytest.raises(ValueError):
            ac.parse_cmd(None)  # type: ignore[arg-type]


class TestRun:
    def test_runs_the_driver_without_a_shell(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ac.DRIVER_ENV, _fake_driver(tmp_path, 'echo "args:$*"'))
        out = ac.run("temp 26")
        assert out == {"ok": True, "cmd": "temp 26", "exit": 0, "output": "args:temp 26"}

    def test_driver_failure_is_ok_false_with_its_words(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ac.DRIVER_ENV, _fake_driver(tmp_path, 'echo "ac: no reply" >&2; exit 1'))
        out = ac.run("status")
        assert out["ok"] is False and out["exit"] == 1 and out["output"] == "ac: no reply"

    def test_output_is_bounded(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ac.DRIVER_ENV, _fake_driver(tmp_path, "yes x | head -c 20000"))
        assert len(ac.run("log")["output"]) == ac.MAX_OUTPUT + 3

    def test_timeout(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ac.DRIVER_ENV, _fake_driver(tmp_path, "sleep 5"))
        monkeypatch.setattr(ac, "TIMEOUT_S", 0.2)
        assert ac.run("status")["error"] == "timeout"

    def test_bad_cmd_never_spawns(self, monkeypatch):
        def spawn(*_a, **_k):
            raise AssertionError("spawned")
        monkeypatch.setattr(subprocess, "run", spawn)
        assert ac.run("_loop")["error"] == "bad_cmd"

    def test_missing_driver_names_the_dgx_route(self):
        out = ac.run("status")  # conftest: the driver path does not exist
        assert out["error"] == "no_driver" and "remote(host='dgx'" in out["detail"]

    def test_off_the_dgx_there_is_no_driver(self, monkeypatch, tmp_path):
        monkeypatch.delenv(ac.DRIVER_ENV)
        monkeypatch.setattr(ac.socket, "gethostname", lambda: "room1-tariqul")
        assert ac.driver() is None


class TestServer:
    def test_ac_tool_is_registered(self):
        names = {t.name for t in asyncio.run(server.mcp.list_tools())}
        assert "ac" in names

    def test_tool_refuses_internal_verb(self):
        assert server.ac("_loop")["error"] == "bad_cmd"

    def test_module_alias_does_not_shadow_the_tool(self):
        assert callable(server.ac) and server._ac is ac and os.path.basename(ac.__file__) == "ac.py"
