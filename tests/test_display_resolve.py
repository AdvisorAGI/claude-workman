"""Display auto-detect: no X server needed, every system hook is faked.

The scenario these guard is the 2026-10-10 reboot: the env pinned :1, GNOME's
Xorg came up on :0 (started by gdm with -displayfd, so no ':0' in its argv),
and an Xvfb sat on :99. Capture must land on :0, never :1 and never :99.
"""
from __future__ import annotations

import subprocess

import pytest

from workman import display_resolve as dr
from workman import x11, xtest

# conftest swaps these for blocked stubs per test; keep the real ones.
_REAL_RUN = x11._run
_REAL_XT = x11._xt


@pytest.fixture
def gdm_auth(tmp_path):
    p = tmp_path / "run" / "user" / "1000" / "gdm" / "Xauthority"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"cookie")
    return str(p)


@pytest.fixture
def fake_system(monkeypatch, gdm_auth):
    """Today's DGX: Xorg (gdm, -displayfd) on :0, Xvfb on :99, shell on :0."""
    state = {
        "sockets": [0, 99],
        "servers": [
            dr.XServer(pid=4264, name="Xorg", display=0, auth=gdm_auth),
            dr.XServer(pid=10928, name="Xvfb", display=99),
        ],
        "shell": {"DISPLAY": ":0", "XAUTHORITY": gdm_auth},
        "opens": {":0", ":99"},
        "probes": [],
    }

    def probe(display, xauthority=None):
        state["probes"].append((display, xauthority))
        return display in state["opens"]

    monkeypatch.setenv("WORKMAN_DISPLAY_AUTODETECT", "1")
    monkeypatch.setattr(dr.sys, "platform", "linux")
    monkeypatch.setattr(dr, "_probe", probe)
    monkeypatch.setattr(dr, "_socket_displays", lambda: list(state["sockets"]))
    monkeypatch.setattr(dr, "_x_servers", lambda: list(state["servers"]))
    monkeypatch.setattr(dr, "_shell_env", lambda: state["shell"])
    monkeypatch.setattr(dr, "gdm_xauthority", lambda uid=None: gdm_auth)
    return state


# ---- resolve() --------------------------------------------------------------

def test_configured_display_that_opens_is_kept(fake_system):
    res = dr.resolve(":0")
    assert (res.display, res.source) == (":0", "configured")
    assert res.xauthority is None
    assert fake_system["probes"][0][0] == ":0"


def test_dead_configured_display_falls_back_to_gdm_xorg(fake_system, gdm_auth):
    res = dr.resolve(":1")
    assert res.display == ":0"
    assert res.source == "auto"
    assert res.xauthority == gdm_auth
    assert ":99" not in [d for d, _ in fake_system["probes"]]


def test_display_99_is_never_auto_picked(fake_system):
    fake_system["opens"] = {":99"}
    res = dr.resolve(":1")
    assert res.source == "fallback"
    assert res.display == ":1"
    assert ":99" not in [d for d, _ in fake_system["probes"][1:]]


def test_any_xvfb_display_is_skipped(fake_system):
    fake_system["sockets"] = [5]
    fake_system["servers"] = [dr.XServer(pid=1, name="Xvfb", display=5)]
    fake_system["shell"] = None
    fake_system["opens"] = {":5"}
    res = dr.resolve(":1")
    assert res.source == "fallback"
    assert [d for d, _ in fake_system["probes"]] == [":1"]


def test_gnome_shell_display_beats_lower_numbered_xorg(fake_system, gdm_auth):
    fake_system["sockets"] = [0, 2]
    fake_system["servers"] = [dr.XServer(pid=1, name="Xorg", display=0),
                              dr.XServer(pid=2, name="Xorg", display=2, auth=gdm_auth)]
    fake_system["shell"] = {"DISPLAY": ":2", "XAUTHORITY": gdm_auth}
    fake_system["opens"] = {":0", ":2"}
    res = dr.resolve(":7")
    assert res.display == ":2"
    assert res.reason == "gnome-shell"


def test_gdm_xorg_beats_plain_xorg_without_shell(fake_system, gdm_auth):
    fake_system["sockets"] = [0, 3]
    fake_system["servers"] = [dr.XServer(pid=1, name="Xorg", display=0, auth="/tmp/other"),
                              dr.XServer(pid=2, name="Xorg", display=3, auth=gdm_auth)]
    fake_system["shell"] = None
    fake_system["opens"] = {":0", ":3"}
    res = dr.resolve(":1")
    assert res.display == ":3"
    assert res.reason == "Xorg started by gdm"


