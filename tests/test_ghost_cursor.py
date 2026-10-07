"""Private IPC is real in tmp dirs; every X call and process launch is mocked."""
from __future__ import annotations

from collections import deque
import ctypes as C
import json
import os
from pathlib import Path
import socket
import stat
from types import SimpleNamespace

import pytest

from workman import ghost_cursor as ghost


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path_factory):
    # Unix-domain paths are limited to about 108 bytes. Pytest's long test-name
    # directories intentionally stay out of the IPC path.
    runtime = tmp_path_factory.mktemp("ghost") / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("WORKMAN_POLITE", "1")
    monkeypatch.setattr(ghost, "_CHILDREN", [])

    def forbidden(*_args, **_kwargs):
        pytest.fail("real X access or subprocess launch is forbidden in this test")

    monkeypatch.setattr(ghost, "_load", forbidden)
    monkeypatch.setattr(ghost.subprocess, "Popen", forbidden)


class Function:
    def __init__(self, name, calls, result=1, effect=None):
        self.name, self.calls, self.result, self.effect = name, calls, result, effect

    def __call__(self, *args):
        self.calls.append((self.name, args))
        return self.effect(*args) if self.effect else self.result


def put(pointer, kind, value):
    C.cast(pointer, C.POINTER(kind))[0] = value


@pytest.fixture
def native(monkeypatch):
    calls = []
    x = SimpleNamespace()
    render = SimpleNamespace()
    shape = SimpleNamespace()
    # This allowlist has no event injection, focus, device or cursor operations.
    for name in (
        "XOpenDisplay", "XCloseDisplay", "XDefaultScreen", "XRootWindow", "XInternAtom",
        "XGetSelectionOwner", "XGetVisualInfo", "XFree", "XCreateColormap", "XCreatePixmap",
        "XCreateWindow", "XSetWMHints", "XStoreName", "XChangeProperty", "XMoveWindow",
        "XFlush", "XSync", "XQueryPointer", "XMapRaised", "XRaiseWindow", "XUnmapWindow",
        "XDestroyWindow", "XFreePixmap", "XFreeColormap", "XClearWindow",
    ):
        setattr(x, name, Function(name, calls))
    for name in (
        "XRenderQueryExtension", "XRenderQueryVersion", "XRenderFindVisualFormat",
        "XRenderCreatePicture", "XRenderFreePicture", "XRenderFillRectangle",
        "XRenderCreateSolidFill", "XRenderComposite",
    ):
        setattr(render, name, Function(name, calls))
    for name in ("XShapeQueryExtension", "XShapeQueryVersion", "XShapeCombineRectangles"):
        setattr(shape, name, Function(name, calls))
    x.XOpenDisplay.result = 11
    x.XDefaultScreen.result = 0
    x.XRootWindow.result = 40
    x.XCreateColormap.result = 50
    pixmaps = iter((60, 61))
    x.XCreatePixmap.effect = lambda *_: next(pixmaps)
    pictures = iter((70, 71))
    render.XRenderCreatePicture.effect = lambda *_: next(pictures)
    x.XCreateWindow.result = 80
    render.XRenderCreateSolidFill.result = 90
    atoms = {}
    x.XInternAtom.effect = lambda _dpy, name, _only: atoms.setdefault(name, len(atoms) + 100)
    fmt = ghost.XRenderPictFormat(depth=32, direct=ghost.XRenderDirectFormat(alphaMask=255))
    render.XRenderFindVisualFormat.result = C.pointer(fmt)
    visuals = (ghost.XVisualInfo * 1)(ghost.XVisualInfo(visual=42, screen=0, depth=32))

    def visual_info(_dpy, _mask, _template, count):
        put(count, ghost.INT, 1)
        return C.cast(visuals, C.POINTER(ghost.XVisualInfo))

    x.XGetVisualInfo.effect = visual_info

    def version(major, minor):
        def fill(_dpy, a, b):
            put(a, ghost.INT, major)
            put(b, ghost.INT, minor)
            return 1
        return fill

    render.XRenderQueryVersion.effect = version(0, 11)
    shape.XShapeQueryVersion.effect = version(1, 1)
    captures = SimpleNamespace(attrs=None, hints=None, properties=[], masks=[], fills=[])

    def window(*args):
        captures.attrs = ghost.XSetWindowAttributes.from_buffer_copy(args[-1]._obj)
        return 80

    x.XCreateWindow.effect = window

    def hints(_dpy, _window, pointer):
        captures.hints = ghost.XWMHints.from_buffer_copy(pointer._obj)
        return 1

    x.XSetWMHints.effect = hints
    x.XChangeProperty.effect = lambda *a: captures.properties.append((a[1:6], tuple(a[6]))) or 1
    render.XRenderCreateSolidFill.effect = lambda _d, p: captures.masks.append(p._obj.alpha) or 90
    render.XRenderFillRectangle.effect = lambda *a: captures.fills.append(tuple(
        getattr(a[3]._obj, name) for name in ("red", "green", "blue", "alpha")))
    pointer = [500, 600]

    def query(*args):
        put(args[4], ghost.INT, pointer[0])
        put(args[5], ghost.INT, pointer[1])
        return 1

    x.XQueryPointer.effect = query
    libs = {"X11": x, "Xrender": render, "Xext": shape}
    monkeypatch.setattr(ghost, "_load", lambda name, _fallback: libs[name])
    return SimpleNamespace(x=x, render=render, shape=shape, calls=calls,
                           captures=captures, atoms=atoms, pointer=pointer, fmt=fmt)


