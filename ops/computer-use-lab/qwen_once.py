#!/usr/bin/env python3
"""One local Qwen completion. enable_thinking=false is mandatory."""
from __future__ import annotations

import argparse
import json
import os
import urllib.request


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    base = os.environ.get("DGX_QWEN_URL") or "http://127.0.0.1:8001/v1"
    model = os.environ.get("DGX_QWEN_MODEL") or "qwen3.8-27b"
    prompt = open(args.prompt_file, encoding="utf-8").read()
    cap = int(os.environ.get("CU_TUNE_QWEN_PROMPT_MAX", "4000"))
    if len(prompt) > cap:
        prompt = prompt[:cap]
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": int(os.environ.get("CU_TUNE_QWEN_MAX_TOKENS", "512")),
        "temperature": 0.2,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "cu-tune-qwen/1"},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode())
    text = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    Path = __import__("pathlib").Path
    Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0 if text.strip() else 1


if __name__ == "__main__":
    raise SystemExit(main())
