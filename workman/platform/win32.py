"""Windows backend — ctypes straight to user32/gdi32, no third-party packages.

Design notes that matter more than the API calls:

**Coordinate space.** Windows puts the origin at the *primary* monitor's
top-left, so a monitor placed to the left has negative x. A screenshot cannot
have negative pixels, so this backend defines the contract space as the virtual
desktop with its own top-left at (0, 0), and translates on the way into the OS.
That preserves the one invariant everything else depends on: the coordinate you
read off a screenshot is the coordinate you click.

**DPI.** Without a per-monitor DPI declaration Windows lies to the process about
both the screen size and window rectangles on any display scaled above 100%,
which is most laptops. Awareness is declared once at import, before anything
measures anything.

**SendInput, not mouse_event.** The legacy calls are ignored by protected UI and
by many games; SendInput with MOUSEEVENTF_VIRTUALDESK is what reaches them.
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time

from . import base

PLATFORM = "Windows"
_IS_WINDOWS = sys.platform.startswith("win")

# ---- constants -------------------------------------------------------------
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_VIRTUALDESK = 0x0001, 0x8000, 0x4000
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
WHEEL_DELTA = 120

KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0001, 0x0002, 0x0004

SW_RESTORE, SW_MINIMIZE = 9, 6
WM_CLOSE = 0x0010
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DWMWA_CLOAKED = 14

# xdotool key names -> Windows virtual-key codes.
VK = {
    "return": 0x0D, "enter": 0x0D, "kp_enter": 0x0D, "tab": 0x09, "space": 0x20,
    "backspace": 0x08, "escape": 0x1B, "esc": 0x1B, "delete": 0x2E,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "home": 0x24, "end": 0x23, "prior": 0x21, "page_up": 0x21, "pageup": 0x21,
    "next": 0x22, "page_down": 0x22, "pagedown": 0x22, "insert": 0x2D,
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "menu": 0x12,
    "super": 0x5B, "meta": 0x5B, "win": 0x5B,
    "capslock": 0x14, "printscreen": 0x2C, "print": 0x2C, "pause": 0x13,
    "kp_0": 0x60, "kp_1": 0x61, "kp_2": 0x62, "kp_3": 0x63, "kp_4": 0x64,
    "kp_5": 0x65, "kp_6": 0x66, "kp_7": 0x67, "kp_8": 0x68, "kp_9": 0x69,
    "kp_multiply": 0x6A, "kp_add": 0x6B, "kp_subtract": 0x6D,
    "kp_decimal": 0x6E, "kp_divide": 0x6F,
    "minus": 0xBD, "equal": 0xBB, "bracketleft": 0xDB, "bracketright": 0xDD,
    "backslash": 0xDC, "semicolon": 0xBA, "apostrophe": 0xDE, "quote": 0xDE,
    "grave": 0xC0, "comma": 0xBC, "period": 0xBE, "slash": 0xBF,
}
VK.update({f"f{n}": 0x6F + n for n in range(1, 25)})          # F1=0x70 .. F24
VK.update({ch: ord(ch.upper()) for ch in "abcdefghijklmnopqrstuvwxyz"})
VK.update({d: ord(d) for d in "0123456789"})

#: Keys that must carry KEYEVENTF_EXTENDEDKEY or they arrive as their numpad twin.
EXTENDED = {0x25, 0x26, 0x27, 0x28, 0x24, 0x23, 0x21, 0x22, 0x2D, 0x2E,
            0x5B, 0x5C, 0x6F, 0x2C}


def vk_for(name: str) -> int | None:
    key = (name or "").strip().lower()
    if key in VK:
        return VK[key]
    if len(key) == 1:
        return ord(key.upper())
    return None


# ---- ctypes plumbing -------------------------------------------------------
# Structures are declared unconditionally (harmless anywhere); only the DLL
# handles are Windows-only, so this module imports fine on Linux for tests.
ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
                ("time", ctypes.c_uint32), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_uint16), ("wScan", ctypes.c_uint16),
                ("dwFlags", ctypes.c_uint32), ("time", ctypes.c_uint32),
                ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_uint32), ("wParamL", ctypes.c_uint16),
                ("wParamH", ctypes.c_uint16)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint32), ("u", _INPUTUNION)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", ctypes.c_uint32)]


_dpi_set = False


def _set_restypes(u32, g32, k32) -> None:
    """Declare handle-returning calls as pointers.

    ctypes defaults every return to C `int`, which silently truncates a 64-bit
    HWND/HANDLE to 32 bits. The result is a handle that looks plausible, fails
    every subsequent call, and reports no error — so this is not optional
    tidiness, it is the difference between working and mysteriously not.
    """
    for fn in (u32.GetForegroundWindow, u32.GetDC, u32.GetClipboardData,
               u32.WindowFromPoint):
        fn.restype = ctypes.c_void_p
    for fn in (g32.CreateCompatibleDC, g32.CreateCompatibleBitmap,
               g32.SelectObject):
        fn.restype = ctypes.c_void_p
    for fn in (k32.GlobalLock, k32.GlobalAlloc, k32.OpenProcess):
        fn.restype = ctypes.c_void_p


def _dll():
    """(user32, gdi32, kernel32) with DPI awareness declared once."""
    global _dpi_set
    if not _IS_WINDOWS:
        raise base.Unsupported("the win32 backend only runs on Windows")
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    g32 = ctypes.WinDLL("gdi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _set_restypes(u32, g32, k32)
    if not _dpi_set:
        _dpi_set = True
        try:  # PER_MONITOR_AWARE_V2; Windows 10 1703+
            u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            try:
                ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
            except Exception:
                try:
                    u32.SetProcessDPIAware()
                except Exception:
                    pass
    return u32, g32, k32


def _guard(feature: str, hint: str = ""):
    return base.unsupported(feature, PLATFORM, hint or "this backend needs Windows")


def emit_cursor(kind: str, x: int, y: int) -> None:
    """No overlay daemon on Windows yet; kept so callers stay platform-blind."""
    return None


# ---- geometry --------------------------------------------------------------
def virtual_origin() -> tuple[int, int]:
    """Native coordinates of the virtual desktop's top-left corner."""
    u32, _, _ = _dll()
    return (u32.GetSystemMetrics(SM_XVIRTUALSCREEN),
            u32.GetSystemMetrics(SM_YVIRTUALSCREEN))


