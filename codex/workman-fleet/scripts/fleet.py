"""Fleet v1.1 transport and CLI. StdIO and existing authenticated SSH only."""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shlex
import sys
import uuid
import contextlib
from pathlib import Path

import learning
from device import FIELDS, Refusal, validate

ROOT = Path(__file__).resolve().parent.parent
SSH = ["/usr/bin/ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
       "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2",
       "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes"]
ALIASES = {"mac-mini": "mini", "macbook-air": "air"}
SESSION = os.environ.get("CODEX_THREAD_ID", uuid.uuid4().hex)


def registry():
    r = json.loads((ROOT / "fleet.json").read_text())
    if set(r) != set(learning.NODES):
        raise ValueError("fleet registry must name the four authorized devices")
    return r


def command(node, report=False):
    conf = registry()[node]
    script = str(ROOT / "scripts/device.py") if node == "dgx" else conf["home"] + "/.claude/tools/workman-fleet-v1.1/device.py"
    args = [conf["python"], conf["home"] + "/fleet-report.py", "--workman-json"] if report else [conf["python"], script]
    env = {"PYTHONPATH": conf["source"], "WORKMAN_SHOT_ARCHIVE": "0"}
    if node == "dgx":
        env.update(DISPLAY=conf["display"], WORKMAN_DISPLAY=conf["display"], XAUTHORITY=conf["xauthority"], WORKMAN_GTK_PYTHON=conf["gtk_python"])
        return args, dict(os.environ, **env)
    # Only trusted registry paths enter the remote shell; all user args use stdin.
    return SSH + [conf["ssh"], shlex.join(["env", *[f"{k}={v}" for k, v in env.items()], *args])], None


async def transport(node, action, args, timeout=50, context=None):
    conf = registry()[node]
    cmd, env = command(node, report=action == "report")
    request = {"node": node, "backend": conf["backend"], "action": action, "args": args,
               "context": context or {"project": "workman", "task": "fleet-v1.1", "session": SESSION}}
    p = await asyncio.create_subprocess_exec(*cmd, env=env, stdin=asyncio.subprocess.PIPE,
                                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(p.communicate(json.dumps(request).encode() + b"\n"), timeout)
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()
        return {"ok": False, "code": "timeout", "uncertain": action not in ("status", "shot", "windows", "active", "pointer", "report")}
    except BaseException:
        if p.returncode is None:
            p.kill()
        await p.wait()
        raise
    if p.returncode or len(out) > 20000000:
        return {"ok": False, "code": "transport_error", "uncertain": True}
    try:
        result = json.loads(out)
        if not isinstance(result, dict) or result.get("code") not in learning.CODES:
            raise ValueError()
        if "event" in result and result["event"].get("node") != node:
            raise ValueError()
        return result
    except (ValueError, TypeError):
        return {"ok": False, "code": "transport_error", "uncertain": True}


async def collect(node):
    known = learning.read_events(learning.hub_root() / "by-device" / node / "workman-v1.1.jsonl")
    cursor = known[-1]["id"] if known else None
    total = 0
    for _ in range(20):
        result = await transport(node, "report", {"after": cursor})
        if not result.get("ok"):
            return {"ok": False, "code": result.get("code"), "received": total}
        events = result.get("events", [])
        learning.receive(node, events)
        total += len(events)
        if not result.get("more"):
            return {"ok": True, "received": total}
        if not events:
            return {"ok": False, "code": "transport_error", "received": total}
        cursor = events[-1]["id"]
    return {"ok": False, "code": "backlog_remaining", "received": total}


async def control(node, action, args=None, context=None):
    node = ALIASES.get(node, node)
    if node not in learning.NODES:
        return {"ok": False, "code": "invalid_arguments", "allowed_nodes": learning.NODES}
    args = args or {}
    if context is not None:
        import re
        if not isinstance(context, dict) or set(context) != {"project", "task", "session"} or not all(isinstance(v, str) and re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,79}", v) for v in context.values()):
            return {"ok": False, "code": "invalid_arguments", "node": node}
    try:
        validate(action, args)
    except Refusal:
        return {"ok": False, "code": "invalid_arguments", "node": node}
    # A per-device OS lock also serializes separate CLI / Codex sessions.
    lock_path = learning.hub_root() / (".control-" + node + ".lock")
    import fcntl
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # The user STOP path must never queue behind an input call or its reporting.
    with lock_path.open("a") as f:
        try:
            if action != "input":
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"ok": False, "code": "device_busy", "node": node}
        try:
            urgent_stop = action == "input" and any(v is False for v in args.values())
            # Recall before acting; current input authorization always comes
            # from the live device guard, never from graph observations.
            # A human STOP is unconditional and must not wait for memory or
            # reporter I/O. Its durable local event is collected by the next
            # fleet call/report, which is explicitly indicated in the response.
            relevant = [] if urgent_stop else recall(node)
            import memory_graph
            memory = {"entities": []} if urgent_stop else memory_graph.query(node, "verified_lesson", 20)
            result = await transport(node, action, args, context=context)
        except (OSError, ValueError):
            result = {"ok": False, "code": "transport_error", "uncertain": True}
        # The report is read from the device journal through the existing fleet
        # reporter. Never represent a local-only transport error as device receipt.
        try:
            delivery = {"ok": None, "pending": True, "state": "deferred_for_stop"} if urgent_stop else await collect(node)
        except (OSError, ValueError):
            delivery = {"ok": False, "code": "report_failed"}
        result.update(node=node, action=action, reporting=delivery)
        result["recalled_lessons"] = [l["kind"] for l in relevant] if 'relevant' in locals() else []
        result["recalled_memory"] = [e["id"] for e in memory["entities"]] if 'memory' in locals() else []
        if not result.get("local_recorded") and action != "report":
            result["recording_warning"] = "No local recording confirmed; do not retry the action merely to fix reporting."
        if result.get("uncertain"):
            result["recovery"] = "Observe the device before retrying; input is never automatically retried."
        return result


