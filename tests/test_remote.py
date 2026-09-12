"""workman.remote: one persistent channel per host, JSON lines each way."""
from __future__ import annotations

import base64
import io
import json
import os
import shutil
import signal
import tempfile
import threading
import time
from pathlib import Path

import pytest

from workman import remote


class FakeProc:
    """Stands in for the ssh child: records what was written, answers from a script."""

    def __init__(self, replies: list[str], stderr: list[str] | None = None):
        self.written = io.StringIO()
        self.stdin = self.written
        self.stdout = io.StringIO("".join(r + "\n" for r in replies))
        self.stderr = io.StringIO("".join((s + "\n") for s in (stderr or [])))
        self.terminated = False

    def poll(self):
        return None if not self.terminated else 0

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


@pytest.fixture
def fake_popen(monkeypatch):
    holder = {}

    def popen(cmd, **kw):
        holder["cmd"] = cmd
        return holder["proc"]
    monkeypatch.setattr(remote.subprocess, "Popen", popen)
    return holder


class TestNames:
    def test_aliases_resolve(self):
        assert remote.resolve("mini") == "mac-mini"
        assert remote.resolve("DGX") == "dgx"
        assert remote.resolve("windows") == "wind1"

    def test_unknown_host_is_refused(self):
        with pytest.raises(ValueError):
            remote.resolve("toaster")

    def test_remote_pythons_prefer_the_daemon_venv(self):
        """Finding 5: do not reorder. Over ssh the workman venv has no Screen
        Recording, so capture goes through the daemon-backed computer-mcp venv."""
        assert remote.REMOTE_PYTHONS[0].endswith(
            "computer-mcp/.venv/bin/python")

    def test_is_local_by_hostname(self, monkeypatch):
        monkeypatch.setattr(remote.socket, "gethostname", lambda: "spark-0ed8.local")
        assert remote.is_local("dgx") is True
        assert remote.is_local("mac-mini") is False

    def test_launcher_runs_wm_serve_without_reexec(self):
        script = remote.launcher_script()
        assert "WM_NO_REEXEC=1" in script
        assert "serve" in script
        for py in remote.REMOTE_PYTHONS:
            assert py.replace(" ", "") in script
        assert remote.SENTINEL in script

    def test_relay_child_imports_the_package_under_a_bare_interpreter(self, tmp_path):
        """Regression (R1, 2026-09-12): wm.py run by /usr/bin/python3 spawned
        `python3 -m workman.remote`, which could not import the package."""
        import os
        import subprocess
        bare = "/usr/bin/python3"
        if not os.access(bare, os.X_OK):
            pytest.skip("no system python3")
        argv, env = remote.relay_command("mac-mini")
        assert argv[1:] == ["-m", "workman.remote", "--relay", "mac-mini"]
        assert os.access(argv[0], os.X_OK)
        # The env alone must be enough, even for an interpreter without the venv.
        r = subprocess.run([bare, "-c", "import workman.remote"], env=env, cwd=tmp_path,
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr

    def test_spawn_relay_passes_that_env_and_detaches(self, fake_popen, monkeypatch, tmp_path):
        monkeypatch.setenv("WORKMAN_REMOTE_DIR", str(tmp_path))
        seen = {}

        def popen(cmd, **kw):
            seen.update(kw, cmd=cmd)
        monkeypatch.setattr(remote.subprocess, "Popen", popen)
        remote._spawn_relay("mac-mini")
        root = str(remote.Path(remote.__file__).resolve().parents[1])
        assert seen["cmd"][-2:] == ["--relay", "mac-mini"]
        assert seen["env"]["PYTHONPATH"].split(":")[0] == root
        assert seen["start_new_session"] is True
        assert (tmp_path / "mac-mini.relay.log").exists()

    def test_ssh_options_keep_the_channel_alive(self):
        assert "-T" in remote.SSH
        assert "ServerAliveInterval=15" in " ".join(remote.SSH)
        assert "BatchMode=yes" in " ".join(remote.SSH)

    def test_relay_ssh_never_rides_a_shared_control_master(self):
        """Regression (2026-09-12): a reused mux master skips the fleet-connect
        ProxyCommand, so status() reported transport null."""
        joined = " ".join(remote.SSH)
        assert "ControlMaster=no" in joined and "ControlPath=none" in joined


class TestSession:
    def test_streams_source_then_reads_ready_and_answers_calls(self, fake_popen):
        ready = json.dumps({"ready": True, "backend": "workman", "host": "room1-tariqul"})
        reply = json.dumps({"result": {"ok": True, "pong": 1}})
        proc = FakeProc(["fleet chatter that is not json", ready, reply],
                        stderr=["fleet-connect[mac-mini]: connected via direct 169.254.81.1:22"])
        fake_popen["proc"] = proc
        s = remote.Session("mac-mini", source="print('hello')\n")
        assert s.ready["backend"] == "workman"
        sent = proc.written.getvalue()
        assert sent.startswith("print('hello')\n")
        assert remote.SENTINEL + "\n" in sent
        assert fake_popen["cmd"][-2] == "mac-mini"
        out = s.call("ping", [])
        assert out == {"ok": True, "pong": 1}
        sent = proc.written.getvalue()
        assert json.loads(sent.splitlines()[-1]) == {"verb": "ping", "args": []}
        for _ in range(50):
            if s.transport:
                break
            time.sleep(0.01)
        assert s.transport == "direct 169.254.81.1:22"

    def test_remote_that_never_readies_is_an_error(self, fake_popen):
        fake_popen["proc"] = FakeProc([json.dumps({"ready": False, "error": "no python found on remote"})])
        with pytest.raises(remote.RemoteError) as exc:
            remote.Session("md", source="x\n")
        assert "no python" in str(exc.value)
        assert fake_popen["proc"].terminated is True

    def test_closed_channel_is_reported(self, fake_popen):
        fake_popen["proc"] = FakeProc([json.dumps({"ready": True})])
        s = remote.Session("air", source="x\n")
        with pytest.raises(remote.RemoteError):
            s.call("ping")


class TestCallAndImage:
    def test_local_host_skips_ssh(self, monkeypatch):
        monkeypatch.setattr(remote, "is_local", lambda host: True)
        monkeypatch.setattr(remote, "_local", lambda verb, args: {"ok": True, "verb": verb, "args": args})
        monkeypatch.setattr(remote, "_connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ssh used")))
        assert remote.call("dgx", "ping", ["x"]) == {"ok": True, "verb": "ping", "args": ["x"]}

    def test_image_decodes_base64(self, monkeypatch):
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 10
        monkeypatch.setattr(remote, "call", lambda host, verb, args=None, timeout=60:
                            {"ok": True, "image_b64": base64.b64encode(png).decode(), "format": "png"})
        meta, data = remote.image("mac-mini", "shot", ["-"])
        assert data == png
        assert "image_b64" not in meta and meta["format"] == "png"

    def test_image_without_bytes_returns_meta_only(self, monkeypatch):
        monkeypatch.setattr(remote, "call", lambda host, verb, args=None, timeout=60:
                            {"ok": False, "error": "blank"})
        meta, data = remote.image("mac-mini", "shot", ["-"])
        assert data is None and meta["error"] == "blank"

    def test_status_measures_a_round_trip(self, monkeypatch):
        monkeypatch.setattr(remote, "is_local", lambda host: False)
        calls = []

        def call(host, verb, args=None, timeout=60):
            calls.append(verb)
            if verb == "_transport":
                return {"ok": True, "transport": "direct 169.254.81.1:22", "relay_pid": 1}
            return {"ok": True, "host": "room1-tariqul"}
        monkeypatch.setattr(remote, "call", call)
        st = remote.status("mac-mini")
        assert st["ok"] is True and st["transport"].startswith("direct")
        assert st["rtt_ms"] >= 0 and calls == ["ping", "_transport"]

    def test_status_reports_a_dead_relay(self, monkeypatch):
        monkeypatch.setattr(remote, "is_local", lambda host: False)

        def call(host, verb, args=None, timeout=60):
            raise remote.RemoteError("mac-mini: relay for mac-mini did not come up: timeout")
        monkeypatch.setattr(remote, "call", call)
        st = remote.status("mac-mini")
        assert st["ok"] is False and "did not come up" in st["error"]


