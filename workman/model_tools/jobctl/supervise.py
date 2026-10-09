"""The supervisor: one process per job that runs run-model (direct or role mode),
the read-only codex image path or a grok Imagine child, enforces the timeout
backstop, and leaves pid, child_pid, started, ended, rc and marker files behind.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from .common import _json, _new_images, _read, _tail, _write, clean_env, grace_s  # jobctl:local
from .common import grok_dir, grok_python, home, newest_model, now_iso, park_comeback  # jobctl:local
from .common import image_env, poll_s, run_model_path, runs_dir  # jobctl:local
from .procs import _pid_alive, _signal_tree, adopt, kill_tree, owned, proc_start  # jobctl:local
from .sandbox import BOX_CODEX_HOME, SandboxRefused, image_sandbox  # jobctl:local


class _Sup:
    def __init__(self, d: str):
        self.d = d
        self.spec = _json(os.path.join(d, "job.json")) or {}
        self.job = self.spec.get("job", "")
        self.child = None
        self.extra_pids = []
        self.cancelled = False

    def p(self, name: str) -> str:
        return os.path.join(self.d, name)

    def mark(self, name: str, text: str = "") -> None:
        _write(self.p(name), text)

    # The child is never reaped before we are done signalling it, so its pid (and
    # its process group, which it leads) cannot belong to anyone else meanwhile.
    def child_exited(self) -> bool:
        if self.child is None or self.child.returncode is not None:
            return True
        try:
            r = os.waitid(os.P_PID, self.child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return True
        except (AttributeError, OSError):
            return self.child.poll() is not None
        return r is not None

    def kill_child(self, sig) -> None:
        if self.child is not None and self.child.returncode is None:
            _signal_tree(self.child.pid, sig)

    def kill_extra(self, sig) -> None:
        for pid, start in self.extra_pids:
            kill_tree(pid, sig, self.job, start)

    def on_signal(self, signum, _frame) -> None:
        self.cancelled = True
        self.mark("cancelled", now_iso() + " signal %d\n" % signum)
        self.kill_child(signal.SIGTERM)
        self.kill_extra(signal.SIGTERM)

    def wait_child(self, deadline: float) -> bool:
        """Wait for our child without reaping it. False when the deadline hit."""
        while not self.child_exited():
            if time.time() >= deadline:
                return False
            time.sleep(poll_s())
        return True

    def wait_foreign(self, pid: int, start: str, deadline: float) -> bool:
        while owned(pid, self.job, start) if start else _pid_alive(pid):
            if time.time() >= deadline:
                return False
            time.sleep(poll_s())
        return True

    def stop_child(self) -> None:
        self.kill_child(signal.SIGTERM)
        end = time.time() + 20
        while time.time() < end and not self.child_exited():
            time.sleep(0.2)
        self.kill_child(signal.SIGKILL)

    def stop_foreign(self, pid: int, start: str) -> None:
        kill_tree(pid, signal.SIGTERM, self.job, start)
        end = time.time() + 20
        while time.time() < end and owned(pid, self.job, start):
            time.sleep(0.2)
        kill_tree(pid, signal.SIGKILL, self.job, start)

    def stop_comeback(self, rm_pid) -> None:
        """run-model leaves ~/orchestra/runs/active/<pid>.job for run-comeback
        when a run is killed; a cancel or a caller timeout must not come back."""
        park_comeback(rm_pid)

    def start(self, argv, env, cwd, log: str = "", stdin_path: str = "") -> None:
        stdin = open(stdin_path, "rb") if stdin_path else subprocess.DEVNULL
        try:
            with open(log or self.p("launch.log"), "ab") as lf:
                self.child = subprocess.Popen(argv, stdout=lf, stderr=subprocess.STDOUT,
                                              stdin=stdin, cwd=cwd, env=env,
                                              start_new_session=True, close_fds=True)
        finally:
            if stdin_path:
                stdin.close()
        self.mark("child_pid", str(self.child.pid))
        self.mark("child_start", proc_start(self.child.pid))

    def reap(self) -> int:
        return self.child.wait()

    def run(self) -> int:
        self.mark("pid", str(os.getpid()))
        self.mark("pid_start", proc_start(os.getpid()))
        t0 = time.time()
        self.mark("started", "%s %d\n" % (now_iso(), int(t0)))
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, self.on_signal)
        if self.cancelled:
            return self.finish(143, False)
        deadline = t0 + int(self.spec.get("timeout_s") or 2700) + grace_s()
        env = clean_env()
        env["WORK_TASK_ID"] = self.job
        mode = self.spec.get("mode")
        unit = os.environ.get("WM_JOB_UNIT", "")
        if unit and mode in ("direct", "role"):
            # Already outside any Claude cgroup: run-model runs in place and
            # records this unit in RUNS.jsonl.
            env["SESSION_ESCAPE_DONE"] = "1"
            env["SESSION_ESCAPE_UNIT"] = unit
        runner = {"direct": self.run_direct, "role": self.run_role, "grok": self.run_grok}.get(mode)
        if mode == "codex-image":
            rc, timed_out = self.run_codex_image(env, deadline, t0)
        elif runner:
            rc, timed_out = runner(env, deadline)
        else:
            rc, timed_out = 2, False
        if self.cancelled:
            rc = 143 if rc in (0, None) else rc
        return self.finish(rc, timed_out)

    def run_child(self, argv, env, cwd, deadline: float, **kw):
        """Start the child, wait until it ends or the deadline hits, then reap it."""
        self.start(argv, env, cwd, **kw)
        timed_out = not self.wait_child(deadline)
        if timed_out:
            self.stop_child()
        return self.reap(), timed_out

    def run_direct(self, env, deadline: float):
        s = self.spec
        env["RUN_MODEL_TIMEOUT"] = str(int(s.get("timeout_s") or 2700))
        if s.get("kind") != "agent":
            env["RUN_MODEL_NO_COMMIT"] = "1"
        argv = [run_model_path(), s["cli"], "--effort", s["effort"], "--cwd", s["cwd"], "--out", s["out"],
                "--", _read(self.p("prompt.txt"))]
        rc, timed_out = self.run_child(argv, env, s["cwd"], deadline)
        if "note: timed out after" in _tail(self.p("launch.log"), 8192):
            timed_out = True
        if self.cancelled or timed_out:
            self.stop_comeback(self.child.pid)
        return rc, timed_out

    def run_role(self, env, deadline: float):
        s = self.spec
        argv = [run_model_path(), s["role"], s["cwd"], self.p("prompt.txt"), self.job]
        rc, timed_out = self.run_child(argv, env, s["cwd"], deadline)
        if rc != 0 or self.cancelled:
            return rc, timed_out
        # run-model returned after starting its router in the background: wait for it.
        info = find_role_files(self.job)
        rpid = info.get("router_pid")
        info["router_start"] = adopt(rpid, self.job) if rpid else ""
        _write(self.p("role.json"), json.dumps(info))
        if rpid and info["router_start"]:
            self.extra_pids.append((rpid, info["router_start"]))
        if rpid and not self.wait_foreign(rpid, info["router_start"], deadline):
            timed_out = True
            if info["router_start"]:
                self.stop_foreign(rpid, info["router_start"])
            # an unadopted pid is waited for, never signalled
        return role_exit(info), timed_out

    def run_grok(self, env, deadline: float):
        env["WORKMAN_GROK_WEB_DIR"] = grok_dir()
        argv = [grok_python(), "-E", self.p("jobctl.py"), "grok-job", self.d]
        return self.run_child(argv, env, grok_dir(), deadline)

    def run_codex_image(self, env, deadline: float, t0: float):
        """Image-only path, outside run-model on purpose: run-model cannot pass a
        sandbox flag and the codex config is danger-full-access, so this runs
        `codex exec -s read-only` in the empty work dir, inside the bwrap filesystem
        sandbox (sandbox.py): ~ and every secret under it are unreachable. image_gen
        writes into <job>/generated (bound over generated_images); copied out after."""
        s = self.spec
        codex = shutil.which("codex", path=env["PATH"]) or ""
        node = shutil.which("node", path=env["PATH"]) or ""
        model = newest_model("NEWEST_SOL", "gpt-6.1-sol")
        effort = s.get("effort") or "low"
        try:
            box, codex_in = image_sandbox(self.d, s["cwd"], codex, node)
        except SandboxRefused as exc:
            with open(s["out"], "a", encoding="utf-8") as fh:
                fh.write("workman: image sandbox refused, nothing was started: %s\n" % exc)
            return 6, False
        argv = box + [codex_in, "exec", "--skip-git-repo-check", "-s", "read-only", "-m", model,
                      "-c", 'model_reasoning_effort="%s"' % effort, "-C", s["cwd"],
                      "-o", self.p("final.txt"), "-"]
        rc, timed_out = self.run_child(argv, image_env(env, s["cwd"], BOX_CODEX_HOME), s["cwd"],
                                       deadline, log=s["out"], stdin_path=self.p("prompt.txt"))
        files = [] if self.cancelled else self.collect_images()
        if rc == 0 and not files and not self.cancelled:
            rc = 3   # codex ended but no image came back
        self.ledger(model, effort, rc, t0)
        return rc, timed_out

    def ledger(self, model: str, effort: str, rc: int, t0: float) -> None:
        """One run-model-shaped line in ~/orchestra/runs/RUNS.jsonl so fleet usage
        views see image runs too (run-model writes these lines for every other mode)."""
        import uuid
        line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)), "run_id": str(uuid.uuid4()),
                "task": self.job, "model": "codex", "model_id": model, "effort": effort,
                "cwd": self.spec.get("cwd"), "pid": self.child.pid if self.child else None,
                "exit": rc, "seconds": int(time.time() - t0), "log": self.spec.get("out"),
                "out": self.spec.get("out"), "unit": os.environ.get("WM_JOB_UNIT", ""),
                "via": "workman-image"}
        try:
            os.makedirs(runs_dir(), exist_ok=True)
            with open(os.path.join(runs_dir(), "RUNS.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, separators=(",", ":")) + "\n")
        except OSError:
            pass

    def finish(self, rc, timed_out: bool) -> int:
        rc = 1 if rc is None else int(rc)
        if rc < 0:
            rc = 128 - rc
        if timed_out:
            self.mark("timed_out", now_iso() + "\n")
        self.mark("ended", "%s %d\n" % (now_iso(), int(time.time())))
        self.mark("rc", "%d\n" % rc)
        return rc

    def collect_images(self) -> list:
        """Copy what the built-in tool left in <job>/generated/<session id>/ (bound over
        generated_images inside the sandbox) into out_dir under the given names."""
        out_dir = self.spec.get("out_dir") or self.p("media")
        names = list(self.spec.get("names") or [])
        files, source = [], "codex generated_images"
        m = re.search(r"session id:\s*([0-9a-f-]{36})", _tail(self.spec.get("out", ""), 1 << 20))
        if m:
            gen = os.path.join(self.d, "generated", m.group(1))
            for i, src in enumerate(_new_images(gen, 0), 1):
                ext = os.path.splitext(src)[1]
                stem = os.path.splitext(names[i - 1])[0] if i <= len(names) else "%s-%d" % (self.job[-6:], i)
                dst = os.path.join(out_dir, stem + ext)
                v = 2
                while os.path.exists(dst):
                    dst = os.path.join(out_dir, "%s-v%d%s" % (stem, v, ext))
                    v += 1
                shutil.copy2(src, dst)
                files.append(dst)
        else:
            source = "no codex session id in the log"
        _write(self.p("files.json"), json.dumps({"files": files, "source": source}))
        return files


def find_role_files(job: str) -> dict:
    """run-model role mode writes ~/handoffs/model-runs/<local date>/<name>.{meta,out,err}."""
    base = os.path.join(home(), "handoffs", "model-runs")
    meta = ""
    try:
        days = sorted(os.listdir(base), reverse=True)[:3]
    except OSError:
        days = []
    for day in days:
        cand = os.path.join(base, day, job + ".meta")
        if os.path.isfile(cand):
            meta = cand
            break
    if not meta:
        return {"meta": "", "out": "", "err": "", "router_pid": None}
    stem = meta[:-len(".meta")]
    m = re.search(r"\bpid=(\d+)", _read(meta))
    return {"meta": meta, "out": stem + ".out", "err": stem + ".err",
            "router_pid": int(m.group(1)) if m else None}


def role_exit(info: dict) -> int:
    """The router is not our child, so its exit code is read back from what it
    leaves: the DGX router writes <out>.lane only on success; the Mac router
    logs `model-router: done seat=.. exit=N` on its stderr file."""
    out, err = info.get("out") or "", info.get("err") or ""
    if not out:
        return 9
    errt = _tail(err, 65536)
    m = None
    for m in re.finditer(r"model-router: done seat=\S+ .*?exit=(\d+)", errt):
        pass
    if m:
        return int(m.group(1))
    if _read(out + ".lane").strip():
        return 0
    if os.path.exists(out + ".route") or "every lane failed" in errt or "every seat failed" in errt:
        return 9
    return 0 if _read(out).strip() else 9