def screen_size() -> tuple[int, int]:
    u32, _, _ = _dll()
    return (u32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
            u32.GetSystemMetrics(SM_CYVIRTUALSCREEN))


def _to_native(x: int, y: int) -> tuple[int, int]:
    ox, oy = virtual_origin()
    return (int(x) + ox, int(y) + oy)


def _to_contract(x: int, y: int) -> tuple[int, int]:
    ox, oy = virtual_origin()
    return (int(x) - ox, int(y) - oy)


def monitors() -> list[dict]:
    u32, _, _ = _dll()
    found: list[dict] = []
    # LPARAM is LONG_PTR: an integer. Declaring it as a float type would make
    # the x64 calling convention pass it in an SSE register instead of a
    # general-purpose one, and the callback would read garbage.
    proto = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.POINTER(RECT), ctypes.c_ssize_t)

    def cb(hmon, _hdc, _lprc, _data):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if u32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            x, y = _to_contract(r.left, r.top)
            found.append({"name": f"monitor-{len(found)}", "x": x, "y": y,
                          "w": r.right - r.left, "h": r.bottom - r.top,
                          "primary": bool(info.dwFlags & 1)})
        return 1

    u32.EnumDisplayMonitors(None, None, proto(cb), 0)
    if not found:
        w, h = screen_size()
        found = [{"name": "screen", "x": 0, "y": 0, "w": w, "h": h, "primary": True}]
    return found