def named(native, name):
    return [args for called, args in native.calls if called == name]


def test_click_through_argb_window_never_requests_focus(native):
    overlay = ghost._Overlay(":0")
    try:
        assert named(native, "XOpenDisplay") == [(b":0",)]
        attrs = native.captures.attrs
        assert attrs.override_redirect == 1
        assert attrs.event_mask == attrs.cursor == attrs.do_not_propagate_mask == 0
        create = named(native, "XCreateWindow")[0]
        assert create[1] == 40
        assert create[4:9] == (26, 26, 0, 32, 1)
        assert create[-2] & (1 << 9)
        assert named(native, "XShapeCombineRectangles") == [(11, 80, 2, 0, 0, None, 0, 0, 0)]
        assert native.captures.hints.flags == 1
        assert native.captures.hints.input == 0
        assert not named(native, "XMapRaised")  # Shape is checked while unmapped.
        assert named(native, "XSync") == [(11, 0)]
        assert all(args[-1] == 1 for args in named(native, "XInternAtom"))
        props = native.captures.properties
        assert props[0][0] == (80, native.atoms[b"_NET_WM_WINDOW_TYPE"], 4, 32, 0)
        assert props[0][1] == (native.atoms[b"_NET_WM_WINDOW_TYPE_TOOLTIP"],)
        assert set(props[1][1]) == {native.atoms[key] for key in (
            b"_NET_WM_STATE_SKIP_TASKBAR", b"_NET_WM_STATE_SKIP_PAGER", b"_NET_WM_STATE_ABOVE")}
    finally:
        overlay.close()


def test_own_window_only_raising_hotspot_and_premultiplied_fade(native):
    overlay = ghost._Overlay(":7.1")
    assert overlay.pointer() == (500, 600)
    overlay.draw((300, 400), 1.0)
    overlay.draw((301, 402), .5)
    overlay.draw((301, 402), .49)  # Same quantized opacity: no extra fade allocation.
    overlay.draw((301, 402), 0)
    overlay.hide()
    overlay.close()
    overlay.close()
    assert named(native, "XMoveWindow") == [(11, 80, 299, 399), (11, 80, 300, 401)]
    assert named(native, "XMapRaised") == [(11, 80)]
    assert named(native, "XRaiseWindow") == [(11, 80), (11, 80)]
    assert named(native, "XUnmapWindow") == [(11, 80)]
    assert native.captures.masks == [65535, 32768]
    assert named(native, "XRenderComposite") == [
        (11, 1, 70, 90, 71, 0, 0, 0, 0, 0, 0, 26, 26),
        (11, 1, 70, 90, 71, 0, 0, 0, 0, 0, 0, 26, 26)]
    assert named(native, "XRenderFreePicture") == [(11, 90), (11, 90), (11, 70), (11, 71)]
    assert named(native, "XDestroyWindow") == [(11, 80)]
    assert named(native, "XFreePixmap") == [(11, 60), (11, 61)]
    assert named(native, "XFreeColormap") == [(11, 50)]
    assert named(native, "XCloseDisplay") == [(11,)]


