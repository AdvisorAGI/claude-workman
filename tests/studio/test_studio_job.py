"""Pins the job folder layout, the one heat rule (stop at 86 C, resume under 80 C), one render at a
time, and PID-only kills from the job's own ledger."""
import os
import signal
import subprocess
import sys

import pytest

from workman.studio import job as j


def test_new_job_is_dated_under_root_and_never_overwrites(tmp_path):
    r = j.new_job("shield", title="Shield", day="20260925")
    d = tmp_path / "jobs" / "20260925-shield"
    assert r["job"] == str(d) and set(r["created"]) == set(j.DOCS)
    assert all((d / sub).is_dir() for sub in j.DIRS)
    (d / "BRIEF.md").write_text("mine")
    again = j.new_job("shield", day="20260925")
    assert "BRIEF.md" in again["kept"] and (d / "BRIEF.md").read_text() == "mine"
    with pytest.raises(j.StudioError):
        j.new_job("Bad Slug")


def test_resolve_job_by_path_folder_or_newest_slug(tmp_path):
    j.new_job("ad", day="20260101")
    j.new_job("ad", day="20260925")
    root = tmp_path / "jobs"
    assert j.resolve_job("ad") == root / "20260925-ad"
    assert j.resolve_job("20260101-ad") == root / "20260101-ad"
    assert j.resolve_job(str(root / "20260101-ad")) == root / "20260101-ad"
    with pytest.raises(j.StudioError):
        j.resolve_job("../etc")


def test_status_reports_templates_written_docs_and_qc_result(tmp_path, monkeypatch):
    monkeypatch.setattr(j, "board_temp", lambda root=None: 55.0)
    d = tmp_path / "jobs" / "20260925-x"
    j.new_job("x", day="20260925")
    (d / "SCRIPT.md").write_text("# real script\n")
    (d / "QC.md").write_text("RESULT: FAIL\n\n| 1 | x | FAIL | e | editor |\n")
    st = j.status(d)
    assert st["docs"]["SCRIPT.md"] == "written" and st["docs"]["BRIEF.md"] == "template"
    assert st["qc"] == "FAIL" and st["render"] == {"state": "none"} and st["heat_c"] == 55.0
    with pytest.raises(j.StudioError):
        j.status(tmp_path / "nope")


def test_board_temp_reads_hottest_zone(tmp_path):
    for n, t in enumerate(("45000", "86500", "bad")):
        (tmp_path / f"thermal_zone{n}").mkdir()
        (tmp_path / f"thermal_zone{n}" / "temp").write_text(t)
    assert j.board_temp(tmp_path) == 86.5
    assert j.board_temp(tmp_path / "none") == 0.0


def test_cool_down_starts_under_86_waits_to_80_and_fails_closed():
    assert j.cool_down(temp=lambda: 85.9) == 0.0
    temps = iter([86.0, 84.0, 81.0, 79.9])
    t = {"now": 0.0}
    waited = j.cool_down(temp=lambda: next(temps), sleep=lambda s: t.update(now=t["now"] + s), clock=lambda: t["now"])
    assert waited == 40.0                       # two 20 s polls: 84, 81, then 79.9 releases
    with pytest.raises(j.HeatTimeout):
        j.cool_down(max_wait=30, temp=lambda: 90.0, sleep=lambda s: t.update(now=t["now"] + s),
                    clock=lambda: t["now"])


def test_renders_into_only_counts_this_jobs_out_dir_or_cwd():
    procs = [("1", "ffmpeg -i - /w/other/out/a.mp4", "/w"), ("2", "ffmpeg -i x /w/job/out/ad-v1.mp4", "/"),
             ("3", "ffmpeg -i x out/ad-v1.mp4", "/w/job"), ("4", "ffmpeg -i /w/job/clips/a.mp4 -f null -", "/tmp")]
    assert j.renders_into(procs, "/w/job") == ["2", "3"]


def test_wait_turn_fails_closed_while_another_render_runs(tmp_path):
    t = {"now": 0.0}
    with pytest.raises(j.TurnTimeout, match="still running"):
        j.wait_turn(tmp_path, max_wait=30, busy=lambda: ["4242"], heat=lambda: 0,
                    sleep=lambda s: t.update(now=t["now"] + s), clock=lambda: t["now"])
    calls = []
    j.wait_turn(tmp_path, busy=lambda: [], heat=lambda: calls.append(1), sleep=lambda s: None)
    assert calls == [1]


def test_render_lock_serialises(tmp_path):
    lock = tmp_path / "l.lock"
    with j.render_lock(lock):
        with pytest.raises(j.TurnTimeout):
            with j.render_lock(lock, wait=0, poll=0):
                pass


def test_kill_job_pid_only_signals_ledger_pids_whose_argv_matches(tmp_path):
    d = tmp_path / "job"
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert j.kill_job_pid(d, p.pid)["error"] == "not_tracked"
        j.track_pid(d, p.pid, "render test", "no-such-argv")
        assert j.kill_job_pid(d, p.pid)["error"] == "pid_mismatch" and p.poll() is None
        j.track_pid(d, p.pid, "render test", "time.sleep(30)")
        r = j.kill_job_pid(d, p.pid)
        assert r["ok"] and r["signal"] == signal.SIGTERM
        assert p.wait(timeout=10) != 0
        assert j.kill_job_pid(d, p.pid)["error"] == "not_running"
    finally:
        if p.poll() is None:
            os.kill(p.pid, signal.SIGKILL)


def test_ledger_is_bounded(tmp_path):
    for n in range(j.MAX_PIDS + 5):
        j.track_pid(tmp_path, 100000 + n, "x", "y")
    rows = j.tracked(tmp_path)
    assert len(rows) == j.MAX_PIDS and rows[-1]["pid"] == 100000 + j.MAX_PIDS + 4


def test_qc_verdict_reads_first_result_line():
    assert j.qc_verdict("RESULT: PENDING REVIEW\n") == "PENDING REVIEW"
    assert j.qc_verdict("# QC\nNot run yet.\n") == "none"


def test_ffmpeg_cmd_is_nice_19_and_four_threads():
    cmd = j.ffmpeg_cmd(["-i", "a"])
    assert cmd[:4] == ["nice", "-n", "19", "ffmpeg"] and cmd[cmd.index("-threads") + 1] == "4"