# ---- see -------------------------------------------------------------------
def screenshot(max_dim: int | None = None,
               region: tuple[int, int, int, int] | None = None) -> bytes:
    """Grab the virtual desktop (or a sub-rect) as PNG bytes via GDI BitBlt."""
    u32, g32, _ = _dll()
    if region:
        rx, ry, w, h = region
        nx, ny = _to_native(rx, ry)
    else:
        w, h = screen_size()
        nx, ny = virtual_origin()
    w, h = max(1, int(w)), max(1, int(h))

    screen_dc = u32.GetDC(None)
    mem_dc = g32.CreateCompatibleDC(screen_dc)
    bitmap = g32.CreateCompatibleBitmap(screen_dc, w, h)
    old = g32.SelectObject(mem_dc, bitmap)
    try:
        if not g32.BitBlt(mem_dc, 0, 0, w, h, screen_dc, nx, ny, SRCCOPY):
            raise RuntimeError(f"BitBlt failed (error {ctypes.get_last_error()})")
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = w
        info.bmiHeader.biHeight = -h        # negative: top-down rows
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB
        buf = ctypes.create_string_buffer(w * h * 4)
        if not g32.GetDIBits(mem_dc, bitmap, 0, h, buf, ctypes.byref(info),
                             DIB_RGB_COLORS):
            raise RuntimeError("GetDIBits failed")
        raw = buf.raw
    finally:
        g32.SelectObject(mem_dc, old)
        g32.DeleteObject(bitmap)
        g32.DeleteDC(mem_dc)
        u32.ReleaseDC(None, screen_dc)

    try:  # Pillow is much faster on an 8-megapixel frame when it is available.
        from PIL import Image

        im = Image.frombuffer("RGB", (w, h), raw, "raw", "BGRX", 0, 1)
        if max_dim and max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        import io

        out = io.BytesIO()
        im.save(out, "PNG")
        return out.getvalue()
    except ImportError:
        pass

    rgb = base.bgra_to_rgb(raw, w, h)
    if max_dim:
        rgb, w, h = base.downscale_rgb(rgb, w, h, max_dim)
    return base.png_encode(w, h, rgb)


# ---- windows ---------------------------------------------------------------
def _window_title(u32, hwnd) -> str:
    length = u32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    u32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _process_name(k32, pid: int) -> str:
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = ctypes.c_uint32(260)
        buf = ctypes.create_unicode_buffer(size.value)
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
    finally:
        k32.CloseHandle(handle)
    return ""


def _is_cloaked(hwnd) -> bool:
    """UWP keeps invisible 'ghost' windows around; DWM is the only honest source."""
    try:
        dwm = ctypes.WinDLL("dwmapi")
        value = ctypes.c_int(0)
        dwm.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(value),
                                  ctypes.sizeof(value))
        return bool(value.value)
    except Exception:
        return False


def list_windows() -> list[dict]:
    u32, _, k32 = _dll()
    out: list[dict] = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_ssize_t)

    def cb(hwnd, _lparam):
        if not u32.IsWindowVisible(hwnd):
            return 1
        title = _window_title(u32, hwnd)
        if not title or _is_cloaked(hwnd):
            return 1
        rect = RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(rect))
        pid = ctypes.c_uint32(0)
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        x, y = _to_contract(rect.left, rect.top)
        out.append({
            "id": str(int(hwnd)), "name": title, "app": _process_name(k32, pid.value),
            "pid": str(pid.value), "x": x, "y": y,
            "w": rect.right - rect.left, "h": rect.bottom - rect.top,
            "minimized": bool(u32.IsIconic(hwnd)),
            "maximized": bool(u32.IsZoomed(hwnd)),
        })
        return 1

    u32.EnumWindows(proto(cb), 0)
    return out


def _match(query: str) -> dict | None:
    q = (query or "").lower()
    windows = list_windows()
    for w in windows:
        if w["id"] == query or w["pid"] == query:
            return w
    for w in windows:
        if q in w["name"].lower() or q in (w.get("app") or "").lower():
            return w
    return None


