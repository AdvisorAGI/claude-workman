"""V3 live acceptance on a parent-provided private :99 or :98 X server.

The parent runs this after core review. Never starts a display, changes XI
masters, or uses :0. Input stays in owned scratch windows. Separate processes
share temporary roots. Deferred restore is observed without manually invoking
it, including after the process which armed it exits.
"""
from __future__ import annotations

import argparse
import ctypes as C
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import tempfile
import threading
import time

REPO = Path(__file__).resolve().parents[1]
RESTORE_S = 1.2
QUIET_S = 0.15


def require_private_display() -> str:
    display = os.environ.get("DISPLAY")
    if display not in (":99", ":98") or os.environ.get("WORKMAN_DISPLAY") != display:
        raise SystemExit("Refusing before X access: DISPLAY and WORKMAN_DISPLAY must both be :99 or both be :98")
    return display


def require(condition, detail):
    # Acceptance checks must remain active under python -O.
    if not condition:
        raise AssertionError(detail)


def process_start(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def own_daemons() -> list[dict]:
    from workman import ghost_cursor, polite_restore
    return [{"pid": child.pid, "start": process_start(child.pid), "kind": module.__name__}
            for module in (ghost_cursor, polite_restore)
            for child in getattr(module, "_CHILDREN", []) if child.poll() is None]


def stop_own_daemons(entries: list[dict], root: Path, display: str):
    """Stop only recorded probe children with matching roots and PID identity."""
    for entry in {e["pid"]: e for e in entries}.values():
        pid = entry["pid"]
        if not entry["start"] or process_start(pid) != entry["start"]:
            continue
        try:
            env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
            expected = [f"WORKMAN_SMOKE_ROOT={root}".encode(),
                        f"WORKMAN_DISPLAY={display}".encode(), f"DISPLAY={display}".encode()]
            require(entry["kind"] in ("workman.ghost_cursor", "workman.polite_restore")
                    and all(item in env for item in expected) and entry["kind"].encode() in cmd,
                    {"unrecognized_probe_child": pid})
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, FileNotFoundError):
            if process_start(pid) != entry["start"]:
                continue
            raise
        deadline = time.monotonic() + 3
        stopped = False
        while time.monotonic() < deadline:
            if process_start(pid) != entry["start"]:
                stopped = True
                break
            try:
                if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                    stopped = True  # Exited child already released X resources.
                    break
            except OSError:
                stopped = True
                break
            time.sleep(0.02)
        require(stopped, {"probe_child_did_not_stop": pid})
    from workman import ghost_cursor, polite_restore
    recorded = {entry["pid"] for entry in entries}
    for module in (ghost_cursor, polite_restore):
        for child in getattr(module, "_CHILDREN", []):
            if child.pid in recorded:
                child.wait(timeout=1)  # Reap children launched by this process.


def worker(request: dict, display: str):
    # Validate roots before imports which could open X.
    raw_root = os.environ.get("WORKMAN_SMOKE_ROOT", "")
    require(bool(raw_root), "worker requires a parent-created smoke root")
    root = Path(raw_root).resolve()
    for name, suffix in (("WORKMAN_LEARN_ROOT", "learn"), ("WORKMAN_STATE_DIR", "state"),
                         ("XDG_RUNTIME_DIR", "runtime")):
        require(Path(os.environ.get(name, "/")).resolve() == root / suffix,
                f"worker refuses nonisolated {name}")
    fixture = json.loads((root / "fixture.json").read_text())
    require(fixture["display"] == display, "fixture/display mismatch")
    ids = fixture["windows"]
    from workman import desktop, handback, human, xtest
    human.set_mode(False)
    ch = xtest.channel(display)
    require(ch is not None and ch.display_name == display, "private XTEST unavailable")
    require(int(ch.top_level_focus().get("window", 0)) in ids,
            "worker focus left its scratch windows")
    op = request["op"]
    if op == "focus":
        require(request["window"] in ids, "worker refuses another window")
        result = desktop.focus_window(str(request["window"]), minimize_blockers=False)
    elif op == "type":
        result = desktop.type_text(request["text"], delay_ms=0)
    elif op == "click":
        px, py = request["point"]
        require(any(a <= px < a + w and b <= py < b + h for a, b, w, h in fixture["rects"]),
                "worker click left its scratch windows")
        result = desktop.click(px, py)
    elif op == "hand_back":
        result = handback.hand_back(close_launched_windows=False, park=False)
    else:
        raise ValueError(f"unknown smoke worker operation: {op}")
    return {"result": result, "daemons": own_daemons()}