def recall(node=None):
    if node is not None:
        node = ALIASES.get(node, node)
        if node not in learning.NODES:
            raise ValueError("unknown node")
    rows = learning.hub_events()
    return learning.lessons([e for e in rows if node is None or e["node"] == node])


async def report_all():
    deliveries = await asyncio.gather(*(collect(n) for n in learning.NODES), return_exceptions=True)
    events = learning.hub_events()
    return {n: {"delivery": r if isinstance(r, dict) else {"ok": False, "code": "report_failed"},
                **learning.summary([e for e in events if e["node"] == n])}
            for n, r in zip(learning.NODES, deliveries)}


def main():
    p = argparse.ArgumentParser(description="Workman fleet v1.1: explicit nodes, stdin JSON arguments, local learning.")
    p.add_argument("node", choices=[*learning.NODES, *ALIASES, "all"])
    p.add_argument("action", choices=[*FIELDS, "recall"])
    p.add_argument("--output", type=Path, help="Save screenshot privately; never overwrite a file")
    a = p.parse_args()
    if a.action == "recall":
        result = {"ok": True, "lessons": recall(None if a.node == "all" else a.node)}
    elif a.node == "all":
        if a.action != "report":
            p.error("all only supports report and recall; input always requires a single device")
        result = {"ok": True, "devices": asyncio.run(report_all())}
    else:
        params = {} if sys.stdin.isatty() else json.load(sys.stdin)
        result = asyncio.run(control(a.node, a.action, params))
    data = result.get("data")
    if isinstance(data, dict) and "image" in data:
        raw = base64.b64decode(data.pop("image"))
        if a.output:
            a.output.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(a.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            data["saved"] = str(a.output.absolute())
        else:
            data["image_omitted"] = "Pass --output PATH to save a screenshot; use MCP for inline images."
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