def focus_window(query: str, minimize_blockers: bool = True) -> dict:
    """Raise and focus a window.

    Windows refuses SetForegroundWindow from a process that does not own the
    current foreground, so the input queues are attached first; that is the
    documented way to make the call succeed rather than silently flash the
    taskbar button.
    """
    u32, _, k32 = _dll()
    win = _match(query)
    if win is None:
        return {"ok": False, "error": f"no window matching {query!r}",
                "windows": [w["name"] for w in list_windows()][:40]}
    hwnd = ctypes.c_void_p(int(win["id"]))
    if win.get("minimized"):
        u32.ShowWindow(hwnd, SW_RESTORE)
    fg = u32.GetForegroundWindow()
    this_thread = k32.GetCurrentThreadId()
    fg_thread = u32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = bool(fg_thread) and bool(
        u32.AttachThreadInput(fg_thread, this_thread, True))
    try:
        u32.BringWindowToTop(hwnd)
        u32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            u32.AttachThreadInput(fg_thread, this_thread, False)
    now = _window_title(u32, u32.GetForegroundWindow())
    return {"ok": True, "target": win["id"], "frontmost_now": now,
            "verify": "screenshot before typing"}


def active_window() -> dict:
    u32, _, k32 = _dll()
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        return {"ok": False, "error": "no active window"}
    rect = RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(rect))
    pid = ctypes.c_uint32(0)
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    x, y = _to_contract(rect.left, rect.top)
    return {"ok": True, "id": str(int(hwnd)), "name": _window_title(u32, hwnd),
            "app": _process_name(k32, pid.value), "pid": str(pid.value),
            "x": x, "y": y, "w": rect.right - rect.left, "h": rect.bottom - rect.top}


def kill_window(query: str) -> dict:
    """Close a window, escalating to terminating its process only if it ignores
    WM_CLOSE. The gentler first step is free and often enough."""
    u32, _, k32 = _dll()
    win = _match(query)
    if win is None:
        return {"ok": False, "error": f"no window matching {query!r}"}
    hwnd = ctypes.c_void_p(int(win["id"]))
    u32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    time.sleep(0.5)
    if not u32.IsWindow(hwnd):
        return {"ok": True, "killed": win["id"], "how": "WM_CLOSE",
                "warning": "the app may have prompted to save"}
    handle = k32.OpenProcess(0x0001, False, int(win["pid"]))  # PROCESS_TERMINATE
    if not handle:
        return {"ok": False, "error": "window ignored WM_CLOSE and could not be "
                                      "terminated", "window": win["name"]}
    try:
        k32.TerminateProcess(handle, 1)
    finally:
        k32.CloseHandle(handle)
    return {"ok": True, "killed": win["id"], "how": "TerminateProcess",
            "warning": "process terminated without saving"}


# ---- input -----------------------------------------------------------------
def _send(*inputs: INPUT) -> int:
    u32, _, _ = _dll()
    n = len(inputs)
    array = (INPUT * n)(*inputs)
    return u32.SendInput(n, array, ctypes.sizeof(INPUT))


def _mouse_input(flags: int, x: int | None = None, y: int | None = None,
                 data: int = 0) -> INPUT:
    dx = dy = 0
    if x is not None and y is not None:
        w, h = screen_size()
        # SendInput's absolute space is 0..65535 across the virtual desktop,
        # measured from its top-left — which is exactly the contract space, so
        # no origin translation is needed here.
        dx = int(round(int(x) * 65535 / max(1, w - 1)))
        dy = int(round(int(y) * 65535 / max(1, h - 1)))
        flags |= MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi = MOUSEINPUT(dx, dy, ctypes.c_uint32(data & 0xFFFFFFFF).value,
                        flags, 0, 0)
    return inp


def _key_input(vk: int, up: bool = False, unit: int | None = None) -> INPUT:
    """One INPUT record. `unit` is a UTF-16 code unit for literal text; a
    non-BMP character is two units and must be sent as two records."""
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    if unit is not None:
        flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
        inp.ki = KEYBDINPUT(0, unit, flags, 0, 0)
        return inp
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in EXTENDED:
        flags |= KEYEVENTF_EXTENDEDKEY
    inp.ki = KEYBDINPUT(vk, 0, flags, 0, 0)
    return inp


