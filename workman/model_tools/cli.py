"""Command line: python -m workman.model_tools {run,status,result,cancel,image,video,quota,models}.
"""
from __future__ import annotations

import argparse
import json
from .transport import DEFAULT_MACHINE
from .agents import agent_cancel, agent_result, agent_run, agent_status
from .catalog import models_available
from .media import image_generate, media_quota, video_generate


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m workman.model_tools",
                                description="Launch non-Claude CLI agents and media jobs (zero Claude usage).")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="agent_run")
    r.add_argument("cli", help="codex|grok|cursor|astra|luna|terra (or '' with --role)")
    r.add_argument("prompt", nargs="+")
    r.add_argument("--role", default=None)
    r.add_argument("--effort", default="high")
    r.add_argument("--cwd", default="")
    r.add_argument("--machine", default=DEFAULT_MACHINE)
    r.add_argument("--out", default=None)
    r.add_argument("--timeout", type=int, default=2700)
    s = sub.add_parser("status", help="agent_status")
    s.add_argument("job", nargs="?")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--reconcile", action="store_true")
    g = sub.add_parser("result", help="agent_result")
    g.add_argument("job")
    g.add_argument("--lines", type=int, default=40)
    c = sub.add_parser("cancel", help="agent_cancel")
    c.add_argument("job")
    i = sub.add_parser("image", help="image_generate")
    i.add_argument("prompt", nargs="+")
    i.add_argument("--provider", default="codex")
    i.add_argument("--out-dir", default="")
    i.add_argument("-n", type=int, default=1)
    i.add_argument("--size", default=None)
    i.add_argument("--machine", default="")
    i.add_argument("--wait", type=float, default=0)
    v = sub.add_parser("video", help="video_generate")
    v.add_argument("prompt", nargs="+")
    v.add_argument("--aspect", default="9:16")
    v.add_argument("--duration", default="15s")
    v.add_argument("--ref", action="append", default=[])
    v.add_argument("--resolution", default="")
    v.add_argument("--out-dir", default="")
    v.add_argument("--wait", type=float, default=0)
    q = sub.add_parser("quota", help="media_quota")
    q.add_argument("--machine", default="")
    mo = sub.add_parser("models", help="models_available")
    mo.add_argument("--machine", default="all")
    mo.add_argument("--refresh", action="store_true")
    return p


def _dispatch(a) -> dict:
    if a.cmd == "run":
        res = agent_run(a.cli, " ".join(a.prompt), effort=a.effort, cwd=a.cwd, machine=a.machine,
                        role=a.role, out=a.out, timeout_s=a.timeout)
    elif a.cmd == "status":
        res = agent_status(a.job, limit=a.limit, reconcile=a.reconcile)
    elif a.cmd == "result":
        res = agent_result(a.job, max_lines=a.lines)
    elif a.cmd == "cancel":
        res = agent_cancel(a.job)
    elif a.cmd == "image":
        res = image_generate(" ".join(a.prompt), provider=a.provider, out_dir=a.out_dir, n=a.n,
                             size=a.size, machine=a.machine, wait_s=a.wait)
    elif a.cmd == "video":
        res = video_generate(" ".join(a.prompt), aspect_ratio=a.aspect, duration=a.duration,
                             refs=a.ref, resolution=a.resolution, out_dir=a.out_dir, wait_s=a.wait)
    elif a.cmd == "quota":
        res = media_quota(a.machine)
    else:
        res = models_available(a.machine, refresh=a.refresh)
    return res


def main(argv: list[str] | None = None) -> int:
    res = _dispatch(_parser().parse_args(argv))
    print(json.dumps(res, indent=1))
    return 0 if res.get("ok") else 1
