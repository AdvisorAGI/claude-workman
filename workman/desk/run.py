"""Run one CLI seat as a subprocess and stream stdout/stderr."""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable

from .vendors import build_argv, launch_env

OnChunk = Callable[[str], None]


class Job:
    def __init__(self) -> None:
        self.proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    def busy(self) -> bool:
        with self._lock:
            proc = self.proc
        return proc is not None and proc.poll() is None

    def stop(self) -> None:
        with self._lock:
            proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                proc.terminate()
            else:
                os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            try:
                proc.terminate()
            except OSError:
                pass

    def run(
        self,
        vendor_id: str,
        binary: str,
        model: str,
        prompt: str,
        cwd: str | None,
        auto_approve: bool,
        on_chunk: OnChunk,
        *,
        session_id: str | None = None,
        resume: bool = False,
        sandbox: str | None = None,
    ) -> int:
        prompt = (prompt or "").strip()
        if not prompt:
            on_chunk("empty prompt\n")
            return 2
        work = Path(cwd).expanduser() if cwd else Path.cwd()
        if not work.is_dir():
            on_chunk(f"cwd does not exist: {work}\n")
            return 2
        tmp: str | None = None
        if vendor_id in ("grok", "workman") and len(prompt) > 4000:
            handle = tempfile.NamedTemporaryFile(
                "w", suffix=".txt", delete=False, encoding="utf-8")
            handle.write(prompt)
            handle.close()
            tmp = handle.name
        argv = build_argv(
            vendor_id, binary, model, prompt,
            prompt_file=tmp, auto_approve=auto_approve,
            session_id=session_id, resume=resume, sandbox=sandbox,
        )
        env = launch_env()
        env["PYTHONUNBUFFERED"] = "1"
        kwargs: dict = dict(
            args=argv, cwd=str(work), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=0,
        )
        if os.name != "nt":
            kwargs["start_new_session"] = True
        else:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(**kwargs)
        except OSError as exc:
            on_chunk(f"could not start {binary}: {exc}\n")
            return 127
        with self._lock:
            self.proc = proc
        assert proc.stdout is not None
        try:
            while True:
                buf = proc.stdout.read(256)
                if not buf:
                    break
                on_chunk(buf.decode("utf-8", "replace"))
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass
            code = proc.wait()
            with self._lock:
                if self.proc is proc:
                    self.proc = None
            if tmp:
                try:
                    Path(tmp).unlink()
                except OSError:
                    pass
        return code


class Jobs:
    """One subprocess per key so a board run and a chat can overlap."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}

    def get(self, key: str) -> Job:
        with self._lock:
            job = self._jobs.get(key)
            if job is None:
                job = Job()
                self._jobs[key] = job
            return job

    def stop(self, key: str | None = None) -> None:
        with self._lock:
            if key:
                jobs = [self._jobs[key]] if key in self._jobs else []
            else:
                jobs = list(self._jobs.values())
        for job in jobs:
            job.stop()
