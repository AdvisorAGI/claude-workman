"""Cooperative X11 actions with a persisted target and deferred handback."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import math
import os
import threading
import time

from . import owner_pause, polite_state


class Interrupted(RuntimeError):
    def __init__(self, result: dict):
        super().__init__(result.get("error", "interrupted"))
        self.result = result


def _linux(module) -> bool:
    return getattr(module, "PLATFORM", "") == "Linux/X11" or getattr(module, "__name__", "") in (
        "workman.x11", "workman.platform.linux_x11")


def shared_display(module) -> bool:
    return not _linux(module) or polite_state.display_key(module.display_name()).split(".")[0] == ":0"


def enabled(module=None) -> bool:
    if module is None:
        from . import desktop
        module = desktop.active()
    if not _linux(module):
        return False
    switch = os.environ.get("WORKMAN_POLITE")
    return switch == "1" if switch is not None else shared_display(module)


def gate_enabled(module) -> bool:
    """Every display honours Escape; private displays ignore presence stamps."""
    return True


def presence_enabled(module) -> bool:
    return shared_display(module) or enabled(module) or os.environ.get("WORKMAN_POLITE") == "0"


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
    store: object = None
    stamp: int | None = None
    pointer: tuple[int, int] | None = None
    expected: tuple[int, int] | None = None
    focus: dict | None = None
    expected_focus: dict | None = None
    target: dict | None = None
    focus_may_change: bool = False
    restoring: bool = False
    started: bool = False
    last_ghost: float = 0.0
    interrupted: dict | None = None
    progress: dict = field(default_factory=dict)
    report: dict = field(default_factory=dict)


_CURRENT: ContextVar[Action | None] = ContextVar("workman_polite_action", default=None)
# Kept for the public reset hook; snapshots now live in the display store.
_PENDING: dict = {}
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


def _focus(state: Action) -> dict:
    fn = getattr(state.module, "input_focus", None)
    return fn() if fn is not None else {"ok": False, "error": "focus_unavailable"}


def _same_focus(a, b) -> bool:
    if not a or not b or not a.get("ok") or not b.get("ok"):
        return False
    return a.get("window") == b.get("window") and all(
        not a.get(k) or not b.get(k) or a[k] == b[k] for k in ("pid", "class"))


def _stop(state: Action, result: dict) -> None:
    if state.interrupted is None:
        state.interrupted = result
        _ghost("hide", state)
    raise Interrupted(dict(state.interrupted, **state.progress))


def _interrupt(state: Action, error="human_active") -> None:
    _stop(state, owner_pause.refusal(state.name, error=error))


def foreign_hold(**detail) -> None:
    state = current()
    if state is not None:
        state.progress.update(detail)
        _interrupt(state)
    raise Interrupted({"ok": False, "error": "human_active", **detail})


def keyboard_guard() -> None:
    """The final check before each key press, including modifiers and paste."""
    state = current()
    if state is None or state.restoring:
        return
    now = _focus(state)
    if state.target is None or not _same_focus(state.target, now):
        _stop(state, {"ok": False, "error": "focus_changed", "action": state.name,
                      "intended_window": state.target, "current_window": now,
                      "instruction": "Focus or click the intended window again before sending keys."})


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
    if gate_action in owner_pause.KEYBOARD_ACTIONS:
        keyboard_guard()
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
            _stop(state, {"ok": False, "error": "pointer_unreadable", "detail": str(exc)})
        if p is None:
            _stop(state, {"ok": False, "error": "pointer_unreadable"})
        if p != state.expected:
            owner_pause.mark_human_input()
            _interrupt(state)
    now = time.monotonic()
    if state.expected is not None and now - state.last_ghost >= 0.1 and not state.restoring:
        _ghost("notify", state, *state.expected)
        state.last_ghost = now


def _save_expected(state):
    if state.store is not None and state.store.data.get("snapshot"):
        state.store.data["expected_pointer"] = state.expected
        state.store.data["expected_focus"] = state.expected_focus
        state.store.write()


def placed(x: int, y: int) -> None:
    if (state := current()) is not None:
        state.expected = (int(x), int(y))
        _save_expected(state)
        if not state.restoring:
            _ghost("notify", state, *state.expected)
            state.last_ghost = time.monotonic()


def set_target(focus: dict | None) -> None:
    if (state := current()) is not None and not state.restoring:
        state.target = focus if focus and focus.get("ok") else None
        state.store.data["target"] = state.target
        state.store.write()


def target_pointer() -> None:
    """Record the actual client under a primary-button press, never a guess."""
    if (state := current()) is not None and not state.restoring:
        from . import desktop
        p = _pointer(state)
        hit = desktop._window_at_point(*p) if p is not None else None
        set_target(dict(hit, window=int(hit["id"])) if hit and hit.get("ok") else None)


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


def _stale(state, data) -> bool:
    return (data.get("inflight", False)
            or data.get("stamp") != owner_pause.human_input_stamp()
            or list(_pointer(state) or ()) != data.get("expected_pointer")
            or not _same_focus(data.get("expected_focus"), _focus(state)))


def _discard(data, reason):
    for key in ("snapshot", "due", "inflight", "expected_pointer", "expected_focus", "stamp"):
        data.pop(key, None)
    data["last_restore"] = {"ok": True, "restore": reason}


def _start(state: Action, release: bool) -> None:
    data = state.store.data
    state.target = data.get("target")
    if release:
        return  # Cleanup must survive broken reads and Escape.
    ready = getattr(state.module, "polite_input_ready", None)
    if ready is not None and not ready():
        raise Interrupted({"ok": False, "error": "owned_state_unavailable",
                           "detail": "polite input requires the persistent XTEST channel"})
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
    state.expected = _pointer(state)
    state.expected_focus = _focus(state)
    if state.expected is None:
        raise Interrupted({"ok": False, "error": "pointer_unreadable"})
    if data.get("snapshot") and _stale(state, data):
        _discard(data, "skipped_owner_changed")
    if not data.get("snapshot"):
        data["snapshot"] = {"pointer": state.expected, "focus": state.expected_focus}
    state.pointer = tuple(data["snapshot"]["pointer"])
    state.focus = data["snapshot"]["focus"]
    state.started = True
    # Cancel the old deadline durably before the first side effect. A crashed
    # transaction is not eligible for a later blind restore.
    data.update(due=None, inflight=True, stamp=state.stamp,
                expected_pointer=state.expected, expected_focus=state.expected_focus)
    state.store.write()
    _ghost("notify", state, *state.expected)


def observe_focus() -> None:
    if (state := current()) is not None and state.focus_may_change:
        state.expected_focus = _focus(state)
        _save_expected(state)


def _schedule(display: str) -> dict:
    try:
        from . import polite_restore
        return polite_restore.ensure(display)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _schedule_report(state):
    state.report["scheduler"] = _schedule(state.display)
    if not state.report["scheduler"].get("ok"):
        state.report["restore"] = "restore_unarmed"
        state.store.data["last_restore"] = {"ok": False, "restore": "restore_unarmed", "error": "restore_failed",
                                           "scheduler": state.report["scheduler"]}
        state.store.write()


def _record_restore(store, out):
    _discard(store.data, out["restore"])
    store.data["last_restore"] = out
    store.write()
    return out


def _restore_failure(store, exc):
    return _record_restore(store, {"ok": False, "error": "restore_failed", "restore": "restore_failed", "detail": str(exc)})


def _finish(state: Action, failed: bool) -> None:
    from . import handback
    data = state.store.data
    if state.interrupted is not None or failed:
        state.report["release"] = handback.release_owned()
    if not state.started:
        if state.name in ("mouse_up", "key_up") and data.get("snapshot"):
            data["due"] = time.monotonic() + _seconds("WORKMAN_POLITE_RESTORE_AFTER_S", 5.0)
            state.store.write()
            state.report["restore"] = "deferred"
            _schedule_report(state)
        return
    try:
        guard()
        if state.focus_may_change:
            sleep(0.08)
            observe_focus()
    except Interrupted:
        _discard(data, "skipped_owner_changed")
        state.report["restore"] = "skipped_owner_changed"
        state.store.write()
        _ghost("hide", state)
        return
    data.update(inflight=False, expected_pointer=state.expected,
                expected_focus=state.expected_focus,
                due=time.monotonic() + _seconds("WORKMAN_POLITE_RESTORE_AFTER_S", 5.0))
    state.store.write()
    state.report.update(restore="deferred", restore_after_s=_seconds("WORKMAN_POLITE_RESTORE_AFTER_S", 5.0),
                        intended_window=state.target, current_window=state.expected_focus,
                        last_restore=data.get("last_restore"))
    _schedule_report(state)


def restore_pending(module=None, *, force=False) -> dict:
    """Timer/hand_back API. Shares the action lock and refuses stale snapshots."""
    from . import desktop
    module = module or desktop.active()
    if not enabled(module):
        return {"ok": True, "restore": "disabled"}
    display = module.display_name()
    with _ACTION_LOCK:
        try:
            with polite_state.locked(display, blocking=force) as store:
                if store is None:
                    return {"ok": True, "restore": "deferred_action_running"}
                data = store.data
                if not data.get("snapshot"):
                    return data.get("last_restore", {"ok": True, "restore": "nothing_pending"})
                state = Action("focus_window", module, display, store=store, restoring=True,
                               stamp=data.get("stamp"), expected=tuple(data.get("expected_pointer") or ()),
                               expected_focus=data.get("expected_focus"))
                if not force and (data.get("due") is not None and time.monotonic() < data["due"]):
                    return {"ok": True, "restore": "deferred"}
                try:
                    stale = _stale(state, data)
                except Exception as exc:
                    return _restore_failure(store, exc)
                if stale:
                    return _record_restore(store, {"ok": True, "restore": "skipped_owner_changed"})
                if not force and (data.get("due") is None or time.monotonic() < data["due"]):
                    return {"ok": True, "restore": "deferred"}
                if owner_pause.blocked("focus_window") or owner_pause.human_active(_seconds("WORKMAN_POLITE_QUIET_S", 2.0)):
                    return {"ok": True, "restore": "deferred_owner_active"}
                from . import x11
                ch = x11._xt()
                if ch is None:
                    return _restore_failure(store, "XTEST channel unavailable")
                try:
                    holds = ch.xtest_held()
                except Exception as exc:
                    return _restore_failure(store, exc)
                if holds.get("keys") or holds.get("buttons"):
                    return {"ok": True, "restore": "deferred_until_release"}
                snapshot = data["snapshot"]
                token = _CURRENT.set(state)
                out = {"ok": True, "restore": "restored"}
                try:
                    # Pointer first: a focus-follows-mouse WM must not undo the
                    # final EWMH activation. The desktop viewer guard still applies.
                    if list(state.expected) != list(snapshot["pointer"]):
                        guard("move")
                        out["pointer"] = desktop.move(*snapshot["pointer"])
                        out["pointer_readback"] = list(_pointer(state) or ())
                        out["pointer_verified"] = (out["pointer"].get("ok") and
                                                    out["pointer_readback"] == snapshot["pointer"])
                        if not out["pointer_verified"]:
                            out.update(ok=False, error="restore_failed", restore="restore_failed")
                    if snapshot["focus"].get("ok") and not _same_focus(snapshot["focus"], _focus(state)):
                        guard("focus_window")
                        out["focus"] = module.restore_input_focus(snapshot["focus"])
                        if not out["focus"].get("ok"):
                            out.update(ok=False, error="restore_failed", restore="restore_failed")
                    out["current_window"] = _focus(state)
                except Interrupted as exc:
                    out = {"ok": False, "restore": "skipped_owner_changed", "error": exc.result["error"]}
                except Exception as exc:
                    out = {"ok": False, "restore": "restore_failed", "error": "restore_failed", "detail": str(exc)}
                finally:
                    _CURRENT.reset(token)
                _ghost("hide", state)
                return _record_restore(store, out)
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": "restore_failed", "restore": "restore_failed", "detail": str(exc)}


def run(action: str, call):
    """One nested scope; clipboard paste belongs inside run('paste', ...)."""
    if current() is not None:
        return call()
    from . import desktop
    module = desktop.active()
    if not enabled(module):
        # Even direct outer CLI scopes honour Escape on a private display.
        if action in owner_pause.INPUT_ACTIONS and owner_pause.blocked(action):
            return owner_pause.refusal(action)
        return call()
    with _ACTION_LOCK:
        try:
            with polite_state.locked(module.display_name()) as store:
                return _run(action, call, module, store)
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": "polite_state_unavailable", "detail": str(exc)}


def _run(action: str, call, module, store):
    state = Action(action, module, module.display_name(), store=store)
    token = _CURRENT.set(state)
    result = None
    failed = False
    try:
        _start(state, action in ("mouse_up", "key_up"))
        if action not in ("mouse_up", "key_up"):
            guard()
        result = call()
        failed = isinstance(result, dict) and result.get("ok") is False
    except Interrupted as exc:
        result = exc.result
        failed = True
    except Exception as exc:
        result = {"ok": False, "error": "backend_error", "detail": f"{type(exc).__name__}: {exc}", **state.progress}
        failed = True
    finally:
        try:
            _finish(state, failed)
        except Exception as exc:
            state.report.update(restore="restore_failed", restore_error=f"{type(exc).__name__}: {exc}")
        _CURRENT.reset(token)
    if isinstance(result, dict):
        result = dict(result)
        if state.interrupted is not None and action not in ("mouse_up", "key_up"):
            result.update(state.interrupted)
            result.update(state.progress)
        if state.report.get("restore") in ("restore_failed", "restore_unarmed") and result.get("ok"):
            result.update(ok=False, error="restore_failed", input_completed=True)
        result["polite"] = state.report
    return result


def action(name: str):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            return run(name, lambda: fn(*args, **kwargs))
        return wrapped
    return decorate
