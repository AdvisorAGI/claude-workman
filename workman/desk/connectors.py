"""Read-only audits for client-side Workman MCP registration.

An installed CLI is not proof that it can use Workman.  Each client owns its
own MCP registration and approval state, so Desk exposes this audit instead of
silently treating every discovered CLI as a connected agent.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

from .vendors import launch_env, which


@dataclass(frozen=True)
class Client:
    id: str
    label: str
    binary_names: tuple[str, ...]
    args: tuple[str, ...]


CLIENTS = (
    Client("codex", "ChatGPT / Codex", ("codex",), ("mcp", "list")),
    Client("claude", "Claude Code", ("claude",), ("mcp", "list")),
    Client("cursor", "Cursor", ("agent", "cursor-agent"), ("mcp", "list")),
    Client("grok", "Grok CLI", ("grok",), ("mcp", "list")),
)


def _classify(output: str, returncode: int) -> tuple[str, str]:
    text = output.lower()
    if "workman" not in text:
        return "missing", "No Workman registration reported by this client."
    if "pending approval" in text or "needs approval" in text:
        return "pending", "Workman is registered but still awaits this client's approval."
    if returncode == 0:
        return "ready", "Workman registration was reported by this client."
    return "unknown", "The client mentioned Workman but its status command did not complete."


def audit(*, timeout: float = 8.0) -> list[dict]:
    """Return a bounded, credential-free status per installed CLI.

    This invokes only each CLI's listing command.  It never edits a config,
    starts a desktop-control server, or sends a model prompt.
    """
    rows: list[dict] = []
    for client in CLIENTS:
        binary = next((which(name) for name in client.binary_names if which(name)), None)
        if not binary:
            rows.append({"id": client.id, "label": client.label, "status": "unavailable",
                         "detail": "CLI is not installed or is not on PATH."})
            continue
        try:
            proc = subprocess.run([binary, *client.args], capture_output=True, text=True,
                                  timeout=timeout, env=launch_env())
            output = (proc.stdout or "") + (proc.stderr or "")
            status, detail = _classify(output, proc.returncode)
        except subprocess.TimeoutExpired:
            status, detail = "unknown", "Status command timed out; no configuration was changed."
        except OSError as exc:
            status, detail = "unknown", f"Could not run status command: {exc}"
        rows.append({"id": client.id, "label": client.label, "status": status,
                     "detail": detail})
    return rows