def test_steel_arrow_has_transparency_edge_and_varied_cold_grey():
    pixels = ghost.steel_pixels()
    assert len(pixels) == 26 * 26
    assert pixels[0] == 0
    assert pixels[26 + 1] == 0xFF26313A
    assert all(pixel == 0 or pixel >> 24 == 255 for pixel in pixels)
    fill = set(pixels) - {0, 0xFF26313A}
    assert len(fill) > 40
    for pixel in fill:
        red, green, blue = ((pixel >> shift) & 255 for shift in (16, 8, 0))
        assert red < green < blue and blue - red == 16


@pytest.mark.parametrize("failure", ["open", "compositor", "shape", "shape_version", "render_version", "visual", "alpha"])
def test_display_failures_close_without_mapping(native, failure):
    if failure == "open":
        native.x.XOpenDisplay.result = None
    elif failure == "compositor":
        native.x.XGetSelectionOwner.result = 0
    elif failure == "shape":
        native.shape.XShapeQueryExtension.result = 0
    elif failure == "shape_version":
        native.shape.XShapeQueryVersion.effect = lambda *_: 1  # Leaves version 0.0.
    elif failure == "render_version":
        native.render.XRenderQueryVersion.effect = lambda *_: 1
        native.render.XRenderQueryExtension.effect = lambda _d, a, b: put(a, ghost.INT, 0) or 1
    elif failure == "visual":
        native.x.XGetVisualInfo.effect = lambda *_: None
    else:
        native.fmt.direct.alphaMask = 0
    with pytest.raises(ghost.GhostError):
        ghost._Overlay(":42")
    assert not named(native, "XMapRaised")
    assert not named(native, "XCreateWindow")
    assert len(named(native, "XCloseDisplay")) == (0 if failure == "open" else 1)


def test_partial_allocation_failure_releases_all_acquired_resources(native):
    results = iter((70, 0))
    native.render.XRenderCreatePicture.effect = lambda *_: next(results)
    with pytest.raises(ghost.GhostError):
        ghost._Overlay(":0")
    assert named(native, "XRenderFreePicture") == [(11, 70)]
    assert named(native, "XFreePixmap") == [(11, 60), (11, 61)]
    assert named(native, "XFreeColormap") == [(11, 50)]
    assert named(native, "XCloseDisplay") == [(11,)]


class Drawing:
    def __init__(self):
        self.draws, self.hides, self.closed, self.queries = [], 0, 0, 0

    def draw(self, position, alpha):
        self.draws.append((position, alpha))

    def hide(self):
        self.hides += 1

    def close(self):
        self.closed += 1

    def pointer(self):
        self.queries += 1
        return (5, 6)


def packet(action="move", position=(30, 40), at=100, wall=500):
    return json.dumps([action, *position, int(at * 1e9), wall]).encode()


def test_fade_and_idle_deadline_use_last_sent_activity():
    drawing = Drawing()
    state = ghost._State(100, (5, 6), None)
    state.message(packet(), 100.2, drawing)  # IPC latency must not extend the fade.
    state.tick(100.5, (5, 6), drawing)
    state.tick(101, (5, 6), drawing)
    assert drawing.draws == [((30, 40), .75), ((30, 40), .5)]
    assert state.wait(101) == ghost.TICK_S
    state.tick(102, (5, 6), drawing)
    assert drawing.hides == 1 and state.position is None
    assert state.wait(102) == 58  # Hidden overlay sleeps, rather than polling.
    assert state.wait(160) == 0


def test_owner_stamp_hides_and_rejects_queued_old_positions():
    drawing = Drawing()
    state = ghost._State(100, (5, 6), (400, 400, 1))
    state.message(packet(wall=500), 100, drawing)
    state.tick(100, (5, 6), drawing)
    state.owner((600, 600, 1), drawing)
    assert state.position is None and drawing.hides == 1
    state.message(packet(at=100.1, wall=550), 100.1, drawing)
    assert state.position is None
    state.message(packet(at=100.2, wall=700), 100.2, drawing)
    state.tick(100.2, (30, 40), drawing)
    assert state.position == (30, 40) and drawing.hides == 1


