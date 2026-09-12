"""One persistent X connection for input and capture: no process per action.

The xdotool path spawns a process for every pointer step and every typed
character, which is 8 to 20 spawns per human move and one per keystroke, and
ffmpeg for every screenshot. A person using the machine costs the machine
almost nothing, so Human Mode should not either. This module keeps one
Display open (ctypes to libX11, libXtst, libXi, libXext) and sends the same
XTEST requests xdotool would, in-process:

    ch = channel(":0")            # None when X or the libraries are absent
    ch.move(100, 200)             # XTestFakeMotionEvent + XFlush
    ch.type_text("héllo\n", 0)    # keysym -> keycode, scratch remap for the rest
    ch.screenshot()               # XGetImage -> PNG (Pillow)

Everything the server drives through `x11.py` prefers this channel and falls
back to the subprocess path when it is unavailable (WORKMAN_XTEST=0 forces
the fallback, which is what the test suite and the "before" benchmark use).

Beyond input it also answers the questions the hand-back and the Escape
listener need without spawning: which keys and buttons the XTEST devices are
holding (XQueryDeviceState), the core keymap (XQueryKeymap), a polite
WM_DELETE_WINDOW, and waking a DPMS-blanked screen.

Nothing here grabs. XTEST events are injected as a slave device; the owner's
physical devices are untouched and untaken.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import io
import os
import threading
import time

# ---- X constants ---------------------------------------------------------
CurrentTime = 0
ZPixmap = 2
AllPlanes = 0xFFFFFFFF
NoEventMask = 0
ClientMessage = 33
GenericEvent = 35
MappingNotify = 34
LSBFirst = 0
NoSymbol = 0
KeyPress = 2

# XInput2
XIAllDevices = 0
XIAllMasterDevices = 1
XI_RawKeyPress = 13
XI_RawKeyRelease = 14
XI_RawButtonPress = 15
XI_RawButtonRelease = 16
XI_RawMotion = 17
XI_RAW_KINDS = {XI_RawKeyPress: "key_press", XI_RawKeyRelease: "key_release",
                XI_RawButtonPress: "button_press",
                XI_RawButtonRelease: "button_release", XI_RawMotion: "motion"}
# XInput 1 device state classes
KeyClass, ButtonClass, ValuatorClass = 0, 1, 2

DPMSModeOn = 0

#: xdotool's key aliases, so the same combo strings work on both paths.
KEY_ALIASES = {
    "ctrl": "Control_L", "control": "Control_L", "alt": "Alt_L", "shift": "Shift_L",
    "super": "Super_L", "win": "Super_L", "meta": "Meta_L", "enter": "Return",
    "return": "Return", "kp_enter": "KP_Enter", "esc": "Escape", "escape": "Escape",
    "backspace": "BackSpace", "tab": "Tab", "space": "space", "delete": "Delete",
    "del": "Delete", "insert": "Insert", "home": "Home", "end": "End",
    "pageup": "Prior", "page_up": "Prior", "prior": "Prior", "pagedown": "Next",
    "page_down": "Next", "next": "Next", "up": "Up", "down": "Down",
    "left": "Left", "right": "Right", "caps_lock": "Caps_Lock", "menu": "Menu",
    "print": "Print", "scroll_lock": "Scroll_Lock", "pause": "Pause",
    "minus": "minus", "equal": "equal", "plus": "plus", "comma": "comma",
    "period": "period", "slash": "slash", "backslash": "backslash",
    "semicolon": "semicolon", "apostrophe": "apostrophe", "grave": "grave",
    "bracketleft": "bracketleft", "bracketright": "bracketright",
}
MODIFIER_KEYSYMS = {"Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L",
                    "Shift_R", "Super_L", "Super_R", "Meta_L", "Meta_R",
                    "ISO_Level3_Shift", "Mode_switch"}

_ENV_OFF = "WORKMAN_XTEST"
_RETRY_S = 10.0


# ---- ctypes structures ---------------------------------------------------
class XImage(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int), ("height", ctypes.c_int),
        ("xoffset", ctypes.c_int), ("format", ctypes.c_int),
        ("data", ctypes.c_void_p), ("byte_order", ctypes.c_int),
        ("bitmap_unit", ctypes.c_int), ("bitmap_bit_order", ctypes.c_int),
        ("bitmap_pad", ctypes.c_int), ("depth", ctypes.c_int),
        ("bytes_per_line", ctypes.c_int), ("bits_per_pixel", ctypes.c_int),
        ("red_mask", ctypes.c_ulong), ("green_mask", ctypes.c_ulong),
        ("blue_mask", ctypes.c_ulong), ("obdata", ctypes.c_void_p),
        ("create_image", ctypes.c_void_p), ("destroy_image", ctypes.c_void_p),
        ("get_pixel", ctypes.c_void_p), ("put_pixel", ctypes.c_void_p),
        ("sub_image", ctypes.c_void_p), ("add_pixel", ctypes.c_void_p),
    ]


class XErrorEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int), ("display", ctypes.c_void_p),
        ("resourceid", ctypes.c_ulong), ("serial", ctypes.c_ulong),
        ("error_code", ctypes.c_ubyte), ("request_code", ctypes.c_ubyte),
        ("minor_code", ctypes.c_ubyte),
    ]


class XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int), ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong), ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int), ("data", ctypes.c_long * 5),
    ]


class XGenericEventCookie(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int), ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
        ("extension", ctypes.c_int), ("evtype", ctypes.c_int),
        ("cookie", ctypes.c_uint), ("data", ctypes.c_void_p),
    ]


class XEvent(ctypes.Union):
    _fields_ = [("type", ctypes.c_int), ("xcookie", XGenericEventCookie),
                ("pad", ctypes.c_long * 24)]


class XIRawEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int), ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
        ("extension", ctypes.c_int), ("evtype", ctypes.c_int),
        ("time", ctypes.c_ulong), ("deviceid", ctypes.c_int),
        ("sourceid", ctypes.c_int), ("detail", ctypes.c_int),
        ("flags", ctypes.c_int),
    ]


class XIEventMask(ctypes.Structure):
    _fields_ = [("deviceid", ctypes.c_int), ("mask_len", ctypes.c_int),
                ("mask", ctypes.POINTER(ctypes.c_ubyte))]


class XIDeviceInfo(ctypes.Structure):
    _fields_ = [("deviceid", ctypes.c_int), ("name", ctypes.c_char_p),
                ("use", ctypes.c_int), ("attachment", ctypes.c_int),
                ("enabled", ctypes.c_int), ("num_classes", ctypes.c_int),
                ("classes", ctypes.c_void_p)]


class XDeviceState(ctypes.Structure):
    _fields_ = [("device_id", ctypes.c_ulong), ("num_classes", ctypes.c_int),
                ("data", ctypes.c_void_p)]


class XKeyState(ctypes.Structure):
    _fields_ = [("cls", ctypes.c_ubyte), ("length", ctypes.c_ubyte),
                ("num_keys", ctypes.c_short), ("keys", ctypes.c_ubyte * 32)]


class XButtonState(ctypes.Structure):
    _fields_ = [("cls", ctypes.c_ubyte), ("length", ctypes.c_ubyte),
                ("num_buttons", ctypes.c_short), ("buttons", ctypes.c_ubyte * 32)]


_ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
_IO_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)
_IO_EXIT_HANDLER = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
_DESTROY_IMAGE = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(XImage))


class ChannelError(RuntimeError):
    """The X connection is gone or a library call is unavailable."""


def _load(name: str, soname: str):
    path = ctypes.util.find_library(name) or soname
    return ctypes.CDLL(path)


def keysym_for_char(ch: str) -> int:
    """The X keysym for one character, the way XStringToKeysym would map it."""
    if ch == "\n" or ch == "\r":
        return 0xFF0D  # Return
    if ch == "\t":
        return 0xFF09  # Tab
    code = ord(ch)
    if 0x20 <= code <= 0x7E or 0xA0 <= code <= 0xFF:
        return code
    return 0x01000000 | code


def parse_combo(combo: str) -> list[str]:
    """'ctrl+shift+t' -> ['Control_L', 'Shift_L', 't'] (xdotool spelling)."""
    parts = [p for p in str(combo).split("+") if p != ""]
    if not parts:
        return [str(combo)]
    out = []
    for part in parts:
        out.append(KEY_ALIASES.get(part.lower(), part))
    return out


class Channel:
    """One Display, one lock, every primitive the backend needs."""

    def __init__(self, display: str | None = None):
        self.display_name = display or os.environ.get("DISPLAY") or ":0"
        self.lock = threading.RLock()
        self.x11 = _load("X11", "libX11.so.6")
        self.xtst = _load("Xtst", "libXtst.so.6")
        try:
            self.xi = _load("Xi", "libXi.so.6")
        except OSError:
            self.xi = None
        try:
            self.xext = _load("Xext", "libXext.so.6")
        except OSError:
            self.xext = None
        self._bind()
        self.dead = False
        self.last_error: str | None = None
        self._install_handlers()
        self.dpy = self.x11.XOpenDisplay(self.display_name.encode())
        if not self.dpy:
            raise ChannelError(f"cannot open display {self.display_name}")
        self._arm_io_exit()
        self.screen = self.x11.XDefaultScreen(self.dpy)
        self.root = self.x11.XRootWindow(self.dpy, self.screen)
        ev = ctypes.c_int(); er = ctypes.c_int(); ma = ctypes.c_int(); mi = ctypes.c_int()
        if not self.xtst.XTestQueryExtension(self.dpy, ev, er, ma, mi):
            self.close()
            raise ChannelError("XTEST extension missing on " + self.display_name)
        self._keymap: dict[int, tuple[int, int]] | None = None
        self._scratch: dict[int, int] = {}      # keysym -> scratch keycode
        self._scratch_free: list[int] = []
        self._min_kc = ctypes.c_int(); self._max_kc = ctypes.c_int()
        self.x11.XDisplayKeycodes(self.dpy, self._min_kc, self._max_kc)
        self._xi_opcode: int | None = None
        self._raw_selected = False

    # ---- plumbing -----------------------------------------------------------
    def _bind(self) -> None:
        x = self.x11
        vp, ci, cu, cul = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong
        P = ctypes.POINTER
        x.XOpenDisplay.restype = vp; x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XCloseDisplay.argtypes = [vp]
        x.XDefaultScreen.argtypes = [vp]; x.XDefaultScreen.restype = ci
        x.XRootWindow.argtypes = [vp, ci]; x.XRootWindow.restype = cul
        x.XDisplayWidth.argtypes = [vp, ci]; x.XDisplayWidth.restype = ci
        x.XDisplayHeight.argtypes = [vp, ci]; x.XDisplayHeight.restype = ci
        x.XFlush.argtypes = [vp]; x.XSync.argtypes = [vp, ci]
        x.XQueryPointer.argtypes = [vp, cul, P(cul), P(cul), P(ci), P(ci), P(ci), P(ci), P(cu)]
        x.XQueryPointer.restype = ci
        x.XStringToKeysym.argtypes = [ctypes.c_char_p]; x.XStringToKeysym.restype = cul
        x.XKeysymToString.argtypes = [cul]; x.XKeysymToString.restype = ctypes.c_char_p
        x.XKeysymToKeycode.argtypes = [vp, cul]; x.XKeysymToKeycode.restype = ctypes.c_ubyte
        x.XDisplayKeycodes.argtypes = [vp, P(ci), P(ci)]
        x.XGetKeyboardMapping.argtypes = [vp, ctypes.c_ubyte, ci, P(ci)]
        x.XGetKeyboardMapping.restype = P(cul)
        x.XChangeKeyboardMapping.argtypes = [vp, ci, ci, P(cul), ci]
        x.XFree.argtypes = [vp]
        x.XQueryKeymap.argtypes = [vp, ctypes.POINTER(ctypes.c_char)]
        x.XGetImage.argtypes = [vp, cul, ci, ci, cu, cu, cul, ci]
        x.XGetImage.restype = P(XImage)
        x.XInternAtom.argtypes = [vp, ctypes.c_char_p, ci]; x.XInternAtom.restype = cul
        x.XSendEvent.argtypes = [vp, cul, ci, ctypes.c_long, vp]; x.XSendEvent.restype = ci
        x.XGetGeometry.argtypes = [vp, cul, P(cul), P(ci), P(ci), P(cu), P(cu), P(cu), P(cu)]
        x.XGetGeometry.restype = ci
        x.XTranslateCoordinates.argtypes = [vp, cul, cul, ci, ci, P(ci), P(ci), P(cul)]
        x.XTranslateCoordinates.restype = ci
        x.XSetErrorHandler.argtypes = [_ERROR_HANDLER]; x.XSetErrorHandler.restype = vp
        x.XSetIOErrorHandler.argtypes = [_IO_HANDLER]; x.XSetIOErrorHandler.restype = vp
        x.XGetErrorText.argtypes = [vp, ci, ctypes.c_char_p, ci]
        x.XResetScreenSaver.argtypes = [vp]
        x.XNextEvent.argtypes = [vp, P(XEvent)]
        x.XPending.argtypes = [vp]; x.XPending.restype = ci
        x.XGetEventData.argtypes = [vp, P(XGenericEventCookie)]; x.XGetEventData.restype = ci
        x.XFreeEventData.argtypes = [vp, P(XGenericEventCookie)]
        x.XQueryExtension.argtypes = [vp, ctypes.c_char_p, P(ci), P(ci), P(ci)]
        x.XQueryExtension.restype = ci
        x.XConnectionNumber.argtypes = [vp]; x.XConnectionNumber.restype = ci
        if hasattr(x, "XSetIOErrorExitHandler"):
            x.XSetIOErrorExitHandler.argtypes = [vp, _IO_EXIT_HANDLER, vp]
        t = self.xtst
        t.XTestQueryExtension.argtypes = [vp, P(ci), P(ci), P(ci), P(ci)]
        t.XTestQueryExtension.restype = ci
        t.XTestFakeMotionEvent.argtypes = [vp, ci, ci, ci, cul]
        t.XTestFakeButtonEvent.argtypes = [vp, cu, ci, cul]
        t.XTestFakeKeyEvent.argtypes = [vp, cu, ci, cul]
        if self.xi is not None:
            i = self.xi
            i.XIQueryVersion.argtypes = [vp, P(ci), P(ci)]; i.XIQueryVersion.restype = ci
            i.XISelectEvents.argtypes = [vp, cul, P(XIEventMask), ci]; i.XISelectEvents.restype = ci
            i.XIQueryDevice.argtypes = [vp, ci, P(ci)]; i.XIQueryDevice.restype = P(XIDeviceInfo)
            i.XIFreeDeviceInfo.argtypes = [P(XIDeviceInfo)]
            i.XOpenDevice.argtypes = [vp, cul]; i.XOpenDevice.restype = vp
            i.XCloseDevice.argtypes = [vp, vp]
            i.XQueryDeviceState.argtypes = [vp, vp]; i.XQueryDeviceState.restype = P(XDeviceState)
            i.XFreeDeviceState.argtypes = [P(XDeviceState)]
        if self.xext is not None:
            e = self.xext
            e.DPMSQueryExtension.argtypes = [vp, P(ci), P(ci)]; e.DPMSQueryExtension.restype = ci
            e.DPMSInfo.argtypes = [vp, P(ctypes.c_ushort), P(ctypes.c_ubyte)]; e.DPMSInfo.restype = ci
            e.DPMSForceLevel.argtypes = [vp, ctypes.c_ushort]; e.DPMSForceLevel.restype = ci

    def _install_handlers(self) -> None:
        # A protocol error must be recorded, not printed-and-exit. An I/O error
        # (server gone) marks the channel dead; libX11 >= 1.7 lets us stop it
        # from calling exit() through XSetIOErrorExitHandler.
        chan = self

        def on_error(dpy, ev):
            buf = ctypes.create_string_buffer(256)
            try:
                err = ctypes.cast(ev, ctypes.POINTER(XErrorEvent)).contents
                chan.x11.XGetErrorText(dpy, err.error_code, buf, 256)
                chan.last_error = (f"{buf.value.decode(errors='replace')} "
                                   f"(request {err.request_code}.{err.minor_code})")
            except Exception:
                chan.last_error = "X error"
            return 0

        def on_io_error(dpy):
            chan.dead = True
            chan.last_error = "X connection lost"
            return 0

        def on_io_exit(dpy, data):
            chan.dead = True

        self._on_error = _ERROR_HANDLER(on_error)
        self._on_io_error = _IO_HANDLER(on_io_error)
        self._on_io_exit = _IO_EXIT_HANDLER(on_io_exit)
        self.x11.XSetErrorHandler(self._on_error)
        self.x11.XSetIOErrorHandler(self._on_io_error)

    def _arm_io_exit(self) -> None:
        if hasattr(self.x11, "XSetIOErrorExitHandler"):
            self.x11.XSetIOErrorExitHandler(self.dpy, self._on_io_exit, None)

    def _check(self) -> None:
        if self.dead:
            raise ChannelError("X connection lost on " + self.display_name)

    def close(self) -> None:
        with self.lock:
            dpy = getattr(self, "dpy", None)
            if dpy and not self.dead:
                try:
                    self._revert_scratch()
                    self.x11.XCloseDisplay(dpy)
                except Exception:
                    pass
            self.dpy = None
            self.dead = True

    def flush(self) -> None:
        self.x11.XFlush(self.dpy)

    def fileno(self) -> int:
        return self.x11.XConnectionNumber(self.dpy)

    # ---- see ----------------------------------------------------------------
    def screen_size(self) -> tuple[int, int]:
        with self.lock:
            self._check()
            return (self.x11.XDisplayWidth(self.dpy, self.screen),
                    self.x11.XDisplayHeight(self.dpy, self.screen))

    def pointer_position(self) -> dict:
        with self.lock:
            self._check()
            root = ctypes.c_ulong(); child = ctypes.c_ulong()
            rx = ctypes.c_int(); ry = ctypes.c_int(); wx = ctypes.c_int(); wy = ctypes.c_int()
            mask = ctypes.c_uint()
            self.x11.XQueryPointer(self.dpy, self.root, root, child, rx, ry, wx, wy, mask)
            return {"x": rx.value, "y": ry.value, "window": child.value or None,
                    "buttons": self._buttons_from_mask(mask.value),
                    "mask": mask.value}

    @staticmethod
    def _buttons_from_mask(mask: int) -> list[int]:
        # Button1Mask = 1 << 8 ... Button5Mask = 1 << 12
        return [b for b in range(1, 6) if mask & (1 << (7 + b))]

    def screenshot(self, region: tuple[int, int, int, int] | None = None,
                   max_dim: int | None = None, compress_level: int = 1) -> bytes:
        """PNG bytes of the root window (or a region), via XGetImage.

        compress_level 1 is deliberate: the caller downscales and re-encodes
        the frame anyway, so a fast, larger intermediate PNG beats a slow,
        small one. Pass 6 when the bytes are the final artifact.
        """
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - Pillow is a project dep
            raise ChannelError("Pillow missing for XGetImage capture") from exc
        with self.lock:
            self._check()
            sw, sh = self.screen_size()
            x, y, w, h = (0, 0, sw, sh) if region is None else region
            x, y = max(0, int(x)), max(0, int(y))
            w, h = max(1, min(int(w), sw - x)), max(1, min(int(h), sh - y))
            img_p = self.x11.XGetImage(self.dpy, self.root, x, y, w, h, AllPlanes, ZPixmap)
            if not img_p:
                raise ChannelError(self.last_error or "XGetImage failed")
            try:
                img = img_p.contents
                stride = img.bytes_per_line
                size = stride * img.height
                raw = ctypes.string_at(img.data, size)
                bpp = img.bits_per_pixel
                if bpp == 32:
                    mode_raw = "BGRX" if img.byte_order == LSBFirst else "XRGB"
                elif bpp == 24:
                    mode_raw = "BGR" if img.byte_order == LSBFirst else "RGB"
                else:
                    raise ChannelError(f"unsupported bits_per_pixel {bpp}")
                im = Image.frombuffer("RGB", (img.width, img.height), raw, "raw",
                                      mode_raw, stride, 1)
            finally:
                destroy = _DESTROY_IMAGE(img_p.contents.destroy_image)
                destroy(img_p)
        if max_dim:
            scale = min(1.0, max_dim / max(im.size))
            if scale < 1.0:
                im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))),
                               Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG", compress_level=compress_level)
        return buf.getvalue()

    # ---- pointer ------------------------------------------------------------
    def move(self, x: int, y: int) -> None:
        with self.lock:
            self._check()
            self.xtst.XTestFakeMotionEvent(self.dpy, self.screen, int(x), int(y), CurrentTime)
            self.flush()

    def button(self, button: int, down: bool) -> None:
        with self.lock:
            self._check()
            self.xtst.XTestFakeButtonEvent(self.dpy, int(button), 1 if down else 0, CurrentTime)
            self.flush()

    def click(self, x: int | None, y: int | None, button: int = 1, count: int = 1,
              gap_ms: int = 12) -> None:
        with self.lock:
            if x is not None and y is not None:
                self.move(x, y)
            for i in range(max(1, int(count))):
                self.button(button, True)
                self.button(button, False)
                if i < count - 1:
                    time.sleep(gap_ms / 1000.0)

    # ---- keyboard -----------------------------------------------------------
    def _load_keymap(self) -> dict[int, tuple[int, int]]:
        """keysym -> (keycode, level) for levels 0 (plain) and 1 (Shift).

        Group 1 only. Anything else (other groups, AltGr levels, symbols not
        on the keyboard at all) goes through a scratch keycode, the way
        xdotool does it.
        """
        per = ctypes.c_int()
        lo, hi = self._min_kc.value, self._max_kc.value
        count = hi - lo + 1
        syms = self.x11.XGetKeyboardMapping(self.dpy, lo, count, per)
        table: dict[int, tuple[int, int]] = {}
        free: list[int] = []
        try:
            n = per.value
            for i in range(count):
                row = [syms[i * n + j] for j in range(n)]
                if not any(row):
                    free.append(lo + i)
                    continue
                for level in (0, 1):
                    if level < n and row[level] and row[level] not in table:
                        table[row[level]] = (lo + i, level)
                # A letter key with only the lowercase symbol listed still
                # produces the capital with Shift.
                if n > 1 and row[0] and not row[1]:
                    name = self.x11.XKeysymToString(row[0])
                    if name and len(name) == 1 and name.islower():
                        upper = self.x11.XStringToKeysym(name.upper())
                        if upper and upper not in table:
                            table[upper] = (lo + i, 1)
        finally:
            self.x11.XFree(syms)
        # Scratch keycodes: the top of the range, away from real keys.
        self._scratch_free = sorted(free, reverse=True)[:8]
        return table

    def keymap(self) -> dict[int, tuple[int, int]]:
        if self._keymap is None:
            self._keymap = self._load_keymap()
        return self._keymap

    def invalidate_keymap(self) -> None:
        self._keymap = None

    def keysym(self, name: str) -> int:
        """Keysym for an xdotool-style key name or a single character."""
        if len(name) == 1:
            return keysym_for_char(name)
        name = KEY_ALIASES.get(name.lower(), name)
        sym = self.x11.XStringToKeysym(name.encode())
        if not sym:
            sym = self.x11.XStringToKeysym(name.capitalize().encode())
        if not sym:
            sym = self.x11.XStringToKeysym(name.upper().encode())
        return int(sym)

    def _scratch_keycode(self, sym: int) -> int:
        code = self._scratch.get(sym)
        if code:
            return code
        if not self._scratch_free:
            self.keymap()
        if not self._scratch_free:
            raise ChannelError("no free keycode to type " + hex(sym))
        # Reuse the oldest scratch slot when all are taken.
        if len(self._scratch) >= len(self._scratch_free):
            old_sym = next(iter(self._scratch))
            code = self._scratch.pop(old_sym)
        else:
            code = self._scratch_free[len(self._scratch)]
        arr = (ctypes.c_ulong * 2)(sym, sym)
        self.x11.XChangeKeyboardMapping(self.dpy, code, 2, arr, 1)
        self.x11.XSync(self.dpy, 0)
        self._scratch[sym] = code
        return code

    def _revert_scratch(self) -> None:
        for sym, code in list(self._scratch.items()):
            arr = (ctypes.c_ulong * 2)(NoSymbol, NoSymbol)
            try:
                self.x11.XChangeKeyboardMapping(self.dpy, code, 2, arr, 1)
            except Exception:
                pass
        self._scratch.clear()
        if getattr(self, "dpy", None):
            self.x11.XSync(self.dpy, 0)

    def keycode_for(self, sym: int) -> tuple[int, int]:
        """(keycode, level) for a keysym; level 1 means hold Shift."""
        if not sym:
            raise ChannelError("no keysym")
        hit = self.keymap().get(sym)
        if hit:
            return hit
        code = self.x11.XKeysymToKeycode(self.dpy, sym)
        if code:
            # Present in a group or level the table skipped: press it plain
            # and let the server's own mapping decide the level.
            return int(code), 0
        return self._scratch_keycode(sym), 0

    def _fake_key(self, keycode: int, down: bool) -> None:
        self.xtst.XTestFakeKeyEvent(self.dpy, int(keycode), 1 if down else 0, CurrentTime)

    def key(self, name: str, down: bool) -> None:
        """Press or release one key by xdotool name ('ctrl', 'Return', 'a')."""
        with self.lock:
            self._check()
            sym = self.keysym(name)
            if not sym:
                raise ChannelError(f"unknown key {name!r}")
            code, level = self.keycode_for(sym)
            self._fake_key(code, down)
            self.flush()

    def press_combo(self, combo: str, hold_ms: int = 0) -> list[str]:
        """xdotool `key` semantics: modifiers down in order, base key tap,
        modifiers up in reverse. Returns the resolved key names."""
        names = parse_combo(combo)
        with self.lock:
            self._check()
            resolved = []
            for n in names:
                sym = self.keysym(n)
                if not sym:
                    raise ChannelError(f"unknown key {n!r} in {combo!r}")
                resolved.append((n, self.keycode_for(sym)))
            mods, (base, (bcode, blevel)) = resolved[:-1], resolved[-1]
            held = []
            try:
                for _n, (code, _lvl) in mods:
                    self._fake_key(code, True)
                    held.append(code)
                shift = None
                if blevel == 1 and not any(n in ("Shift_L", "Shift_R") for n, _ in mods):
                    shift = self.keycode_for(self.keysym("Shift_L"))[0]
                    self._fake_key(shift, True)
                    held.append(shift)
                self._fake_key(bcode, True)
                self.flush()
                if hold_ms:
                    time.sleep(hold_ms / 1000.0)
                self._fake_key(bcode, False)
            finally:
                for code in reversed(held):
                    self._fake_key(code, False)
                self.flush()
            return [n for n, _ in resolved]

    def needs_remap(self, sym: int) -> bool:
        """True when this keysym is on no key of the current keymap, so typing
        it would mean a scratch keycode (an odd or empty `event.code`)."""
        if not sym:
            return False
        if sym in self.keymap():
            return False
        return not self.x11.XKeysymToKeycode(self.dpy, sym)

    def type_text(self, text: str, delay_ms: int = 0, dwell_ms: int = 0) -> int:
        """Type characters one keysym at a time. Returns the count typed.

        dwell_ms keeps each key down before the release (a real keystroke is
        not zero-length); delay_ms is the gap after the release. Capitals and
        shifted symbols are typed with a real Shift press around the key.
        """
        typed = 0
        shift_code = None
        with self.lock:
            self._check()
            for ch in text:
                sym = keysym_for_char(ch)
                code, level = self.keycode_for(sym)
                if level == 1:
                    if shift_code is None:
                        shift_code = self.keycode_for(self.keysym("Shift_L"))[0]
                    self._fake_key(shift_code, True)
                self._fake_key(code, True)
                self.flush()
                if dwell_ms > 0:
                    time.sleep(dwell_ms / 1000.0)
                self._fake_key(code, False)
                if level == 1:
                    self._fake_key(shift_code, False)
                self.flush()
                typed += 1
                if delay_ms > 0:
                    time.sleep(delay_ms / 1000.0)
        return typed

    # ---- window facts (EWMH, no subprocess) -----------------------------------
    def _atom(self, name: str) -> int:
        return self.x11.XInternAtom(self.dpy, name.encode(), 0)

    def _property(self, window: int, name: str, expect: str | None = None) -> bytes | None:
        """Raw bytes of a window property, or None when absent."""
        x = self.x11
        if not hasattr(self, "_prop_bound"):
            x.XGetWindowProperty.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long,
                ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_void_p)]
            x.XGetWindowProperty.restype = ctypes.c_int
            self._prop_bound = True
        atype = ctypes.c_ulong(); fmt = ctypes.c_int(); n = ctypes.c_ulong()
        after = ctypes.c_ulong(); data = ctypes.c_void_p()
        req_type = self._atom(expect) if expect else 0  # AnyPropertyType
        rc = x.XGetWindowProperty(self.dpy, int(window), self._atom(name), 0, 4096, 0,
                                  req_type, atype, fmt, n, after, data)
        if rc != 0 or not data.value or n.value == 0:
            if data.value:
                x.XFree(data)
            return None
        try:
            size = n.value * (4 if fmt.value == 32 else fmt.value // 8)
            if fmt.value == 32:
                # 32-bit items are stored as C longs.
                size = n.value * ctypes.sizeof(ctypes.c_long)
            return ctypes.string_at(data, size)
        finally:
            x.XFree(data)

    def _identity(self, wid: int) -> dict:
        """WM_CLASS, title and pid of one client window (caller holds the lock)."""
        cls = self._property(wid, "WM_CLASS", "STRING") or b""
        parts = [p.decode(errors="replace") for p in cls.split(b"\0") if p]
        title = self._property(wid, "_NET_WM_NAME", "UTF8_STRING")
        if title is None:
            title = self._property(wid, "WM_NAME") or b""
        pid = self._property(wid, "_NET_WM_PID", "CARDINAL")
        longsz = ctypes.sizeof(ctypes.c_long)
        return {"ok": True, "id": wid,
                "class": parts[-1] if parts else "",
                "instance": parts[0] if parts else "",
                "name": title.decode(errors="replace").rstrip("\0"),
                "pid": int.from_bytes(pid[:longsz], "little") if pid else None}

    def _frame_rect(self, wid: int) -> tuple[int, int, int, int] | None:
        """Root-relative frame rect of a client, including _NET_FRAME_EXTENTS."""
        x = self.x11
        root = ctypes.c_ulong()
        rx = ctypes.c_int(); ry = ctypes.c_int()
        w = ctypes.c_uint(); h = ctypes.c_uint()
        bw = ctypes.c_uint(); depth = ctypes.c_uint()
        if not x.XGetGeometry(self.dpy, int(wid), ctypes.byref(root),
                              ctypes.byref(rx), ctypes.byref(ry),
                              ctypes.byref(w), ctypes.byref(h),
                              ctypes.byref(bw), ctypes.byref(depth)):
            return None
        absx = ctypes.c_int(); absy = ctypes.c_int(); child = ctypes.c_ulong()
        if not x.XTranslateCoordinates(self.dpy, int(wid), self.root, 0, 0,
                                       ctypes.byref(absx), ctypes.byref(absy),
                                       ctypes.byref(child)):
            return None
        left = right = top = bottom = 0
        raw = self._property(int(wid), "_NET_FRAME_EXTENTS", "CARDINAL")
        longsz = ctypes.sizeof(ctypes.c_long)
        if raw and len(raw) >= 4 * longsz:
            vals = [int.from_bytes(raw[i:i + longsz], "little")
                    for i in range(0, 4 * longsz, longsz)]
            left, right, top, bottom = vals
        return (absx.value - left, absy.value - top,
                int(w.value) + left + right, int(h.value) + top + bottom)

    def _client_ids(self, name: str) -> list[int]:
        raw = self._property(self.root, name, "WINDOW")
        if not raw:
            return []
        longsz = ctypes.sizeof(ctypes.c_long)
        return [int.from_bytes(raw[i:i + longsz], "little")
                for i in range(0, len(raw) - (len(raw) % longsz), longsz)
                if int.from_bytes(raw[i:i + longsz], "little")]

    def active_window_info(self) -> dict:
        """Active window id, WM_CLASS and title via EWMH properties."""
        with self.lock:
            self._check()
            raw = self._property(self.root, "_NET_ACTIVE_WINDOW", "WINDOW")
            if not raw:
                return {"ok": False, "error": "no active window"}
            wid = int.from_bytes(raw[:ctypes.sizeof(ctypes.c_long)], "little")
            if not wid:
                return {"ok": False, "error": "no active window"}
            return self._identity(wid)

    def window_at_point(self, x: int, y: int) -> dict | None:
        """Topmost EWMH client whose frame contains (x, y), or None.

        Uses _NET_CLIENT_LIST_STACKING (bottom-to-top) plus geometry, not
        XQueryPointer's child: under Mutter that child is often the frame
        window, which has no WM_CLASS of the viewer.
        """
        with self.lock:
            self._check()
            ids = self._client_ids("_NET_CLIENT_LIST_STACKING") or self._client_ids(
                "_NET_CLIENT_LIST")
            for wid in reversed(ids):
                rect = self._frame_rect(wid)
                if rect is None:
                    continue
                rx, ry, rw, rh = rect
                if rx <= x < rx + rw and ry <= y < ry + rh:
                    return self._identity(wid)
            return None

    # ---- held state ----------------------------------------------------------
    def core_keys_down(self) -> list[int]:
        """Keycodes the core keyboard reports as down (every device merged)."""
        with self.lock:
            self._check()
            buf = (ctypes.c_char * 32)()
            self.x11.XQueryKeymap(self.dpy, buf)
            raw = bytes(buf)
            return [i for i in range(256) if raw[i >> 3] & (1 << (i & 7))]

    def xi_devices(self) -> list[dict]:
        if self.xi is None:
            return []
        with self.lock:
            self._check()
            n = ctypes.c_int()
            info = self.xi.XIQueryDevice(self.dpy, XIAllDevices, n)
            out = []
            try:
                for i in range(n.value):
                    d = info[i]
                    out.append({"id": d.deviceid, "name": (d.name or b"").decode(errors="replace"),
                                "use": d.use, "attachment": d.attachment,
                                "enabled": bool(d.enabled)})
            finally:
                self.xi.XIFreeDeviceInfo(info)
            return out

    def xtest_device_ids(self) -> set[int]:
        return {d["id"] for d in self.xi_devices() if "XTEST" in d["name"]}

    def device_state(self, device_id: int) -> dict:
        """Keys and buttons one XInput device currently holds down."""
        if self.xi is None:
            raise ChannelError("libXi missing")
        with self.lock:
            self._check()
            dev = self.xi.XOpenDevice(self.dpy, device_id)
            if not dev:
                raise ChannelError(self.last_error or f"XOpenDevice({device_id}) failed")
            try:
                st = self.xi.XQueryDeviceState(self.dpy, dev)
                if not st:
                    raise ChannelError(self.last_error or "XQueryDeviceState failed")
                try:
                    keys: list[int] = []
                    buttons: list[int] = []
                    p = st.contents.data
                    for _ in range(st.contents.num_classes):
                        cls = ctypes.cast(p, ctypes.POINTER(ctypes.c_ubyte))[0]
                        length = ctypes.cast(p, ctypes.POINTER(ctypes.c_ubyte))[1]
                        if cls == KeyClass:
                            ks = ctypes.cast(p, ctypes.POINTER(XKeyState)).contents
                            keys = [k for k in range(min(256, ks.num_keys))
                                    if ks.keys[k >> 3] & (1 << (k & 7))]
                        elif cls == ButtonClass:
                            bs = ctypes.cast(p, ctypes.POINTER(XButtonState)).contents
                            buttons = [b for b in range(1, min(256, bs.num_buttons + 1))
                                       if bs.buttons[b >> 3] & (1 << (b & 7))]
                        p += length
                    return {"id": device_id, "keys": keys, "buttons": buttons}
                finally:
                    self.xi.XFreeDeviceState(st)
            finally:
                self.xi.XCloseDevice(self.dpy, dev)

    def xtest_held(self) -> dict:
        """What the XTEST devices (every agent's input) are holding right now."""
        keys: set[int] = set(); buttons: set[int] = set()
        for did in sorted(self.xtest_device_ids()):
            try:
                st = self.device_state(did)
            except ChannelError:
                continue
            keys.update(st["keys"]); buttons.update(st["buttons"])
        return {"keys": sorted(keys), "buttons": sorted(buttons)}

    def release_xtest_held(self) -> dict:
        """Let go of every key and button the XTEST devices hold. The owner's
        physical keys are a different device and are never touched."""
        held = self.xtest_held()
        with self.lock:
            for code in held["keys"]:
                self._fake_key(code, False)
            for b in held["buttons"]:
                self.xtst.XTestFakeButtonEvent(self.dpy, b, 0, CurrentTime)
            self.flush()
        after = self.xtest_held()
        return {"released_keys": held["keys"], "released_buttons": held["buttons"],
                "still_held": after}

    def keycode_names(self, codes: list[int]) -> list[str]:
        out = []
        with self.lock:
            for c in codes:
                sym = self.keymap()
                name = None
                for s, (kc, lvl) in sym.items():
                    if kc == c and lvl == 0:
                        name = self.x11.XKeysymToString(s)
                        break
                out.append(name.decode() if name else f"keycode{c}")
        return out

    # ---- windows and screen ---------------------------------------------------
    def close_window(self, window_id: int) -> None:
        """Ask the client to close (WM_DELETE_WINDOW): the app may prompt."""
        with self.lock:
            self._check()
            proto = self.x11.XInternAtom(self.dpy, b"WM_PROTOCOLS", 0)
            delete = self.x11.XInternAtom(self.dpy, b"WM_DELETE_WINDOW", 0)
            ev = XClientMessageEvent()
            ev.type = ClientMessage
            ev.window = int(window_id)
            ev.message_type = proto
            ev.format = 32
            ev.data[0] = delete
            ev.data[1] = CurrentTime
            self.x11.XSendEvent(self.dpy, int(window_id), 0, NoEventMask, ctypes.byref(ev))
            self.flush()

    def screen_awake(self) -> dict:
        """DPMS power level, and whether the screen is on."""
        if self.xext is None:
            return {"dpms": False, "on": True}
        with self.lock:
            self._check()
            ev = ctypes.c_int(); er = ctypes.c_int()
            if not self.xext.DPMSQueryExtension(self.dpy, ev, er):
                return {"dpms": False, "on": True}
            level = ctypes.c_ushort(); state = ctypes.c_ubyte()
            self.xext.DPMSInfo(self.dpy, level, state)
            return {"dpms": True, "enabled": bool(state.value), "level": level.value,
                    "on": level.value == DPMSModeOn or not state.value}

    def wake_screen(self) -> dict:
        info = self.screen_awake()
        with self.lock:
            self._check()
            self.x11.XResetScreenSaver(self.dpy)
            if info.get("dpms") and not info.get("on"):
                self.xext.DPMSForceLevel(self.dpy, DPMSModeOn)
            self.flush()
        return {"was": info, "now": self.screen_awake()}

    # ---- XInput2 raw events (for the listener) --------------------------------
    def xi_opcode(self) -> int:
        if self._xi_opcode is None:
            op = ctypes.c_int(); ev = ctypes.c_int(); er = ctypes.c_int()
            if not self.x11.XQueryExtension(self.dpy, b"XInputExtension", op, ev, er):
                raise ChannelError("XInputExtension missing")
            major = ctypes.c_int(2); minor = ctypes.c_int(2)
            if self.xi is None or self.xi.XIQueryVersion(self.dpy, major, minor) != 0:
                raise ChannelError("XInput2 unavailable")
            self._xi_opcode = op.value
        return self._xi_opcode

    def select_raw_events(self) -> None:
        """Listen to raw key, button and motion events on the root window.
        Raw events name their source device, and nothing is grabbed."""
        op = self.xi_opcode()
        mask_len = 3
        mask = (ctypes.c_ubyte * mask_len)()
        for bit in (XI_RawKeyPress, XI_RawKeyRelease, XI_RawButtonPress,
                    XI_RawButtonRelease, XI_RawMotion):
            mask[bit >> 3] |= 1 << (bit & 7)
        # Master devices only: selecting XIAllDevices delivers every raw event
        # twice (once for the master, once for the slave). The master copy
        # still carries the slave in `sourceid`, which is all we need.
        em = XIEventMask(XIAllMasterDevices, mask_len,
                         ctypes.cast(mask, ctypes.POINTER(ctypes.c_ubyte)))
        self._raw_mask = mask  # keep alive
        with self.lock:
            self._check()
            self.xi.XISelectEvents(self.dpy, self.root, ctypes.byref(em), 1)
            self.flush()
        self._raw_selected = True

    def next_raw_event(self) -> dict | None:
        """Block for the next event; return a raw input event or None for
        anything else (a MappingNotify invalidates the keymap on the way)."""
        ev = XEvent()
        self.x11.XNextEvent(self.dpy, ctypes.byref(ev))
        if self.dead:
            raise ChannelError("X connection lost")
        if ev.type == MappingNotify:
            self.invalidate_keymap()
            return None
        if ev.type != GenericEvent or ev.xcookie.extension != self._xi_opcode:
            return None
        if not self.x11.XGetEventData(self.dpy, ctypes.byref(ev.xcookie)):
            return None
        try:
            raw = ctypes.cast(ev.xcookie.data, ctypes.POINTER(XIRawEvent)).contents
            kind = XI_RAW_KINDS.get(raw.evtype)
            if kind is None:
                return None
            return {"kind": kind, "source": raw.sourceid, "device": raw.deviceid,
                    "detail": raw.detail, "time": raw.time}
        finally:
            self.x11.XFreeEventData(self.dpy, ctypes.byref(ev.xcookie))


# ---- module-level cache ------------------------------------------------------
_CACHE: dict[str, Channel] = {}
_FAILED: dict[str, float] = {}
_CACHE_LOCK = threading.Lock()


def enabled() -> bool:
    return os.environ.get(_ENV_OFF, "1") not in ("0", "false", "no", "off")


def channel(display: str | None = None) -> Channel | None:
    """The shared channel for a display, or None when it cannot be had.

    A failure is remembered for a few seconds so a box without X does not pay
    for a fresh attempt on every action, and is retried after that so a
    restarted X server is picked up.
    """
    if not enabled():
        return None
    name = display or os.environ.get("WORKMAN_DISPLAY") or os.environ.get("DISPLAY") or ":0"
    with _CACHE_LOCK:
        ch = _CACHE.get(name)
        if ch is not None:
            if not ch.dead:
                return ch
            _CACHE.pop(name, None)
        failed_at = _FAILED.get(name)
        if failed_at and time.monotonic() - failed_at < _RETRY_S:
            return None
        try:
            ch = Channel(name)
        except Exception:
            _FAILED[name] = time.monotonic()
            return None
        _FAILED.pop(name, None)
        _CACHE[name] = ch
        return ch


def reset() -> None:
    """Drop cached channels (tests, or after a display change)."""
    with _CACHE_LOCK:
        for ch in _CACHE.values():
            ch.close()
        _CACHE.clear()
        _FAILED.clear()
