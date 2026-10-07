from __future__ import annotations

from workman.desk import connectors


def test_classify_requires_registration_and_approval():
    assert connectors._classify("No MCP servers configured", 0)[0] == "missing"
    assert connectors._classify("workman - Pending approval", 0)[0] == "pending"
    assert connectors._classify("workman enabled", 0)[0] == "ready"


def test_audit_is_read_only(monkeypatch):
    class Result:
        returncode = 0
        stdout = "workman enabled"
        stderr = ""

    monkeypatch.setattr(connectors, "which", lambda name: "/bin/echo" if name == "codex" else None)
    monkeypatch.setattr(connectors.subprocess, "run", lambda *args, **kwargs: Result())
    rows = connectors.audit()
    assert rows[0]["status"] == "ready"
    assert rows[1]["status"] == "unavailable"
