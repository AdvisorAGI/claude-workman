"""Live acceptance confined to the existing agent X server :99.

Never accepts another display. Owns two scratch windows and temporary stamps;
does not start/stop a server or listener, and does not touch real owner state.
"""
from __future__ import annotations

import ctypes as C
import json
import os
from pathlib import Path
import tempfile
import threading
import time


def main():
    if os.environ.get("WORKMAN_DISPLAY") != ":99" or os.environ.get("DISPLAY") != ":99":
        raise SystemExit("Refusing: both WORKMAN_DISPLAY and DISPLAY must be :99")
    os.environ["XAUTHORITY"] = ""
    with tempfile.TemporaryDirectory(prefix="workman-polite-smoke-") as root:
        os.environ["WORKMAN_LEARN_ROOT"] = root
        os.environ["WORKMAN_STATE_DIR"] = root
        os.environ["WORKMAN_POLITE_MAX_WAIT_S"] = "0.06"
        os.environ["WORKMAN_POLITE_QUIET_S"] = "0.15"
        from workman import desktop, human, owner_pause, polite, server, xtest

        ch = xtest.channel(":99")
        assert ch is not None and ch.display_name == ":99"
        x, d = ch.x11, ch.dpy
        x.XCreateSimpleWindow.argtypes = [C.c_void_p, C.c_ulong, C.c_int, C.c_int,
                                         C.c_uint, C.c_uint, C.c_uint, C.c_ulong, C.c_ulong]
        x.XCreateSimpleWindow.restype = C.c_ulong
        x.XStoreName.argtypes = [C.c_void_p, C.c_ulong, C.c_char_p]
        x.XSelectInput.argtypes = [C.c_void_p, C.c_ulong, C.c_long]
        x.XMapWindow.argtypes = [C.c_void_p, C.c_ulong]
        x.XMoveWindow.argtypes = [C.c_void_p, C.c_ulong, C.c_int, C.c_int]
        x.XDestroyWindow.argtypes = [C.c_void_p, C.c_ulong]
        x.XLookupString.argtypes = [C.c_void_p, C.c_char_p, C.c_int, C.c_void_p, C.c_void_p]
        x.XLookupString.restype = C.c_int
        x.XSetInputFocus.argtypes = [C.c_void_p, C.c_ulong, C.c_int, C.c_ulong]

        class KeyEvent(C.Structure):
            _fields_ = [("type", C.c_int), ("serial", C.c_ulong), ("send_event", C.c_int),
                        ("display", C.c_void_p), ("window", C.c_ulong), ("root", C.c_ulong),
                        ("subwindow", C.c_ulong), ("time", C.c_ulong), ("x", C.c_int),
                        ("y", C.c_int), ("x_root", C.c_int), ("y_root", C.c_int),
                        ("state", C.c_uint), ("keycode", C.c_uint), ("same_screen", C.c_int)]

        windows = []
        old_pointer = ch.pointer_position()
        old_focus = ch.input_focus()
        reports = {}
        text = {}

        def focus(wid):
            x.XSetInputFocus(d, wid, 1, 0)
            x.XSync(d, 0)

        def pump():
            x.XSync(d, 0)
            event = xtest.XEvent()
            while x.XPending(d):
                x.XNextEvent(d, C.byref(event))
                if event.type == 2:
                    key = C.cast(C.byref(event), C.POINTER(KeyEvent)).contents
                    buf = C.create_string_buffer(64)
                    n = x.XLookupString(C.byref(event), buf, 63, None, None)
                    text[key.window] = text.get(key.window, "") + buf.raw[:n].decode("utf-8", "replace")

        def clear_presence():
            owner_pause._stamp_path(owner_pause.HUMAN_STAMP).unlink(missing_ok=True)
            owner_pause._last_mark["human"] = 0

        def interrupt_after(seconds, call):
            timer = threading.Timer(seconds, owner_pause.mark_human_input)
            timer.start()
            started = time.monotonic()
            try:
                result = call()
                elapsed = time.monotonic() - started
            finally:
                timer.join()
            assert result.get("error") == "human_active", result
            assert elapsed < seconds + 0.5, (elapsed, result)
            assert not ch.pointer_position()["buttons"], result
            return result

        try:
            for label, px in (("owner", 50), ("agent", 600)):
                wid = x.XCreateSimpleWindow(d, ch.root, px, 50, 400, 300, 0, 0, 0x334455)
                windows.append(wid)
                x.XStoreName(d, wid, f"Workman polite scratch {label}".encode())
                x.XSelectInput(d, wid, 1 | 4 | 8)
                x.XMapWindow(d, wid)
            x.XSync(d, 0)
            time.sleep(0.3)
            owner, agent = windows
            for wid, px in zip(windows, (50, 600)):
                x.XMoveWindow(d, wid, px, 50)
            x.XSync(d, 0)
            time.sleep(0.15)
            reports["fixture_frames"] = [ch._frame_rect(wid) for wid in windows]
            focus(owner)
            ch.move(200, 180)
            human.set_mode(False)
            os.environ.pop("WORKMAN_POLITE", None)
            owner_pause.mark_human_input()
            reports["private_default"] = desktop.press_key("a")
            assert reports["private_default"].get("ok"), reports
            pump()
            assert text.get(owner) == "a", text
            os.environ["WORKMAN_POLITE"] = "1"
            reports["start_refusal"] = desktop.type_text("must-not-arrive", delay_ms=0)
            assert reports["start_refusal"].get("error") == "human_active", reports
            pump()
            assert text.get(owner) == "a", text
            clear_presence()

            reports["burst_restore"] = server.batch([
                {"action": "focus_window", "query": str(agent), "learn_shortcuts": False},
                {"action": "move", "x": 720, "y": 160},
                {"action": "type_text", "text": "politeburst", "delay_ms": 0},
            ])
            pump()
            assert reports["burst_restore"].get("ok"), reports["burst_restore"]
            assert text.get(agent) == "politeburst", text
            assert ch.input_focus()["window"] == owner, ch.input_focus()
            assert (ch.pointer_position()["x"], ch.pointer_position()["y"]) == (200, 180)

            reports["click_restore"] = desktop.click(720, 160)
            time.sleep(0.12)  # Observe the WM's asynchronous click-focus handling.
            assert reports["click_restore"].get("ok"), reports["click_restore"]
            assert ch.input_focus()["window"] == owner, (ch.input_focus(), reports["click_restore"])

            focus(agent)
            before = text.get(agent, "")
            payload = "abcdefghijklmnopqrstuvwxyz0123456789abcd"
            reports["type_interrupt"] = interrupt_after(
                0.12, lambda: desktop.type_text(payload, delay_ms=35))
            pump()
            received = text.get(agent, "")[len(before):]
            result = reports["type_interrupt"]
            assert 0 < len(received) < len(payload), (received, result)
            assert received == payload[:len(received)], (received, result)
            assert result["typed_len"] == len(received), (received, result)
            assert result["remaining"] == payload[len(received):], result
            reports["typed_observed"] = len(received)

            clear_presence()
            reports["scroll_interrupt"] = interrupt_after(
                0.08, lambda: desktop.scroll("down", amount=5000))
            assert reports["scroll_interrupt"].get("remaining", 0) > 0, reports["scroll_interrupt"]
            assert reports["scroll_interrupt"].get("human_input_age_s") is not None, reports["scroll_interrupt"]
            clear_presence()
            reports["drag_interrupt"] = interrupt_after(
                0.08, lambda: desktop.drag(700, 200, 950, 300))
            clear_presence()
            reports["human_move_interrupt"] = interrupt_after(
                0.05, lambda: human.human_move(1100, 600))
            assert not ch.core_keys_down(), ch.core_keys_down()
            reports["verified"] = ["private display gate off", "start refusal", "focus+type burst",
                                   "pointer/focus restore", "exact partial text", "scroll stop",
                                   "drag stop/release", "human move stop"]
        finally:
            print(json.dumps(reports, indent=2), flush=True)
            # These resources and cleanup belong exclusively to this :99 probe.
            from workman import ghost_cursor
            ghost_cursor.hide(":99")
            focus(old_focus["window"])
            ch.move(old_pointer["x"], old_pointer["y"])
            for wid in windows:
                x.XDestroyWindow(d, wid)
            x.XSync(d, 0)


if __name__ == "__main__":
    main()
