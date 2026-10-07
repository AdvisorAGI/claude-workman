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
    "and stopped. Check the completed and remaining progress. Wait a few seconds, screenshot to see what changed, "
    "then retry from the partial progress in this result."
)

# mouse_up and key_up are deliberately absent: a paused agent must still be
# able to let go of a button or modifier it is holding, or the owner inherits
# a stuck drag or a held Ctrl.
MOUSE_ACTIONS = {
    "click", "click_with", "move", "drag", "hover", "scroll", "scroll_at",
    "mouse_down",
}
KEYBOARD_ACTIONS = {"type_text", "press_key", "key_down", "paste"}
BOTH_ACTIONS = {"focus_window", "kill_window", "window_action", "window_geometry"}
INPUT_ACTIONS = MOUSE_ACTIONS | KEYBOARD_ACTIONS | BOTH_ACTIONS

HUMAN_STAMP = "human-input.stamp"
AGENT_STAMP = "agent-input.stamp"
AGENT_ESCAPE_STAMP = "agent-escape.stamp"

#: Seconds of no physical input before the owner counts as away from the desk.
HUMAN_QUIET_S = float(os.environ.get("WORKMAN_HUMAN_QUIET_S", "1.0"))
#: How long one agent action will wait for that quiet before giving up.
HUMAN_YIELD_S = float(os.environ.get("WORKMAN_HUMAN_YIELD_S", "3.0"))
#: Legacy stamp ages remain status information, never event attribution.
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


def human_input_stamp() -> int | None:
    """Exact stamp identity for detecting even recently aged mid-action input."""
    try:
        return os.stat(_stamp_path(HUMAN_STAMP)).st_mtime_ns
    except FileNotFoundError:
        return None


def human_input_age() -> float | None:
    return _age(HUMAN_STAMP)


def agent_claims(event: dict | None = None, *, display: str = ":0", escape=False) -> bool:
    """Consume an exact key/button or absolute-motion claim, never an action."""
    if not event or event.get("kind") not in ("key_press", "key_release", "button_press", "button_release", "motion"):
        return False
    with _claims(display) as ledger:
        now = time.monotonic()
        for claim in ledger:
            if (claim["kind"] == event.get("kind") and claim["detail"] == event.get("detail")
                    and 0 <= now - claim["at"] <= 0.5):
                if claim["kind"] == "motion" and claim.get("position") != event.get("position"):
                    continue
                timestamp = event.get("time")
                # Server times are 32-bit milliseconds and wrap around.
                if timestamp is None or not 0 <= ((int(timestamp) - claim["server_time"]) & 0xffffffff) <= claim["span"]:
                    continue
                ledger.remove(claim)
                return True
    return False


from contextlib import contextmanager
import hashlib
import tempfile
import uuid


@contextmanager
def _claims(display):
    from .polite_state import display_key
    root = _root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = hashlib.sha256(display_key(display).encode()).hexdigest()[:20]
    path = root / f"agent-events-{key}.json"
    lock = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            ledger = json.loads(path.read_text())
            if not isinstance(ledger, list):
                ledger = []
        except (OSError, ValueError):
            ledger = []
        now = time.monotonic()
        ledger = [c for c in ledger if isinstance(c, dict) and 0 <= now - c.get("at", -1) <= 0.5]
        yield ledger
        fd, tmp = tempfile.mkstemp(dir=root, prefix=".agent-events-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(ledger[-256:], stream)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    finally:
        os.close(lock)


def claim_agent_event(display: str, kind: str, detail: int, server_time: int, *, position=None) -> str:
    """Register just before this connection emits one event, never an action."""
    token = uuid.uuid4().hex
    with _claims(display) as ledger:
        ledger.append({"token": token, "kind": kind, "detail": int(detail),
                       "server_time": int(server_time) & 0xffffffff, "span": 50,
                       "at": time.monotonic(), "position": list(position) if position is not None else None})
    return token


def finish_agent_event(display: str, token: str, server_time: int | None):
    with _claims(display) as ledger:
        for claim in list(ledger):
            if claim["token"] == token:
                if server_time is None:
                    ledger.remove(claim)
                else:
                    claim["span"] = min(50, (int(server_time) - claim["server_time"]) & 0xffffffff)
                break


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
    from . import polite, desktop
    on = polite.enabled()
    quiet = polite._seconds("WORKMAN_POLITE_QUIET_S", 2.0) if on else HUMAN_QUIET_S
    limit = polite._seconds("WORKMAN_POLITE_MAX_WAIT_S", 20.0) if on else HUMAN_YIELD_S
    return {
        "input_switches": {"mouse": s["mouse"], "keyboard": s["keyboard"]},
        "paused_by": s.get("paused_by"),
        "paused_at": s.get("paused_at"),
        "human_input_age_s": human_input_age(),
        "human_active": human_active(quiet),
        "agent_input_age_s": agent_input_age(),
        "quiet_s": quiet,
        "yield_s": limit,
        "polite": on,
        "gate_enabled": polite.gate_enabled(desktop.active()),
        "root": str(_root()),
    }