class FakeRelaySession:
    instances: list["FakeRelaySession"] = []

    def __init__(self, alias, source=None):
        self.alias, self.transport, self.ready, self.closed = alias, "test", {"ready": True}, False
        FakeRelaySession.instances.append(self)

    def alive(self):
        return True

    def call(self, verb, args=None, timeout=60.0):
        return {"ok": True, "verb": verb}

    def close(self):
        self.closed = True


@pytest.fixture
def relay_dir(monkeypatch):
    d = tempfile.mkdtemp(prefix="wmr", dir="/tmp")  # AF_UNIX paths cap near 108 bytes
    monkeypatch.setenv("WORKMAN_REMOTE_DIR", d)
    monkeypatch.setattr(remote, "Session", FakeRelaySession)
    monkeypatch.setattr(remote, "IDLE_EXIT_S", 5.0)
    FakeRelaySession.instances.clear()
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


def _appears(path: Path, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    return path.exists()


class TestStop:
    """Item 6 (2026-09-12): stop never signals a stale pid, and every relay
    exit, SIGTERM included, removes the socket and the info file."""

    def test_stop_asks_the_relay_over_its_socket_and_it_cleans_up(self, relay_dir):
        result: list[int] = []
        t = threading.Thread(target=lambda: result.append(remote.relay_main("mac-mini")))
        t.start()
        assert _appears(relay_dir / "mac-mini.json")
        out = remote.stop("mac-mini")
        t.join(5)
        assert out == {"ok": True, "stopped_pid": os.getpid(), "via": "socket"}
        assert result == [0] and FakeRelaySession.instances[0].closed is True
        assert not (relay_dir / "mac-mini.sock").exists()
        assert not (relay_dir / "mac-mini.json").exists()

    def test_sigterm_runs_the_relay_cleanup(self, relay_dir):
        before = signal.getsignal(signal.SIGTERM)
        sent: list[bool] = []

        def terminate():
            # Only once the relay's own handler is in place: never kill the test run.
            if _appears(relay_dir / "mac-mini.json") and signal.getsignal(signal.SIGTERM) is not before:
                sent.append(True)
                signal.pthread_kill(threading.main_thread().ident, signal.SIGTERM)
        threading.Thread(target=terminate, daemon=True).start()
        with pytest.raises(SystemExit):
            remote.relay_main("mac-mini")
        assert sent == [True]
        assert not (relay_dir / "mac-mini.sock").exists()
        assert not (relay_dir / "mac-mini.json").exists()
        assert signal.getsignal(signal.SIGTERM) is before

    def test_a_stale_pid_that_is_not_the_relay_is_never_signalled(self, relay_dir, monkeypatch):
        (relay_dir / "mac-mini.json").write_text(json.dumps({"alias": "mac-mini", "pid": 424242}))
        monkeypatch.setattr(remote, "_cmdline", lambda pid: ["/usr/bin/gedit", "notes.txt"])
        monkeypatch.setattr(remote.os, "kill", lambda pid, sig: pytest.fail(f"signalled {pid}"))
        out = remote.stop("mac-mini")
        assert out["ok"] is False and "stale" in out["error"]
        assert not (relay_dir / "mac-mini.json").exists()

    def test_a_confirmed_relay_without_a_socket_gets_sigterm(self, relay_dir, monkeypatch):
        (relay_dir / "mac-mini.json").write_text(json.dumps({"alias": "mac-mini", "pid": 424242}))
        monkeypatch.setattr(remote, "_cmdline",
                            lambda pid: ["/x/python", "-m", "workman.remote", "--relay", "mac-mini"])
        sent = []
        monkeypatch.setattr(remote.os, "kill", lambda pid, sig: sent.append((pid, sig)))
        assert remote.stop("mac-mini") == {"ok": True, "stopped_pid": 424242, "via": "sigterm"}
        assert sent == [(424242, signal.SIGTERM)]

    def test_nothing_running_is_reported(self, relay_dir):
        out = remote.stop("mac-mini")
        assert out["ok"] is False and "no relay" in out["error"]

    def test_relay_cmdline_match(self):
        assert remote.is_relay_cmdline(["py", "-m", "workman.remote", "--relay", "mini"], "mac-mini")
        assert not remote.is_relay_cmdline(["py", "-m", "workman.remote", "--relay", "md"], "mac-mini")
        assert not remote.is_relay_cmdline(["python3", "other.py", "--relay", "mac-mini"], "mac-mini")
        assert not remote.is_relay_cmdline(["py", "-m", "workman.remote", "--relay"], "mac-mini")
        assert not remote.is_relay_cmdline([], "mac-mini")

    def test_cmdline_reads_a_live_process(self):
        argv = remote._cmdline(os.getpid())
        assert argv and "python" in argv[0]