def test_explicit_hide_prevents_out_of_order_resurrection():
    drawing = Drawing()
    state = ghost._State(100, (5, 6), None)
    state.message(packet(), 100, drawing)
    state.message(packet("hide", at=100.2), 100.2, drawing)
    state.message(packet(at=100.1), 100.3, drawing)
    assert state.position is None and drawing.hides == 1
    assert not state.expected


def test_expected_agent_positions_before_and_after_motion_do_not_flicker():
    drawing = Drawing()
    state = ghost._State(100, (5, 6), None)
    state.message(packet(position=(10, 10)), 100, drawing)
    state.message(packet(position=(20, 20), at=100.02), 100.02, drawing)
    state.tick(100.03, (10, 10), drawing)  # The earlier queued agent step.
    state.tick(100.04, (20, 20), drawing)
    state.tick(100.08, (30, 30), drawing)  # Notification is slightly behind X.
    assert state.pending is not None
    state.message(packet(position=(30, 30), at=100.09), 100.09, drawing)
    state.tick(100.20, (30, 30), drawing)
    assert state.pending is None and drawing.hides == 0


def test_independent_pointer_motion_hides_even_if_it_keeps_moving():
    drawing = Drawing()
    state = ghost._State(100, (5, 6), None)
    state.message(packet(), 100, drawing)
    state.tick(100.01, (80, 90), drawing)
    state.tick(100.07, (81, 91), drawing)
    state.tick(100.12, (82, 92), drawing)
    assert state.position is None and drawing.hides == 1


def test_expired_expected_positions_do_not_mask_owner_movement():
    drawing = Drawing()
    state = ghost._State(100, (5, 6), None)
    state.message(packet(), 100, drawing)
    state.tick(100.4, (30, 40), drawing)
    state.tick(100.51, (30, 40), drawing)
    assert drawing.hides == 1


@pytest.mark.parametrize("bad", [
    b"x", b"{}", b"null", b"[]", b"[1,2]", b"\xff", b"[\"move\",1,2,true,4]",
    b"[\"move\",true,2,100000000000,500]", b"[\"move\",999999,2,100000000000,500]",
    b"[\"other\",1,2,100000000000,500]", packet(at=110), packet(at=90),
])
def test_malformed_or_stale_ipc_does_not_change_state(bad):
    drawing = Drawing()
    state = ghost._State(100, (5, 6), None)
    state.message(bad, 100, drawing)
    assert state.position is None and drawing.hides == 0


def test_owner_stamp_uses_existing_learn_root(monkeypatch, tmp_path):
    path = tmp_path / "learn"
    path.mkdir()
    monkeypatch.setenv("WORKMAN_LEARN_ROOT", str(path))
    assert ghost._owner_stamp() is None
    stamp = path / "human-input.stamp"
    stamp.touch()
    before = ghost._owner_stamp()
    os.utime(stamp, ns=(before[0] + 1000000, before[0] + 1000000))
    after = ghost._owner_stamp()
    assert after != before and after[0] == before[0] + 1000000


class Server:
    def __init__(self, packets=()):
        self.packets = deque(packets)

    def recv(self, _size):
        if self.packets:
            return self.packets.popleft()
        raise BlockingIOError()


