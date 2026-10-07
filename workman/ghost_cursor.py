"""A display-only steel arrow. Importing or notifying never opens X in the caller.

``notify(display, x, y)`` sends a best-effort position and starts one isolated
daemon on demand. ``hide(display)`` only sends a hide; it never starts a daemon.
The daemon has no input, device, cursor-definition or focus operations. It owns
only a tiny click-through window, private IPC, and a read-only pointer query.

Run a bounded foreground screenshot demo with:
    python -m workman.ghost_cursor --display :0 --demo 400 300 --timeout 5
This draws at the supplied coordinates without moving the real pointer.
"""
from __future__ import annotations

import argparse
import ctypes as C
import ctypes.util
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import signal
import socket
import stat
import subprocess
import sys
import time
from collections import deque

try:
    import fcntl
except ImportError:  # Import remains harmless on non-X11 platforms.
    fcntl = None

SIZE, HOTSPOT = 26, (1, 1)
FADE_S, IDLE_S, TICK_S = 2.0, 60.0, 0.05
MOTION_GRACE_S, EXPECTED_S, RESTART_S = 0.10, 0.30, 5.0
_MAX_PACKET = 256
_CHILDREN: list[subprocess.Popen] = []
VP, INT, UINT, ULONG = C.c_void_p, C.c_int, C.c_uint, C.c_ulong


class GhostError(RuntimeError):
    pass


def _display_name(display: str) -> str:
    if (not isinstance(display, str) or not display or len(display) > 256
            or any(ch.isspace() or ord(ch) < 32 for ch in display)):
        raise GhostError("an explicit X display is required")
    # Xlib's omitted screen is screen zero. Other screens stay independent.
    display = re.sub(r"(:\d+)\.0$", r"\1", display)
    return re.sub(r"^unix/?(?=:\d)", "", display)


def _enabled(display: str) -> bool:
    return (sys.platform.startswith("linux") and fcntl is not None
            and os.environ.get("WORKMAN_POLITE", "1") != "0"
            and re.search(r":99(?:\.\d+)?$", display) is None)


def _private_dir(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise GhostError("ghost runtime directory must be owned by this user, mode 0700")


def _runtime_dir(create: bool) -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        parent = Path(base)
        if not parent.is_absolute():
            raise GhostError("XDG_RUNTIME_DIR must be absolute")
        _private_dir(parent)
        path = parent / "workman-ghost"
    else:
        path = Path("/tmp") / f"workman-ghost-{os.getuid()}"
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    _private_dir(path)
    return path


def _paths(display: str, create: bool = False) -> tuple[Path, Path]:
    root = _runtime_dir(create)
    token = hashlib.sha256(display.encode()).hexdigest()[:20]
    sock = root / f"{token}.sock"
    if len(os.fsencode(sock)) >= 104:
        raise GhostError("ghost socket path is too long")
    return sock, root / f"{token}.lock"


def _send(path: Path, packet: bytes) -> bool:
    try:
        info = path.lstat()
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
            client.setblocking(False)
            return client.sendto(packet, str(path)) == len(packet)
    except OSError:
        return False


def _packet(action: str, x: int = 0, y: int = 0) -> bytes:
    return json.dumps([action, x, y, time.monotonic_ns(), time.time_ns()],
                      separators=(",", ":")).encode("ascii")


def _open_lock(path: Path) -> int:
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise GhostError("unsafe ghost lock file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _unlink_socket(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise GhostError("unsafe ghost socket entry")
    path.unlink()


def _bind_socket(path: Path) -> socket.socket:
    # Only called while holding the display lock. Never unlink a live peer.
    _unlink_socket(path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    bound = False
    try:
        server.bind(str(path))
        bound = True
        os.chmod(path, 0o600)
        server.setblocking(False)
        return server
    except BaseException:
        server.close()
        if bound:
            _unlink_socket(path)
        raise


def _start(display: str, path: Path, lock_path: Path, packet: bytes) -> None:
    # Transferring the locked open-file description and an already bound socket
    # closes both bootstrap races: no duplicate process, no lost first position.
    fd = _open_lock(lock_path)
    server = None
    transferred = False
    try:
        now = time.monotonic()
        try:
            previous = float(os.pread(fd, 64, 0))
        except ValueError:
            previous = -RESTART_S
        if math.isfinite(previous) and 0 <= now - previous < RESTART_S:
            return
        os.ftruncate(fd, 0)
        os.pwrite(fd, str(now).encode("ascii"), 0)
        server = _bind_socket(path)
        if not _send(path, packet):
            raise GhostError("cannot queue the initial ghost position")
        root = Path(__file__).resolve().parent.parent
        env = os.environ.copy()
        env["DISPLAY"] = display
        # Do not let an editable install or inherited PYTHONPATH select ~/workman.
        code = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
                "from workman.ghost_cursor import main; raise SystemExit(main())")
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(root), "--display", display,
             "--daemon", "--socket-fd", str(server.fileno()), "--lock-fd", str(fd)],
            cwd=str(root), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, pass_fds=(fd, server.fileno()), start_new_session=True,
        )
        transferred = True
        _CHILDREN[:] = [p for p in _CHILDREN if p.poll() is None]
        _CHILDREN.append(child)
    finally:
        if server is not None:
            server.close()
            if not transferred:
                _unlink_socket(path)
        # No LOCK_UN: the child still owns this same locked description.
        os.close(fd)


