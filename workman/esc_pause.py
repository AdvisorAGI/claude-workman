"""Listen-only Escape: pause agent mouse/keyboard, do not steal the key.

Physical Escape with no modifiers writes the owner-pause switch OFF and lets
go of everything the XTEST devices (every agent) were holding, so the owner
gets the machine exactly as it is: nothing stuck, nothing undone. The event
is not consumed, so Escape still works in the focused app. Resume is explicit
(`--resume`, `input_control(action='resume')`, or Workman Input Controls
"Enable both"), never a second Escape.

The same listener is what makes agents yield: every physical mouse or key
event touches the human-input stamp that `owner_pause.human_active()` reads,
so an agent action waits for the desk to go quiet before injecting.

Linux reads XInput2 raw events straight from the X connection (no xinput
subprocess, no text parsing, no polling: the process sleeps in XNextEvent).
Raw events name their source device, so keys injected through XTEST are told
apart from a real keyboard. The owner's Mac keyboard arrives through the
Deskflow KVM as XTEST too, so an XTEST event is his unless a workman process
stamped an injection in the last second (`owner_pause.agent_claims`).
XRecord cannot do any of that: it reports an injected Escape exactly like a
pressed one, and an agent's own Escape would pause every agent mid-task.

macOS needs Accessibility for the process that runs this (Terminal, Workman,
or the LaunchAgent).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time

from . import owner_pause

MAC_ESCAPE = 53
X11_ESCAPE = 9
# evdev keycodes for Shift, Control, Alt and Super, left and right.
X11_MODIFIERS = {50, 62, 37, 105, 64, 108, 133, 134}


def xtest_device_ids(listing: str) -> set[int]:
    """Ids of the XTEST devices in `xinput list` output: the ones xdotool and
    agents inject through."""
    ids = set()
    for line in listing.splitlines():
        if "XTEST" in line:
            m = re.search(r"\bid=(\d+)", line)
            if m:
                ids.add(int(m.group(1)))
    return ids


class RawKeyWatcher:
    """Folds `xinput test-xi2 --root` lines into physical Escape presses.

    Raw events print `EVENT type 13 (RawKeyPress)`, then `device: 3 (9)`
    where the number in brackets is the source device, then `detail: 9`.
    Kept as the fallback path for a box whose libXi cannot be loaded.
    """

    def __init__(self, injected: set[int]):
        self.injected = set(injected)
        self.held: set[int] = set()
        self._kind: str | None = None
        self._source: int | None = None

    def feed(self, line: str) -> bool:
        """True when this line completes a physical, unmodified Escape press."""
        line = line.strip()
        if line.startswith("EVENT type"):
            self._kind = ("press" if "(RawKeyPress)" in line
                          else "release" if "(RawKeyRelease)" in line else None)
            self._source = None
            return False
        if self._kind is None:
            return False
        if line.startswith("device:"):
            m = re.search(r"\((\d+)\)", line)
            self._source = int(m.group(1)) if m else None
            return False
        if not line.startswith("detail:"):
            return False
        kind, source = self._kind, self._source
        self._kind = None
        if source is None or source in self.injected:
            return False
        try:
            code = int(line.split()[1])
        except (IndexError, ValueError):
            return False
        if code in X11_MODIFIERS:
            if kind == "press":
                self.held.add(code)
            else:
                self.held.discard(code)
            return False
        return kind == "press" and code == X11_ESCAPE and not self.held


class RawInputWatcher:
    """Decides, per raw XInput2 event, whether the owner is at the desk and
    whether he just pressed Escape.

    `feed(event)` takes {"kind", "source", "detail"} as `xtest.Channel.
    next_raw_event` returns it and answers "escape" | "human" | "agent" |
    None. Physical devices are always the owner. XTEST devices are the owner
    too (Deskflow KVM) unless a workman process claimed an injection within
    the last second; `claims` is injected so tests do not need the stamps.
    """

    def __init__(self, injected: set[int], claims=None):
        self.injected = set(injected)
        self.held: set[int] = set()
        self.claims = claims or owner_pause.agent_claims

    def feed(self, ev: dict | None) -> str | None:
        if not ev:
            return None
        kind, source, code = ev.get("kind"), ev.get("source"), ev.get("detail")
        if source in self.injected:
            if kind == "key_press" and code == X11_ESCAPE:
                if self.claims(escape=True):
                    return "agent"
            elif self.claims(escape=False):
                return "agent"
        # From here the event is the owner's, physical or KVM-relayed.
        if kind in ("key_press", "key_release") and code in X11_MODIFIERS:
            if kind == "key_press":
                self.held.add(code)
            else:
                self.held.discard(code)
            return "human"
        if kind == "key_press" and code == X11_ESCAPE and not self.held:
            return "escape"
        return "human"


def _log(msg: str) -> None:
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def release_agent_holds(display: str | None = None) -> dict:
    """Let go of every key and button the XTEST devices hold."""
    try:
        from . import xtest
        ch = xtest.channel(display)
        if ch is None:
            return {"ok": False, "error": "no XTest channel"}
        out = ch.release_xtest_held()
        out["ok"] = True
        return out
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _on_physical_escape(display: str | None = None) -> dict:
    state = owner_pause.pause_from_escape()
    released = release_agent_holds(display)
    _log("owner-pause: Escape paused agent input " + json.dumps(state)
         + " released=" + json.dumps(released, default=str))
    return {"state": state, "released": released}


def _pid_path():
    return owner_pause._root() / "esc-listener.pid"


def _write_pid() -> None:
    try:
        p = _pid_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(__import__("os").getpid()))
    except OSError:
        pass


def listener_pid() -> int | None:
    """Pid of a running listener, or None."""
    import os
    try:
        pid = int(_pid_path().read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def start_with_input_on() -> dict:
    """A fresh listener starts with the switches ON when the previous pause
    was Escape's (it is the one that owns that state); a pause the owner set
    by hand in Workman Input Controls is left alone."""
    s = owner_pause.state()
    if (not s["mouse"] or not s["keyboard"]) and s.get("paused_by") == "escape":
        return owner_pause.resume()
    return s


def run_darwin() -> int:
    try:
        import Quartz as Q
    except ImportError:
        _log("owner-pause: Quartz missing; pip install pyobjc-framework-Quartz")
        return 1

    mask = (Q.CGEventMaskBit(Q.kCGEventKeyDown) | Q.CGEventMaskBit(Q.kCGEventMouseMoved)
            | Q.CGEventMaskBit(Q.kCGEventLeftMouseDown) | Q.CGEventMaskBit(Q.kCGEventRightMouseDown)
            | Q.CGEventMaskBit(Q.kCGEventScrollWheel) | Q.CGEventMaskBit(Q.kCGEventLeftMouseDragged))

    def callback(proxy, etype, event, refcon):
        source_pid = Q.CGEventGetIntegerValueField(event, Q.kCGEventSourceUnixProcessID)
        if source_pid:
            # Posted by a process (an agent). The owner's devices report 0.
            return event
        owner_pause.mark_human_input()
        if etype != Q.kCGEventKeyDown:
            return event
        keycode = Q.CGEventGetIntegerValueField(event, Q.kCGKeyboardEventKeycode)
        if keycode != MAC_ESCAPE:
            return event
        if Q.CGEventGetIntegerValueField(event, Q.kCGKeyboardEventAutorepeat):
            return event
        flags = int(Q.CGEventGetFlags(event))
        # Ignore if Shift/Ctrl/Alt/Command are down. Device flags (0xFFFF0000) stay.
        if flags & 0xFFFF:
            return event
        _on_physical_escape()
        return event

    tap = Q.CGEventTapCreate(
        Q.kCGSessionEventTap,
        Q.kCGHeadInsertEventTap,
        Q.kCGEventTapOptionListenOnly,
        mask,
        callback,
        None,
    )
    if not tap:
        _log(
            "owner-pause: event tap failed. Grant Accessibility to the app "
            "running this process in System Settings > Privacy & Security > "
            "Accessibility, then restart it."
        )
        return 2
    start_with_input_on()
    _write_pid()
    source = Q.CFMachPortCreateRunLoopSource(None, tap, 0)
    Q.CFRunLoopAddSource(Q.CFRunLoopGetCurrent(), source, Q.kCFRunLoopCommonModes)
    Q.CGEventTapEnable(tap, True)
    _log("owner-pause: Escape will pause agent input. Resume with --resume.")
    Q.CFRunLoopRun()
    return 0


def run_x11_native(display: str | None = None) -> int:
    """XInput2 raw events on the X connection itself. Returns 1 when the
    channel cannot be had so the caller can fall back to xinput."""
    try:
        from . import xtest
        ch = xtest.Channel(display)
        ch.select_raw_events()
        injected = ch.xtest_device_ids()
    except Exception as exc:
        _log(f"owner-pause: native XI2 listener unavailable ({exc}); trying xinput")
        return 1
    if not injected:
        _log("owner-pause: no XTEST device found, so agent keys cannot be told "
             "from yours; not listening. Use Workman Input Controls STOP BOTH")
        return 1
    watcher = RawInputWatcher(injected)
    start_with_input_on()
    _write_pid()
    _log(f"owner-pause: listening on {ch.display_name} (XI2 raw, no grab); XTEST ids "
         f"{sorted(injected)}. Escape pauses agent input; resume with --resume.")
    import os
    debug = os.environ.get("WORKMAN_ESC_DEBUG") == "1"
    while True:
        try:
            ev = ch.next_raw_event()
        except xtest.ChannelError as exc:
            _log(f"owner-pause: {exc}; exiting so the service restarts")
            return 3
        verdict = watcher.feed(ev)
        if debug and ev:
            _log(f"owner-pause[debug]: {verdict} {ev}")
        if verdict == "human":
            owner_pause.mark_human_input()
        elif verdict == "escape":
            owner_pause.mark_human_input()
            _on_physical_escape(ch.display_name)


def run_x11_xinput() -> int:
    if not shutil.which("xinput"):
        _log("owner-pause: xinput missing (apt install xinput); "
             "use Workman Input Controls STOP BOTH")
        return 1
    try:
        listing = subprocess.run(["xinput", "list"], capture_output=True,
                                 text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        _log(f"owner-pause: xinput list failed: {exc}")
        return 1
    injected = xtest_device_ids(listing)
    if not injected:
        _log("owner-pause: no XTEST device found, so agent keys cannot be told "
             "from yours; not listening. Use Workman Input Controls STOP BOTH")
        return 1
    cmd = ["xinput", "test-xi2", "--root"]
    if shutil.which("stdbuf"):
        # Piped, xinput buffers by the kilobyte and a press would arrive late.
        cmd = ["stdbuf", "-oL", *cmd]
    watcher = RawKeyWatcher(injected)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    start_with_input_on()
    _write_pid()
    _log("owner-pause: Escape will pause agent input (xinput fallback). Resume with --resume.")
    try:
        for line in proc.stdout:
            if line.startswith("EVENT type") and "Raw" in line:
                owner_pause.mark_human_input()
            if watcher.feed(line):
                _on_physical_escape()
    finally:
        proc.terminate()
    return proc.wait()


def run_x11(display: str | None = None) -> int:
    rc = run_x11_native(display)
    if rc != 1:
        return rc
    return run_x11_xinput()


def status(display: str | None = None) -> dict:
    out = owner_pause.status()
    out["listener_pid"] = listener_pid()
    try:
        from . import xtest
        ch = xtest.channel(display)
        if ch is not None:
            out["agent_held"] = ch.xtest_held()
            out["input_channel"] = "xtest"
        else:
            out["input_channel"] = "xdotool"
    except Exception as exc:
        out["agent_held_error"] = str(exc)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Pause agent input on physical Escape")
    p.add_argument("--resume", action="store_true", help="Turn agent mouse and keyboard back ON")
    p.add_argument("--pause", action="store_true", help="Pause now without waiting for Escape")
    p.add_argument("--status", action="store_true", help="Print the switch, presence and held state")
    p.add_argument("--release", action="store_true",
                   help="Let go of every key and button agents are holding")
    p.add_argument("--display", default=None, help="X display to listen on (default $DISPLAY)")
    args = p.parse_args(argv)
    if args.status:
        print(json.dumps(status(args.display), indent=1, default=str))
        return 0
    if args.release:
        print(json.dumps(release_agent_holds(args.display), default=str))
        return 0
    if args.resume:
        print(json.dumps(owner_pause.resume()))
        return 0
    if args.pause:
        print(json.dumps(owner_pause.pause_from_escape()))
        print(json.dumps(release_agent_holds(args.display), default=str))
        return 0
    if sys.platform == "darwin":
        return run_darwin()
    if sys.platform.startswith("linux"):
        return run_x11(args.display)
    _log("owner-pause: unsupported platform " + sys.platform)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