def fake_loop(monkeypatch, packets=(), on_wait=None):
    clock = SimpleNamespace(now=100.0, waits=[])
    drawing, server = Drawing(), Server(packets)
    monkeypatch.setattr(ghost.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(ghost.time, "monotonic_ns", lambda: int(clock.now * 1e9))
    monkeypatch.setattr(ghost, "_Overlay", lambda display: drawing)
    monkeypatch.setattr(ghost, "_owner_stamp", lambda: None)

    def wait(readers, writes, errors, timeout):
        assert readers == [server] and writes == errors == []
        clock.waits.append(timeout)
        clock.now += timeout
        if on_wait:
            on_wait(clock, server)
        return [], [], []

    monkeypatch.setattr(ghost.select, "select", wait)
    return clock, drawing, server


def test_daemon_exits_at_60_seconds_and_does_no_hidden_polling(monkeypatch):
    clock, drawing, server = fake_loop(monkeypatch, [packet()])
    ghost._serve(":4", server)
    assert clock.now == 160
    assert 38 <= len(drawing.draws) <= 42
    assert drawing.hides == 1 and drawing.closed == 1
    assert 57.9 <= clock.waits[-1] <= 58
    assert drawing.queries <= 43  # No X queries during the long hidden wait.


def test_owner_stamp_is_checked_before_queued_ipc(monkeypatch):
    clock, drawing, server = fake_loop(monkeypatch, [packet(wall=500)])
    monkeypatch.setattr(ghost, "_owner_stamp", lambda: (600, 600, 1))
    ghost._serve(":4", server)
    assert not drawing.draws and drawing.queries == 1
    assert clock.waits == [60] and drawing.closed == 1


def test_demo_is_bounded_and_only_draws_supplied_position(monkeypatch):
    clock, drawing, server = fake_loop(monkeypatch)
    ghost._serve(":0", server, deadline=100.5, demo=(400, 300))
    assert clock.now == 100.5 and drawing.closed == 1
    assert drawing.draws and all(pos == (400, 300) for pos, _ in drawing.draws)
    assert drawing.hides == 0


def test_exception_in_loop_closes_overlay(monkeypatch):
    _clock, drawing, server = fake_loop(monkeypatch, [packet()])
    monkeypatch.setattr(ghost.select, "select", lambda *_: (_ for _ in ()).throw(OSError("closed")))
    with pytest.raises(OSError):
        ghost._serve(":1", server)
    assert drawing.closed == 1


def test_runtime_and_socket_are_private_and_alias_isolated(monkeypatch):
    assert ghost._display_name(":0.0") == ":0"
    assert ghost._display_name("unix:0.0") == ":0"
    assert ghost._display_name("unix/:0") == ":0"
    first, lock = ghost._paths(":0", create=True)
    assert ghost._paths(":1", create=True)[0] != first
    assert ghost._paths(":0.1", create=True)[0] != first
    assert stat.S_IMODE(first.parent.stat().st_mode) == 0o700
    fd = ghost._open_lock(lock)
    server = ghost._bind_socket(first)
    try:
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600
        assert stat.S_IMODE(first.stat().st_mode) == 0o600
        assert ghost._send(first, b"hello")
        assert server.recv(256) == b"hello"
        with pytest.raises(BlockingIOError):
            ghost._open_lock(lock)
    finally:
        server.close()
        first.unlink()
        os.close(fd)


@pytest.mark.parametrize("kind", ["permissions", "symlink", "relative", "foreign"])
def test_insecure_runtime_fails_closed(monkeypatch, tmp_path, kind):
    root = Path(os.environ["XDG_RUNTIME_DIR"])
    if kind == "permissions":
        root.chmod(0o755)
    elif kind == "symlink":
        link = tmp_path / "link"
        link.symlink_to(root, target_is_directory=True)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(link))
    elif kind == "relative":
        monkeypatch.setenv("XDG_RUNTIME_DIR", "relative")
    else:
        monkeypatch.setattr(ghost.os, "getuid", lambda: root.stat().st_uid + 1)
    with pytest.raises(ghost.GhostError):
        ghost._paths(":0", create=True)
    ghost.notify(":0", 1, 2)  # Errors never escape to the input caller.


