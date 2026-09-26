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
        self.proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()

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
        if vendor_id == "grok" and len(prompt) > 4000:
            handle = tempfile.NamedTemporaryFile(
                "w", suffix=".txt", delete=False, encoding="utf-8")
            handle.write(prompt)
            handle.close()
            tmp = handle.name
        argv = build_argv(
            vendor_id, binary, model, prompt,
            prompt_file=tmp, auto_approve=auto_approve,
        )
        kwargs: dict = dict(
            args=argv, cwd=str(work), env=launch_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
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
            for line in proc.stdout:
                on_chunk(line)
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
