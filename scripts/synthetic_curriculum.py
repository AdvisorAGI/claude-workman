#!/usr/bin/env python3
"""Drive a small model through a synthetic curriculum of desktop workflows.

One task teaches nothing durable: a model that clicks a search box correctly
once may have been lucky, and a single run cannot show whether step count is
what breaks it. This runner holds a curriculum ordered by how many actions a
task needs to compose -- two for a plain navigation, six for a search whose
result must then be read back -- and drives every one of them through the same
driver, so a failure can be read against task length rather than guessed at.

The trajectories the driver already writes are the teaching artifact; this
script only decides what the model is asked and records what came back.

Every task is deliberately READ-ONLY. The desktop it drives is the owner's
live machine with a live mailbox, so no task may open a message, send, delete,
archive, download, or mark anything. Navigation, search and reading only, and
the rail is repeated inside each task string where the model will actually
read it.

    ~/claude-atmos-workman/.venv/bin/python scripts/synthetic_curriculum.py \
        --endpoint http://127.0.0.1:8001/v1 --model qwen3.8-27b \
        --notes ~/.claude/skills/workman/LEARNINGS.md
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DRIVER = os.path.join(HERE, "small_model_drive.py")

sys.path.insert(0, HERE)
import small_model_drive as smd  # noqa: E402  (path set just above)

# The frame every task must start from. Measured need, not taste: one run of
# this curriculum left the browser on a second Gmail account (/u/3) inside a
# category view with stale text in a focused search box, and every later task
# inherited it. A click on an already-focused field changes no pixels, the
# frame diff calls that a miss, and three of those in a row abort the run --
# so one task's drift silently scores the next three as model failures.
RESET_URL = "https://mail.google.com/mail/u/0/#inbox"

# The rail goes in every task: the model reads the task, not this docstring.
RAIL = ("Only search, navigate and read. Do not open any email and do not "
        "click anything that sends, deletes, archives, downloads or marks "
        "anything.")

CURRICULUM = [
    {
        "name": "nav-read",
        "steps_expected": 3,
        "task": "Open a new browser tab and go to https://example.com . Then "
                "read the big heading on the page and call done with that "
                f"heading as the answer. {RAIL}",
    },
    {
        "name": "gmail-count",
        "steps_expected": 4,
        "task": "Switch to the Gmail tab in Chrome. Click the 'Search mail' "
                "box, type exactly: from:google.com  and press Return. Then "
                "read the result counter near the top right of the list (it "
                "looks like '1-50 of 2,328') and call done with that counter "
                f"as the answer. {RAIL}",
    },
    {
        "name": "gmail-top3",
        "steps_expected": 5,
        "task": "Switch to the Gmail tab in Chrome. Click the 'Search mail' "
                "box, type exactly: from:google.com console  and press "
                "Return. Then read the three newest rows in the result list "
                "and call done with the sender, subject and date of each of "
                f"those three. {RAIL}",
    },
    {
        "name": "two-tab",
        "steps_expected": 6,
        "task": "Open a new browser tab and go to https://example.org . Read "
                "its heading. Then switch back to the Gmail tab in Chrome and "
                "call done with an answer naming both the heading you read on "
                f"example.org and what the Gmail tab is showing now. {RAIL}",
    },
    {
        "name": "dated-search",
        "steps_expected": 6,
        "task": "Switch to the Gmail tab in Chrome. Click the 'Search mail' "
                "box, type exactly: from:google.com after:2026/09/01  and "
                "press Return. Then call done with an answer saying how many "
                "results came back and listing the sender and date of each "
                f"row you can see. {RAIL}",
    },
]


def reset_desktop(args: argparse.Namespace) -> dict:
    """Put the browser back on a known page before a task is scored.

    Same path the driver's own open_url takes -- focus Chrome, address bar,
    type, Return -- so a task starts where the model expects to be and no
    state from the previous task is attributed to this one.
    """
    backend = smd.Backend(args.python, smd.DEFAULT_WM)
    focused = backend.focus("Chrome")
    time.sleep(0.5)
    backend.key("Escape")          # drop any open menu or suggestion list
    time.sleep(0.2)
    backend.key(smd.URL_BAR_KEY)
    time.sleep(0.3)
    backend.type(args.reset_url)
    backend.key("Return")
    time.sleep(3.0)
    return focused if isinstance(focused, dict) else {"focus": str(focused)}


def run_one(item: dict, args: argparse.Namespace) -> dict:
    cmd = [args.python, "-u", DRIVER,
           "--endpoint", args.endpoint, "--model", args.model,
           "--task", item["task"],
           "--send-width", str(args.send_width), "--temp", str(args.temp),
           "--max-steps", str(args.max_steps), "--runs", args.runs]
    for path in args.notes:
        cmd += ["--notes", path]

    started = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - started

    answer, run_dir = None, None
    for line in proc.stdout.splitlines():
        if line.startswith("ANSWER: "):
            answer = line[len("ANSWER: "):]
        elif line.startswith("run dir: "):
            run_dir = line[len("run dir: "):].strip()

    # Step count comes off the trajectory, not the log: the log can be cut off
    # mid-write, the jsonl is one line per landed action.
    steps = None
    if run_dir:
        jsonl = os.path.join(os.path.dirname(DRIVER), "..", run_dir, "steps.jsonl")
        jsonl = os.path.normpath(jsonl)
        if os.path.exists(jsonl):
            with open(jsonl, encoding="utf-8") as fh:
                steps = sum(1 for line in fh if line.strip())

    return {
        "name": item["name"],
        "steps_expected": item["steps_expected"],
        "steps_taken": steps,
        "answered": answer is not None,
        "answer": answer,
        "exit_code": proc.returncode,
        "seconds": round(elapsed, 1),
        "run_dir": run_dir,
        "stderr_tail": proc.stderr.strip()[-400:] or None,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--endpoint", default="http://127.0.0.1:8001/v1")
    p.add_argument("--model", default="qwen3.8-27b")
    p.add_argument("--notes", action="append", default=[], metavar="PATH")
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--runs", default=os.path.join(HERE, "..", "runs"))
    p.add_argument("--send-width", type=int, default=1568)
    p.add_argument("--temp", type=float, default=0.1)
    p.add_argument("--max-steps", type=int, default=22)
    p.add_argument("--only", default=None,
                   help="comma-separated task names to run instead of all")
    p.add_argument("--out", default=None, help="write the summary json here")
    p.add_argument("--reset-url", default=RESET_URL,
                   help="page to return the browser to before each task")
    p.add_argument("--no-reset", dest="reset", action="store_false", default=True,
                   help="score tasks from whatever state the last one left")
    args = p.parse_args(argv)

    wanted = CURRICULUM
    if args.only:
        names = {n.strip() for n in args.only.split(",")}
        wanted = [t for t in CURRICULUM if t["name"] in names]
        missing = names - {t["name"] for t in CURRICULUM}
        if missing:
            print(f"no such task(s): {', '.join(sorted(missing))}")
            return 2

    results = []
    for i, item in enumerate(wanted, 1):
        print(f"\n=== [{i}/{len(wanted)}] {item['name']} "
              f"(expects ~{item['steps_expected']} steps) ===", flush=True)
        if args.reset:
            reset_desktop(args)
            print(f"  reset -> {args.reset_url}", flush=True)
        res = run_one(item, args)
        results.append(res)
        print(f"  steps={res['steps_taken']} exit={res['exit_code']} "
              f"{res['seconds']}s", flush=True)
        if res["answer"]:
            print(f"  answer: {res['answer'][:300]}", flush=True)
        if res["stderr_tail"]:
            print(f"  stderr: {res['stderr_tail'][:200]}", flush=True)

    print("\n=== curriculum summary ===")
    print(f"{'task':<14}{'exp':>4}{'took':>6}{'answered':>10}{'secs':>7}")
    for r in results:
        print(f"{r['name']:<14}{r['steps_expected']:>4}"
              f"{str(r['steps_taken']):>6}{str(r['answered']):>10}"
              f"{r['seconds']:>7}")
    answered = sum(1 for r in results if r["answered"])
    print(f"\nanswered {answered}/{len(results)}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2)
        print(f"summary -> {args.out}")
    return 0 if answered == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
