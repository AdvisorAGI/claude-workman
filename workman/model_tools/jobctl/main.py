"""Entry point of the bundled program. Each subcommand prints one `@@WM@@<json>`
line (run and grok-job are long-lived processes and print nothing).
"""
from __future__ import annotations

import base64
import json
from .common import _emit, _err  # jobctl:local
from .status import job_result, job_status  # jobctl:local
from .supervise import _Sup  # jobctl:local
from .cancel import cmd_units, job_cancel  # jobctl:local
from .launch import cmd_launch  # jobctl:local
from .catalog import cmd_models  # jobctl:local
from .grok_media import cmd_grok_job, cmd_grok_quota  # jobctl:local


_SPEC_B64 = ""   # set by the controller in the program it sends for `launch`


def main(argv) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "run":
        return _Sup(argv[1]).run()
    if cmd == "grok-job":
        return cmd_grok_job(argv[1])
    try:
        if cmd == "launch":
            spec = json.loads(base64.b64decode(_SPEC_B64).decode("utf-8")) if _SPEC_B64 else {}
            return _emit(cmd_launch(spec))
        if cmd == "status":
            return _emit({"ok": True, "jobs": [job_status(j) for j in argv[1:]]})
        if cmd == "result":
            return _emit(job_result(argv[1], int(argv[2]) if len(argv) > 2 else 40))
        if cmd == "cancel":
            return _emit(job_cancel(argv[1]))
        if cmd == "models":
            return _emit(cmd_models())
        if cmd == "units":
            return _emit(cmd_units())
        if cmd == "grok-quota":
            return _emit(cmd_grok_quota())
    except Exception as exc:  # noqa: BLE001 - the controller gets a reason, not a trace
        return _emit(_err("jobctl_error", "%s: %s" % (type(exc).__name__, exc)))
    return _emit(_err("unknown_command", repr(cmd)))
