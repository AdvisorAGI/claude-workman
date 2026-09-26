"""Workman Desk: argv, model parse, loopback bind."""
from __future__ import annotations

import json
from pathlib import Path

from workman.desk.vendors import build_argv, parse_cursor_models, parse_grok_models
from workman.desk.server import serve


def test_grok_argv_uses_prompt_and_strips_to_list():
    cmd = build_argv("grok", "/bin/grok", "grok-4.6", "hello")
    assert cmd[0] == "/bin/grok"
    assert "-p" in cmd
    assert "hello" in cmd
    assert "--no-auto-update" in cmd
    assert "--always-approve" not in cmd
    joined = " ".join(cmd)
    assert "XAI_API_KEY" not in joined
    assert "sh -c" not in joined


def test_grok_prompt_file_and_approve():
    cmd = build_argv("grok", "grok", "grok-4.6", "x", prompt_file="/tmp/p.txt",
                     auto_approve=True)
    assert "--prompt-file" in cmd
    assert "/tmp/p.txt" in cmd
    assert "--always-approve" in cmd
    assert "-p" not in cmd


def test_claude_codex_cursor_argv():
    claude = build_argv("claude", "claude", "sonnet", "fix it")
    assert claude[:2] == ["claude", "--output-format"]
    assert "-p" in claude and "fix it" in claude
    assert "--model" in claude and "sonnet" in claude
    codex = build_argv("codex", "codex", "o3", "review", auto_approve=True)
    assert codex[:2] == ["codex", "exec"]
    assert "--full-auto" in codex
    cursor = build_argv("cursor", "cursor-agent", "auto", "hi")
    assert "-p" in cursor and "hi" in cursor


def test_launch_env_drops_xai_key(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test-not-a-real-key")
    from workman.desk.vendors import launch_env
    env = launch_env()
    assert "XAI_API_KEY" not in env


def test_unknown_vendor_raises():
    try:
        build_argv("nope", "x", "m", "p")
    except ValueError as exc:
        assert "unknown vendor" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_parse_models():
    grok = parse_grok_models("Available models:\n  * grok-4.6 (default)\n  - grok-4.5\n")
    assert grok == ["grok-4.6", "grok-4.5"]
    cursor = parse_cursor_models("Available models\nauto - Auto (default)\ngpt-5.2 - GPT\n")
    assert cursor[0] == "auto"
    assert "gpt-5.2" in cursor


def test_status_loopback_and_html(tmp_path, monkeypatch):
    from workman.desk import server as S
    monkeypatch.setattr(S, "catalog", lambda **k: [])
    monkeypatch.setenv("WORKMAN_BOARD_SESSIONS", str(tmp_path))
    (tmp_path / "s.json").write_text(json.dumps({
        "id": "s", "task": "Ship desk", "items": [
            {"text": "Build", "state": "working"}
        ]
    }))
    httpd = serve("127.0.0.1", 0)
    host, port = httpd.server_address[:2]
    assert host in ("127.0.0.1", "localhost")
    import urllib.request
    try:
        html = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2).read().decode()
        assert "Workman Desk" in html
        assert "Vendor" in html
        status = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/status", timeout=2).read())
        assert status["ok"] is True
        assert any(t["task"] == "Ship desk" for t in status["tasks"])
    finally:
        httpd.shutdown()
        httpd.server_close()