def test_candidate_that_does_not_open_is_passed_over(fake_system):
    fake_system["sockets"] = [0, 1, 99]
    fake_system["servers"].append(dr.XServer(pid=3, name="Xorg", display=1))
    fake_system["opens"] = {":1", ":99"}  # :0 is gone, :1 is live
    res = dr.resolve(":4")
    assert res.display == ":1"


def test_no_config_resolves_too(fake_system, monkeypatch):
    for var in ("WORKMAN_DISPLAY", "DISPLAY"):
        monkeypatch.setenv(var, "x")
        monkeypatch.delenv(var)
    res = dr.resolve(None)
    assert res.display == ":0" and res.source == "auto"


def test_pinned_headless_display_is_never_switched(fake_system):
    fake_system["opens"] = {":0"}  # :99 is down
    res = dr.resolve(":99")
    assert res.display == ":99"
    assert res.source == "configured"
    assert fake_system["probes"] == []


def test_autodetect_off_is_passthrough(fake_system, monkeypatch):
    monkeypatch.setenv("WORKMAN_DISPLAY_AUTODETECT", "0")
    res = dr.resolve(":1")
    assert (res.display, res.source) == (":1", "passthrough")
    assert fake_system["probes"] == []


@pytest.mark.parametrize("plat", ["darwin", "win32"])
def test_non_linux_is_passthrough(fake_system, monkeypatch, plat):
    monkeypatch.setattr(dr.sys, "platform", plat)
    res = dr.resolve(":1")
    assert (res.display, res.source) == (":1", "passthrough")
    assert fake_system["probes"] == []


@pytest.mark.parametrize("name,num", [(":0", 0), (":10.0", 10), ("host:0", None), ("", None), (None, None)])
def test_display_number(name, num):
    assert dr.display_number(name) == num


def test_probe_uses_xdpyinfo_with_timeout(monkeypatch):
    seen = {}

    def fake_run(argv, env=None, timeout=None, **kw):
        seen.update(argv=argv, display=env["DISPLAY"], xauth=env.get("XAUTHORITY"), timeout=timeout)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(dr.shutil, "which", lambda b: "/usr/bin/xdpyinfo")
    monkeypatch.setattr(dr.subprocess, "run", fake_run)
    assert dr._probe(":0", "/run/user/1000/gdm/Xauthority") is True
    assert seen["argv"] == ["/usr/bin/xdpyinfo", "-display", ":0"]
    assert seen["xauth"] == "/run/user/1000/gdm/Xauthority"
    assert seen["timeout"] == dr.PROBE_TIMEOUT_S


def test_probe_timeout_is_a_no(monkeypatch):
    def hang(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 2)

    monkeypatch.setattr(dr.shutil, "which", lambda b: "/usr/bin/xdpyinfo")
    monkeypatch.setattr(dr.subprocess, "run", hang)
    assert dr._probe(":0") is False


# ---- x11 integration ----------------------------------------------------------

@pytest.fixture
def fresh_x11(monkeypatch):
    monkeypatch.setattr(x11, "DISPLAY", ":1")
    monkeypatch.setattr(x11, "_DISPLAY_STATE", {"resolved": False, "last_try": 0.0, "logged": None})
    monkeypatch.setenv("XAUTHORITY", "placeholder")  # records the original for undo
    monkeypatch.delenv("XAUTHORITY")
    monkeypatch.setenv("WORKMAN_DISPLAY", ":1")


def test_current_display_resolves_once_sets_xauthority_and_logs(fake_system, fresh_x11, gdm_auth, capsys):
    import os
    assert x11.current_display() == ":0"
    assert os.environ["XAUTHORITY"] == gdm_auth
    n = len(fake_system["probes"])
    assert x11.current_display() == ":0"
    assert len(fake_system["probes"]) == n  # cached, no second probe
    err = capsys.readouterr().err
    assert err.count("workman: X display :0") == 1