def notify(display: str, x: int, y: int) -> None:
    """Best effort. Safe before or just after agent motion; never injects input.

    Explicit screen :99 and WORKMAN_POLITE=0 are disabled. Calls never wait for
    X/startup or a busy IPC queue, and display failures cannot fail an action.
    """
    try:
        display = _display_name(display)
        if not _enabled(display) or not _coordinates(x, y):
            return
        path, lock = _paths(display, create=True)
        packet = _packet("move", x, y)
        if not _send(path, packet):
            _start(display, path, lock, packet)
    except Exception:
        pass


def hide(display: str) -> None:
    """Queue an immediate unmap on owner input, without creating any resources."""
    try:
        display = _display_name(display)
        path, _ = _paths(display)
        _send(path, _packet("hide"))
    except Exception:
        pass


def _coordinates(x, y) -> bool:
    return type(x) is int and type(y) is int and -32767 <= x <= 32767 and -32767 <= y <= 32767


def steel_pixels() -> tuple[int, ...]:
    """26px steel gradient with a dark one-pixel edge, tip at HOTSPOT."""
    polygon = ((1, 1), (1, 22), (7, 17), (11, 25), (15, 23), (11, 15), (21, 15))

    def inside(x, y):
        hit = False
        for (ax, ay), (bx, by) in zip(polygon, polygon[1:] + polygon[:1]):
            if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
                hit = not hit
        return hit

    mask = {(x, y) for y in range(SIZE) for x in range(SIZE) if inside(x + .25, y + .25)}
    mask.add(HOTSPOT)
    pixels = []
    for y in range(SIZE):
        for x in range(SIZE):
            if (x, y) not in mask:
                pixels.append(0)
            elif any((x + dx, y + dy) not in mask for dx, dy in
                     ((-1, 0), (1, 0), (0, -1), (0, 1))):
                pixels.append(0xFF26313A)
            else:
                grey = max(100, min(221, 220 - x * 5 - y * 2 + (y % 2) * 9))
                pixels.append(0xFF000000 | ((grey - 9) << 16) | (grey << 8) | (grey + 7))
    return tuple(pixels)


class XVisualInfo(C.Structure):
    _fields_ = [("visual", VP), ("visualid", ULONG), ("screen", INT),
                ("depth", INT), ("visual_class", INT), ("red_mask", ULONG),
                ("green_mask", ULONG), ("blue_mask", ULONG),
                ("colormap_size", INT), ("bits_per_rgb", INT)]


