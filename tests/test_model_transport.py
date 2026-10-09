"""Machines, job ids, the ssh path, systemd units and reconcile."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from model_fakes import FAKE_RUN_MODEL, _exe, fake_log, fleet, wait_state  # noqa: F401
from workman import model_tools as mt
from workman.model_tools import catalog as mcat  # noqa: F401
from workman.model_tools import transport as tp  # noqa: F401
from workman.model_tools.jobctl import common, grok_media, procs, status, supervise  # noqa: F401


class TestTransport:
    def test_mac_job_goes_over_the_fleet_ssh_alias(self, fleet):
        r = mt.agent_run("codex", "hello mac", machine="mac-one", effort="high")
        assert r["ok"], r
        assert "-m1-codex-" in r["job"] and r["machine"] == "mac-one"
        assert "mac-one python3 -I - launch" in fake_log(fleet, "fake-ssh.log")
        st = wait_state(r["job"])
        assert st["state"] == "finished" and st["machine"] == "mac-one"
        assert "mac-one python3 -I - status" in fake_log(fleet, "fake-ssh.log")

    def test_unreachable_machine_fails_clearly(self, fleet, monkeypatch):
        monkeypatch.setenv("FAKE_SSH_DOWN", "mac-air")
        r = mt.agent_run("codex", "x", machine="mac-air")
        assert r["ok"] is False and r["error"] == "unreachable" and r["layer"] == "ssh"
        assert "Connection refused" in r["detail"]
        assert fake_log(fleet) == ""

    def test_systemd_unit_on_linux(self, fleet, monkeypatch):
        monkeypatch.delenv("WM_JOBCTL_NO_SYSTEMD")
        if not sys.platform.startswith("linux"):
            pytest.skip("systemd path is Linux only")
        r = mt.agent_run("codex", "hi", machine="dgx")
        assert r["ok"] and r["how"] == "systemd"
        assert r["unit"] == f"run-model-{r['job']}.service"
        argv = fake_log(fleet, "fake-systemd-run.log")
        for flag in ("--user", "--collect", "ExitType=cgroup", "RuntimeMaxSec=", f"--unit=run-model-{r['job']}"):
            assert flag in argv
        assert wait_state(r["job"])["state"] == "finished"
        assert "esc=1" in fake_log(fleet), "inside our own unit run-model must run in place"

    def test_cancel_through_systemctl(self, fleet, monkeypatch):
        monkeypatch.delenv("WM_JOBCTL_NO_SYSTEMD")
        if not sys.platform.startswith("linux"):
            pytest.skip("systemd path is Linux only")
        r = mt.agent_run("codex", "SLEEPLONG", machine="dgx")
        wait_state(r["job"], states=("running",))
        c = mt.agent_cancel(r["job"])
        assert c["state"] == "cancelled" and any("systemctl stop" in h for h in c["how"])
        assert f"stop run-model-{r['job']}" in fake_log(fleet, "fake-systemctl.log")

    def test_systemd_failure_never_falls_back_to_nohup(self, fleet, monkeypatch):
        monkeypatch.delenv("WM_JOBCTL_NO_SYSTEMD")
        monkeypatch.setenv("FAKE_SYSTEMD_FAIL", "1")
        if not sys.platform.startswith("linux"):
            pytest.skip("systemd path is Linux only")
        r = mt.agent_run("codex", "hi", machine="dgx")
        assert r["ok"] is False and r["error"] == "systemd_run_failed" and r["launch_unconfirmed"]
        assert r["unit_if_systemd"] == f"run-model-{r['job']}.service"
        time.sleep(0.5)
        assert fake_log(fleet) == "", "a second (nohup) run started after the systemd attempt"
        assert not (Path(r["dir"]) / "pid").exists()
        st = mt.agent_status(r["job"])
        assert st["state"] == "launch_failed" and "Failed to connect to bus" in st["launch_error"]
        rec = mt.agent_status(reconcile=True)["reconcile"]
        assert [u["job"] for u in rec["unconfirmed_launches"]] == [r["job"]]

    def test_reconcile_lists_units_without_a_job_record(self, fleet):
        units = fleet["home"] / "fake-units"
        units.mkdir()
        orphan = mt.new_job_id("dgx", "codex")
        (units / f"run-model-{orphan}.pid").write_text(str(os.getpid()))
        (units / "run-model-untracked-20261008T000000Z-1.pid").write_text(str(os.getpid()))
        ok = mt.agent_run("codex", "hello", machine="dgx")
        wait_state(ok["job"])
        (units / f"run-model-{ok['job']}.pid").write_text(str(os.getpid()))   # has its records
        rec = mt.agent_status(reconcile=True)["reconcile"]["machines"]["dgx"]
        assert [o["job"] for o in rec["orphans"]] == [orphan]
        assert rec["orphans"][0]["registry"] is False and rec["workman_units"] == 2
        assert rec["other_run_model_units"] == ["run-model-untracked-20261008T000000Z-1.service"]


class TestJobIds:
    def test_job_ids_name_their_machine(self, monkeypatch):
        monkeypatch.setenv("WORKMAN_MACHINE", "dgx")
        for m, code in (("dgx", "dgx"), ("mac-one", "m1"), ("mac-air", "air")):
            j = mt.new_job_id(m, "codex")
            assert mt.JOB_RE.match(j) and f"-{code}-codex-" in j
            assert mt.machine_of(j) == m
            assert common.JOB_RE.match(j)
        assert mt.resolve_machine("local") == "dgx"
        assert mt.resolve_machine("m1") == "mac-one"
        assert mt.resolve_machine("") == "mac-one"
        with pytest.raises(ValueError):
            mt.resolve_machine("hetzner")