def test_run_re_resolves_when_display_will_not_open(fake_system, fresh_x11, monkeypatch):
    monkeypatch.setattr(x11, "_DISPLAY_STATE", {"resolved": True, "last_try": 0.0, "logged": None})
    calls = []

    def fake_run(cmd, env=None, **kw):
        calls.append(env["DISPLAY"])
        if env["DISPLAY"] == ":1":
            return subprocess.CompletedProcess(cmd, 1, "", "Error: Can't open display: :1")
        return subprocess.CompletedProcess(cmd, 0, "1920 1080", "")

    monkeypatch.setattr(x11.subprocess, "run", fake_run)
    r = _REAL_RUN(["xdotool", "getdisplaygeometry"])
    assert r.returncode == 0
    assert calls == [":1", ":0"]
    assert x11.DISPLAY == ":0"


def test_run_does_not_re_resolve_on_other_errors(fake_system, fresh_x11, monkeypatch):
    monkeypatch.setattr(x11, "_DISPLAY_STATE", {"resolved": True, "last_try": 0.0, "logged": None})
    calls = []

    def fake_run(cmd, env=None, **kw):
        calls.append(env["DISPLAY"])
        return subprocess.CompletedProcess(cmd, 1, "", "no such window")

    monkeypatch.setattr(x11.subprocess, "run", fake_run)
    _REAL_RUN(["xdotool", "windowactivate", "42"])
    assert calls == [":1"]
    assert fake_system["probes"] == []


def test_refresh_keeps_a_display_that_still_opens(fake_system, fresh_x11, monkeypatch):
    monkeypatch.setattr(x11, "DISPLAY", ":0")
    assert x11.refresh_display(force=True) is False
    assert x11.DISPLAY == ":0"
    assert fake_system["probes"] == [(":0", None)]


def test_refresh_is_rate_limited(fake_system, fresh_x11, monkeypatch):
    monkeypatch.setattr(x11, "_DISPLAY_STATE", {"resolved": True, "last_try": 0.0, "logged": None})
    assert x11.refresh_display() is True       # :1 dead -> :0
    monkeypatch.setattr(x11, "DISPLAY", ":1")   # pretend it died again at once
    assert x11.refresh_display() is False      # inside RERESOLVE_MIN_S
    assert x11.DISPLAY == ":1"


def test_refresh_off_when_autodetect_disabled(fake_system, fresh_x11, monkeypatch):
    monkeypatch.setenv("WORKMAN_DISPLAY_AUTODETECT", "0")
    assert x11.refresh_display(force=True) is False
    assert fake_system["probes"] == []


def test_xt_re_resolves_when_channel_cannot_open(fake_system, fresh_x11, monkeypatch):
    monkeypatch.setattr(x11, "_DISPLAY_STATE", {"resolved": True, "last_try": 0.0, "logged": None})
    monkeypatch.setenv("WORKMAN_XTEST", "1")
    sentinel = object()
    asked = []

    def fake_channel(display=None):
        asked.append(display)
        return sentinel if display == ":0" else None

    monkeypatch.setattr(xtest, "channel", fake_channel)
    assert _REAL_XT() is sentinel
    assert asked == [":1", ":0"]


def test_xt_disabled_does_not_probe(fake_system, fresh_x11, monkeypatch):
    monkeypatch.setattr(x11, "_DISPLAY_STATE", {"resolved": True, "last_try": 0.0, "logged": None})
    monkeypatch.setenv("WORKMAN_XTEST", "0")
    assert _REAL_XT() is None
    assert fake_system["probes"] == []


def test_screenshot_ffmpeg_path_uses_resolved_display(fake_system, fresh_x11, monkeypatch):
    seen = []

    def fake_run(cmd, timeout=20, input_text=None):
        seen.append(list(cmd))
        if cmd[0] == "ffmpeg":
            open(cmd[-1], "wb").write(b"\x89PNG fake")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(x11, "_run", fake_run)
    monkeypatch.setattr(x11, "_require", lambda b: None)
    monkeypatch.setattr(x11, "screen_size", lambda: (1920, 1080))
    assert x11.screenshot().startswith(b"\x89PNG")
    ff = [c for c in seen if c[0] == "ffmpeg"][0]
    assert ff[ff.index("-i") + 1] == ":0"
    x11.screenshot(region=(10, 20, 30, 40))
    ff = [c for c in seen if c[0] == "ffmpeg"][-1]
    assert ff[ff.index("-i") + 1] == ":0+10,20"


def test_linux_backend_reports_resolved_display(fake_system, fresh_x11):
    from workman.platform import linux_x11
    assert linux_x11.display_name() == ":0"