class XRenderDirectFormat(C.Structure):
    _fields_ = [(name, C.c_short) for name in
                ("red", "redMask", "green", "greenMask", "blue", "blueMask", "alpha", "alphaMask")]


class XRenderPictFormat(C.Structure):
    _fields_ = [("id", ULONG), ("type", INT), ("depth", INT),
                ("direct", XRenderDirectFormat), ("colormap", ULONG)]


class XRenderColor(C.Structure):
    _fields_ = [(name, C.c_ushort) for name in ("red", "green", "blue", "alpha")]


class XSetWindowAttributes(C.Structure):
    _fields_ = [("background_pixmap", ULONG), ("background_pixel", ULONG),
                ("border_pixmap", ULONG), ("border_pixel", ULONG),
                ("bit_gravity", INT), ("win_gravity", INT), ("backing_store", INT),
                ("backing_planes", ULONG), ("backing_pixel", ULONG), ("save_under", INT),
                ("event_mask", C.c_long), ("do_not_propagate_mask", C.c_long),
                ("override_redirect", INT), ("colormap", ULONG), ("cursor", ULONG)]


class XWMHints(C.Structure):
    _fields_ = [("flags", C.c_long), ("input", INT), ("initial_state", INT),
                ("icon_pixmap", ULONG), ("icon_window", ULONG), ("icon_x", INT),
                ("icon_y", INT), ("icon_mask", ULONG), ("window_group", ULONG)]


def _load(name: str, fallback: str):
    return C.CDLL(ctypes.util.find_library(name) or fallback)


def _prototype(lib, name, result, *args):
    fn = getattr(lib, name)
    fn.restype, fn.argtypes = result, list(args)


def _bind_x11(x):
    _prototype(x, "XOpenDisplay", VP, C.c_char_p)
    _prototype(x, "XCloseDisplay", INT, VP)
    _prototype(x, "XDefaultScreen", INT, VP)
    _prototype(x, "XRootWindow", ULONG, VP, INT)
    _prototype(x, "XInternAtom", ULONG, VP, C.c_char_p, INT)
    _prototype(x, "XGetSelectionOwner", ULONG, VP, ULONG)
    _prototype(x, "XGetVisualInfo", C.POINTER(XVisualInfo), VP, C.c_long,
               C.POINTER(XVisualInfo), C.POINTER(INT))
    _prototype(x, "XFree", INT, VP)
    _prototype(x, "XCreateColormap", ULONG, VP, ULONG, VP, INT)
    _prototype(x, "XCreatePixmap", ULONG, VP, ULONG, UINT, UINT, UINT)
    _prototype(x, "XCreateWindow", ULONG, VP, ULONG, INT, INT, UINT, UINT, UINT,
               INT, UINT, VP, ULONG, C.POINTER(XSetWindowAttributes))
    _prototype(x, "XSetWMHints", INT, VP, ULONG, C.POINTER(XWMHints))
    _prototype(x, "XStoreName", INT, VP, ULONG, C.c_char_p)
    _prototype(x, "XChangeProperty", INT, VP, ULONG, ULONG, ULONG, INT, INT, VP, INT)
    _prototype(x, "XMoveWindow", INT, VP, ULONG, INT, INT)
    _prototype(x, "XFlush", INT, VP)
    _prototype(x, "XSync", INT, VP, INT)
    _prototype(x, "XQueryPointer", INT, VP, ULONG, C.POINTER(ULONG), C.POINTER(ULONG),
               C.POINTER(INT), C.POINTER(INT), C.POINTER(INT), C.POINTER(INT), C.POINTER(UINT))
    for name in ("XMapRaised", "XRaiseWindow", "XUnmapWindow", "XDestroyWindow",
                 "XFreePixmap", "XFreeColormap", "XClearWindow"):
        _prototype(x, name, INT, VP, ULONG)


