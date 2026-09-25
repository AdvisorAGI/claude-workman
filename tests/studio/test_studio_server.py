"""The six studio_ tools are registered on the MCP server, answer with dicts, and never raise."""
import asyncio
import json

from workman import server


def test_studio_tools_are_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"studio_new_job", "studio_status", "studio_inspect", "studio_voice", "studio_assemble",
            "studio_qc"} <= names


def test_new_job_then_status_and_errors_come_back_as_dicts(tmp_path):
    r = server.studio_new_job("tool-test", "Tool test")
    assert r["ok"] and r["job"].startswith(str(tmp_path / "jobs")) and "JOB.md" in r["created"]
    st = server.studio_status("tool-test")
    assert st["ok"] and st["docs"]["QC.md"] == "template" and st["render"] == {"state": "none"}
    bad = server.studio_status("no-such-job")
    assert bad["ok"] is False and "StudioError" in bad["error"]
    assert server.studio_inspect(str(tmp_path / "missing.mp4"))["ok"] is False
    assert server.studio_assemble("tool-test")["ok"] is False                # no spec.json yet
    assert server.studio_voice("tool-test", action="tts", name="../x")["ok"] is False
    assert server.studio_voice("tool-test", action="nope", name="x")["error"] == "unknown action 'nope'"


def test_voice_onset_reads_a_saved_take(tmp_path):
    d = server.studio_new_job("onset-test")["job"]
    text = "Look closer."
    al = {"characters": list(text), "character_start_times_seconds": [0.1 + k * 0.05 for k in range(len(text))],
          "character_end_times_seconds": [0.15 + k * 0.05 for k in range(len(text))]}
    (tmp_path / "jobs" / d.rsplit("/", 1)[1] / "audio" / "vo1.json").write_text(json.dumps({"alignment": al}))
    r = server.studio_voice("onset-test", action="onset", name="vo1", phrase="closer.")
    assert r["ok"] and abs(r["onset"] - (0.1 + 5 * 0.05)) < 1e-9
