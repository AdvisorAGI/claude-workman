"""models_available: which CLIs, models and run-model roles each machine has now,
cached for 10 minutes in memory and in ~/.cache/workman/model-tools/.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from .transport import MACHINES, _call, _now_iso, local_machine, resolve_machine
from .jobctl.common import _err


MODELS_TTL = 600


_MODELS_CACHE: dict = {}


_LOCK = threading.Lock()


def _models_cache_file(machine: str) -> Path:
    return Path(os.path.expanduser("~/.cache/workman/model-tools")) / f"models-{machine}.json"


def _models_one(machine: str, refresh: bool) -> dict:
    now = time.time()
    with _LOCK:
        hit = _MODELS_CACHE.get(machine)
    if not refresh and hit and now - hit[0] < MODELS_TTL:
        return dict(hit[1], cached=True, age_s=int(now - hit[0]))
    f = _models_cache_file(machine)
    if not refresh and f.is_file():
        try:
            disk = json.loads(f.read_text(encoding="utf-8"))
            if now - float(disk.get("_ts", 0)) < MODELS_TTL:
                data = {k: v for k, v in disk.items() if k != "_ts"}
                with _LOCK:
                    _MODELS_CACHE[machine] = (float(disk["_ts"]), data)
                return dict(data, cached=True, age_s=int(now - float(disk["_ts"])))
        except (OSError, ValueError, KeyError):
            pass
    data = _call(machine, ["models"], timeout=150)
    data["checked"] = _now_iso()
    if data.get("ok"):
        with _LOCK:
            _MODELS_CACHE[machine] = (now, data)
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(dict(data, _ts=now)), encoding="utf-8")
        except OSError:
            pass
    return dict(data, cached=False)


def models_available(machine: str = "all", refresh: bool = False) -> dict:
    """Which CLIs and models each machine has now: codex version and login, grok
    models, cursor-agent models, run-model direct CLIs and role table. Cached 10 min."""
    if (machine or "all").strip().lower() == "all":
        names = list(MACHINES)
        if local_machine() == "local":
            names.append("local")
    else:
        try:
            names = [resolve_machine(machine)]
        except ValueError as exc:
            return _err("bad_machine", str(exc))
    out: dict[str, dict] = {}

    def fetch(m: str) -> None:
        out[m] = _models_one(m, refresh)

    threads = [threading.Thread(target=fetch, args=(m,)) for m in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return {"ok": any(v.get("ok") for v in out.values()), "ttl_s": MODELS_TTL,
            "machines": {m: out[m] for m in names}}
