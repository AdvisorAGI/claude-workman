"""Installed CLI sources dispatch every input verb through Workman tools."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import json
import os
import subprocess

import pytest

from workman import desktop, owner_pause, polite
from workman.platform import darwin
from test_polite import seat


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


@pytest.mark.parametrize("reason", ["input_disabled", "human_active"])
def test_paste_start_refusal_never_changes_clipboard(cli, seat, monkeypatch, reason):
    mod, calls, _ = cli
    if reason == "input_disabled":
        owner_pause.pause_from_escape()
    else:
        monkeypatch.setattr(owner_pause, "wait_for_human_quiet", lambda **kwargs: True)
    assert mod.run("paste", ["exact text"])["error"] == reason
    assert calls == [] and seat.ch.xtst.events == []


@pytest.mark.parametrize("target_exists", [False, True])
def test_paste_missing_or_changed_target_never_changes_clipboard(cli, seat, monkeypatch, target_exists):
    mod, calls, _ = cli
    monkeypatch.setattr(seat.ch, "top_level_focus", seat.ch.input_focus)
    monkeypatch.setattr(polite, "_schedule", lambda display: {"ok": True})
    focused = polite.run("focus_window", lambda: polite.set_target(
        seat.ch.input_focus() if target_exists else None) or {"ok": True})
    assert focused["ok"], focused
    if target_exists:
        seat.focus = 200
    out = mod.run("paste", ["exact text"])
    assert out["error"] == "focus_changed"
    assert out["intended_window"] == ({"ok": True, "window": 100, "revert_to": 1} if target_exists else None)
    assert out["current_window"]["window"] == seat.focus
    assert calls == [] and seat.ch.xtst.events == []


@pytest.mark.parametrize("platform,shared_gate", [("macOS", True), ("Linux/X11", False)])
def test_paste_keyboard_pause_before_clipboard_without_polite_scope(
        cli, monkeypatch, platform, shared_gate):
    mod, calls, _ = cli
    monkeypatch.setattr(desktop, "active", lambda: SimpleNamespace(PLATFORM=platform))
    monkeypatch.setattr(polite, "enabled", lambda *args: False)
    monkeypatch.setattr(polite, "presence_enabled", lambda module: shared_gate)
    owner_pause._write({"mouse": True, "keyboard": False})
    assert mod.run("paste", ["exact text"])["error"] == "input_disabled"
    assert calls == []


def test_paste_owner_stays_busy_before_clipboard_without_polite_scope(cli, monkeypatch):
    mod, calls, _ = cli
    monkeypatch.setattr(desktop, "active", lambda: SimpleNamespace(PLATFORM="macOS"))
    monkeypatch.setattr(owner_pause, "human_active", lambda: True)
    monkeypatch.setattr(owner_pause, "wait_for_human_quiet", lambda: True)
    assert mod.run("paste", ["exact text"])["error"] == "human_active"
    assert calls == []


@pytest.mark.parametrize("platform,shortcut", [("macOS", "super+v"), ("Linux/X11", "ctrl+v")])
@pytest.mark.parametrize("clipboard_exit", [0, 1])
def test_paste_uses_one_scope_clipboard_then_desktop_shortcut(
        cli, monkeypatch, platform, shortcut, clipboard_exit):
    mod, calls, _ = cli
    events = []
    text = "exact Unicode: café\nsecond line"
    backend = darwin if platform == "macOS" else SimpleNamespace(
        PLATFORM=platform, display_name=lambda: ":98")
    monkeypatch.setattr(desktop, "active", lambda: backend)
    monkeypatch.setattr(polite, "current", lambda: object())
    monkeypatch.setattr(polite, "guard", lambda action: events.append(("guard", action)))

    def scope(action, call):
        events.append(("scope", action))
        return call()

    monkeypatch.setattr(polite, "run", scope)
    key_result = {"ok": False, "error": "focus_changed"}
    monkeypatch.setattr(desktop, "press_key", lambda key: events.append(("key", key)) or key_result)

    def pbcopy(argv, **kwargs):
        assert platform == "macOS" and argv == ["pbcopy"]
        assert kwargs == {"input": text, "text": True, "capture_output": True, "timeout": 5}
        events.append(("clipboard", "pbcopy"))
        return SimpleNamespace(returncode=clipboard_exit)

    def xclip(argv, **kwargs):
        assert platform == "Linux/X11"
        assert argv == ["xclip", "-display", ":98", "-selection", "clipboard", "-in"]
        assert kwargs == {"stdin": subprocess.PIPE, "stdout": subprocess.DEVNULL,
                          "stderr": subprocess.DEVNULL, "start_new_session": True}

        def communicate(**kwargs):
            assert kwargs == {"input": text.encode("utf-8"), "timeout": 5}
            events.append(("clipboard", "xclip"))

        return SimpleNamespace(returncode=clipboard_exit, communicate=communicate)

    monkeypatch.setattr(mod.subprocess, "run", pbcopy)
    monkeypatch.setattr(mod.subprocess, "Popen", xclip)
    out = mod.run("paste", [text])
    assert events[:2] == [("scope", "paste"), ("guard", "paste")]
    assert len([event for event in events if event[0] == "scope"]) == 1
    assert calls == []
    if clipboard_exit:
        assert out["error"] == "clipboard_failed" and len(events) == 3
    else:
        assert out == key_result and events[-1] == ("key", shortcut)


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("xclip", 5), BrokenPipeError()])
def test_failed_xclip_writer_is_stopped_and_reaped(cli, monkeypatch, failure):
    mod, calls, _ = cli
    monkeypatch.setattr(desktop, "active", lambda: SimpleNamespace(
        PLATFORM="Linux/X11", display_name=lambda: ":98"))
    monkeypatch.setattr(polite, "enabled", lambda *args: False)
    monkeypatch.setattr(polite, "presence_enabled", lambda module: False)
    events = []

    def communicate(**kwargs):
        events.append(("communicate", kwargs))
        if "input" in kwargs:
            raise failure

    writer = SimpleNamespace(communicate=communicate, poll=lambda: None,
                             kill=lambda: events.append(("kill",)))
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *args, **kwargs: writer)
    monkeypatch.setattr(desktop, "press_key", lambda key: pytest.fail("failed clipboard sent key"))
    out = mod.run("paste", ["text"])
    assert out["error"] == "clipboard_failed"
    assert out["detail"] == type(failure).__name__
    assert events == [("communicate", {"input": b"text", "timeout": 5}),
                      ("kill",), ("communicate", {"timeout": 1})]
    assert calls == []


def test_missing_pbcopy_returns_refusal_without_key(cli, monkeypatch):
    mod, _, _ = cli
    monkeypatch.setattr(desktop, "active", lambda: darwin)
    monkeypatch.setattr(desktop, "press_key", lambda key: pytest.fail("missing clipboard sent key"))

    def missing(*args, **kwargs):
        raise FileNotFoundError("pbcopy")

    monkeypatch.setattr(mod.subprocess, "run", missing)
    out = mod.run("paste", ["text"])
    assert out == {"ok": False, "error": "clipboard_failed", "via": "pbcopy", "detail": "FileNotFoundError"}


def test_private_display_paste_ignores_shared_presence(cli, monkeypatch):
    mod, calls, _ = cli
    monkeypatch.delenv("WORKMAN_POLITE", raising=False)
    monkeypatch.setattr(desktop, "active", lambda: SimpleNamespace(
        PLATFORM="Linux/X11", display_name=lambda: ":99"))
    monkeypatch.setattr(owner_pause, "human_active", lambda *args: pytest.fail("private owner activity"))
    monkeypatch.setattr(owner_pause, "wait_for_human_quiet", lambda *args: pytest.fail("private owner wait"))
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, communicate=lambda **kwargs: None))
    monkeypatch.setattr(desktop, "press_key", lambda key: {"ok": True, "key": key})
    assert mod.run("paste", ["text"]) == {"ok": True, "key": "ctrl+v"}
    assert calls == []


def test_legacy_paste_keeps_daemon_api(cli):
    mod, calls, refusal = cli
    mod.KIND = "atmos"
    mod.S = SimpleNamespace(computer_paste=lambda **kwargs: calls.append(kwargs) or refusal)
    assert mod.run("paste", ["legacy text"]) == refusal
    assert calls == [{"text": "legacy text"}]


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


@pytest.mark.parametrize("verb,args", [
    ("pause", ["status"]), ("pause", ["pause"]),
    ("pause", ["resume", "confirmed"]), ("pause", ["release"]),
    ("resume", []), ("handoff", []),
])
def test_fleet_all_allows_safety_verbs_and_preserves_exit(tmp_path, verb, args):
    stub = tmp_path / "wm.py"
    stub.write_text("import json,sys\nprint(json.dumps(sys.argv[1:]))\nsys.exit(1)\n")
    script = Path(__file__).parents[1] / "ops" / "cli" / "fleet-wm.sh"
    env = dict(os.environ, FLEET_WM_OUT=str(tmp_path / "shots"), WM_PY_SOURCE=str(stub))
    out = subprocess.run(["bash", str(script), "all", verb, *args],
                         env=env, capture_output=True, text=True)
    assert out.returncode == 1
    dispatched = [json.loads(line) for line in out.stdout.splitlines() if line.startswith("[")]
    assert dispatched == [["--host", host, verb, *args] for host in ("dgx", "mac-mini", "md", "air")]


def test_live_smoke_cannot_run_on_owner_display(tmp_path):
    script = Path(__file__).parents[1] / "scripts" / "polite_smoke.py"
    import sys
    env = dict(os.environ, WORKMAN_DISPLAY=":0", DISPLAY=":0")
    out = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True)
    assert out.returncode != 0 and "Refusing" in out.stderr
