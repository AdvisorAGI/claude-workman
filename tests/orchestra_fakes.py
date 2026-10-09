"""Shared fakes for the orchestra tests: a fake HOME with config dirs,
transcripts, the live registry and the desktop store, a fake /proc, record
builders, and the front-end fakes (desk, Chrome driver, picker, AT-SPI).
Nothing here reaches a real display, Chrome, AT-SPI, transcript or session."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from workman import front_compact as fc
from workman import orchestra_tools as ot
from workman.orchestra import common as cm
from workman.orchestra import compact as cp
from workman.orchestra import model_effort as me
from workman.orchestra import registry as rg

NOW = 1_800_000_000.0


SECRET = "sk-ant-oat01-SECRETSECRETSECRETSECRET"


BRIDGE = "cse_01BRIDGEBRIDGEBRIDGEBRIDGE"


SID_A = "aaaaaaaa-1111-4111-8111-111111111111"


SID_B = "bbbbbbbb-2222-4222-8222-222222222222"


SID_C = "cccccccc-3333-4333-8333-333333333333"


SID_D = "dddddddd-4444-4444-8444-444444444444"


LOCAL = "local_eeeeeeee-5555-4555-8555-555555555555"


def ts(ago: float) -> str:
    return cm.iso(NOW - ago).replace("Z", ".000Z")


def user(text, ago=600, **kw):
    return {"type": "user", "timestamp": ts(ago), "cwd": "/home/x/proj",
            "message": {"role": "user", "content": text}, **kw}


def asst(text="done", stop="end_turn", ago=500, mid="m1", usage=(10, 1000, 200, 50),
         model="claude-opus-5-5", effort="high", tools=(), **kw):
    content = [{"type": "text", "text": text}] if text else []
    content += [{"type": "tool_use", "id": tid, "name": name, "input": inp}
                for tid, name, inp in tools]
    u = dict(zip(("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
                  "output_tokens"), usage))
    return {"type": "assistant", "timestamp": ts(ago), "effort": effort, "entrypoint": "sdk-cli",
            "cwd": "/home/x/proj", "uuid": f"u-{mid}-{ago}",
            "message": {"id": mid, "model": model, "stop_reason": stop, "content": content,
                        "usage": u}, **kw}


def result(tid, content="ok", is_error=False, ago=450, **kw):
    return {"type": "user", "timestamp": ts(ago),
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                                      "content": content,
                                                      "is_error": is_error}]}, **kw}


ASK = ("q1", "AskUserQuestion", {"questions": [{
    "question": "Merge PR #6 into master?", "header": "Merge",
    "options": [{"label": "Approve once"}, {"label": "Skip"}]}]})


def write(path: Path, records, age_s: float = 600, extra_lines=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in records] + list(extra_lines)
    path.write_text("\n".join(lines) + "\n")
    os.utime(path, (NOW - age_s, NOW - age_s))
    return path


class World:
    """A fake HOME with config dirs, a fake /proc, and builders."""

    def __init__(self, tmp: Path):
        self.home = tmp / "home"
        self.proc = tmp / "proc"
        self.home.mkdir()
        self.proc.mkdir()

    def cfg(self, name=".claude-a", email="a@example.com", uuid="acct-a"):
        d = self.home / name
        (d / "projects").mkdir(parents=True, exist_ok=True)
        if email:
            (d / ".claude.json").write_text(json.dumps({
                "oauthAccount": {"emailAddress": email, "accountUuid": uuid},
                "primaryApiKey": SECRET, "accessToken": SECRET}))
        return d

    def transcript(self, sid, records, cfg=".claude-a", age_s=600, extra_lines=()):
        return write(self.home / cfg / "projects" / "-home-x-proj" / f"{sid}.jsonl", records,
                     age_s, extra_lines)

    def process(self, pid, args, ppid=1, unit="app.slice"):
        d = self.proc / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in args) + b"\0")
        (d / "stat").write_text(f"{pid} (claude) S {ppid} 1 1 0")
        (d / "cgroup").write_text(f"0::/user.slice/user@1000.service/app.slice/{unit}\n")

    def registry(self, pid, sid, cfg=".claude-a", status="idle", waiting=None, bridge=True,
                 entrypoint="sdk-cli", host=""):
        d = self.home / cfg / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        data = {"pid": pid, "sessionId": sid, "cwd": "/home/x/proj", "kind": "interactive",
                "entrypoint": entrypoint, "name": "proj-ab", "status": status,
                "messagingSocketPath": "/run/user/1000/cc-socks/secret.sock",
                "updatedAt": 1, "startedAt": 1}
        if waiting:
            data["waitingFor"] = waiting
        if bridge:
            data["bridgeSessionId"] = BRIDGE
        if host:
            data["hostSessionId"] = host
        (d / f"{pid}.json").write_text(json.dumps(data))

    def desktop(self, sid, local=LOCAL, acct="acct-desk", model="claude-opus-5-5",
                effort="xhigh", title="Desk title"):
        d = self.home / ".config" / "Claude" / "claude-code-sessions" / acct / "org"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{local}.json").write_text(json.dumps({
            "sessionId": local, "cliSessionId": sid, "cwd": "/home/x/proj", "title": title,
            "model": model, "effort": effort, "isArchived": False, "lastActivityAt": 1,
            "permissionMode": "auto", "remoteMcpServersConfig": [{"token": SECRET}]}))


def read_world(tmp_path, monkeypatch) -> World:
    """A fake HOME with one config dir, no caller id, no tmux."""
    for k in cm.SELF_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("WORKMAN_ORCHESTRA_DIR", raising=False)
    monkeypatch.delenv("WORKMAN_FLEET_FEED", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(rg, "tmux_pane_pids", lambda run=None: set())
    w = World(tmp_path)
    w.cfg()
    return w


def lst(world, **kw):
    return ot.sessions_list(home=world.home, now=NOW, proc_root=world.proc, **kw)


FRAME = b"header strip pixels"


CALLER = "99999999-0000-4000-8000-000000000000"   # the calling session: never a target here


GOOD = fc.hash_frame(FRAME)


IN_BAND = (0, 350_000, 0, 10)


def _blocked(*a, **kw):
    raise AssertionError(f"test reached a real subprocess: {a!r}")


def control_world(tmp_path, monkeypatch) -> World:
    """read_world plus a known caller, private logs and lock, and every
    subprocess blocked."""
    w = read_world(tmp_path, monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CALLER)
    monkeypatch.setenv("WORKMAN_FRONT_COMPACT_LOG", str(tmp_path / "compact-log.jsonl"))
    monkeypatch.setenv("WORKMAN_MODEL_EFFORT_LOG", str(tmp_path / "model-effort-log.jsonl"))
    monkeypatch.setattr(fc, "FRONTEND_LOCK", str(tmp_path / "frontend.lock"))
    monkeypatch.setattr(fc, "_run", _blocked)
    monkeypatch.setattr(fc.subprocess, "Popen", _blocked)
    monkeypatch.setattr(fc, "process_names", lambda: [])
    w.tmp = tmp_path
    w.cfg(".claude-d", email="desk@example.com", uuid="acct-desk")
    return w


def real_age(path: Path, age_s: float) -> None:
    """Runners read the clock, so their transcripts age in real time."""
    t = time.time() - age_s
    os.utime(path, (t, t))


def desktop_target(world, records=None, age_s=120, learn=True):
    world.desktop(SID_A)
    p = world.transcript(SID_A, records or [user("go"), asst(usage=IN_BAND)], cfg=".claude-d")
    real_age(p, age_s)
    if learn:
        fc.save_header(LOCAL, GOOD)
    return p


def rc_target(world, records=None, age_s=120):
    p = world.transcript(SID_B, records or [user("go"), asst(usage=IN_BAND)])
    real_age(p, age_s)
    world.process(4000, ["/x/claude", "rc", "--name", "DGX A"])
    world.process(4242, ["/x/claude", "--print", "--sdk-url", "u", "--model", "claude-opus-5-5",
                         "--effort", "high"], ppid=4000, unit="claude-rc-a.service")
    world.registry(4242, SID_B)
    return p


def headless_target(world):
    p = world.transcript(SID_B, [user("go"), asst(usage=IN_BAND)])
    real_age(p, 120)
    world.process(5000, ["claude", "-p", "--model", "claude-sonnet-5-5"],
                  unit="run-claude-seat-x.service")
    world.registry(5000, SID_B, bridge=False)
    return p


def guard(out, name):
    return next(g for g in out["guards"] if g["name"] == name)


def log_lines(world, name="compact-log.jsonl"):
    p = world.tmp / name
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


class FakeDriver:
    def __init__(self, transcript, page_title="proj-ab - Claude Code", focus_role="entry",
                 compacts=True, after_compact=""):
        self.transcript, self.page_title = transcript, page_title
        self.focus_role, self.compacts = focus_role, compacts
        self.after_compact = after_compact   # extra lines written right after the boundary
        self.calls, self.typed, self.keys = [], [], []

    def open_url(self, url):
        self.calls.append(("open", url))
        return {"ok": True}

    def wait_load(self, timeout_s=20):
        return {"ok": True}

    def read_page(self):
        return {"ok": True, "title": self.page_title, "text": ""}

    def click_text(self, text):
        self.calls.append(("click", text))
        return {"ok": True}

    def focused(self):
        return {"role": self.focus_role}

    def type(self, text):
        self.typed.append(text)
        return {"ok": True}

    def key(self, key):
        assert key.lower() not in ("escape", "esc"), "Escape pressed"
        self.keys.append(key)
        if key == "Return" and self.typed and self.typed[-1] == "/compact" and self.compacts:
            with open(self.transcript, "a") as fh:
                fh.write('{"type":"system","subtype":"compact_boundary"}\n')
                fh.write(self.after_compact)
            real_age(Path(self.transcript), 120)   # the compacted session has gone quiet
        return {"ok": True}


class FakePicker:
    def __init__(self, ok=True, shows=True, on_pick=None):
        self.ok, self._shows, self.on_pick = ok, shows, on_pick
        self.picks = []

    def pick(self, current, target):
        self.picks.append((current[0], target[0]))
        if self.on_pick:
            self.on_pick(target)
        return {"ok": self.ok, "error": "boom"}

    def shows(self, labels):
        return self._shows


def fake_desk(argv, timeout):
    """ffmpeg grabs the learned header; xdg-open succeeds."""
    out = FRAME if argv[0] == "ffmpeg" else b""
    return subprocess.CompletedProcess(argv, 0, out, b"")


class FakeA11y:
    """A footer with two openers; clicking one opens its menu."""

    def __init__(self, items=("Opus 5.5", "Sonnet 5.5")):
        self.open, self.items, self.clicks = False, items, []

    def tree(self, app=None, actionable_only=True, limit=4000):
        nodes = [{"name": "Auto", "role": "push button"},
                 {"name": "Fable 5.1", "role": "push button"},
                 {"name": "High", "role": "push button"}]
        if self.open:
            nodes += [{"name": n, "role": "menu item"} for n in self.items]
        return nodes

    def click_element(self, name, role=None, app=None):
        self.clicks.append(name)
        self.open = name in ("Fable 5.1", "High") and not self.open
        return {"ok": True}


def compact(world, session=SID_A[:8], **kw):
    kw.setdefault("procs", lambda: [])
    return cp.session_compact(session, home=world.home, proc_root=world.proc, **kw)


def set_model(world, session=SID_A[:8], **kw):
    kw.setdefault("procs", lambda: [])
    kw.setdefault("sleep", lambda s: None)
    return me.session_set_model_effort(session, home=world.home, proc_root=world.proc, **kw)