_BUTTON_FLAGS = {
    1: (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    2: (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    3: (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
}


def move(x: int, y: int) -> dict:
    emit_cursor("move", x, y)
    _send(_mouse_input(MOUSEEVENTF_MOVE, x, y))
    return {"ok": True, "at": [x, y]}


def click(x: int, y: int, button: int = 1, count: int = 1) -> dict:
    down, up = _BUTTON_FLAGS.get(button, _BUTTON_FLAGS[1])
    emit_cursor("click", x, y)
    _send(_mouse_input(MOUSEEVENTF_MOVE, x, y))
    for _ in range(max(1, count)):
        _send(_mouse_input(down), _mouse_input(up))
        time.sleep(0.03)
    return {"ok": True, "clicked": [x, y], "button": button, "count": count}


def click_with(x: int, y: int, button: int = 1, count: int = 1,
               modifiers: list[str] | None = None) -> dict:
    mods, unknown = base.normalize_modifiers(modifiers)
    if unknown:
        return {"ok": False, "error": f"unknown modifier(s) {unknown}",
                "supported": sorted(base.MODIFIERS)}
    for m in mods:
        _send(_key_input(vk_for(m)))
    try:
        out = click(x, y, button=button, count=count)
        out["modifiers"] = mods
        return out
    finally:
        # Released even if the click throws: a stuck Ctrl makes the machine
        # unusable for the human who comes back to it.
        for m in reversed(mods):
            _send(_key_input(vk_for(m), up=True))


def mouse_down(button: int = 1, x: int | None = None, y: int | None = None) -> dict:
    down, _ = _BUTTON_FLAGS.get(button, _BUTTON_FLAGS[1])
    if x is not None and y is not None:
        _send(_mouse_input(MOUSEEVENTF_MOVE, x, y))
        emit_cursor("move", x, y)
    _send(_mouse_input(down))
    return {"ok": True, "held": button, "at": [x, y] if x is not None else None}


def mouse_up(button: int = 1, x: int | None = None, y: int | None = None) -> dict:
    _, up = _BUTTON_FLAGS.get(button, _BUTTON_FLAGS[1])
    if x is not None and y is not None:
        _send(_mouse_input(MOUSEEVENTF_MOVE, x, y))
    _send(_mouse_input(up))
    return {"ok": True, "released": button}


def drag(from_x: int, from_y: int, to_x: int, to_y: int) -> dict:
    emit_cursor("move", from_x, from_y)
    _send(_mouse_input(MOUSEEVENTF_MOVE, from_x, from_y))
    _send(_mouse_input(MOUSEEVENTF_LEFTDOWN))
    steps = 12
    for i in range(1, steps + 1):
        _send(_mouse_input(MOUSEEVENTF_MOVE,
                           int(from_x + (to_x - from_x) * i / steps),
                           int(from_y + (to_y - from_y) * i / steps)))
        time.sleep(0.01)
    _send(_mouse_input(MOUSEEVENTF_LEFTUP))
    emit_cursor("click", to_x, to_y)
    return {"ok": True, "from": [from_x, from_y], "to": [to_x, to_y]}


def scroll(direction: str, amount: int = 3) -> dict:
    spec = {"up": (MOUSEEVENTF_WHEEL, WHEEL_DELTA),
            "down": (MOUSEEVENTF_WHEEL, -WHEEL_DELTA),
            "right": (MOUSEEVENTF_HWHEEL, WHEEL_DELTA),
            "left": (MOUSEEVENTF_HWHEEL, -WHEEL_DELTA)}.get(direction)
    if spec is None:
        return {"ok": False, "error": "direction must be up|down|left|right"}
    flag, delta = spec
    for _ in range(max(1, amount)):
        _send(_mouse_input(flag, data=delta))
        time.sleep(0.01)
    return {"ok": True, "scrolled": direction, "amount": amount}


def scroll_at(x: int, y: int, direction: str, amount: int = 3) -> dict:
    move(x, y)
    out = scroll(direction, amount)
    if out.get("ok"):
        out["at"] = [x, y]
    return out


def hover(x: int, y: int, settle_ms: int = 350) -> dict:
    move(x, y)
    time.sleep(max(0, settle_ms) / 1000.0)
    return {"ok": True, "at": [x, y], "settled_ms": settle_ms}


def pointer_position() -> dict:
    u32, _, _ = _dll()
    point = POINT()
    u32.GetCursorPos(ctypes.byref(point))
    x, y = _to_contract(point.x, point.y)
    return {"ok": True, "x": x, "y": y}


def type_text(text: str, delay_ms: int = 40) -> dict:
    """Type literal text as unicode scan codes.

    KEYEVENTF_UNICODE bypasses the keyboard layout entirely, so a colon still
    arrives as a colon on a German layout and emoji survive.
    """
    for ch in text:
        encoded = ch.encode("utf-16-le")
        for i in range(0, len(encoded), 2):
            unit = int.from_bytes(encoded[i:i + 2], "little")
            _send(_key_input(0, unit=unit), _key_input(0, up=True, unit=unit))
        time.sleep(max(0, delay_ms) / 1000.0)
    return {"ok": True, "typed_len": len(text)}


def press_key(key: str) -> dict:
    """xdotool key syntax, translated: 'Return', 'ctrl+c', 'super+l', 'KP_0'."""
    mods, name = base.split_combo(key)
    vk = vk_for(name)
    if vk is None:
        return {"ok": False, "error": f"unknown key {name!r}",
                "hint": "use xdotool names: Return, Tab, Escape, ctrl+c, super+d"}
    events = [_key_input(vk_for(m)) for m in mods]
    events += [_key_input(vk), _key_input(vk, up=True)]
    events += [_key_input(vk_for(m), up=True) for m in reversed(mods)]
    _send(*events)
    return {"ok": True, "key": key}


def key_down(key: str) -> dict:
    vk = vk_for(key)
    if vk is None:
        return {"ok": False, "error": f"unknown key {key!r}"}
    _send(_key_input(vk))
    return {"ok": True, "held": key}


def key_up(key: str) -> dict:
    vk = vk_for(key)
    if vk is None:
        return {"ok": False, "error": f"unknown key {key!r}"}
    _send(_key_input(vk, up=True))
    return {"ok": True, "released": key}


# ---- environment -----------------------------------------------------------
def clipboard_get(selection: str = "clipboard") -> dict:
    if selection == "primary":
        return {"ok": False, "error": "Windows has no PRIMARY selection",
                "hint": "use selection='clipboard'"}
    u32, _, k32 = _dll()
    if not u32.OpenClipboard(None):
        return {"ok": False, "error": "another app is holding the clipboard open"}
    try:
        handle = u32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return {"ok": True, "text": "", "selection": "clipboard"}
        k32.GlobalLock.restype = ctypes.c_void_p
        ptr = k32.GlobalLock(ctypes.c_void_p(handle))
        try:
            text = ctypes.wstring_at(ptr) if ptr else ""
        finally:
            k32.GlobalUnlock(ctypes.c_void_p(handle))
        return {"ok": True, "text": text, "selection": "clipboard"}
    finally:
        u32.CloseClipboard()


def clipboard_set(text: str = "", selection: str = "clipboard") -> dict:
    if selection == "primary":
        return {"ok": False, "error": "Windows has no PRIMARY selection",
                "hint": "use selection='clipboard'"}
    u32, _, k32 = _dll()
    if not u32.OpenClipboard(None):
        return {"ok": False, "error": "another app is holding the clipboard open"}
    try:
        u32.EmptyClipboard()
        data = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(data)
        k32.GlobalAlloc.restype = ctypes.c_void_p
        handle = k32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return {"ok": False, "error": "GlobalAlloc failed"}
        k32.GlobalLock.restype = ctypes.c_void_p
        ptr = k32.GlobalLock(ctypes.c_void_p(handle))
        ctypes.memmove(ptr, ctypes.byref(data), size)
        k32.GlobalUnlock(ctypes.c_void_p(handle))
        # Ownership passes to the clipboard here; do not free the handle.
        u32.SetClipboardData(CF_UNICODETEXT, ctypes.c_void_p(handle))
        return {"ok": True, "set_len": len(text), "selection": "clipboard"}
    finally:
        u32.CloseClipboard()


# ---- optional capabilities -------------------------------------------------
SW_SHOWNORMAL, SW_SHOWMINIMIZED, SW_MAXIMIZE = 1, 2, 3
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0004, 0x0010

_SHOW_ACTIONS = {"minimize": SW_MINIMIZE, "unminimize": SW_RESTORE,
                 "maximize": SW_MAXIMIZE, "unmaximize": SW_RESTORE}


def list_windows_rich() -> list[dict]:
    """Same as list_windows: this backend already reports minimized/maximized,
    so there is no second, richer source to consult."""
    return list_windows()


def window_action(query: str, action: str) -> dict:
    u32, _, _ = _dll()
    if action == "activate":
        return focus_window(query)
    win = _match(query)
    if win is None:
        return {"ok": False, "error": f"no window matching {query!r}"}
    hwnd = ctypes.c_void_p(int(win["id"]))
    if action in _SHOW_ACTIONS:
        u32.ShowWindow(hwnd, _SHOW_ACTIONS[action])
        return {"ok": True, "action": action, "window": win["name"]}
    if action in ("above", "unabove", "pin", "unpin"):
        top = HWND_TOPMOST if action in ("above", "pin") else HWND_NOTOPMOST
        u32.SetWindowPos(hwnd, ctypes.c_void_p(top), 0, 0, 0, 0,
                         SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        return {"ok": True, "action": action, "window": win["name"]}
    if action == "close":
        u32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return {"ok": True, "action": "close", "window": win["name"],
                "note": "the app may prompt about unsaved work"}
    if action in ("fullscreen", "unfullscreen"):
        return base.unsupported("fullscreen toggling", PLATFORM,
                                "Windows has no WM-level fullscreen; send the "
                                "app's own shortcut, usually press_key('F11')")
    return {"ok": False, "error": f"unknown action {action!r}",
            "actions": ["activate", "close", "above", "unabove",
                        *sorted(_SHOW_ACTIONS)]}


def window_geometry(query: str, x: int | None = None, y: int | None = None,
                    w: int | None = None, h: int | None = None) -> dict:
    u32, _, _ = _dll()
    win = _match(query)
    if win is None:
        return {"ok": False, "error": f"no window matching {query!r}"}
    hwnd = ctypes.c_void_p(int(win["id"]))
    if win.get("maximized"):
        # A maximized window ignores SetWindowPos, exactly as on X11.
        u32.ShowWindow(hwnd, SW_RESTORE)
    nx, ny = _to_native(x if x is not None else win["x"],
                        y if y is not None else win["y"])
    u32.SetWindowPos(hwnd, None, nx, ny,
                     w if w is not None else win["w"],
                     h if h is not None else win["h"],
                     SWP_NOZORDER | SWP_NOACTIVATE)
    after = _match(win["id"]) or win
    return {"ok": True, "window": win["name"],
            "geometry": {k: after.get(k) for k in ("x", "y", "w", "h")}}


def workspaces() -> dict:
    return base.unsupported("virtual desktops", PLATFORM,
                            "Windows exposes no public API for its virtual "
                            "desktops; switch with press_key('ctrl+super+Right')")


def set_workspace(index: int = 0) -> dict:
    return workspaces()


def move_to_workspace(query: str, index: int = 0) -> dict:
    return workspaces()


def display_name() -> str:
    try:
        w, h = screen_size()
        return f"win32:{w}x{h}"
    except base.Unsupported:
        return "win32:unavailable"


def platform_info() -> dict:
    info = {
        "backend": "win32",
        "os": "windows",
        "input_channel": "sendinput",
        "modifier_super": "Win",
        "contract": list(base.CONTRACT),
        "notes": ("Coordinates are relative to the virtual desktop's top-left, "
                  "so they match screenshot pixels even with a monitor left of "
                  "the primary one."),
    }
    if not _IS_WINDOWS:
        info.update({"ok": False, "available": False,
                     "error": "loaded for inspection on a non-Windows host"})
        return info
    try:
        release = subprocess.run(
            ["cmd", "/c", "ver"], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        release = ""
    w, h = screen_size()
    ox, oy = virtual_origin()
    info.update({"ok": True, "available": True, "os_version": release,
                 "display": display_name(), "virtual_desktop": {"w": w, "h": h},
                 "virtual_origin": {"x": ox, "y": oy},
                 "dpi_aware": _dpi_set, "pillow": _has_pillow()})
    return info


def _has_pillow() -> bool:
    try:
        import PIL  # noqa: F401

        return True
    except ImportError:
        return False