def test_runtime_fallback_is_private(monkeypatch, tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("fallback")
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    real_path = Path
    monkeypatch.setattr(ghost, "Path", lambda value: real_path(tmp_path) if value == "/tmp" else real_path(value))
    path, _ = ghost._paths(":0", create=True)
    assert path.parent == tmp_path / f"workman-ghost-{os.getuid()}"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_lock_and_socket_symlinks_are_rejected_without_changing_targets(tmp_path):
    path, lock = ghost._paths(":0", create=True)
    target = tmp_path / "precious"
    target.write_text("unchanged")
    lock.symlink_to(target)
    with pytest.raises(OSError):
        ghost._open_lock(lock)
    path.symlink_to(target)
    with pytest.raises(ghost.GhostError):
        ghost._bind_socket(path)
    assert not ghost._send(path, b"hello")
    assert target.read_text() == "unchanged" and path.is_symlink()


def test_launcher_transfers_lock_and_queue_and_pins_this_worktree(monkeypatch):
    launches, inherited = [], []

    def launch(argv, **kwargs):
        launches.append((argv, kwargs))
        inherited.extend(os.dup(fd) for fd in kwargs["pass_fds"])
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(ghost.subprocess, "Popen", launch)
    monkeypatch.setenv("DISPLAY", ":88")
    monkeypatch.setenv("PYTHONPATH", "/some/live/checkout")
    try:
        ghost.notify(":0.0", 100, 200)
        ghost.notify(":0", 101, 201)
        assert len(launches) == 1
        argv, kwargs = launches[0]
        root = str(Path(ghost.__file__).resolve().parent.parent)
        assert argv[:2] == [ghost.sys.executable, "-c"] and "sys.path.insert(0" in argv[2]
        assert argv[3:7] == [root, "--display", ":0", "--daemon"]
        assert kwargs["cwd"] == root and kwargs["env"]["DISPLAY"] == ":0"
        assert os.environ["DISPLAY"] == ":88"
        assert kwargs["env"]["PYTHONPATH"] == "/some/live/checkout"
        assert kwargs["close_fds"] is True and kwargs["start_new_session"] is True
        assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == ghost.subprocess.DEVNULL
        path, lock = ghost._paths(":0")
        with pytest.raises(BlockingIOError):
            ghost._open_lock(lock)
        with socket.socket(fileno=os.dup(inherited[1])) as server:
            first = json.loads(server.recv(256))
            second = json.loads(server.recv(256))
            assert first[:3] == ["move", 100, 200] and second[:3] == ["move", 101, 201]
        ghost.hide(":0")
        with socket.socket(fileno=os.dup(inherited[1])) as server:
            assert json.loads(server.recv(256))[0] == "hide"
    finally:
        for fd in inherited:
            os.close(fd)


def test_spawn_failure_is_throttled_across_callers_and_cleans_socket(monkeypatch):
    starts = []
    clock = [100.0]
    monkeypatch.setattr(ghost.time, "monotonic", lambda: clock[0])

    def fail(*args, **kwargs):
        starts.append(args)
        raise OSError("cannot start")

    monkeypatch.setattr(ghost.subprocess, "Popen", fail)
    ghost.notify(":0", 10, 20)
    ghost.notify(":0", 11, 21)
    path, _ = ghost._paths(":0")
    assert not path.exists() and len(starts) == 1
    clock[0] += ghost.RESTART_S
    ghost.notify(":0", 12, 22)
    assert len(starts) == 2 and not path.exists()


@pytest.mark.parametrize("display", [":99", ":99.0", ":99.1", "", None, ":0\x00", ":0 bad"])
def test_disabled_or_invalid_display_never_creates_resources(display):
    ghost.notify(display, 1, 2)
    ghost.hide(display)
    assert not (Path(os.environ["XDG_RUNTIME_DIR"]) / "workman-ghost").exists()


def test_polite_off_and_invalid_coordinates_never_start(monkeypatch):
    monkeypatch.setenv("WORKMAN_POLITE", "0")
    ghost.notify(":0", 1, 2)
    monkeypatch.setenv("WORKMAN_POLITE", "1")
    for x, y in ((True, 1), (1.0, 2), ("3", 4), (-32768, 2), (32768, 2)):
        ghost.notify(":0", x, y)
    assert not (Path(os.environ["XDG_RUNTIME_DIR"]) / "workman-ghost").exists()


def test_hide_never_launches_but_works_when_polite_is_off(monkeypatch):
    ghost.hide(":0")
    assert not (Path(os.environ["XDG_RUNTIME_DIR"]) / "workman-ghost").exists()
    path, _ = ghost._paths(":0", create=True)
    server = ghost._bind_socket(path)
    try:
        monkeypatch.setenv("WORKMAN_POLITE", "0")
        ghost.hide(":0")
        assert json.loads(server.recv(256))[0] == "hide"
    finally:
        server.close()
        path.unlink()


def test_nonblocking_send_on_full_queue_and_stale_socket():
    path, _ = ghost._paths(":0", create=True)
    server = ghost._bind_socket(path)
    try:
        for _ in range(1000):
            if not ghost._send(path, b"hello"):
                break
        else:
            pytest.fail("datagram queue did not fill")
        assert not ghost._send(path, b"hello")
    finally:
        server.close()
    assert not ghost._send(path, b"hello")
    rebound = ghost._bind_socket(path)
    rebound.close()
    path.unlink()


def test_demo_cli_holds_display_lock_and_cleans_resources(monkeypatch):
    observed = []

    def serve(display, server, deadline, demo):
        observed.append((display, deadline, demo))
        assert server.getsockname() == str(ghost._paths(display)[0])
        with pytest.raises(BlockingIOError):
            ghost._open_lock(ghost._paths(display)[1])

    monkeypatch.setattr(ghost, "_serve", serve)
    monkeypatch.setattr(ghost.time, "monotonic", lambda: 100.0)
    assert ghost.main(["--display", ":0", "--demo", "400", "300", "--timeout", "5"]) == 0
    assert observed == [(":0", 105.0, (400, 300))]
    path, lock = ghost._paths(":0")
    assert not path.exists()
    os.close(ghost._open_lock(lock))


@pytest.mark.parametrize("timeout", ["0", "-1", "31", "nan", "inf"])
def test_demo_rejects_unbounded_lifetime(timeout):
    with pytest.raises(SystemExit) as error:
        ghost.main(["--display", ":0", "--demo", "1", "2", "--timeout", timeout])
    assert error.value.code == 2


def test_demo_error_and_signal_clean_up(monkeypatch, capsys):
    monkeypatch.setattr(ghost, "_serve", lambda *_: (_ for _ in ()).throw(ghost.GhostError("no display")))
    assert ghost.main(["--display", ":0", "--demo", "1", "2"]) == 1
    assert "no display" in capsys.readouterr().err
    assert not ghost._paths(":0")[0].exists()
    monkeypatch.setattr(ghost, "_serve", lambda *_: ghost._stop())
    assert ghost.main(["--display", ":0", "--demo", "1", "2"]) == 0
    assert not ghost._paths(":0")[0].exists()


def test_live_lock_refusal_never_unlinks_existing_socket(monkeypatch, capsys):
    path, lock = ghost._paths(":0", create=True)
    fd, server = ghost._open_lock(lock), ghost._bind_socket(path)
    try:
        assert ghost.main(["--display", ":0", "--demo", "1", "2"]) == 1
        assert path.exists() and "temporarily unavailable" in capsys.readouterr().err.lower()
        assert ghost._send(path, b"still alive")
        assert server.recv(256) == b"still alive"
    finally:
        server.close()
        path.unlink()
        os.close(fd)


def test_inherited_daemon_descriptors_are_adopted_and_closed(monkeypatch):
    path, lock = ghost._paths(":0", create=True)
    fd, server = ghost._open_lock(lock), ghost._bind_socket(path)
    daemon_fd, socket_fd = os.dup(fd), os.dup(server.fileno())
    observed = []
    monkeypatch.setattr(ghost, "_serve", lambda *args: observed.append(args[0]))
    try:
        assert ghost.main(["--display", ":0", "--daemon", "--socket-fd", str(socket_fd),
                           "--lock-fd", str(daemon_fd)]) == 0
        assert observed == [":0"] and not path.exists()
        with pytest.raises(OSError):
            os.fstat(daemon_fd)
        with pytest.raises(OSError):
            os.fstat(socket_fd)
    finally:
        server.close()
        os.close(fd)


def test_wrong_display_daemon_socket_cannot_remove_either_endpoint(monkeypatch, capsys):
    first, first_lock = ghost._paths(":0", create=True)
    second, second_lock = ghost._paths(":1", create=True)
    fd0, fd1 = ghost._open_lock(first_lock), ghost._open_lock(second_lock)
    server0, server1 = ghost._bind_socket(first), ghost._bind_socket(second)
    try:
        assert ghost.main(["--display", ":1", "--daemon", "--socket-fd", str(os.dup(server0.fileno())),
                           "--lock-fd", str(os.dup(fd1))]) == 1
        assert "socket does not match" in capsys.readouterr().err
        assert first.exists() and second.exists()
        assert ghost._send(first, b"zero") and ghost._send(second, b"one")
        assert server0.recv(256) == b"zero" and server1.recv(256) == b"one"
    finally:
        server0.close()
        server1.close()
        first.unlink()
        second.unlink()
        os.close(fd0)
        os.close(fd1)


def test_wrong_lock_descriptor_does_not_steal_display_lock(capsys):
    first, first_lock = ghost._paths(":0", create=True)
    _second, second_lock = ghost._paths(":1", create=True)
    fd0, fd1 = ghost._open_lock(first_lock), ghost._open_lock(second_lock)
    server = ghost._bind_socket(first)
    socket_fd = os.dup(server.fileno())
    try:
        assert ghost.main(["--display", ":0", "--daemon", "--socket-fd", str(socket_fd),
                           "--lock-fd", str(os.dup(fd1))]) == 1
        assert "lock does not match" in capsys.readouterr().err
        assert first.exists()
        with pytest.raises(BlockingIOError):
            ghost._open_lock(first_lock)
    finally:
        os.close(socket_fd)  # Rejected before socket adoption.
        server.close()
        first.unlink()
        os.close(fd0)
        os.close(fd1)


def test_cleanup_will_not_unlink_a_replacement_socket():
    path, _lock = ghost._paths(":0", create=True)
    first = ghost._bind_socket(path)
    identity = ghost._socket_identity(path)
    path.unlink()
    second = ghost._bind_socket(path)
    try:
        ghost._remove_owned_socket(path, identity)
        assert path.exists() and ghost._send(path, b"new")
        assert second.recv(256) == b"new"
    finally:
        first.close()
        second.close()
        path.unlink()


def test_ipc_wakes_a_hidden_daemon_and_restarts_its_idle_deadline(monkeypatch):
    sent = [False]

    def deliver(clock, server):
        if not sent[0]:
            sent[0] = True
            # Model select being woken early by new data after 10s of quiet.
            clock.now = 110
            server.packets.append(packet(at=110))

    clock, drawing, server = fake_loop(monkeypatch, on_wait=deliver)
    ghost._serve(":1", server)
    assert clock.waits[0] == 60
    assert clock.now == 170 and drawing.closed == 1
    assert drawing.draws[0] == ((30, 40), 1.0)


def test_stamp_change_during_fade_hides_at_next_active_tick(monkeypatch):
    clock, drawing, server = fake_loop(monkeypatch, [packet(wall=500)])
    monkeypatch.setattr(ghost, "_owner_stamp", lambda: None if clock.now == 100 else (600, 600, 1))
    ghost._serve(":0", server)
    assert len(drawing.draws) == 1 and drawing.hides == 1
    assert drawing.queries == 2  # Initial pointer and one active tick only.
    assert clock.waits[0] == ghost.TICK_S and clock.waits[1] > 59


def test_two_displays_start_independent_daemons(monkeypatch):
    launches, inherited = [], []

    def launch(argv, **kwargs):
        launches.append(argv)
        inherited.extend(os.dup(fd) for fd in kwargs["pass_fds"])
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(ghost.subprocess, "Popen", launch)
    try:
        ghost.notify(":0", 1, 2)
        ghost.notify(":1", 3, 4)
        ghost.notify("unix/:0.0", 5, 6)
        assert len(launches) == 2
        assert [argv[5] for argv in launches] == [":0", ":1"]
        ghost.hide(":0")
        with socket.socket(fileno=os.dup(inherited[1])) as zero:
            assert [json.loads(zero.recv(256))[0] for _ in range(3)] == ["move", "move", "hide"]
        with socket.socket(fileno=os.dup(inherited[3])) as one:
            assert json.loads(one.recv(256))[:3] == ["move", 3, 4]
            with pytest.raises(BlockingIOError):
                one.recv(256)
    finally:
        for fd in inherited:
            os.close(fd)


def test_long_runtime_path_and_unsafe_socket_mode_fail_closed(monkeypatch):
    original = Path(os.environ["XDG_RUNTIME_DIR"])
    long = original / ("a" * 80)
    long.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(long))
    with pytest.raises(ghost.GhostError, match="too long"):
        ghost._paths(":0", create=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(original))
    path, _ = ghost._paths(":0", create=True)
    server = ghost._bind_socket(path)
    try:
        path.chmod(0o666)
        assert not ghost._send(path, b"no")
    finally:
        server.close()
        path.unlink()
