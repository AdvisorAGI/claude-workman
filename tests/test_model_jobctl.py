"""Pure helpers of the target-side jobctl package, and the bundle."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from model_fakes import FAKE_RUN_MODEL, _exe, fake_log, fleet, wait_state  # noqa: F401
from workman.model_tools import catalog as mcat  # noqa: F401
from workman.model_tools import transport as tp  # noqa: F401
from workman.model_tools.jobctl import common, grok_media, procs, status, supervise  # noqa: F401


class TestHelpers:
    def test_codex_final_message(self):
        log = "OpenAI Codex\nuser\nprompt\ncodex\nworking\ntokens used\n12,345\nThe answer.\nSecond line."
        assert status.codex_final(log) == "The answer.\nSecond line."
        assert status.codex_final("no marker here") == ""

    def test_clamp_text(self):
        text, cut = status.clamp_text("\n".join(f"l{i}" for i in range(100)), 5)
        assert cut and text.splitlines() == [f"l{i}" for i in range(95, 100)]
        text, cut = status.clamp_text("a" * 10_000, 50)
        assert cut and len(text) == common.RESULT_CAP
        text, cut = status.clamp_text("\x1b[31mred\x1b[0m", 5)
        assert text == "red" and not cut

    def test_clean_env_is_an_allow_list(self):
        base = {"PATH": "/usr/bin", "LANG": "C", "LC_ALL": "C", "HOME": "/x", "SSH_AUTH_SOCK": "/s",
                "WORK_TASK_ID": "wm-x", "RUN_MODEL_TIMEOUT": "5", "BASH_ENV": "/tmp/x", "ENV": "/tmp/y",
                "NODE_OPTIONS": "--require x", "PYTHONSTARTUP": "x", "PYTHONPATH": "x", "LD_PRELOAD": "x",
                "openai_api_key": "x", "OPENAI_API_KEY": "x", "OPENAI_ADMIN_KEY": "x",
                "REPLICATE_API_TOKEN": "x", "XAI_API_KEY": "x", "GH_TOKEN": "x",
                "AWS_SECRET_ACCESS_KEY": "x", "CLAUDE_CODE_OAUTH_TOKEN": "x", "CLAUDECODE": "1",
                "ANTHROPIC_BASE_URL": "x", "RUN_MODEL_TOKEN": "x", "run_model_key": "x",
                "WM_JOBCTL_KEEP_ENV": "EXTRA_OK,EXTRA_API_KEY,extra_token", "EXTRA_OK": "1",
                "EXTRA_API_KEY": "x", "extra_token": "x", "Path": "x"}
        env = common.clean_env(base)
        assert set(env) == {"PATH", "LANG", "LC_ALL", "HOME", "SSH_AUTH_SOCK", "WORK_TASK_ID",
                            "RUN_MODEL_TIMEOUT", "WM_JOBCTL_KEEP_ENV", "EXTRA_OK"}
        for k in env:
            assert k in common.ENV_ALLOW or k.startswith(common.ENV_ALLOW_PREFIX) or k == "EXTRA_OK"
            assert not re.search(r"key|token|secret|passw|claude|anthropic", k, re.I)
        assert env["PATH"].split(":")[0].endswith("/.local/bin")

    def test_loader_variables_never_pass_even_when_kept(self):
        base = {"PATH": "/usr/bin", "BASH_ENV": "/x", "ENV": "/x", "NODE_OPTIONS": "--require x",
                "PYTHONSTARTUP": "/x", "PYTHONPATH": "/x", "LD_PRELOAD": "/x", "DYLD_INSERT_LIBRARIES": "/x",
                "GIT_SSH_COMMAND": "x", "bash_env": "/x", "KEPT_OK": "1",
                "WM_JOBCTL_KEEP_ENV": "BASH_ENV,ENV,NODE_OPTIONS,PYTHONSTARTUP,PYTHONPATH,LD_PRELOAD,"
                                      "DYLD_INSERT_LIBRARIES,GIT_SSH_COMMAND,bash_env,KEPT_OK"}
        env = common.clean_env(base)
        assert set(env) == {"PATH", "HOME", "WM_JOBCTL_KEEP_ENV", "KEPT_OK"}

    def test_image_env_isolates_home(self, tmp_path):
        work = str(tmp_path / "work")
        env = common.image_env({"PATH": "/home/u/bin:/home/u/.local/bin:/usr/bin", "HOME": "/real",
                                "LANG": "C", "LC_ALL": "C", "WORK_TASK_ID": "wm-x", "SSH_AUTH_SOCK": "/s",
                                "XDG_CONFIG_HOME": "/real/.c", "TMPDIR": "/real/tmp",
                                "RUN_MODEL_TIMEOUT": "5", "DBUS_SESSION_BUS_ADDRESS": "x"},
                               work, "/tmp/wm-sbx/codex-home")
        assert env["HOME"] == work and env["CODEX_HOME"] == "/tmp/wm-sbx/codex-home"
        assert set(env) == {"PATH", "HOME", "CODEX_HOME", "LANG", "LC_ALL", "WORK_TASK_ID"}
        assert env["PATH"] == "/tmp/wm-sbx/bin:/usr/local/bin:/usr/bin:/bin"   # never ~/bin or ~/.local/bin

    def test_owned_needs_the_recorded_start_time(self):
        job = "wm-20261008T000000Z-dgx-codex-abcdef"
        p = subprocess.Popen(["bash", "-c", "sleep 30; true", job])  # cmdline names the job
        try:
            time.sleep(0.1)
            start = procs.proc_start(p.pid)
            assert start
            assert procs.owned(p.pid, job, "") is False      # a cmdline alone is no proof
            assert procs.owned(p.pid, job, "123") is False   # a wrong start time is no proof
            assert procs.owned(p.pid, job, start) is True
            assert procs.kill_tree(p.pid, signal.SIGTERM, job, "123") is False
            assert p.poll() is None
            assert procs.adopt(p.pid, job) == start
            assert procs.adopt(p.pid, "wm-20261008T000000Z-dgx-codex-000000") == ""
            assert procs.kill_tree(p.pid, signal.SIGTERM, job, start) is True
            p.wait(timeout=5)
        finally:
            if p.poll() is None:
                p.kill()

    @pytest.mark.parametrize("info,rc", [
        ({}, 9),
        ({"lane": "luna\n"}, 0),
        ({"route": "x"}, 9),
        ({"err": "model-router: done seat=sol rung=1 exit=0 result=ok seconds=3 out=o"}, 0),
        ({"err": "model-router: done seat=sol rung=2 exit=2 result=failed seconds=3 out=o"}, 2),
        ({"err": "model-router: every seat failed or was unavailable"}, 9),
    ])
    def test_role_exit(self, tmp_path, info, rc):
        out = tmp_path / "x.out"
        out.write_text("answer\n" if info else "")
        files = {"out": str(out), "err": str(tmp_path / "x.err")}
        if "lane" in info:
            (tmp_path / "x.out.lane").write_text(info["lane"])
        if "route" in info:
            (tmp_path / "x.out.route").write_text(info["route"])
            out.write_text("")
        if "err" in info:
            (tmp_path / "x.err").write_text(info["err"] + "\n")
        assert supervise.role_exit(files if info else {"out": ""}) == rc

    def test_run_model_header_parsing(self):
        text = FAKE_RUN_MODEL.replace("__CLIS__", "cursor|grok|codex")
        assert common.direct_clis(text) == ["cursor", "grok", "codex"]
        assert common.run_model_roles(text)["cheap"].startswith("Codex gpt-6-luna low")

    def test_real_run_model_header_parses(self):
        real = Path.home() / "bin" / "run-model"
        if not real.is_file():
            pytest.skip("no ~/bin/run-model on this machine")
        text = real.read_text(errors="replace")
        assert "codex" in common.direct_clis(text)
        assert {"cheap", "review", "build-sol"} <= set(common.run_model_roles(text))


class TestBundleAndSize:
    """The target-side program and the owner's size rules (CODING-STANDARDS 2026-10-08)."""

    PKG = Path(__file__).resolve().parents[1] / "workman" / "model_tools"

    def test_bundle_is_one_program_with_every_module(self):
        import ast
        from workman.model_tools import jobctl
        src = jobctl.bundle()
        ast.parse(src, feature_version=(3, 8))          # the Macs run Python 3.9.6
        compile(src, "jobctl-bundle", "exec")
        assert not [ln for ln in src.splitlines() if ln.lstrip().startswith("from .")]
        on_disk = {p.stem for p in (self.PKG / "jobctl").glob("*.py")} - {"__init__"}
        assert on_disk == set(jobctl.MODULE_ORDER)
        assert src.count("from __future__ import annotations") == 1

    def test_bundle_runs_standalone(self, tmp_path):
        from workman.model_tools import jobctl
        prog = jobctl.bundle() + "\nsys.exit(main(sys.argv[1:]))\n"
        r = subprocess.run([sys.executable, "-I", "-", "status", "wm-20261008T000000Z-dgx-codex-000000"],
                           input=prog, capture_output=True, text=True, timeout=60,
                           env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")})
        reply = json.loads(r.stdout.split("@@WM@@", 1)[1])
        assert reply["ok"] and reply["jobs"][0]["state"] == "unknown"

    def test_files_and_functions_stay_small(self):
        import ast
        tests = Path(__file__).resolve().parent
        files = list(self.PKG.rglob("*.py")) + list(tests.glob("test_model_*.py")) + [tests / "model_fakes.py"]
        for f in files:
            n = len(f.read_text().splitlines())
            assert n <= 600, f"{f.name}: {n} lines, over the 600-line hard cap"
        for f in self.PKG.rglob("*.py"):
            for node in ast.walk(ast.parse(f.read_text())):
                if isinstance(node, ast.FunctionDef):
                    size = node.end_lineno - node.lineno + 1
                    assert size <= 60, f"{f.name}:{node.name} is {size} lines, over 60"
