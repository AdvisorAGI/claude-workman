"""Nested, cooperative X11 input actions. No device grabs or extra masters."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import math
import threading
import os
import time

from . import owner_pause


class Interrupted(RuntimeError):
    def __init__(self, result: dict):
        super().__init__(result.get("error", "interrupted"))
        self.result = result


def _linux(module) -> bool:
    return getattr(module, "PLATFORM", "") == "Linux/X11" or getattr(module, "__name__", "") in (
        "workman.x11", "workman.platform.linux_x11")


def enabled(module=None) -> bool:
    if module is None:
        from . import desktop
        module = desktop.active()
    if not _linux(module):
        return False
    switch = os.environ.get("WORKMAN_POLITE")
    return switch == "1" if switch is not None else module.display_name().split(".")[0] == ":0"


def gate_enabled(module) -> bool:
    """Private displays ignore shared owner files by default; explicit 0 is legacy."""
    return not _linux(module) or enabled(module) or os.environ.get("WORKMAN_POLITE") == "0"


def _seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
        return max(0.0, value) if math.isfinite(value) else default
    except ValueError:
        return default


@dataclass
class Action:
    name: str
    module: object
    display: str
    stamp: int | None = None
    pointer: tuple[int, int] | None = None
    expected: tuple[int, int] | None = None
    focus: dict | None = None
    changed_focus: bool = False
    focus_may_change: bool = False
    last_ghost: float = 0.0
    interrupted: dict | None = None
    progress: dict = field(default_factory=dict)
    report: dict = field(default_factory=dict)


_CURRENT: ContextVar[Action | None] = ContextVar("workman_polite_action", default=None)
_PENDING: dict[str, Action] = {}
_ACTION_LOCK = threading.RLock()


def current() -> Action | None:
    return _CURRENT.get()


def _ghost(method: str, state: Action, *coords) -> None:
    try:
        from . import ghost_cursor
        getattr(ghost_cursor, method)(state.display, *coords)
    except Exception:
        pass


def _pointer(state: Action) -> tuple[int, int] | None:
    p = state.module.pointer_position()
    if p.get("ok"):
        return int(p["x"]), int(p["y"])
    return None


def _interrupt(state: Action, error="human_active") -> None:
    if state.interrupted is None:
        state.interrupted = owner_pause.refusal(state.name, error=error)
        _ghost("hide", state)
    raise Interrupted(dict(state.interrupted, **state.progress))


def foreign_hold(**detail) -> None:
    state = current()
    if state is not None:
        state.progress.update(detail)
        _interrupt(state)
    raise Interrupted({"ok": False, "error": "human_active", **detail})


def guard(action: str | None = None) -> None:
    """Check before each injection. Never wait or resume within an action."""
    state = current()
    if state is None:
        return
    if state.interrupted is not None:
        raise Interrupted(dict(state.interrupted, **state.progress))
    gate_action = action or state.name
    if gate_action not in owner_pause.INPUT_ACTIONS:
        gate_action = "focus_window"
    if owner_pause.blocked(gate_action):
        _interrupt(state, "input_disabled")
    if owner_pause.human_input_stamp() != state.stamp:
        _interrupt(state)
    foreign = getattr(state.module, "foreign_holds", None)
    if foreign is not None and (holds := foreign()):
        foreign_hold(foreign_holds=holds)
    if state.expected is not None:
        try:
            p = _pointer(state)
        except Exception as exc:
            raise Interrupted({"ok": False, "error": "pointer_unreadable",
                               "detail": str(exc), **state.progress}) from exc
        if p is None:
            raise Interrupted({"ok": False, "error": "pointer_unreadable", **state.progress})
        if p != state.expected:
            owner_pause.mark_human_input()
            _interrupt(state)
    # Key bursts and long holds need activity updates even without motion.
    now = time.monotonic()
    if state.expected is not None and now - state.last_ghost >= 0.1:
        _ghost("notify", state, *state.expected)
        state.last_ghost = now


def placed(x: int, y: int) -> None:
    state = current()
    if state is not None:
        state.expected = (int(x), int(y))
        _ghost("notify", state, int(x), int(y))
        state.last_ghost = time.monotonic()


def may_change_focus() -> None:
    if (state := current()) is not None:
        state.focus_may_change = True


def progress(**values) -> None:
    if (state := current()) is not None:
        state.progress.update(values)


def sleep(seconds: float) -> None:
    if current() is None:
        time.sleep(max(0.0, seconds))
        return
    remaining = max(0.0, seconds)
    guard()
    while remaining > 0:
        step = min(0.02, remaining)
        time.sleep(step)
        remaining -= step
        guard()


def _start(state: Action, release: bool) -> None:
    if release:
        # A release must not depend on owner-file, pointer or focus reads succeeding.
        return
    ready = getattr(state.module, "polite_input_ready", None)
    if ready is not None and not ready():
        raise Interrupted({"ok": False, "error": "owned_state_unavailable",
                           "detail": "polite input requires the persistent XTEST channel"})
    if not release:
        gate_action = state.name if state.name in owner_pause.INPUT_ACTIONS else "focus_window"
        if owner_pause.blocked(gate_action):
            _interrupt(state, "input_disabled")
        moved = getattr(state.module, "foreign_pointer_motion", None)
        if moved is not None and moved():
            owner_pause.mark_human_input()
        if owner_pause.wait_for_human_quiet(
                max_wait=_seconds("WORKMAN_POLITE_MAX_WAIT_S", 20.0),
                quiet_s=_seconds("WORKMAN_POLITE_QUIET_S", 2.0)):
            _interrupt(state)
        if owner_pause.blocked(gate_action):
            _interrupt(state, "input_disabled")
    state.stamp = owner_pause.human_input_stamp()
    state.pointer = state.expected = _pointer(state)
    if state.pointer is None:
        raise Interrupted({"ok": False, "error": "pointer_unreadable"})
    focus = getattr(state.module, "input_focus", None)
    state.focus = focus() if focus is not None else None
    if state.pointer is not None and not release:
        _ghost("notify", state, *state.pointer)


def _finish(state: Action, failed: bool) -> None:
    from . import handback
    if state.interrupted is not None or failed:
        state.report["release"] = handback.release_owned()
    if handback.owned_holds():
        state.report["restore"] = "deferred_until_release"
        _PENDING[state.display] = state
        return
    _PENDING.pop(state.display, None)
    try:
        guard()
        if state.focus_may_change:
            # WMs handle click-focus after the XTEST request has returned.
            # This short, interruptible settling period prevents a late focus
            # change from landing after handback (observed on xfwm4).
            sleep(0.08)
            observe_focus()
        if owner_pause.human_active(_seconds("WORKMAN_POLITE_QUIET_S", 2.0)):
            _interrupt(state)
    except Interrupted:
        state.report["restore"] = "skipped_owner_active"
        _ghost("hide", state)
        return
    # Focus is sampled after each input; restore only an observed action change.
    if state.changed_focus and state.focus and state.focus.get("ok"):
        restore = getattr(state.module, "restore_input_focus", None)
        if restore is not None:
            guard("focus_window")
            state.report["focus"] = restore(state.focus)
    if state.pointer is not None and state.expected != state.pointer:
        guard()
        from . import desktop
        state.report["pointer"] = desktop.move(*state.pointer)
        state.report["pointer_readback"] = list(_pointer(state) or ())
        state.report["pointer_verified"] = state.report["pointer_readback"] == list(state.pointer)
    state.report["restore"] = "checked"
    if state.expected is not None:
        _ghost("notify", state, *state.expected)


def observe_focus() -> None:
    state = current()
    if state is not None and state.focus_may_change and state.focus and state.focus.get("ok"):
        fn = getattr(state.module, "input_focus", None)
        if fn is not None:
            now = fn()
            if now.get("ok") and now.get("window") != state.focus.get("window"):
                state.changed_focus = True


def run(action: str, call):
    """Run a result-returning callable in one nested action scope."""
    if current() is not None:
        return call()
    with _ACTION_LOCK:
        return _run(action, call)


def _run(action: str, call):
    from . import desktop
    module = desktop.active()
    if not enabled(module):
        return call()
    display = module.display_name()
    state = _PENDING.pop(display, None)
    resumed = state is not None
    if state is None:
        state = Action(action, module, display)
    state.name = action
    state.progress = {}
    state.report = {}
    token = _CURRENT.set(state)
    result = None
    failed = False
    try:
        if not resumed:
            _start(state, action in ("mouse_up", "key_up"))
        if action not in ("mouse_up", "key_up"):
            guard()
        result = call()
        failed = isinstance(result, dict) and result.get("ok") is False
    except Interrupted as exc:
        result = exc.result
        failed = True
    except Exception as exc:
        result = getattr(exc, "result", None)
        if not isinstance(result, dict):
            result = {"ok": False, "error": "backend_error", "detail": f"{type(exc).__name__}: {exc}",
                      **state.progress}
        failed = True
    finally:
        try:
            _finish(state, failed)
        except Interrupted as exc:
            state.report["restore"] = "interrupted"
            if not failed:
                result = exc.result
        except Exception as exc:
            state.report["restore_error"] = f"{type(exc).__name__}: {exc}"
        _CURRENT.reset(token)
    if isinstance(result, dict):
        result = dict(result)
        if state.interrupted is not None and action not in ("mouse_up", "key_up"):
            # Preserve sequence-local progress, while making interruption visible at top level.
            result.update(state.interrupted)
            result.update(state.progress)
        result["polite"] = state.report
    return result


def action(name: str):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            return run(name, lambda: fn(*args, **kwargs))
        return wrapped
    return decorate