class KeyEvent(C.Structure):
    _fields_ = [("type", C.c_int), ("serial", C.c_ulong), ("send_event", C.c_int),
                ("display", C.c_void_p), ("window", C.c_ulong), ("root", C.c_ulong),
                ("subwindow", C.c_ulong), ("time", C.c_ulong), ("x", C.c_int),
                ("y", C.c_int), ("x_root", C.c_int), ("y_root", C.c_int),
                ("state", C.c_uint), ("keycode", C.c_uint), ("same_screen", C.c_int)]


class PropertyEvent(C.Structure):
    _fields_ = [("type", C.c_int), ("serial", C.c_ulong), ("send_event", C.c_int),
                ("display", C.c_void_p), ("window", C.c_ulong), ("atom", C.c_ulong),
                ("time", C.c_ulong), ("state", C.c_int)]


class MessageData(C.Union):
    _fields_ = [("b", C.c_char * 20), ("s", C.c_short * 10), ("l", C.c_long * 5)]


class ClientMessage(C.Structure):
    _fields_ = [("type", C.c_int), ("serial", C.c_ulong), ("send_event", C.c_int),
                ("display", C.c_void_p), ("window", C.c_ulong), ("message_type", C.c_ulong),
                ("format", C.c_int), ("data", MessageData)]


class ClassHint(C.Structure):
    _fields_ = [("res_name", C.c_char_p), ("res_class", C.c_char_p)]


