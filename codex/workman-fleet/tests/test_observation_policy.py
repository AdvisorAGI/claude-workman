import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import observation
import observation_policy
import server


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKMAN_FLEET_LEARN_ROOT", str(tmp_path / "hub"))
    monkeypatch.setenv("WORKMAN_LEARN_ROOT", str(tmp_path / "local"))
    observation._originals.clear()


def test_default_off_then_on_restores_last_enabled_level_across_instances():
    policy = observation_policy.TaskPolicy("workman-fleet-v1.1")
    assert policy.status()["level"] == "off"
    assert policy.set("medium")["revision"] == 1
    assert observation_policy.TaskPolicy("workman-fleet-v1.1").status()["level"] == "medium"
    assert policy.set("off")["level"] == "off"
    assert policy.set("on")["level"] == "medium"


def test_state_is_private_atomic_bounded_and_contains_no_observation_payload():
    policy = observation_policy.TaskPolicy("bounded-test")
    for index in range(40):
        policy.set("high" if index % 2 else "low")
    state = policy.status()
    assert state["revision"] == 40 and state["shared_across_sessions"] is True
    assert len(list((policy.directory / "history").glob("*.json"))) == observation_policy.HISTORY_SLOTS
    assert oct(policy.file.stat().st_mode & 0o777) == "0o600"
    encoded = policy.file.read_text()
    assert "window" not in encoded and "screen" not in encoded and "typed" not in encoded


def test_independent_process_recalls_same_task_setting():
    observation_policy.TaskPolicy("parallel-task").set("max")
    command = [sys.executable, "-c", "import json,observation_policy;print(json.dumps(observation_policy.TaskPolicy('parallel-task').status()))"]
    env = dict(os.environ, PYTHONPATH=str(SCRIPTS))
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 0 and json.loads(result.stdout)["level"] == "max"


def test_corrupt_or_conflicting_current_setting_fails_closed():
    policy = observation_policy.TaskPolicy("safe-failure")
    policy.directory.mkdir(parents=True)
    policy.file.write_text('{"level":"max"}')
    with pytest.raises(ValueError):
        policy.status()


def test_health_is_local_and_reports_segment_limits():
    policy = observation_policy.TaskPolicy("health-test")
    result = policy.health()
    assert result["remote_probe_performed"] is False
    assert result["crash_recovery"]["parallel_session_lock"] is True
    assert all(row["segment_limit_bytes"] > 0 for row in result["evidence_store"]["journal_segments"].values())


def test_server_profile_switch_is_durable_and_auto_view_obeys_off(monkeypatch):
    observed = AsyncMock(return_value={"v": 1, "observation_id": "a" * 32})
    monkeypatch.setattr(observation, "observe", observed)
    asyncio.run(server.fleet_observe("air", task_id="shared-task", profile_action="set", level="high"))
    asyncio.run(server.fleet_observe("air", task_id="shared-task"))
    assert observed.await_args.args[3] == "compact" and observed.await_args.args[5] == 10
    asyncio.run(server.fleet_observe("air", task_id="shared-task", profile_action="off"))
    asyncio.run(server.fleet_observe("air", task_id="shared-task"))
    assert observed.await_args.args[3] == "full"


@pytest.mark.parametrize("task_id", ["", "../escape", "personal task", "x" * 81])
def test_task_identity_is_nonsecret_and_path_safe(task_id):
    with pytest.raises(ValueError):
        observation_policy.TaskPolicy(task_id)
