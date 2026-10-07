"""Debounce worker tests use state files in tmp dirs and fake input only."""
import pytest

fcntl = pytest.importorskip("fcntl")
import os
from types import SimpleNamespace
from pathlib import Path

from workman import polite, polite_restore, polite_state


def test_timer_spawn_inherits_display_config_and_checkout(monkeypatch):
    calls = []
    monkeypatch.setattr(polite_restore.subprocess, "Popen", lambda argv, **kwargs: calls.append((argv, kwargs)) or SimpleNamespace(pid=42, poll=lambda: None))
    with polite_state.locked(":99"):
        out = polite_restore.ensure(":99")
    assert out["timer"] == "started" and out["pid"] == 42
    argv, options = calls[0]
    assert "--display" in argv and argv[argv.index("--display") + 1] == ":99"
    assert options["env"]["WORKMAN_STATE_DIR"] == os.environ["WORKMAN_STATE_DIR"]
    assert options["env"]["WORKMAN_LEARN_ROOT"] == os.environ["WORKMAN_LEARN_ROOT"]
    assert options["env"]["WORKMAN_DISPLAY"] == ":99"
    assert Path(options["cwd"]) == Path(polite_restore.__file__).resolve().parent.parent
    assert options["pass_fds"] and options["start_new_session"]


def test_timer_exit_unlocks_before_new_action_can_arm_snapshot(monkeypatch):
    _, action_lock = polite_state.paths(":99")
    action_lock.parent.mkdir(parents=True, exist_ok=True)
    timer_fd = os.open(str(action_lock) + ".timer", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(timer_fd, fcntl.LOCK_EX)
    now = [0.0]
    monkeypatch.setattr(polite_restore.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(polite_restore.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(polite, "restore_pending", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("empty timer opened X")))
    try:
        polite_restore.serve(":99", timer_fd)
        # The old timer fd is still open. Its lock must already be free, under
        # the action lock, so ensure() cannot mistake an exiting timer for live.
        other = os.open(str(action_lock) + ".timer", os.O_RDWR)
        try:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(other)
    finally:
        os.close(timer_fd)
    assert now[0] == 60


def test_timer_waits_for_deadline_without_x_reads_and_rechecks_latest_state(monkeypatch):
    now, restores = [0.0], []
    with polite_state.locked(":99") as store:
        store.data.update(snapshot={"pointer": [10, 20]}, due=5.0)
        store.write()
    monkeypatch.setattr(polite_restore.time, "monotonic", lambda: now[0])
    def restore(*args):
        restores.append(now[0])
        with polite_state.locked(":99") as store:
            store.data.pop("snapshot")
            store.write()
    monkeypatch.setattr(polite, "restore_pending", restore)
    def wait(seconds):
        now[0] += seconds
        if 2 <= now[0] < 3:
            with polite_state.locked(":99") as store:
                store.data["due"] = 8.0  # a fresh action rearmed while the timer slept
                store.write()
    monkeypatch.setattr(polite_restore.time, "sleep", wait)
    polite_restore.serve(":99")
    assert restores == [8.0]