def _bind_extensions(render, shape):
    for lib, name in ((render, "XRenderQueryExtension"), (shape, "XShapeQueryExtension"),
                      (render, "XRenderQueryVersion"), (shape, "XShapeQueryVersion")):
        _prototype(lib, name, INT, VP, C.POINTER(INT), C.POINTER(INT))
    _prototype(render, "XRenderFindVisualFormat", C.POINTER(XRenderPictFormat), VP, VP)
    _prototype(render, "XRenderCreatePicture", ULONG, VP, ULONG, C.POINTER(XRenderPictFormat), ULONG, VP)
    _prototype(render, "XRenderFreePicture", None, VP, ULONG)
    _prototype(render, "XRenderFillRectangle", None, VP, INT, ULONG,
               C.POINTER(XRenderColor), INT, INT, UINT, UINT)
    _prototype(render, "XRenderCreateSolidFill", ULONG, VP, C.POINTER(XRenderColor))
    _prototype(render, "XRenderComposite", None, VP, INT, ULONG, ULONG, ULONG,
               INT, INT, INT, INT, INT, INT, UINT, UINT)
    _prototype(shape, "XShapeCombineRectangles", None, VP, ULONG, INT, INT, INT, VP, INT, INT, INT)


class _Overlay:
    """An independent X connection; raises and paints only its own window."""

    def __init__(self, display: str):
        self.x = _load("X11", "libX11.so.6")
        _bind_x11(self.x)
        self.dpy = self.x.XOpenDisplay(display.encode())
        self.window = self.colormap = 0
        self.pixmaps, self.pictures = [], []
        self.mapped = False
        self.position = self.level = None
        if not self.dpy:
            raise GhostError("cannot open X display")
        try:
            screen = self.x.XDefaultScreen(self.dpy)
            self.root = self.x.XRootWindow(self.dpy, screen)
            atom = self.x.XInternAtom(self.dpy, f"_NET_WM_CM_S{screen}".encode(), 1)
            if not atom or not self.x.XGetSelectionOwner(self.dpy, atom):
                raise GhostError("an X11 compositor is required for the ARGB ghost")
            self.render = _load("Xrender", "libXrender.so.1")
            shape = _load("Xext", "libXext.so.6")
            _bind_extensions(self.render, shape)
            for lib, extension, version, minimum in (
                (self.render, "XRenderQueryExtension", "XRenderQueryVersion", (0, 10)),
                (shape, "XShapeQueryExtension", "XShapeQueryVersion", (1, 1)),
            ):
                a, b = INT(), INT()
                if not getattr(lib, extension)(self.dpy, C.byref(a), C.byref(b)):
                    raise GhostError("required X extension is unavailable")
                if (not getattr(lib, version)(self.dpy, C.byref(a), C.byref(b))
                        or (a.value, b.value) < minimum):
                    raise GhostError("required X extension version is unavailable")
            template = XVisualInfo(screen=screen, depth=32, visual_class=4)
            count = INT()
            visuals = self.x.XGetVisualInfo(self.dpy, 2 | 4 | 8, C.byref(template), C.byref(count))
            if not visuals:
                raise GhostError("no 32-bit ARGB visual")
            try:
                for i in range(count.value):
                    visual = visuals[i].visual
                    fmt = self.render.XRenderFindVisualFormat(self.dpy, visual)
                    if fmt and fmt.contents.depth == 32 and fmt.contents.direct.alphaMask:
                        self._create(visual, fmt, shape)
                        break
                else:
                    raise GhostError("no 32-bit ARGB visual")
            finally:
                self.x.XFree(visuals)
            # Shape/hints must have succeeded before the first map. Protocol or
            # connection failures terminate only this standalone process.
            self.x.XSync(self.dpy, 0)
        except BaseException:
            self.close()
            raise

    def _atoms(self, property_name: bytes, values: tuple[bytes, ...]):
        # Use the compositor/WM's existing atoms. Do not allocate global atoms.
        found = [self.x.XInternAtom(self.dpy, name, 1) for name in values]
        found = [atom for atom in found if atom]
        prop = self.x.XInternAtom(self.dpy, property_name, 1)
        if not prop or not found:
            return
        atoms = (ULONG * len(found))(*found)
        # Xlib expects native longs for format 32, including on 64-bit machines.
        self.x.XChangeProperty(self.dpy, self.window, prop, 4, 32, 0, atoms, len(atoms))

    def _create(self, visual, fmt, shape):
        x, r, dpy = self.x, self.render, self.dpy
        self.colormap = x.XCreateColormap(dpy, self.root, visual, 0)
        if not self.colormap:
            raise GhostError("cannot allocate ghost colormap")
        for _ in range(2):
            pixmap = x.XCreatePixmap(dpy, self.root, SIZE, SIZE, 32)
            if not pixmap:
                raise GhostError("cannot allocate ghost pixmap")
            self.pixmaps.append(pixmap)
            picture = r.XRenderCreatePicture(dpy, pixmap, fmt, 0, None)
            if not picture:
                raise GhostError("cannot allocate ghost picture")
            self.pictures.append(picture)
        pixels = steel_pixels()
        for y in range(SIZE):
            start = 0
            while start < SIZE:
                pixel = pixels[y * SIZE + start]
                end = start + 1
                while end < SIZE and pixels[y * SIZE + end] == pixel:
                    end += 1
                color = XRenderColor(*(((pixel >> shift) & 255) * 257 for shift in (16, 8, 0, 24)))
                r.XRenderFillRectangle(dpy, 1, self.pictures[0], C.byref(color), start, y, end - start, 1)
                start = end
        attrs = XSetWindowAttributes(background_pixmap=self.pixmaps[1], border_pixel=0,
                                     override_redirect=1, colormap=self.colormap)
        self.window = x.XCreateWindow(dpy, self.root, 0, 0, SIZE, SIZE, 0, 32, 1,
                                     visual, 1 | (1 << 3) | (1 << 9) | (1 << 13), C.byref(attrs))
        if not self.window:
            raise GhostError("cannot create ghost window")
        # ShapeInput=2, ShapeSet=0, zero rectangles: clicks reach the window below.
        shape.XShapeCombineRectangles(dpy, self.window, 2, 0, 0, None, 0, 0, 0)
        hints = XWMHints(flags=1, input=0)
        x.XSetWMHints(dpy, self.window, C.byref(hints))
        x.XStoreName(dpy, self.window, b"Workman steel ghost")
        self._atoms(b"_NET_WM_WINDOW_TYPE", (b"_NET_WM_WINDOW_TYPE_TOOLTIP",))
        self._atoms(b"_NET_WM_STATE", (b"_NET_WM_STATE_SKIP_TASKBAR", b"_NET_WM_STATE_SKIP_PAGER",
                                      b"_NET_WM_STATE_ABOVE"))

    def pointer(self) -> tuple[int, int] | None:
        root, child, mask = ULONG(), ULONG(), UINT()
        rx, ry, wx, wy = INT(), INT(), INT(), INT()
        same = self.x.XQueryPointer(self.dpy, self.root, C.byref(root), C.byref(child),
                                   C.byref(rx), C.byref(ry), C.byref(wx), C.byref(wy), C.byref(mask))
        return (rx.value, ry.value) if same else None

    def draw(self, position: tuple[int, int], alpha: float) -> None:
        x, r, dpy = self.x, self.render, self.dpy
        level = max(0, min(40, round(alpha * 40)))
        if not level:
            self.hide()
            return
        if level != self.level:
            color = XRenderColor(0, 0, 0, round(65535 * level / 40))
            mask = r.XRenderCreateSolidFill(dpy, C.byref(color))
            if not mask:
                raise GhostError("cannot allocate ghost fade mask")
            try:
                r.XRenderComposite(dpy, 1, self.pictures[0], mask, self.pictures[1],
                                   0, 0, 0, 0, 0, 0, SIZE, SIZE)
            finally:
                r.XRenderFreePicture(dpy, mask)
            x.XClearWindow(dpy, self.window)
            self.level = level
        if position != self.position:
            x.XMoveWindow(dpy, self.window, position[0] - HOTSPOT[0], position[1] - HOTSPOT[1])
            self.position = position
        if not self.mapped:
            x.XMapRaised(dpy, self.window)
            self.mapped = True
        else:
            x.XRaiseWindow(dpy, self.window)
        x.XFlush(dpy)

    def hide(self) -> None:
        if self.mapped:
            self.x.XUnmapWindow(self.dpy, self.window)
            self.x.XFlush(self.dpy)
            self.mapped = False

    def close(self) -> None:
        if not self.dpy:
            return
        if self.window:
            self.x.XDestroyWindow(self.dpy, self.window)
            self.window = 0
        for picture in self.pictures:
            self.render.XRenderFreePicture(self.dpy, picture)
        self.pictures.clear()
        for pixmap in self.pixmaps:
            self.x.XFreePixmap(self.dpy, pixmap)
        self.pixmaps.clear()
        if self.colormap:
            self.x.XFreeColormap(self.dpy, self.colormap)
            self.colormap = 0
        self.x.XCloseDisplay(self.dpy)
        self.dpy = None


