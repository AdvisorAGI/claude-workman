"""Pick up where an interrupted action stopped.

A physical Escape (or the owner touching the mouse) stops an agent action
part-way: half a sentence typed, a drag dropped mid-air, three of seven
wheel clicks done. The human-mode primitives already report exactly where
they stopped (`remaining`, `at`, `to`, `uncorrected_typo` ...). This module
remembers the last such report and turns it into the short list of actions
that finishes the job, so "resume" means continue, not start over.
"""
from __future__ import annotations

import time

_LAST: dict = {"entry": None}

#: Tools whose interrupted result can be continued.
RESUMABLE = {"type_text", "click", "move", "hover", "drag", "scroll", "press_key",
             "key_hold", "mouse_button", "click_element", "perform_element_action",
             "set_element_value", "chrome_type", "chrome_click_text"}


def interrupted(result) -> bool:
    if not isinstance(result, dict) or result.get("ok") is not False:
        return False
    err = result.get("error")
    if err in ("interrupted", "input_disabled", "human_active", "bot_challenge"):
        return True
    reason = result.get("reason")
    return isinstance(reason, dict) and reason.get("error") in ("input_disabled", "human_active")


def remember(tool: str, args: dict, result):
    """Store `result` when it is an interruption; return `result` unchanged."""
    if tool in RESUMABLE and interrupted(result):
        _LAST["entry"] = {"tool": tool, "args": dict(args), "result": result,
                          "at": time.time()}
    elif tool in RESUMABLE and isinstance(result, dict) and result.get("ok"):
        # The same tool succeeded afterwards: the old interruption is stale.
        last = _LAST["entry"]
        if last and last["tool"] == tool:
            _LAST["entry"] = None
    return result


def last() -> dict | None:
    return _LAST["entry"]


def clear() -> None:
    _LAST["entry"] = None


def plan(entry: dict | None = None) -> list[dict]:
    """Batch-style steps that finish the interrupted action."""
    entry = entry or _LAST["entry"]
    if not entry:
        return []
    tool, args, res = entry["tool"], dict(entry.get("args") or {}), entry.get("result") or {}
    args.pop("space", None)  # remembered args are already screen pixels
    steps: list[dict] = []
    if tool in ("type_text", "chrome_type"):
        remaining = res.get("remaining")
        if remaining is None:
            remaining = args.get("text", "")
        if res.get("uncorrected_typo"):
            steps.append({"action": "press_key", "key": "BackSpace"})
        if remaining:
            step = {"action": tool, **args, "text": remaining}
            steps.append(step)
        return steps
    if tool == "scroll":
        remaining = res.get("remaining")
        if remaining is None:
            remaining = args.get("amount", 3)
        if remaining and remaining > 0:
            step = {"action": "scroll", **args, "amount": int(remaining)}
            steps.append(step)
        return steps
    if tool == "drag":
        at = res.get("at")
        if res.get("released") is False:
            steps.append({"action": "mouse_button", "button": 1, "press": False})
        if at and res.get("to"):
            steps.append({"action": "drag", "from_x": int(at[0]), "from_y": int(at[1]),
                          "to_x": int(res["to"][0]), "to_y": int(res["to"][1])})
        else:
            steps.append({"action": "drag", **args})
        return steps
    if tool in ("click", "move", "hover"):
        target = res.get("target")
        step = {"action": tool, **args}
        if target:
            step["x"], step["y"] = int(target[0]), int(target[1])
        return [step]
    return [{"action": tool, **args}]
