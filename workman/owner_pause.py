"""Owner pause for agent mouse/keyboard. Physical input always stays usable.

Two facts live here, both as small files under WORKMAN_LEARN_ROOT
(default ~/.grok/workman-learn), so every workman process on the machine
and the Escape listener agree without talking to each other:

* The switch file (fleet-input-switch.json), the one fleet-wm already
  honours. Escape writes it OFF. Agents must stop injecting until the owner
  resumes (Enable both, or `python -m workman.esc_pause --resume`). A second
  Escape does not resume: Escape is a normal key in apps.

* Three timestamps, kept as file mtimes because touching a file is the
  cheapest cross-process signal there is:
    human-input.stamp   the listener saw the owner use a physical device
                        (or XTEST input no agent claimed, which is how his
                        Mac keyboard arrives through the Deskflow KVM)
    agent-input.stamp   a workman process injected input just now
    agent-escape.stamp  a workman process injected Escape just now
  `human_active()` is what makes agent input yield: while the owner is on
  the mouse or keyboard, `desktop._safe` waits a little and then refuses
  with `human_active` instead of fighting him for the pointer.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None

INSTRUCTION = (
    "Owner paused agent input (Escape). Do not click, type, or move the "
    "pointer. Leave the physical mouse and keyboard to the owner. Wait until "
    "the owner says they are done, or input_switches are both ON again."
)
HUMAN_ACTIVE_INSTRUCTION = (
    "The owner is using the mouse or keyboard right now. Agent input yielded "
    "and was not sent. Wait a few seconds, screenshot to see what changed, "
    "then retry from the partial progress in this result."
)

# mouse_up and key_up are deliberately absent: a paused agent must still be
# able to let go of a button or modifier it is holding, or the owner inherits
# a stuck drag or a held Ctrl.
MOUSE_ACTIONS = {
    "click", "click_with", "move", "drag", "hover", "scroll", "scroll_at",
    "mouse_down",
}
KEYBOARD_ACTIONS = {"type_text", "press_key", "key_down"}
BOTH_ACTIONS = {"focus_window", "kill_window", "window_action", "window_geometry"}
INPUT_ACTIONS = MOUSE_ACTIONS | KEYBOARD_ACTIONS | BOTH_ACTIONS

HUMAN_STAMP = "human-input.stamp"
AGENT_STAMP = "agent-input.stamp"
AGENT_ESCAPE_STAMP = "agent-escape.stamp"

#: Seconds of no physical input before the owner counts as away from the desk.
HUMAN_QUIET_S = float(os.environ.get("WORKMAN_HUMAN_QUIET_S", "1.0"))
#: How long one agent action will wait for that quiet before giving up.
HUMAN_YIELD_S = float(os.environ.get("WORKMAN_HUMAN_YIELD_S", "3.0"))
#: An XTEST event this soon after an agent stamp is the agent's own.
AGENT_FRESH_S = 1.0
AGENT_ESCAPE_FRESH_S = 0.5
_MARK_THROTTLE_S = 0.1

_last_mark = {"agent": 0.0, "human": 0.0}


def _root() -> Path:
    return Path(os.environ.get("WORKMAN_LEARN_ROOT", "~/.grok/workman-learn")).expanduser()


def path() -> Path:
    return _root() / "fleet-input-switch.json"


def state() -> dict:
    try:
        raw = json.loads(path().read_text())
        if type(raw.get("mouse")) is bool and type(raw.get("keyboard")) is bool:
            out = {"mouse": raw["mouse"], "keyboard": raw["keyboard"]}
            if type(raw.get("paused_by")) is str:
                out["paused_by"] = raw["paused_by"]
            if type(raw.get("paused_at")) is str:
                out["paused_at"] = raw["paused_at"]
            return out
    except FileNotFoundError:
        return {"mouse": True, "keyboard": True}
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return {"mouse": False, "keyboard": False}


def _write(current: dict) -> dict:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: current[k] for k in ("mouse", "keyboard")}
    if current.get("paused_by"):
        payload["paused_by"] = current["paused_by"]
        payload["paused_at"] = current.get("paused_at") or time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
    data = json.dumps(payload)
    lock_path = p.with_suffix(".switch-lock")
    lock_f = lock_path.open("a")
    try:
        if fcntl is not None:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
        tmp = p.with_suffix(".switch-new")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, p)
    finally:
        lock_f.close()
    return payload


def blocked(action: str) -> bool:
    """True when this desktop action must not run."""
    s = state()
    if action in MOUSE_ACTIONS:
        return not s["mouse"]
    if action in KEYBOARD_ACTIONS:
        return not s["keyboard"]
    if action in BOTH_ACTIONS:
        return not s["mouse"] or not s["keyboard"]
    return False


def pause_from_escape() -> dict:
    current = dict(state())
    if current.get("mouse") is False and current.get("keyboard") is False:
        return current
    current["mouse"] = False
    current["keyboard"] = False
    current["paused_by"] = "escape"
    current["paused_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return _write(current)


def resume() -> dict:
    return _write({"mouse": True, "keyboard": True})


def refusal(action: str, error: str = "input_disabled") -> dict:
    s = state()
    out = {
        "ok": False,
        "error": error,
        "action": action,
        "input_switches": {"mouse": s["mouse"], "keyboard": s["keyboard"]},
    }
    if error == "human_active":
        out["human_input_age_s"] = human_input_age()
        out["instruction"] = HUMAN_ACTIVE_INSTRUCTION
    else:
        out["instruction"] = INSTRUCTION
    return out


# ---- presence stamps -------------------------------------------------------

def _stamp_path(name: str) -> Path:
    return _root() / name


def _touch(name: str) -> None:
    p = _stamp_path(name)
    try:
        os.utime(p, None)
    except FileNotFoundError:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a"):
            pass
        os.utime(p, None)
    except OSError:
        pass


def _age(name: str) -> float | None:
    try:
        return max(0.0, time.time() - os.stat(_stamp_path(name)).st_mtime)
    except OSError:
        return None


def mark_agent_input(escape: bool = False) -> None:
    """Called by the backend right before it injects. Throttled: a 20-step
    pointer path costs one utime, not twenty."""
    now = time.monotonic()
    if escape:
        _touch(AGENT_ESCAPE_STAMP)
    if now - _last_mark["agent"] >= _MARK_THROTTLE_S:
        _last_mark["agent"] = now
        _touch(AGENT_STAMP)


def mark_human_input() -> None:
    """Called by the listener for every physical (or unclaimed XTEST) event.
    Throttled to a few syscalls a second however fast the mouse moves."""
    now = time.monotonic()
    if now - _last_mark["human"] >= _MARK_THROTTLE_S * 2:
        _last_mark["human"] = now
        _touch(HUMAN_STAMP)


def agent_input_age() -> float | None:
    return _age(AGENT_STAMP)


def agent_escape_age() -> float | None:
    return _age(AGENT_ESCAPE_STAMP)


def human_input_age() -> float | None:
    return _age(HUMAN_STAMP)


def agent_claims(escape: bool = False) -> bool:
    """Would an XTEST event arriving now be a workman agent's own?"""
    age = agent_escape_age() if escape else agent_input_age()
    limit = AGENT_ESCAPE_FRESH_S if escape else AGENT_FRESH_S
    return age is not None and age <= limit


