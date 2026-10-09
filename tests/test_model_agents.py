"""agent_run, agent_status, agent_result and agent_cancel through the fakes."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from model_fakes import FAKE_RUN_MODEL, _exe, fake_log, fleet, wait_state  # noqa: F401
from workman import model_tools as mt
from workman.model_tools import catalog as mcat  # noqa: F401
from workman.model_tools import transport as tp  # noqa: F401
from workman.model_tools.jobctl import common, grok_media, procs, status, supervise  # noqa: F401


class TestAgentRun:
    def test_direct_run_detaches_finishes_and_strips_secrets(self, fleet):
        r = mt.agent_run("codex", "say hi in one line", effort="low", machine="dgx")
        assert r["ok"] is True, r
        assert mt.JOB_RE.match(r["job"]) and "-dgx-codex-" in r["job"]
        assert r["how"] == "nohup" and r["mode"] == "direct"
        st = wait_state(r["job"])
        assert st["state"] == "finished" and st["exit"] == 0
        assert st["model"] == "fake-codex-1" and st["effort"] == "low" and st["machine"] == "dgx"
        log = fake_log(fleet)
        assert "effort=low" in log and "tmo=2700" in log and f"task={r['job']}" in log
        assert "okey=none" in log and "ckey=none" in log, "API key or Claude token leaked to the worker"
        assert ("scrub lkey=none admin=none repl=none node=none pystart=none bashenv=none envv=none"
                in log), log
        sourced = fake_log(fleet, "bash-env-sourced.log")
        assert "run-model" not in sourced, "BASH_ENV reached the worker"
        res = mt.agent_result(r["job"])
        assert res["source"] == "final_message" and res["text"] == "FINAL ANSWER from codex"
        assert res["out"].endswith("/out.log")

    def test_default_cwd_is_the_job_scratch_dir(self, fleet):
        r = mt.agent_run("grok", "hello", machine="dgx")
        assert r["ok"], r
        assert r["cwd"] == r["launch_log"].rsplit("/", 1)[0]

    @pytest.mark.parametrize("kwargs,error", [
        ({"cli": "claude"}, "unknown_cli"),
        ({"cli": "codex", "effort": "ultra"}, "bad_effort"),
        ({"cli": "codex", "role": "nope"}, "unknown_role"),
        ({"cli": "codex", "role": "review-grok"}, "role_cli_mismatch"),
        ({"cli": "codex", "machine": "mars"}, "bad_machine"),
        ({"cli": "codex", "out": "relative.log"}, "bad_out"),
        ({"cli": "codex", "cwd": "relative/dir"}, "bad_cwd"),
        ({"cli": "codex", "timeout_s": 5}, "bad_timeout"),
        ({"cli": "codex", "prompt": "x" * 140000}, "prompt_too_big"),
        ({"cli": "codex", "prompt": "   "}, "empty_prompt"),
    ])
    def test_validation_refuses_before_launch(self, fleet, kwargs, error):
        args = {"prompt": "hi", "machine": "dgx", **kwargs}
        r = mt.agent_run(**args)
        assert r["ok"] is False and r["error"] == error
        assert fake_log(fleet) == "" and fake_log(fleet, "fake-ssh.log") == ""

    def test_role_mode_waits_for_the_router(self, fleet):
        r = mt.agent_run("codex", "summarise this in a line", role="cheap", machine="dgx")
        assert r["ok"] and r["mode"] == "role" and r["effort"] == "router"
        st = wait_state(r["job"])
        assert st["state"] == "finished" and st["exit"] == 0 and st["model"] == "luna"
        assert st["effort"] == "router"
        role = json.loads((Path(st["dir"]) / "role.json").read_text())
        assert role["router_pid"] and role["router_start"], "the router is adopted with its start time"
        res = mt.agent_result(r["job"])
        assert res["source"] == "role_out" and "summarise this" in res["text"]
        assert "role=cheap" in fake_log(fleet)

    def test_role_result_prefers_the_codex_final_message(self, fleet):
        # The Mac router copies the whole codex transcript into .out; the DGX one only the answer.
        r = mt.agent_run("codex", "workdir: x\nreasoning effort: low\ncodex\nPONG 42\ntokens used\n10,459\nPONG 42",
                         role="cheap", machine="dgx")
        st = wait_state(r["job"])
        assert st["effort"] == "low"
        res = mt.agent_result(r["job"])
        assert res["source"] == "role_final_message" and res["text"] == "PONG 42"

    def test_role_mode_unavailable_without_router(self, fleet):
        (fleet["home"] / "bin" / "model-router").unlink()
        r = mt.agent_run("codex", "x", role="cheap", machine="dgx")
        assert r["ok"] is False and r["error"] == "role_mode_unavailable"

    def test_cli_the_target_run_model_lacks_is_refused(self, fleet):
        _exe(fleet["home"] / "bin" / "run-model", FAKE_RUN_MODEL.replace("__CLIS__", "cursor|grok|codex|astra"))
        r = mt.agent_run("luna", "x", machine="dgx")
        assert r["ok"] is False and r["error"] == "cli_unsupported_here"
        assert r["supported"] == ["cursor", "grok", "codex", "astra"]

    def test_missing_run_model_is_refused(self, fleet):
        (fleet["home"] / "bin" / "run-model").unlink()
        r = mt.agent_run("codex", "x", machine="dgx")
        assert r["ok"] is False and r["error"] == "run_model_missing"

    def test_missing_cwd_is_refused_and_leaves_nothing(self, fleet):
        r = mt.agent_run("codex", "x", machine="dgx", cwd="/nonexistent/dir/here")
        assert r["ok"] is False and r["error"] == "cwd_missing"
        assert not list((fleet["home"] / "orchestra" / "runs" / "workman").glob("wm-*"))
        assert not list((fleet["home"] / "orchestra" / "runs" / "workman-jobs").glob("wm-*.json")), \
            "a launch the target refused must not leave a job record"

    def test_launch_without_a_reply_keeps_the_job_record(self, fleet, monkeypatch):
        real = tp._call

        def no_reply(machine, args, **kw):
            if args[0] == "launch":
                return {"ok": False, "error": "timeout", "detail": "no reply", "machine": machine}
            return real(machine, args, **kw)
        monkeypatch.setattr(tp, "_call", no_reply)
        r = mt.agent_run("codex", "hi", machine="dgx")
        assert r["error"] == "timeout" and r["launch_unconfirmed"] is True
        assert mt.JOB_RE.match(r["job"]) and r["unit_if_systemd"] == f"run-model-{r['job']}.service"
        assert tp._load(r["job"])["launch"] == "unconfirmed"
        rec = mt.agent_status(reconcile=True)["reconcile"]
        assert [u["job"] for u in rec["unconfirmed_launches"]] == [r["job"]]


class TestLifecycle:
    def test_failed_run_reports_exit_and_log_tail(self, fleet):
        r = mt.agent_run("codex", "FAILNOW please", machine="dgx")
        st = wait_state(r["job"])
        assert st["state"] == "failed" and st["exit"] == 3
        res = mt.agent_result(r["job"])
        assert res["source"] == "log_tail" and res["text"] == "boom"

    def test_run_model_timeout_is_marked_and_not_revived(self, fleet):
        r = mt.agent_run("codex", "TIMEOUTNOW", machine="dgx")
        st = wait_state(r["job"])
        assert st["state"] == "timed_out" and st["exit"] == 143
        stopped = list((fleet["home"] / "orchestra" / "runs" / "active" / "stopped").glob("*.job"))
        assert len(stopped) == 1, "a timed-out run must not stay eligible for run-comeback"

    def test_supervisor_backstop_ends_a_run_past_its_timeout(self, fleet, monkeypatch):
        monkeypatch.setenv("WM_JOBCTL_GRACE", "0")
        job = mt.new_job_id("dgx", "codex")
        spec = {"job": job, "kind": "agent", "mode": "direct", "cli": "codex", "effort": "low",
                "cwd": "", "out": "", "timeout_s": 1, "machine": "dgx", "prompt": "SLEEPLONG"}
        res = tp._call("dgx", ["launch"], spec=spec, with_self=True)
        assert res["ok"], res
        st = wait_state(job, timeout=40)
        assert st["state"] == "timed_out"

    def test_cancel_kills_the_tree_and_stops_comeback(self, fleet):
        r = mt.agent_run("codex", "SLEEPLONG", machine="dgx")
        st = wait_state(r["job"], states=("running",))
        assert st["state"] == "running"
        d = Path(st["dir"])
        end = time.time() + 10
        while not (d / "child_pid").exists() and time.time() < end:
            time.sleep(0.05)
        child = int((d / "child_pid").read_text())
        c = mt.agent_cancel(r["job"])
        assert c["ok"] and c["state"] == "cancelled", c
        time.sleep(0.3)
        assert not procs._pid_alive(child)
        assert (fleet["home"] / "orchestra" / "runs" / "active" / "stopped" / f"{child}.job").exists()
        assert mt.agent_status(r["job"])["state"] == "cancelled"
        again = mt.agent_cancel(r["job"])
        assert again["state"] == "cancelled" and again["note"] == "already ended"

    def test_lost_when_the_supervisor_dies_without_an_exit_code(self, fleet):
        r = mt.agent_run("codex", "SLEEPLONG", machine="dgx")
        st = wait_state(r["job"], states=("running",))
        d = Path(st["dir"])
        end = time.time() + 10
        while not (d / "child_pid").exists() and time.time() < end:
            time.sleep(0.05)
        os.killpg(int((d / "child_pid").read_text()), signal.SIGKILL)
        os.kill(int((d / "pid").read_text()), signal.SIGKILL)
        time.sleep(0.3)
        assert mt.agent_status(r["job"])["state"] == "lost"

    def test_lost_job_is_never_signalled(self, fleet):
        r = mt.agent_run("codex", "SLEEPLONG", machine="dgx")
        st = wait_state(r["job"], states=("running",))
        d = Path(st["dir"])
        end = time.time() + 10
        while not (d / "child_pid").exists() and time.time() < end:
            time.sleep(0.05)
        child = int((d / "child_pid").read_text())
        os.kill(int((d / "pid").read_text()), signal.SIGKILL)   # the supervisor dies, the worker lives
        time.sleep(0.3)
        assert mt.agent_status(r["job"])["state"] == "lost"
        c = mt.agent_cancel(r["job"])
        assert c["ok"] and c["state"] == "lost" and c["how"] == []
        time.sleep(0.2)
        assert procs._pid_alive(child), "a lost job's process was signalled"
        os.killpg(child, signal.SIGKILL)

    def test_cancel_refuses_a_pid_and_unit_that_are_not_the_jobs(self, fleet):
        job = mt.new_job_id("dgx", "codex")
        d = fleet["home"] / "orchestra" / "runs" / "workman" / job
        d.mkdir(parents=True)
        foreign = subprocess.Popen(["sleep", "30"])
        try:
            (d / "job.json").write_text(json.dumps({"job": job, "kind": "agent", "mode": "direct",
                                                    "unit": "ssh.service"}))
            (d / "pid").write_text(str(foreign.pid))
            (d / "pid_start").write_text("1")
            (d / "started").write_text("2026-10-08T00:00:00Z %d\n" % int(time.time()))
            units = fleet["home"] / "fake-units"
            units.mkdir()
            (units / "ssh.pid").write_text(str(foreign.pid))          # the fake reports ssh active
            assert mt.agent_status(job)["state"] == "running"         # only because the unit says so
            c = mt.agent_cancel(job)
            assert c["ok"] is False and c["error"] == "not_owned"
            assert "not run-model-" in c["detail"] and "not this job's supervisor" in c["detail"]
            time.sleep(0.2)
            assert foreign.poll() is None, "a foreign process was signalled"
            assert "stop ssh" not in fake_log(fleet, "fake-systemctl.log")
            assert not (d / "cancelled").exists()
        finally:
            foreign.kill()
            foreign.wait()

    def test_status_does_not_trust_a_reused_pid(self, fleet):
        job = mt.new_job_id("dgx", "codex")
        d = fleet["home"] / "orchestra" / "runs" / "workman" / job
        d.mkdir(parents=True)
        (d / "job.json").write_text(json.dumps({"job": job, "kind": "agent", "mode": "direct", "unit": ""}))
        (d / "pid").write_text(str(os.getpid()))                     # alive, but not this job's
        (d / "started").write_text("2026-10-08T00:00:00Z %d\n" % int(time.time()))
        assert mt.agent_status(job)["state"] == "lost"

    def test_result_is_capped(self, fleet):
        r = mt.agent_run("codex", "BIGLOG", machine="dgx")
        wait_state(r["job"])
        res = mt.agent_result(r["job"], max_lines=10_000)
        assert res["truncated"] is True
        assert len(res["text"]) <= mt.RESULT_CAP
        assert len(res["text"].splitlines()) <= 200
        small = mt.agent_result(r["job"], max_lines=3)
        assert small["text"].splitlines()[-1].startswith("line 3000")
        assert len(small["text"].splitlines()) == 3

    def test_status_of_recent_jobs(self, fleet):
        a = mt.agent_run("codex", "one", machine="dgx")
        b = mt.agent_run("grok", "two", machine="dgx", effort="medium")
        wait_state(a["job"]), wait_state(b["job"])
        allst = mt.agent_status()
        assert allst["ok"] and allst["counts"].get("finished") == 2
        jobs = {j["job"]: j for j in allst["jobs"]}
        assert jobs[b["job"]]["cli"] == "grok" and jobs[b["job"]]["effort"] == "medium"
        assert jobs[a["job"]]["prompt"] == "one"

    @pytest.mark.parametrize("bad", ["", "../etc", "wm-x", "wm-20261008T000000Z-zz-codex-abcdef"])
    def test_bad_job_ids_are_refused(self, fleet, bad):
        assert mt.agent_result(bad)["error"] == "bad_job"
        assert mt.agent_cancel(bad)["error"] == "bad_job"