class Fixture:
    def __init__(self, ch, xtest):
        self.ch, self.xtest, self.x, self.d = ch, xtest, ch.x11, ch.dpy
        self.windows, self.text, self.buttons = [], {}, {}
        self.timestamp = 0
        bindings = {
            "XCreateSimpleWindow": (C.c_ulong, [C.c_void_p, C.c_ulong, C.c_int, C.c_int,
                                               C.c_uint, C.c_uint, C.c_uint, C.c_ulong, C.c_ulong]),
            "XStoreName": (C.c_int, [C.c_void_p, C.c_ulong, C.c_char_p]),
            "XSetClassHint": (C.c_int, [C.c_void_p, C.c_ulong, C.POINTER(ClassHint)]),
            "XSelectInput": (C.c_int, [C.c_void_p, C.c_ulong, C.c_long]),
            "XMapWindow": (C.c_int, [C.c_void_p, C.c_ulong]),
            "XMoveResizeWindow": (C.c_int, [C.c_void_p, C.c_ulong, C.c_int, C.c_int, C.c_uint, C.c_uint]),
            "XDestroyWindow": (C.c_int, [C.c_void_p, C.c_ulong]),
            "XLookupString": (C.c_int, [C.c_void_p, C.c_char_p, C.c_int, C.c_void_p, C.c_void_p]),
            "XChangeProperty": (C.c_int, [C.c_void_p, C.c_ulong, C.c_ulong, C.c_ulong,
                                          C.c_int, C.c_int, C.c_void_p, C.c_int]),
            "XSendEvent": (C.c_int, [C.c_void_p, C.c_ulong, C.c_int, C.c_long, C.c_void_p]),
            "XTranslateCoordinates": (C.c_int, [C.c_void_p, C.c_ulong, C.c_ulong, C.c_int, C.c_int,
                                                C.POINTER(C.c_int), C.POINTER(C.c_int), C.POINTER(C.c_ulong)]),
        }
        for name, (result, args) in bindings.items():
            fn = getattr(self.x, name)
            fn.restype, fn.argtypes = result, args

    def create(self):
        width, height = self.ch.screen_size()
        require(width >= 800 and height >= 500, {"private_display_too_small": [width, height]})
        self.w, self.h = min(400, (width - 160) // 2), min(300, height - 160)
        for label, px in (("owner", 40), ("agent", width // 2 + 30)):
            wid = self.x.XCreateSimpleWindow(self.d, self.ch.root, px, 60, self.w, self.h, 0, 0, 0x334455)
            require(bool(wid), "scratch window creation failed")
            self.windows.append(wid)
            self.x.XStoreName(self.d, wid, f"Workman polite smoke {os.getpid()} {label}".encode())
            hint = ClassHint(b"polite-smoke", b"WorkmanPoliteSmoke")
            self.x.XSetClassHint(self.d, wid, C.byref(hint))
            self.x.XSelectInput(self.d, wid, 1 | 4 | 8 | (1 << 22))
            self.x.XMapWindow(self.d, wid)
        self.x.XSync(self.d, 0)
        time.sleep(0.4)
        for wid, px in zip(self.windows, (40, width // 2 + 30)):
            self.x.XMoveResizeWindow(self.d, wid, px, 60, self.w, self.h)
        self.x.XSync(self.d, 0)
        time.sleep(0.2)
        return self.windows

    def pump(self):
        self.x.XSync(self.d, 0)
        event = self.xtest.XEvent()
        while self.x.XPending(self.d):
            self.x.XNextEvent(self.d, C.byref(event))
            if event.type == 2:
                key = C.cast(C.byref(event), C.POINTER(KeyEvent)).contents
                if key.window in self.windows:
                    buf = C.create_string_buffer(64)
                    size = self.x.XLookupString(C.byref(event), buf, 63, None, None)
                    self.text[key.window] = self.text.get(key.window, "") + buf.raw[:size].decode("utf-8", "replace")
            elif event.type == 4:
                button = C.cast(C.byref(event), C.POINTER(KeyEvent)).contents
                key = (button.window, button.keycode)
                self.buttons[key] = self.buttons.get(key, 0) + 1
            elif event.type == 28:
                prop = C.cast(C.byref(event), C.POINTER(PropertyEvent)).contents
                if prop.window in self.windows:
                    self.timestamp = int(prop.time)

    def rect(self, wid):
        require(wid in self.windows, "fixture geometry is scratch-only")
        px, py, child = C.c_int(), C.c_int(), C.c_ulong()
        require(self.x.XTranslateCoordinates(self.d, wid, self.ch.root, 0, 0,
                                            C.byref(px), C.byref(py), C.byref(child)), "scratch geometry unavailable")
        return px.value, py.value, self.w, self.h

    def point(self, wid, fx=0.5, fy=0.5):
        px, py, width, height = self.rect(wid)
        return px + int(width * fx), py + int(height * fy)

    def focus(self, wid):
        """Fixture owner focus via EWMH source 2 and a fresh server timestamp."""
        require(wid in self.windows, "fixture focus is scratch-only")
        self.timestamp = 0
        value = C.c_ubyte(1)
        self.x.XChangeProperty(self.d, wid, self.ch._atom("_WORKMAN_SMOKE_TIME"),
                               self.ch._atom("INTEGER"), 8, 0, C.byref(value), 1)
        deadline = time.monotonic() + 1
        while not self.timestamp and time.monotonic() < deadline:
            self.pump()
            time.sleep(0.005)
        require(self.timestamp > 0, "no nonzero fixture server timestamp")
        message = ClientMessage(type=33, display=self.d, window=wid,
                                message_type=self.ch._atom("_NET_ACTIVE_WINDOW"), format=32)
        message.data.l[0], message.data.l[1] = 2, self.timestamp
        active = self.ch.active_window_info()
        message.data.l[2] = int(active.get("window", active.get("id", 0)))
        event = self.xtest.XEvent()
        C.memmove(C.byref(event), C.byref(message), C.sizeof(message))
        require(self.x.XSendEvent(self.d, self.ch.root, False, (1 << 19) | (1 << 20), C.byref(event)),
                "EWMH fixture focus request failed")
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            self.pump()
            if self.focused() == wid:
                return
            time.sleep(0.01)
        raise AssertionError({"fixture_focus_failed": wid, "readback": self.ch.focused_window_identity()})

    def focused(self):
        return int(self.ch.top_level_focus().get("window", 0))

    def pointer(self):
        p = self.ch.pointer_position()
        return int(p["x"]), int(p["y"])

    def close(self):
        # Do not focus or move to a pre-existing, unowned window during cleanup.
        for wid in self.windows:
            self.x.XDestroyWindow(self.d, wid)
        self.x.XSync(self.d, 0)


def smoke(display: str, root: Path):
    from workman import desktop, ghost_cursor, handback, human, owner_pause, polite, xtest
    ch = xtest.channel(display)
    require(ch is not None and ch.display_name == display, "private XTEST channel unavailable")
    initial = ch.xtest_held()
    require(not initial.get("keys") and not initial.get("buttons") and not ch.core_keys_down()
            and not ch.pointer_position()["buttons"], "private display has pre-existing holds; refusing fixture input")
    fixture = Fixture(ch, xtest)
    reports = {"display": display, "restore_after_s": RESTORE_S, "checks": [], "ok": False}
    daemons = []

    def call_process(op, **kwargs):
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker",
                               json.dumps(dict(op=op, **kwargs))], cwd=REPO, env=os.environ.copy(),
                              capture_output=True, text=True, timeout=12)
        try:
            output = json.loads(proc.stdout)
        except ValueError:
            raise AssertionError({"worker_exit": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr})
        daemons.extend(output.get("daemons", []))
        require(proc.returncode == 0, {"worker_exit": proc.returncode, "output": output, "stderr": proc.stderr})
        return output["result"]

    def check_ok(name, result):
        reports[name] = result
        require(isinstance(result, dict) and result.get("ok"), {name: result})
        return result

    def clear_presence():
        owner_pause._stamp_path(owner_pause.HUMAN_STAMP).unlink(missing_ok=True)
        owner_pause._last_mark["human"] = 0

    def no_holds():
        holds = ch.xtest_held()
        require(not holds.get("keys") and not holds.get("buttons"), {"XTEST_still_held": holds})
        require(not ch.core_keys_down(), {"core_keys_down": ch.core_keys_down()})
        require(not ch.pointer_position()["buttons"], "core pointer buttons still held")
        return holds

    def hand_back(name, cross_process=False):
        result = (call_process("hand_back") if cross_process else
                  handback.hand_back(close_launched_windows=False, park=False))
        check_ok(name, result)
        no_holds()
        return result

    def prepare_owner():
        clear_presence()
        hand_back("fixture_reset")
        fixture.focus(owner)
        point = fixture.point(owner)
        ch.move(*point)
        ch.flush()
        # Fixture owner motion is setup between cases, rather than an event
        # under test. Forget only this process's previous agent placement.
        from workman import x11
        x11._LAST_PLACED["at"] = None
        return point

    def hold_focus(seconds, window):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            fixture.pump()
            require(fixture.focused() == window,
                    {"restored_too_soon": fixture.ch.focused_window_identity(), "expected": window})
            time.sleep(0.01)

    def await_restore(point, name):
        deadline = time.monotonic() + RESTORE_S + 2
        while time.monotonic() < deadline:
            fixture.pump()
            if fixture.focused() == owner and fixture.pointer() == point:
                reports[name] = {"focus": owner, "pointer": list(point), "verified": True}
                no_holds()
                return
            time.sleep(0.02)
        raise AssertionError({"deferred_restore_missing": name, "focus": fixture.ch.focused_window_identity(),
                              "pointer": fixture.pointer(), "expected_pointer": point,
                              "dependency": "private forced-polite scheduler must survive subprocess exit"})

    def interrupt_after(seconds, call, name):
        clear_presence()
        fired = threading.Event()

        def interrupt():
            owner_pause.mark_human_input()
            fired.set()

        timer = threading.Timer(seconds, interrupt)
        timer.start()
        started = time.monotonic()
        try:
            result = call()
            elapsed = time.monotonic() - started
        finally:
            timer.join(timeout=1)
        reports[name] = result
        require(fired.is_set() and result.get("error") == "human_active", {name: result})
        require(elapsed < seconds + 0.6, {"interrupt_latency_s": elapsed, name: result})
        reports[f"{name}_holds"] = no_holds()
        return result

    try:
        owner, agent = fixture.create()
        rects = [fixture.rect(wid) for wid in fixture.windows]
        (root / "fixture.json").write_text(json.dumps({"display": display, "windows": fixture.windows, "rects": rects}))
        reports["fixture_rects"] = rects
        fixture.focus(owner)
        ch.move(*fixture.point(owner))
        human.set_mode(False)
        os.environ.pop("WORKMAN_POLITE", None)
        owner_pause.mark_human_input()
        check_ok("private_default_ignores_stamp", desktop.type_text("default", delay_ms=0))
        fixture.pump()
        require(fixture.text.get(owner) == "default", fixture.text)
        owner_pause.pause_from_escape()  # Simulated Escape in the temporary learn root only.
        before = dict(fixture.text)
        reports["private_default_escape_type"] = desktop.type_text("mustnotarrive", delay_ms=0)
        point = fixture.pointer()
        reports["private_default_escape_move"] = desktop.move(*fixture.point(agent))
        fixture.pump()
        for name in ("private_default_escape_type", "private_default_escape_move"):
            require(reports[name].get("error") == "input_disabled", reports[name])
        require(fixture.text == before and fixture.pointer() == point, "paused default private display injected input")
        owner_pause.resume()  # Resume only our simulated private-root pause.
        clear_presence()
        reports["checks"].extend(["private default ignores stamps", "private default honors Escape"])

        os.environ["WORKMAN_POLITE"] = "1"  # Intentional private-display v3 acceptance only.
        owner_pause.mark_human_input()
        reports["start_refusal"] = desktop.type_text("mustnotarrive", delay_ms=0)
        fixture.pump()
        require(reports["start_refusal"].get("error") == "human_active", reports["start_refusal"])
        require(fixture.text == before, "start refusal sent text")
        clear_presence()

        point = prepare_owner()
        check_ok("separate_focus", desktop.focus_window(str(agent), minimize_blockers=False))
        check_ok("separate_move", desktop.move(*fixture.point(agent)))
        check_ok("separate_type", desktop.type_text("separate", delay_ms=0))
        fixture.pump()
        require(fixture.text.get(agent) == "separate" and fixture.text.get(owner) == "default", fixture.text)
        hold_focus(RESTORE_S * 0.2, agent)
        await_restore(point, "separate_deferred_restore")
        reports["checks"].append("separate focus then type; deferred verified focus/pointer restore")

        point = prepare_owner()
        check_ok("separate_click", desktop.click(*fixture.point(agent)))
        check_ok("click_then_type", desktop.type_text("click", delay_ms=0))
        fixture.pump()
        require(fixture.text.get(agent) == "separateclick" and fixture.focused() == agent, fixture.text)
        started = time.monotonic()
        hand_back("immediate_hand_back")
        require(time.monotonic() - started < RESTORE_S * 0.75, "hand_back waited for deferred idle")
        require(fixture.focused() == owner and fixture.pointer() == point, "hand_back did not restore immediately")
        reports["checks"].append("click then type; hand_back immediate restore")

        prepare_owner()
        check_ok("mismatch_target", desktop.focus_window(str(agent), minimize_blockers=False))
        fixture.focus(owner)  # Simulated owner change, both windows belong to us.
        fixture.pump()
        before = dict(fixture.text)
        reports["target_mismatch"] = desktop.type_text("mustnotarrive", delay_ms=0)
        fixture.pump()
        require(reports["target_mismatch"].get("error") == "focus_changed", reports["target_mismatch"])
        require(fixture.text == before, {"focus_changed_sent_text": fixture.text, "before": before})
        require(reports["target_mismatch"].get("typed_len", 0) == 0, reports["target_mismatch"])
        mismatch = reports["target_mismatch"]
        require(mismatch.get("intended_window", {}).get("window") == agent
                and mismatch.get("current_window", {}).get("window") == owner, mismatch)
        reports["process_target_mismatch"] = call_process("type", text="mustnotarrive")
        fixture.pump()
        require(reports["process_target_mismatch"].get("error") == "focus_changed"
                and fixture.text == before, reports["process_target_mismatch"])
        hold_focus(RESTORE_S + 0.15, owner)
        reports["checks"].append("target mismatch refuses with no text; owner focus survives stale restore")

        prepare_owner()
        check_ok("owner_change_pending_click", desktop.click(*fixture.point(agent)))
        fixture.focus(owner)
        changed_point = fixture.point(owner, 0.7, 0.7)
        ch.move(*changed_point)  # Simulated owner motion, still in our scratch window.
        ch.flush()
        owner_pause.mark_human_input()
        deadline = time.monotonic() + RESTORE_S + 0.2
        while time.monotonic() < deadline:
            fixture.pump()
            require(fixture.focused() == owner and fixture.pointer() == changed_point,
                    "stale pending restore overwrote simulated owner focus/pointer")
            time.sleep(0.02)
        reports["checks"].append("pending restore leaves subsequent owner focus/pointer changes intact")

        point = prepare_owner()
        check_ok("process_focus", call_process("focus", window=agent))
        check_ok("process_click", call_process("click", point=list(fixture.point(agent))))
        check_ok("process_type", call_process("type", text="process"))
        fixture.pump()
        require(fixture.text.get(agent) == "separateclickprocess", fixture.text)
        hold_focus(RESTORE_S * 0.65, agent)
        check_ok("process_debounce_type", call_process("type", text="debounce"))
        hold_focus(RESTORE_S * 0.5, agent)  # Past old deadline, before new deadline.
        await_restore(point, "process_debounce_restore")
        fixture.pump()
        require(fixture.text.get(agent) == "separateclickprocessdebounce", fixture.text)
        reports["checks"].append("cross-process focus/type and new action resets restore debounce")

        point = prepare_owner()
        check_ok("process_handback_focus", call_process("focus", window=agent))
        check_ok("process_handback_click", call_process("click", point=list(fixture.point(agent))))
        started = time.monotonic()
        hand_back("process_immediate_hand_back", cross_process=True)
        require(time.monotonic() - started < RESTORE_S, "cross-process hand_back waited for idle deadline")
        require(fixture.focused() == owner and fixture.pointer() == point, "cross-process hand_back missed original snapshot")
        reports["checks"].append("cross-process hand_back restores original burst snapshot")

        prepare_owner()
        check_ok("interrupt_target", desktop.focus_window(str(agent), minimize_blockers=False))
        check_ok("interrupt_pointer", desktop.move(*fixture.point(agent)))
        fixture.pump()
        before = fixture.text.get(agent, "")
        payload = "abcdefghijklmnopqrstuvwxyz0123456789abcd"
        result = interrupt_after(0.12, lambda: desktop.type_text(payload, delay_ms=35), "type_interrupt")
        fixture.pump()
        received = fixture.text.get(agent, "")[len(before):]
        require(0 < len(received) < len(payload) and received == payload[:len(received)], {"received": received, "result": result})
        require(result.get("typed_len") == len(received) and result.get("remaining") == payload[len(received):], result)
        reports["typed_observed"] = len(received)

        clear_presence()
        hand_back("after_type_interrupt")
        check_ok("scroll_target", desktop.click(*fixture.point(agent)))
        fixture.pump()
        prior_scroll = fixture.buttons.get((agent, 5), 0)
        result = interrupt_after(0.08, lambda: desktop.scroll("down", amount=5000), "scroll_interrupt")
        fixture.pump()
        sent = fixture.buttons.get((agent, 5), 0) - prior_scroll
        require(0 < sent < 5000 and result.get("amount") == sent and result.get("remaining") == 5000 - sent,
                {"scroll_observed": sent, "result": result})
        require(result.get("human_input_age_s") is not None, result)

        clear_presence()
        hand_back("after_scroll_interrupt")
        check_ok("drag_target", desktop.focus_window(str(agent), minimize_blockers=False))
        start, end = fixture.point(agent, 0.25, 0.3), fixture.point(agent, 0.75, 0.7)
        result = interrupt_after(0.04, lambda: desktop.drag(*start, *end), "drag_interrupt")
        require(result.get("dragged") is False and result.get("at") == list(start), result)

        clear_presence()
        hand_back("after_drag_interrupt")
        check_ok("human_move_target", desktop.focus_window(str(agent), minimize_blockers=False))
        check_ok("human_move_start", desktop.move(*start))
        result = interrupt_after(0.04, lambda: polite.run("move", lambda: human.human_move(*end, rng=random.Random(2))),
                                 "human_move_interrupt")
        require(0 < result.get("points_done", 0) < result.get("points_total", 0), result)
        require(result.get("at") == list(fixture.pointer()) and fixture.pointer() != end, result)
        reports["checks"].extend(["exact partial typing", "exact partial scroll", "drag interruption releases holds",
                                   "human move interruption with partial progress"])

        clear_presence()
        hand_back("after_move_interrupt")
        check_ok("holds_target", desktop.focus_window(str(agent), minimize_blockers=False))
        check_ok("holds_pointer", desktop.move(*fixture.point(agent)))
        # Deliberately bypass local tracking, only on our private fixture.
        code, _ = ch.keycode_for(ch.keysym("Shift_L"))
        require(ch.xtst.XTestFakeKeyEvent(ch.dpy, code, 1, 0), "fixture key hold failed")
        require(ch.xtst.XTestFakeButtonEvent(ch.dpy, 1, 1, 0), "fixture button hold failed")
        ch.flush()
        inherited = ch.xtest_held()
        require(code in inherited.get("keys", []) and 1 in inherited.get("buttons", []), inherited)
        reports["untracked_holds_before"] = inherited
        fixture.pump()
        before = dict(fixture.text)
        reports["untracked_holds_interrupt"] = desktop.type_text("mustnotarrive", delay_ms=0)
        fixture.pump()
        require(reports["untracked_holds_interrupt"].get("error") == "human_active"
                and fixture.text == before, reports["untracked_holds_interrupt"])
        reports["untracked_holds_interrupt_after"] = no_holds()
        require(ch.xtst.XTestFakeKeyEvent(ch.dpy, code, 1, 0), "fixture second key hold failed")
        require(ch.xtst.XTestFakeButtonEvent(ch.dpy, 1, 1, 0), "fixture second button hold failed")
        ch.flush()
        hand_back("untracked_holds_hand_back")
        reports["untracked_holds_after"] = no_holds()
        reports["checks"].append("interrupt and hand_back release server-reported untracked XTEST holds")

        # Parent-requested integration check of actual XI2 attribution. Start
        # a new listener only here, after all raw fixture setup is finished.
        prepare_owner()
        listener_code = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
                         "from workman.esc_pause import main; raise SystemExit(main())")
        listener = subprocess.Popen([sys.executable, "-c", listener_code, str(REPO), "--display", display],
                                    cwd=REPO, env=os.environ.copy(), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
        foreign = None
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and listener.poll() is None:
                try:
                    ready = int((root / "learn" / "esc-listener.pid").read_text()) == listener.pid
                except (OSError, ValueError):
                    ready = False
                if ready:
                    break
                time.sleep(0.02)
            else:
                raise AssertionError({"private_listener_not_ready": listener.poll()})
            time.sleep(0.08)
            reports["private_listener_pid"] = listener.pid
            check_ok("listener_agent_move", desktop.move(*fixture.point(agent, 0.4, 0.4)))
            check_ok("listener_agent_click", desktop.click(*fixture.point(agent)))
            check_ok("listener_agent_type", desktop.type_text("listener", delay_ms=15))
            time.sleep(0.06)  # Allow delayed XI2 events to reach this listener.
            require(owner_pause.human_input_stamp() is None, "listener misclassified this connection's own events")
            fixture.pump()
            before = fixture.text.get(agent, "")
            foreign = xtest.Channel(display)  # Separate, unclaimed private XTEST connection.
            f8_code, _ = foreign.keycode_for(foreign.keysym("F8"))
            fired, failures = threading.Event(), []

            def foreign_tap():
                try:
                    # F8 has no text output in our scratch window. No event
                    # claims or presence stamps accompany this simulated KVM tap.
                    require(foreign.xtst.XTestFakeKeyEvent(foreign.dpy, f8_code, 1, 0), "foreign F8 press failed")
                    require(foreign.xtst.XTestFakeKeyEvent(foreign.dpy, f8_code, 0, 0), "foreign F8 release failed")
                    foreign.flush()
                    fired.set()
                except Exception as exc:
                    failures.append(str(exc))

            timer = threading.Timer(0.12, foreign_tap)
            timer.start()
            started = time.monotonic()
            try:
                result = desktop.type_text(payload, delay_ms=35)
                elapsed = time.monotonic() - started
            finally:
                timer.join(timeout=1)
            fixture.pump()
            received = fixture.text.get(agent, "")[len(before):]
            reports["listener_foreign_xtest_interrupt"] = result
            require(fired.is_set() and not failures, {"foreign_tap_failures": failures})
            require(result.get("error") == "human_active" and elapsed < 0.72, result)
            require(0 < len(received) < len(payload) and received == payload[:len(received)]
                    and result.get("typed_len") == len(received)
                    and result.get("remaining") == payload[len(received):], {"observed": received, "result": result})
            reports["listener_interrupt_holds"] = no_holds()
            reports["checks"].append("private XI2 listener accepts owned events and interrupts on unclaimed XTEST F8")
        finally:
            if listener.poll() is None:
                listener.terminate()  # Only the new listener Popen owned by this test.
            stdout, stderr = listener.communicate(timeout=5)
            reports["private_listener_exit"] = listener.returncode
            reports["private_listener_log"] = (stdout + stderr)[-3000:]
            if foreign is not None:
                foreign.close()
        reports["ok"] = True
    finally:
        try:
            daemons.extend(own_daemons())
            stop_own_daemons(daemons, root, display)
        except Exception as exc:
            reports["ok"] = False
            reports["cleanup_error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            try:
                clear_presence()
                reports["cleanup_release"] = handback.release_all()
                ghost_cursor.hide(display)
                require(reports["cleanup_release"].get("ok"), reports["cleanup_release"])
                reports["cleanup_holds"] = no_holds()
            except Exception as exc:
                reports["ok"] = False
                reports["cleanup_error"] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                fixture.close()
                ch.close()
                print(json.dumps(reports, indent=2), flush=True)


def main():
    display = require_private_display()  # Before imports, subprocesses or any X access.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    args = parser.parse_args()
    sys.path.insert(0, str(REPO))  # Keep the editable live install out of this probe.
    if args.worker is not None:
        print(json.dumps(worker(json.loads(args.worker), display)), flush=True)
        return
    with tempfile.TemporaryDirectory(prefix="workman-polite-smoke-") as directory:
        root = Path(directory)
        for suffix in ("learn", "state", "runtime"):
            (root / suffix).mkdir(mode=0o700)
        os.environ.update(WORKMAN_SMOKE_ROOT=str(root), WORKMAN_LEARN_ROOT=str(root / "learn"),
                          WORKMAN_STATE_DIR=str(root / "state"), XDG_RUNTIME_DIR=str(root / "runtime"),
                          WORKMAN_CURSOR_FIFO=str(root / "cursor.fifo"), WORKMAN_BACKEND="linux",
                          WORKMAN_XTEST="1", WORKMAN_HUMAN_MODE="0", WORKMAN_HUMAN_SPEED="1",
                          WORKMAN_POLITE_MAX_WAIT_S="0.06", WORKMAN_POLITE_QUIET_S=str(QUIET_S),
                          WORKMAN_POLITE_RESTORE_AFTER_S=str(RESTORE_S))
        smoke(display, root)


if __name__ == "__main__":
    main()