def human_active(quiet_s: float | None = None) -> bool:
    """True while the owner has touched a physical device recently."""
    age = human_input_age()
    return age is not None and age < (HUMAN_QUIET_S if quiet_s is None else quiet_s)


def wait_for_human_quiet(max_wait: float | None = None,
                         quiet_s: float | None = None) -> bool:
    """Sleep while the owner is active, up to max_wait. Returns True when he
    is STILL active afterwards (the caller should refuse), False when the
    desk went quiet. Nothing is polled when he is not there: one stat."""
    quiet = HUMAN_QUIET_S if quiet_s is None else quiet_s
    limit = HUMAN_YIELD_S if max_wait is None else max_wait
    deadline = time.monotonic() + limit
    while True:
        age = human_input_age()
        if age is None or age >= quiet:
            return False
        if time.monotonic() >= deadline:
            return True
        time.sleep(min(0.05, max(0.005, quiet - age)))


def status() -> dict:
    """Everything a person or a tool wants to know about the pause state."""
    s = state()
    return {
        "input_switches": {"mouse": s["mouse"], "keyboard": s["keyboard"]},
        "paused_by": s.get("paused_by"),
        "paused_at": s.get("paused_at"),
        "human_input_age_s": human_input_age(),
        "human_active": human_active(),
        "agent_input_age_s": agent_input_age(),
        "quiet_s": HUMAN_QUIET_S,
        "yield_s": HUMAN_YIELD_S,
        "root": str(_root()),
    }
