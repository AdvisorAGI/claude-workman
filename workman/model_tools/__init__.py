"""Non-Claude model tools: Codex, Grok and Cursor CLI agents plus image and video
generation, run as their own detached processes so the work spends no Claude usage.

Owner order 2026-10-08: the orchestrator drives the other CLI agents from Workman
tools. Every agent runs through ~/bin/run-model (direct mode, or a router role from
its header) as its own process, detached from the calling session: a transient
`systemd-run --user` unit on Linux, a detached session on a Mac. The tools return at
once with a job id; model output never streams into the caller. What the caller pays
is the launch call and a bounded status or result read.

    agent_run         launch a job (cli + effort, or a run-model role), return the job id
    agent_status      running / finished / failed / timed_out / cancelled / lost / launch_failed
    agent_result      bounded final text (report file, final message, or log tail)
    agent_cancel      stop a job (only its own unit and processes proven to be its own)
    image_generate    Codex CLI built-in image_gen (ChatGPT plan, read-only sandbox) or grok Imagine
    video_generate    grok Imagine video (Codex CLI has no video generation)
    media_quota       grok Imagine quota, read-only
    models_available  CLIs, models and run-model roles per machine (cached 10 min)

Money rules, enforced in the controller and again on the target: subscription CLIs
only, nothing read from ~/ENV, an allow-listed worker environment (jobctl.common
ENV_ALLOW), a read-only codex sandbox for images, no Claude model ever started.

Layout: transport (machines, ssh, job records), agents, media, catalog, cli; the
target-side half is the jobctl subpackage, bundled into one program per call.
CLI: python -m workman.model_tools {run,status,result,cancel,image,video,quota,models} ...
"""
from __future__ import annotations

from .agents import (CLIS, EFFORTS, ROLE_FAMILY, agent_cancel, agent_result, agent_run,
                     agent_status, reconcile_units)
from .catalog import models_available
from .cli import main
from .jobctl.common import JOB_RE, RESULT_CAP
from .media import codex_image_prompt, image_generate, media_quota, unsafe_subject, video_generate
from .transport import (DEFAULT_MACHINE, MACHINES, TERMINAL, local_machine, machine_of, new_job_id,
                        resolve_machine)

__all__ = ["CLIS", "DEFAULT_MACHINE", "EFFORTS", "JOB_RE", "MACHINES", "RESULT_CAP", "ROLE_FAMILY",
           "TERMINAL", "agent_cancel", "agent_result", "agent_run", "agent_status", "codex_image_prompt",
           "image_generate", "local_machine", "machine_of", "main", "media_quota", "models_available",
           "new_job_id", "reconcile_units", "resolve_machine", "unsafe_subject", "video_generate"]
