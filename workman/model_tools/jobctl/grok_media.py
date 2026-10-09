"""grok Imagine through ~/grok-web-mcp: its functions are imported and called by
the grok interpreter (/usr/bin/python3 -E, which has websocket-client); no
browser automation is reimplemented here. Quota is read before any generation.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import time
from .common import _err, _json, _read, _write, clean_env, expand, grok_dir, grok_python  # jobctl:local


GROK_DRIVER = r'''
import json, os, sys
gd = os.environ["WORKMAN_GROK_WEB_DIR"]
sys.path.insert(0, gd)
os.chdir(gd)
import server
calls = json.loads(sys.argv[1])
out = []
for name, kwargs in calls:
    fn = getattr(server, name, None)
    if fn is None:
        fn = server.mcp._tool_manager.get_tool(name).fn
    out.append(fn(**kwargs))
sys.stdout.write("\n@@GROK@@" + json.dumps(out))
'''


QUOTA_RAW = r'''
import json, os, sys
gd = os.environ["WORKMAN_GROK_WEB_DIR"]
sys.path.insert(0, gd)
os.chdir(gd)
import server
txt = server.imagine_quota()
try:
    json.loads(txt)
except Exception:
    tb = server.tab()
    try:
        txt = json.dumps(server._fetch_json(tb, "/rest/media/imagine/quota_info", body={}))
    finally:
        tb.close()
sys.stdout.write("\n@@GROK@@" + json.dumps([txt]))
'''


def _grok(code: str, payload, timeout: int) -> list:
    env = clean_env()
    env["WORKMAN_GROK_WEB_DIR"] = grok_dir()
    argv = [grok_python(), "-E", "-c", code]
    if payload is not None:
        argv.append(json.dumps(payload))
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=grok_dir(),
                       env=env, stdin=subprocess.DEVNULL)
    tail = (r.stdout or "").rsplit("@@GROK@@", 1)
    if len(tail) != 2:
        raise RuntimeError("grok-web call failed rc=%s: %s" % (r.returncode, (r.stderr or r.stdout or "")[-400:]))
    return json.loads(tail[1])


def quota_verdict(raw, bucket: str) -> dict:
    """grok quota_info: {"status":200,"json":{"image":{"available":N,...},"video":{...}}}.
    ok=True only when the bucket shows capacity; ok=None when it cannot be read."""
    try:
        d = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {"ok": None, "why": "quota reply is not JSON", "raw": str(raw)[:400]}
    if isinstance(d, str) and d.startswith("ERROR"):
        return {"ok": None, "why": d[:300]}
    body = d.get("json") if isinstance(d, dict) and "json" in d else d
    status = d.get("status") if isinstance(d, dict) else None
    if status not in (None, 200) or not isinstance(body, dict):
        return {"ok": None, "why": "quota http status %s" % status, "raw": json.dumps(d)[:400]}

    def find(node, depth=0):
        if not isinstance(node, dict) or depth > 4:
            return None
        if bucket in node and isinstance(node[bucket], dict):
            return node[bucket]
        for v in node.values():
            hit = find(v, depth + 1)
            if hit is not None:
                return hit
        return None

    b = find(body)
    buckets = sorted(k for k, v in body.items() if isinstance(v, dict))
    if b is None:
        return {"ok": None, "why": "no %r bucket in quota reply" % bucket, "buckets": buckets}
    for key in ("available", "remaining", "remainingQueries", "left"):
        if key in b:
            v = b[key]
            if isinstance(v, bool):
                return {"ok": v, "bucket": bucket, key: v, "detail": b}
            if isinstance(v, (int, float)):
                return {"ok": v > 0, "bucket": bucket, key: v, "detail": b}
    return {"ok": None, "why": "bucket %r has no available/remaining field" % bucket, "detail": b}


def cmd_grok_quota() -> dict:
    if not os.path.isfile(os.path.join(grok_dir(), "server.py")):
        return _err("grok_web_missing", "%s/server.py not found" % grok_dir())
    try:
        raw = _grok(QUOTA_RAW, None, 120)[0]
    except Exception as exc:  # noqa: BLE001 - reported to the caller
        return _err("grok_quota_failed", "%s: %s" % (type(exc).__name__, exc))
    return {"ok": True, "raw": raw[:3000] if isinstance(raw, str) else raw,
            "image": quota_verdict(raw, "image"), "video": quota_verdict(raw, "video")}


def _lock_grok(timeout: float):
    path = os.path.join(grok_dir(), ".workman-grok.lock")
    fh = open(path, "w")
    end = time.time() + timeout
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if time.time() > end:
                fh.close()
                raise TimeoutError("another grok Imagine job holds %s" % path)
            time.sleep(2)


def grok_calls(spec: dict, prompt: str) -> list:
    """The grok-web functions to call, in order, for an image or video job."""
    kind = spec.get("kind")
    wait = int(spec.get("wait_seconds") or (180 if kind == "image" else 600))
    if kind == "image":
        ctl = {k: spec[k] for k in ("count", "aspect_ratio") if spec.get(k)}
        calls = [["imagine_set_image_controls", ctl]] if ctl else []
        return calls + [["imagine_image", {"prompt": prompt, "wait_seconds": wait}]]
    kw = {"prompt": prompt, "duration": spec.get("duration") or "15s", "wait_seconds": wait}
    if spec.get("resolution"):
        kw["resolution"] = spec["resolution"]
    if spec.get("refs"):
        kw["image_paths"] = [expand(r) for r in spec["refs"]]
        return [["imagine_video_with_refs", kw]]
    kw["aspect_ratio"] = spec.get("aspect_ratio") or "9:16"
    if spec.get("audio"):
        kw["audio"] = spec["audio"]
    return [["imagine_video", kw]]


def collect_grok(reply: str, kind: str, out_dir: str, result: dict) -> None:
    """Copy the files grok-web downloaded into out_dir and note the post ids."""
    result["reply"] = reply[:600]
    result["post_ids"] = re.findall(r"post_id=([0-9a-f-]{36})", reply)
    if kind == "image":
        result["post_id_prefixes"] = re.findall(r"_([0-9a-f]{8})\.jpg", reply)
    os.makedirs(out_dir, exist_ok=True)
    for src in re.findall(r"(/\S+\.(?:jpg|jpeg|png|webp|mp4))", reply):
        if os.path.isfile(src):
            dst = os.path.join(out_dir, os.path.basename(src))
            if os.path.abspath(dst) != os.path.abspath(src):
                shutil.copy2(src, dst)
            result["files"].append(dst)
    result["ok"] = bool(result["files"]) and reply.startswith("OK")
    if not result["ok"]:
        result["error"] = reply[:300] or "no files"


def _grok_job_locked(spec: dict, d: str, result: dict) -> int:
    kind = spec.get("kind")
    v = quota_verdict(_grok(QUOTA_RAW, None, 120)[0],
                      spec.get("bucket") or ("image" if kind == "image" else "video"))
    result["quota"] = v
    if v.get("ok") is not True:
        result["error"] = "quota_exhausted" if v.get("ok") is False else "quota_unknown"
        return 4
    calls = grok_calls(spec, _read(os.path.join(d, "prompt.txt")))
    outs = _grok(GROK_DRIVER, calls, int(calls[-1][1]["wait_seconds"]) + 300)
    if len(outs) > 1:
        result["controls"] = str(outs[0])[:300]
    collect_grok(str(outs[-1]) if outs else "", kind, spec.get("out_dir") or os.path.join(d, "media"),
                 result)
    return 0 if result["ok"] else 1


def cmd_grok_job(d: str) -> int:
    """One grok Imagine job under the lock: quota again right before spending, then
    the calls, then the files copied out. The outcome goes to result.json."""
    spec = _json(os.path.join(d, "job.json")) or {}
    result = {"ok": False, "kind": spec.get("kind"), "files": []}
    try:
        lock = _lock_grok(600)
    except TimeoutError as exc:
        result["error"] = str(exc)
        _write(os.path.join(d, "result.json"), json.dumps(result))
        return 5
    try:
        rc = _grok_job_locked(spec, d, result)
    except Exception as exc:  # noqa: BLE001 - written to result.json
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
        rc = 1
    finally:
        lock.close()
    _write(os.path.join(d, "result.json"), json.dumps(result))
    return rc
