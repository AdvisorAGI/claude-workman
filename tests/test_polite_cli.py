"""Installed CLI sources dispatch every input verb through Workman tools."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import json
import os
import subprocess

import pytest


@pytest.fixture
def cli(monkeypatch):
    path = Path(__file__).parents[1] / "ops" / "cli" / "wm.py"
    spec = importlib.util.spec_from_file_location("polite_wm_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    calls = []
    refusal = {"ok": False, "error": "human_active"}

    def tool(name):
        def call(*args, **kwargs):
            calls.append((name, args, kwargs))
            return refusal
        return call

    mod.KIND = "workman"
    mod.S = SimpleNamespace(**{name: tool(name) for name in (
        "click", "type_text", "press_key", "move", "hover", "scroll", "drag", "focus_window")})
    # No helper subprocess may inject input or edit the clipboard.
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: pytest.fail("raw subprocess"))
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: pytest.fail("raw subprocess"))
    return mod, calls, refusal


@pytest.mark.parametrize("verb,args,tool", [
    ("click", ["20", "30"], "click"),
    ("type", ["partial text"], "type_text"),
    ("paste", ["partial text"], "type_text"),
    ("key", ["ctrl+a"], "press_key"),
    ("move", ["20", "30"], "move"),
    ("hover", ["20", "30"], "hover"),
    ("scroll", ["down", "4"], "scroll"),
    ("drag", ["20", "30", "40", "50"], "drag"),
    ("focus", ["window"], "focus_window"),
])
def test_dispatch_preserves_gate_refusal(cli, verb, args, tool):
    mod, calls, refusal = cli
    assert mod.run(verb, args) == refusal
    assert len(calls) == 1 and calls[0][0] == tool


def test_burst_is_one_gated_batch(cli):
    mod, calls, refusal = cli
    mod.S.batch = lambda actions: calls.append(actions) or refusal
    actions = [{"action": "focus_window", "query": "scratch"},
               {"action": "type_text", "text": "hello"}]
    assert mod.run("burst", [json.dumps(actions)]) == refusal
    assert calls == [actions]


@pytest.mark.parametrize("verb", ["click", "type", "paste", "key", "move", "scroll", "drag", "focus", "burst"])
def test_fleet_dispatches_to_same_cli_and_preserves_refusal(tmp_path, verb):
    stub = tmp_path / "wm.py"
    stub.write_text("import json,sys\nprint(json.dumps(sys.argv[1:]))\nsys.exit(1)\n")
    script = Path(__file__).parents[1] / "ops" / "cli" / "fleet-wm.sh"
    env = dict(os.environ, FLEET_WM_OUT=str(tmp_path / "shots"), WM_PY_SOURCE=str(stub))
    out = subprocess.run(["bash", str(script), "dgx", verb, "literal argument"],
                         env=env, capture_output=True, text=True)
    assert out.returncode == 1
    assert json.loads(out.stdout) == ["--host", "dgx", verb, "literal argument"]
    out = subprocess.run(["bash", str(script), "all", verb, "literal argument"],
                         env=env, capture_output=True, text=True)
    assert out.returncode == 2 and "one device" in out.stderr


def test_live_smoke_cannot_run_on_owner_display(tmp_path):
    script = Path(__file__).parents[1] / "scripts" / "polite_smoke.py"
    import sys
    env = dict(os.environ, WORKMAN_DISPLAY=":0", DISPLAY=":0")
    out = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True)
    assert out.returncode != 0 and "Refusing" in out.stderr
