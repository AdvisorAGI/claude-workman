"""Target-side job control for workman.model_tools (stdlib only, Python 3.9+).

The controller pipes `bundle()` to `python3 -I -` on the machine that runs the job:
locally, or over the fleet ssh aliases. Nothing has to be installed on a target
beyond ~/bin/run-model and the subscription CLIs it drives. The modules are written
so they also import as a normal package (tests, the controller's own helpers):
intra-package imports carry a `# jobctl:local` marker and are dropped from the
bundle, where every module shares one namespace. The controller appends the call
(and, for `launch`, the spec and the bundle itself) to the program it sends.

Subcommands (each prints one line `@@WM@@<json>`):
    launch                     start a job from _SPEC_B64; returns at once
    run <jobdir>               the supervisor that lives for the whole job
    status <job> [<job> ...]   state of each job on this machine
    units                      run-model-* user units here and the job record of each
    result <job> <max_lines>   bounded final text of a job
    cancel <job>               stop a job cleanly
    models                     which CLIs, models and run-model roles exist here
    grok-quota                 read grok Imagine quota via ~/grok-web-mcp
    grok-job <jobdir>          (child of `run`) grok Imagine image/video job

A job lives in ~/orchestra/runs/workman/<job>/ on the target:
    job.json  prompt.txt  jobctl.py  supervisor.log  launch.log  out.log  final.txt
    pid  pid_start  child_pid  child_start  started  ended  rc
    cancelled  timed_out  launch_failed  files.json  result.json  role.json
"""
from __future__ import annotations

from pathlib import Path

# Definition order of the bundle: a module only uses names from modules above it
# at definition time (defaults, class bodies); function bodies resolve at call time.
MODULE_ORDER = ("common", "procs", "sandbox", "status", "supervise", "cancel", "launch", "catalog",
                "grok_media", "main")
LOCAL_MARK = "# jobctl:local"


def bundle() -> str:
    """All jobctl modules as one Python program, intra-package imports removed."""
    here = Path(__file__).parent
    out = ["from __future__ import annotations"]
    for name in MODULE_ORDER:
        out.append(f"# ---- jobctl/{name}.py")
        for line in (here / f"{name}.py").read_text(encoding="utf-8").splitlines():
            if line.rstrip().endswith(LOCAL_MARK) or line.strip() == "from __future__ import annotations":
                continue
            out.append(line)
    return "\n".join(out) + "\n"
