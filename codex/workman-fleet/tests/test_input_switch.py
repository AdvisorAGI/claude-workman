import asyncio
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import device, fleet, input_switch, lease, learning


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKMAN_LEARN_ROOT", str(tmp_path / "local"))
    monkeypatch.setenv("WORKMAN_FLEET_LEARN_ROOT", str(tmp_path / "hub"))


def backend():
    b = object.__new__(device.Backend)
    b.mac = False; b.gate = lambda a: None
    b.active = lambda: {"focus_token": "current"}
    return b


def test_individual_switches_preserve_other_input_and_survive_process():
    input_switch.update(mouse=False)
    with pytest.raises(input_switch.Disabled): input_switch.check("click")
    input_switch.check("type")
    result = subprocess.check_output([sys.executable, "-c", "import input_switch,json;print(json.dumps(input_switch.state()))"],
                                     env=dict(os.environ, PYTHONPATH=str(Path(input_switch.__file__).parent)))
    assert json.loads(result) == {"mouse": False, "keyboard": True}


def test_stop_does_not_wait_for_input_owner():
    with lease.hold("one", "click"):
        input_switch.update(mouse=False, keyboard=False)
        with pytest.raises(input_switch.Disabled): input_switch.check("key")
    assert not input_switch.state()["mouse"]


def test_disabled_input_never_reaches_workman():
    b = backend(); b.call = lambda *a, **k: pytest.fail("input must be blocked")
    input_switch.update(mouse=False, keyboard=False)
    with pytest.raises(input_switch.Disabled): b.execute("click", {"x": 2, "y": 2})
    with pytest.raises(input_switch.Disabled): b.execute("type", {"text": "test", "expect_focus": "current"})


def test_typing_stops_at_next_small_chunk():
    b = backend(); calls = []
    def call(name, **args):
        calls.append(args["text"]); input_switch.update(keyboard=False)
    b.call = call
    with pytest.raises(input_switch.Disabled): b.execute("type", {"text": "abcdefghijk", "expect_focus": "current"})
    assert calls == ["abcd"]


def test_corrupt_switch_file_fails_closed():
    input_switch.path().parent.mkdir(parents=True)
    input_switch.path().write_text("invalid")
    assert input_switch.state() == {"mouse": False, "keyboard": False}


def test_finish_releases_even_when_restoration_disabled():
    input_switch.update(mouse=False, keyboard=False)
    with lease.hold("one", "reserve"): pass
    r = device.run({"node": "dgx", "action": "finish", "context": {"session": "one"},
        "args": {"restore_focus": "fixture", "restore_x": 10, "restore_y": 20}}, lambda _: backend())
    assert r["ok"] and r["data"]["restoration"] == {"focus": "skipped_input_disabled", "move": "skipped_input_disabled"}
    assert lease.status("two")["available"]


def test_finish_cannot_release_another_session():
    with lease.hold("one", "reserve"): pass
    r = device.run({"node": "dgx", "action": "finish", "context": {"session": "two"},
        "args": {"restore_focus": "fixture", "restore_x": 10, "restore_y": 20}}, lambda _: backend())
    assert r["code"] == "device_busy"


def test_stop_bypasses_hub_operation_lock(monkeypatch):
    root = learning.hub_root(); root.mkdir(parents=True)
    monkeypatch.setattr(fleet, "transport", AsyncMock(return_value={"ok": True, "code": "ok", "local_recorded": True}))
    monkeypatch.setattr(fleet, "collect", AsyncMock(return_value={"ok": True}))
    with (root / ".control-dgx.lock").open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        assert asyncio.run(fleet.control("dgx", "input", {"mouse": False}))["ok"]


def test_switch_booleans_only_and_journal_contains_no_args():
    with pytest.raises(device.Refusal): device.validate("input", {"mouse": "off"})
    r = device.run({"node": "dgx", "action": "input", "args": {"keyboard": False}})
    assert r["event"]["permissions"] == {"mouse_enabled": True, "keyboard_enabled": False}
    assert set(r["event"]) <= {"id", "node", "action", "code", "ts", "ok", "duration_ms", "permissions"}