def _owner_stamp():
    path = Path(os.environ.get("WORKMAN_LEARN_ROOT", "~/.grok/workman-learn")).expanduser()
    try:
        info = (path / "human-input.stamp").stat()
        return info.st_mtime_ns, info.st_ctime_ns, info.st_ino
    except FileNotFoundError:
        return None


class _State:
    def __init__(self, now, pointer, stamp):
        self.last_activity = now
        self.position = None
        self.pointer = pointer
        self.stamp = stamp
        self.expected = deque(maxlen=128)
        self.pending = None
        self.sequence = 0

    def owner(self, stamp, overlay):
        if stamp != self.stamp:
            self.stamp = stamp
            self.position = None
            self.pending = None
            self.expected.clear()
            overlay.hide()

    def message(self, data, now, overlay):
        try:
            action, x, y, sequence, wall_ns = json.loads(data)
        except (ValueError, TypeError, UnicodeError):
            return
        if (action not in ("move", "hide") or not _coordinates(x, y)
                or type(sequence) is not int or type(wall_ns) is not int
                or sequence <= self.sequence or not 0 <= now - sequence / 1e9 <= FADE_S):
            return
        self.sequence = sequence
        if action == "hide":
            self.position = None
            self.expected.clear()
            self.pending = None
            overlay.hide()
            return
        if self.stamp is not None and wall_ns <= self.stamp[0]:
            return  # A queued position from before owner input cannot resurrect it.
        self.last_activity = sequence / 1e9
        self.position = (x, y)
        self.expected.append((self.position, now + EXPECTED_S))
        if self.pending is not None and self.pending[0] == self.position:
            self.pending = None

    def tick(self, now, pointer, overlay):
        while self.expected and self.expected[0][1] < now:
            self.expected.popleft()
        if pointer != self.pointer:
            self.pointer = pointer
            if not any(pos == pointer for pos, _ in self.expected):
                # Position IPC can arrive immediately AFTER an X motion. Give
                # it a short chance to arrive; owner stamps bypass this grace.
                if self.pending is None:
                    self.pending = (pointer, now + MOTION_GRACE_S)
                else:
                    self.pending = (pointer, self.pending[1])
            else:
                self.pending = None
        if self.pending is not None and now >= self.pending[1]:
            self.position = None
            self.pending = None
            self.expected.clear()
            overlay.hide()
        if self.position is not None:
            alpha = max(0.0, 1.0 - (now - self.last_activity) / FADE_S)
            if alpha > 0:
                overlay.draw(self.position, alpha)
            else:
                self.position = None
                overlay.hide()

    def wait(self, now):
        remaining = max(0.0, self.last_activity + IDLE_S - now)
        return min(TICK_S, remaining) if self.position is not None else remaining


def _serve(display, server, deadline=None, demo=None):
    overlay = _Overlay(display)
    try:
        now = time.monotonic()
        state = _State(now, overlay.pointer(), _owner_stamp())
        if demo is not None:
            state.message(_packet("move", *demo), time.monotonic(), overlay)
        while True:
            now = time.monotonic()
            if now - state.last_activity >= IDLE_S or (deadline is not None and now >= deadline):
                break
            # Read the stamp BEFORE queued positions to reject pre-owner moves.
            if state.position is not None:
                state.owner(_owner_stamp(), overlay)
            for _ in range(128):
                try:
                    data = server.recv(_MAX_PACKET + 1)
                except BlockingIOError:
                    break
                if len(data) <= _MAX_PACKET:
                    state.owner(_owner_stamp(), overlay)
                    state.message(data, time.monotonic(), overlay)
            if state.position is not None:
                state.tick(time.monotonic(), overlay.pointer(), overlay)
            now = time.monotonic()
            wait = state.wait(now)
            if deadline is not None:
                wait = min(wait, max(0.0, deadline - now))
            select.select([server], [], [], wait)
    finally:
        overlay.close()


class _Stopped(BaseException):
    pass


def _stop(*_):
    raise _Stopped()


def _check_lock_fd(fd, path):
    held, entry = os.fstat(fd), path.lstat()
    if (not stat.S_ISREG(held.st_mode) or held.st_uid != os.getuid()
            or stat.S_IMODE(held.st_mode) != 0o600 or held.st_nlink != 1
            or (held.st_dev, held.st_ino) != (entry.st_dev, entry.st_ino)):
        raise GhostError("daemon lock does not match its explicit display")


def _socket_identity(path):
    info = path.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise GhostError("unsafe daemon socket")
    return info.st_dev, info.st_ino


def _remove_owned_socket(path, identity):
    try:
        if _socket_identity(path) == identity:
            path.unlink()
    except (OSError, GhostError):
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Display-only steel ghost cursor (no pointer motion)")
    parser.add_argument("--display", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", nargs=2, type=int, metavar=("X", "Y"))
    mode.add_argument("--daemon", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=5.0, help="demo lifetime, 0 < seconds <= 30")
    parser.add_argument("--socket-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--lock-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if (not math.isfinite(args.timeout) or not 0 < args.timeout <= 30
            or (args.demo is not None and not _coordinates(*args.demo))):
        parser.error("demo requires valid coordinates and a timeout between 0 and 30 seconds")
    if args.daemon and (args.socket_fd is None or args.lock_fd is None):
        parser.error("daemon descriptors are supplied only by notify()")
    server, fd, path, identity = None, None, None, None
    previous = {}
    try:
        display = _display_name(args.display)
        path, lock = _paths(display, create=not args.daemon)
        if args.daemon:
            fd = args.lock_fd
            _check_lock_fd(fd, lock)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            server = socket.socket(fileno=args.socket_fd)
            if (server.family != socket.AF_UNIX or server.type != socket.SOCK_DGRAM
                    or server.getsockname() != str(path)):
                raise GhostError("daemon socket does not match its explicit display")
            server.setblocking(False)
        else:
            fd = _open_lock(lock)
            server = _bind_socket(path)
        identity = _socket_identity(path)
        previous = {sig: signal.signal(sig, _stop) for sig in (signal.SIGTERM, signal.SIGINT)}
        deadline = time.monotonic() + args.timeout if args.demo is not None else None
        _serve(display, server, deadline, tuple(args.demo) if args.demo is not None else None)
        return 0
    except _Stopped:
        return 0
    except (OSError, GhostError) as error:
        print(f"workman-ghost: {error}", file=sys.stderr)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if server is not None:
            server.close()
            if identity is not None:
                _remove_owned_socket(path, identity)
        if fd is not None:
            os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
